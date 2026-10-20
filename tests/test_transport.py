import io
import json
import unittest
from http.client import HTTPResponse, IncompleteRead
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from onedrive_backup.transport import PersistentHTTPSOpener
from onedrive_backup.throttle import RequestGovernor
from onedrive_backup.graph import GraphClient, GraphError


class Response:
    def __init__(self, body=b"abc", status=200, headers=None):
        self.body = io.BytesIO(body)
        self.length = len(body)
        self.status = status
        self.reason = "test"
        self.headers = headers or {}
        self.will_close = False

    def read(self, size=-1):
        return self.body.read(size)

    def isclosed(self):
        return self.body.closed or self.body.tell() == self.length

    def close(self):
        self.body.close()


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.proxies = patch("onedrive_backup.transport.getproxies", return_value={})
        self.proxies.start()
        self.addCleanup(self.proxies.stop)
        self.opener = PersistentHTTPSOpener(Mock(), allow_redirects=True)
        self.addCleanup(self.opener.close)

    def test_connection_reused_only_after_body_consumed(self):
        connection = Mock(sock=None)
        connection.getresponse.side_effect = [Response(), Response()]
        with patch("onedrive_backup.transport.HTTPSConnection", return_value=connection) as created:
            with self.opener.open(Request("https://files.test/a"), timeout=30) as response:
                self.assertEqual(response.read(), b"abc")
            with self.opener.open(Request("https://files.test/b"), timeout=30) as response:
                self.assertEqual(response.read(), b"abc")
        self.assertEqual(created.call_count, 1)
        self.assertEqual(connection.request.call_count, 2)

    def test_real_http_json_responses_read_all_with_both_framings(self):
        class Socket:
            def __init__(self, wire):
                self.wire = wire

            def makefile(self, *args):
                return io.BytesIO(self.wire)

        payload = b'{"access_token":"synthetic","expires_in":3600}'
        chunks = [payload[:12], payload[12:]]
        chunked = b"".join(format(len(chunk), "x").encode() + b"\r\n" + chunk + b"\r\n" for chunk in chunks) + b"0\r\n\r\n"
        for headers, body in (
                (b"Content-Length: " + str(len(payload)).encode() + b"\r\n", payload),
                (b"Transfer-Encoding: chunked\r\n", chunked)):
            with self.subTest(headers=headers):
                raw = HTTPResponse(Socket(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + body))
                raw.begin()
                connection = Mock(sock=None)
                connection.getresponse.return_value = raw
                with patch("onedrive_backup.transport.HTTPSConnection", return_value=connection):
                    with self.opener.open(Request("https://login.test/token")) as opened:
                        self.assertEqual(json.load(opened)["access_token"], "synthetic")
                self.assertTrue(raw.isclosed())
                self.opener.close()

    def test_partial_response_discards_connection(self):
        first, second = Mock(sock=None), Mock(sock=None)
        first.getresponse.return_value = Response()
        second.getresponse.return_value = Response()
        with patch("onedrive_backup.transport.HTTPSConnection", side_effect=[first, second]) as created:
            with self.opener.open(Request("https://files.test/a")) as response:
                self.assertEqual(response.read(1), b"a")
            with self.opener.open(Request("https://files.test/b")) as response:
                response.read()
        first.close.assert_called_once()
        self.assertEqual(created.call_count, 2)

    def test_read_failure_discards_connection(self):
        connection = Mock(sock=None)
        response = Response()
        response.read = Mock(side_effect=IncompleteRead(b"a", 2))
        connection.getresponse.return_value = response
        with patch("onedrive_backup.transport.HTTPSConnection", return_value=connection):
            with self.assertRaises(IncompleteRead):
                with self.opener.open(Request("https://files.test/a")) as opened:
                    opened.read()
        connection.close.assert_called_once()
        self.assertFalse(self.opener.idle)

    def test_redirect_does_not_forward_authorization(self):
        first, second = Mock(sock=None), Mock(sock=None)
        first.getresponse.return_value = Response(status=302, headers={"Location": "https://other.test/b"})
        second.getresponse.return_value = Response()
        with patch("onedrive_backup.transport.HTTPSConnection", side_effect=[first, second]):
            with self.opener.open(Request("https://files.test/a", headers={"Authorization": "secret"})) as response:
                response.read()
        self.assertNotIn("Authorization", second.request.call_args.kwargs["headers"])

    def test_insecure_redirect_is_rejected(self):
        connection = Mock(sock=None)
        connection.getresponse.return_value = Response(status=302, headers={"Location": "http://other.test/b"})
        with patch("onedrive_backup.transport.HTTPSConnection", return_value=connection):
            with self.assertRaises(OSError):
                self.opener.open(Request("https://files.test/a"))

    def test_resume_headers_survive_https_redirect(self):
        first, second = Mock(sock=None), Mock(sock=None)
        first.getresponse.return_value = Response(status=302, headers={"Location": "https://other.test/b"})
        second.getresponse.return_value = Response()
        headers = {"Range": "bytes=2-", "If-Range": '"v1"', "Accept-Encoding": "identity"}
        with patch("onedrive_backup.transport.HTTPSConnection", side_effect=[first, second]):
            with self.opener.open(Request("https://files.test/a", headers=headers)) as response:
                response.read()
        forwarded = {key.lower(): value for key, value in second.request.call_args.kwargs["headers"].items()}
        self.assertEqual(forwarded["range"], "bytes=2-")
        self.assertEqual(forwarded["if-range"], '"v1"')

    def test_proxy_uses_existing_urllib_transport(self):
        request = Request("https://files.test/a")
        with patch("onedrive_backup.transport.getproxies", return_value={"https": "http://proxy.test:8080"}), \
                patch("onedrive_backup.transport.proxy_bypass", return_value=False), \
                patch("onedrive_backup.transport.HTTPSConnection") as created:
            self.opener.open(request, timeout=30)
        self.opener.fallback.open.assert_called_once_with(request, timeout=30)
        created.assert_not_called()

    def test_error_response_preserves_retry_after(self):
        connection = Mock(sock=None)
        connection.getresponse.return_value = Response(status=429, headers={"Retry-After": "20"})
        with patch("onedrive_backup.transport.HTTPSConnection", return_value=connection):
            with self.assertRaises(HTTPError) as captured:
                self.opener.open(Request("https://files.test/a"))
            self.assertEqual(captured.exception.headers["Retry-After"], "20")
            captured.exception.close()


class GovernorTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        clock = patch("onedrive_backup.throttle.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.governor = RequestGovernor(4, 8)

    def test_shared_backoff_waits_and_halves_concurrency(self):
        self.governor.backoff(12)
        self.assertEqual(self.governor.limit(), 2)
        def sleep(seconds):
            self.now += seconds
        with patch("onedrive_backup.throttle.time.sleep", side_effect=sleep):
            self.governor.wait()
        self.assertGreaterEqual(self.now, 112)
        # Every subsequent caller uses the same deadline, not its own private wait.
        self.governor.backoff(20)
        self.assertEqual(self.governor.limit(), 1)
        self.assertGreaterEqual(self.governor.pause_until, 132)

    def test_success_alone_does_not_grow_concurrency(self):
        self.now += 100
        for _ in range(100):
            self.governor.success()
        self.assertEqual(self.governor.limit(), 4)

    def test_growth_requires_measured_gain_and_respects_ceiling(self):
        self.now += 31
        rate = 100
        for _ in range(4):
            self.governor.observe(rate, rate, True)
            self.now += 31
            rate *= 1.1
            self.governor.observe(rate, rate, True)
            self.now += 31
        self.assertEqual(self.governor.limit(), 8)
        self.governor.observe(rate * 2, rate * 2, True)
        self.assertEqual(self.governor.limit(), 8)

    def test_no_gain_restores_previous_target_and_holds(self):
        self.now += 31
        self.governor.observe(100, 100, True)
        self.assertEqual(self.governor.limit(), 5)
        self.now += 31
        self.governor.observe(101, 100, True)
        self.assertEqual(self.governor.limit(), 4)
        self.now += 31
        self.governor.observe(100, 100, True)
        self.assertEqual(self.governor.limit(), 4)

    def test_disk_pressure_and_validated_rate_protect_destination(self):
        self.now += 31
        self.governor.observe(100, 100, True)
        self.now += 31
        self.governor.observe(150, 50, True)
        self.assertEqual(self.governor.limit(), 4)
        self.now += 121
        self.governor.observe(100, 100, True, disk_pressure=True)
        self.assertEqual(self.governor.limit(), 3)

    def test_tail_and_service_backoff_do_not_probe(self):
        self.now += 31
        self.governor.observe(100, 100, False)
        self.assertEqual(self.governor.limit(), 4)
        self.governor.backoff(20)
        self.now += 31
        self.governor.observe(100, 100, True)
        self.assertEqual(self.governor.limit(), 2)

    def test_unsupported_ratelimit_headers_are_ignored(self):
        self.governor.headers({"RateLimit-Remaining": "0", "RateLimit-Limit": "100", "RateLimit-Reset": "20"})
        self.assertEqual(self.governor.limit(), 4)
        self.assertEqual(self.governor.pause_until, 0)

    def test_local_budget_counts_batch_subrequests(self):
        governor = RequestGovernor(4, 8, units_per_second=2)
        with patch("onedrive_backup.throttle.time.sleep", side_effect=lambda seconds: setattr(self, "now", self.now + seconds)):
            governor.wait(cost=8)
            governor.wait()
        self.assertEqual(self.now, 104)


class JsonDiagnosticsTest(unittest.TestCase):
    def test_invalid_json_diagnostics_never_include_body(self):
        for stage in ("token", "metadata", "batch"):
            with self.subTest(stage=stage), self.assertRaises(GraphError) as error:
                GraphClient._read_json(io.BytesIO(b"private token or URL, not JSON"), stage)
            self.assertEqual(error.exception.code, "invalid_{}_json_response".format(stage))
            self.assertNotIn("private", str(error.exception))

    def test_json_array_is_reported_as_safe_shape_error(self):
        with self.assertRaises(GraphError) as error:
            GraphClient._read_json(io.BytesIO(b"[]"), "token")
        self.assertEqual(error.exception.code, "invalid_token_json_shape")


if __name__ == "__main__":
    unittest.main()
