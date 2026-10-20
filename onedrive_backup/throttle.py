"""Shared service backoff and bounded adaptive concurrency."""
import logging
import threading
import time

log = logging.getLogger(__name__)


class RequestGovernor:
    def __init__(self, initial, maximum, units_per_second=0):
        self.maximum = max(initial, maximum)
        self.target = initial
        self.lock = threading.Lock()
        self.pause_until = 0.0
        self.next_growth = time.monotonic() + 30
        self.completed = 0
        self.units_per_second = units_per_second
        self.rate_next = 0.0
        self.trial = None

    def limit(self):
        with self.lock:
            return self.target

    def wait(self, cost=1):
        while True:
            with self.lock:
                now = time.monotonic()
                delay = self.pause_until - now
                if self.units_per_second:
                    delay = max(delay, self.rate_next - now)
                if delay <= 0:
                    if self.units_per_second:
                        self.rate_next = now + max(1, cost) / self.units_per_second
                    return
            # Short waits keep long service backoff visible in operational logs.
            if delay >= 1:
                log.info("서비스 요청 대기 seconds=%.1f concurrency=%s", delay, self.limit())
            time.sleep(min(delay, 5))

    def backoff(self, seconds):
        with self.lock:
            now = time.monotonic()
            self.pause_until = max(self.pause_until, now + max(0, seconds))
            previous = self.target
            self.target = max(1, self.target // 2)
            self.next_growth = now + max(60, seconds + 30)
            self.completed = 0
            self.trial = None
        log.warning("서비스 요청 제한 동시성 조정 previous=%s current=%s wait_seconds=%.1f",
                    previous, self.limit(), seconds)

    def headers(self, headers):
        # SharePoint does not support IETF RateLimit headers. Retry-After and
        # an explicitly configured local request budget govern requests instead.
        pass

    def success(self):
        with self.lock:
            self.completed += 1

    def observe(self, received_rate, completed_rate, saturated, disk_pressure=False):
        """Called once per 30s window, not once per file. Probe one slot at a time.

        The validated rate is protected separately: receiving faster while the
        verification queue grows must not be treated as a throughput improvement.
        """
        with self.lock:
            now = time.monotonic()
            if now < self.next_growth:
                return
            previous = self.target
            reason = "hold"
            if disk_pressure:
                self.target = max(1, self.target - 1)
                self.trial = None
                self.next_growth = now + 60
                reason = "verification_backlog"
            elif self.trial is not None:
                old_target, received, validated = self.trial
                improved = received_rate >= received * 1.05
                retained = not validated or completed_rate >= validated * 0.90
                if not improved or not retained:
                    self.target = old_target
                    self.next_growth = now + 120
                    reason = "no_throughput_gain"
                else:
                    self.next_growth = now + 30
                    reason = "throughput_gain"
                self.trial = None
            elif saturated and received_rate > 0 and self.target < self.maximum:
                # Only probe under actual demand, never during a tail of a run.
                self.trial = (self.target, received_rate, completed_rate)
                self.target += 1
                self.next_growth = now + 30
                reason = "throughput_probe"
            else:
                self.next_growth = now + 30
            target = self.target
        log.info("처리량 기반 동시성 previous=%s current=%s reason=%s", previous, target, reason)

    def network_failure(self):
        with self.lock:
            self.target = max(1, self.target - 1)
            self.next_growth = time.monotonic() + 60
            self.completed = 0
            self.trial = None
