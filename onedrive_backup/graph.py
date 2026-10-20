"""Small stdlib Graph client; credentials never follow download redirects."""
import json
import logging
import math
import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .throttle import RequestGovernor
from .transport import PersistentHTTPSOpener


log = logging.getLogger(__name__)


class GraphError(RuntimeError):
    def __init__(self, status, code="request_failed"):
        self.status = status
        self.code = code
        # Never include response bodies, tokens, or signed URLs in exception text.
        super().__init__(f"Graph HTTP {status}: {code}")


class ResumableBody:
    """Resume transient stream failures within a run, using the same signed URL.

    Only a validated 206 response may be appended. The engine still verifies
    source version/size after download and reads the destination back in full.
    """
    def __init__(self, client, metadata):
        self.client = client
        self.url = metadata["@microsoft.graph.downloadUrl"]
        self.size = metadata["size"]
        self.offset = self.retries = 0
        self.stream = self._open()
        self.etag = getattr(self.stream, "headers", {}).get("ETag")

    def _open(self):
        headers = {"Accept-Encoding": "identity"}
        if self.offset:
            headers["Range"] = "bytes={}-".format(self.offset)
            if self.etag:
                headers["If-Range"] = self.etag
        response = self.client._open(Request(self.url, headers=headers), self.client.download_opener)
        if self.offset:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if (response.getcode() != 206 or not match or int(match[1]) != self.offset or
                    int(match[3]) != self.size or not self.offset <= int(match[2]) < self.size):
                response.close()
                raise GraphError(0, "download_resume_range_rejected")
        elif response.getcode() != 200:
            response.close()
            raise GraphError(0, "download_unexpected_partial_response")
        return response

    def read(self, size=-1):
        while True:
            try:
                block = self.stream.read(size)
                if not block and self.offset < self.size:
                    raise OSError("truncated_stream")
                self.offset += len(block)
                return block
            except (OSError, HTTPException):
                self.stream.close()
                if self.retries >= self.client.config.max_http_attempts - 1:
                    raise GraphError(0, "download_stream_retry_exhausted") from None
                self.retries += 1
                self.client.governor.network_failure()
                log.warning("다운로드 연결 재개 offset=%s total=%s attempt=%s", self.offset, self.size, self.retries)
                time.sleep(self.client.delay(self.retries - 1))
                self.stream = self._open()

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise GraphError(0, "insecure_download_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def segment(value):
    return quote(str(value), safe="")


def safe_http_error_code(exc):
    """Extract only a bounded, non-sensitive Graph error category."""
    try:
        raw = exc.read(65537)
        if len(raw) > 65536:
            return "request_failed"
        payload = json.loads(raw.decode("utf-8"))
        error = payload.get("error", {})
        message = error.get("message", "")
        if isinstance(message, str):
            normalized = message.casefold()
            if "mysite url" in normalized and "unable to retrieve" in normalized:
                return "onedrive_not_provisioned"
        code = error.get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", code):
            return code
    except (AttributeError, UnicodeDecodeError, ValueError, TypeError, OSError):
        pass
    return "request_failed"


def safe_error_payload_code(payload):
    try:
        code = payload.get("error", {}).get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", code):
            return code
    except (AttributeError, TypeError):
        pass
    return "request_failed"


class GraphClient:
    def __init__(self, config):
        self.config = config
        self.opener = PersistentHTTPSOpener(build_opener(NoRedirect()))
        self.download_opener = PersistentHTTPSOpener(build_opener(HTTPSRedirect()), allow_redirects=True)
        self.governor = RequestGovernor(config.download_concurrency, config.download_concurrency_max,
                                        config.request_units_per_second)
        self.token = None
        self.expires = 0
        self.token_lock = threading.Lock()

    @staticmethod
    def _read_json(response, stage):
        try:
            payload = json.load(response)
        except (ValueError, UnicodeError):
            raise GraphError(0, "invalid_{}_json_response".format(stage)) from None
        if not isinstance(payload, dict):
            raise GraphError(0, "invalid_{}_json_shape".format(stage))
        return payload

    def delay(self, attempt, retry_after=None):
        if retry_after:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds):
                    return max(0, seconds)
            except ValueError:
                try:
                    return max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError):
                    pass
        return min(300, self.config.retry_base_seconds * 2 ** min(attempt, 10)) + random.random()

    def _open(self, request, opener=None):
        request.add_header("User-Agent", "OneDriveCentralBackup/0.13.0")
        for attempt in range(self.config.max_http_attempts):
            self.governor.wait(getattr(request, "throttle_cost", 1))
            try:
                response = (opener or self.opener).open(request, timeout=self.config.http_timeout_seconds)
                self.governor.headers(getattr(response, "headers", None))
                return response
            except HTTPError as exc:
                status, retry_after = exc.code, exc.headers.get("Retry-After")
                error_code = safe_http_error_code(exc)
                exc.close()
                wait_seconds = self.delay(attempt, retry_after)
                if status in (429, 503):
                    self.governor.backoff(wait_seconds)
                if status not in (429, 500, 502, 503, 504) or attempt + 1 == self.config.max_http_attempts:
                    raise GraphError(status, error_code) from None
                log.warning("Graph 요청 재시도 status=%s code=%s attempt=%s/%s wait_seconds=%.1f",
                            status, error_code, attempt + 1, self.config.max_http_attempts, wait_seconds)
                if status not in (429, 503):
                    time.sleep(wait_seconds)
            except (URLError, TimeoutError, OSError, HTTPException):
                self.governor.network_failure()
                if attempt + 1 == self.config.max_http_attempts:
                    raise GraphError(0, "network_error") from None
                wait_seconds = self.delay(attempt)
                log.warning("Graph 요청 재시도 status=0 code=network_error attempt=%s/%s wait_seconds=%.1f",
                            attempt + 1, self.config.max_http_attempts, wait_seconds)
                time.sleep(wait_seconds)

    def access_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        with self.token_lock:
            if self.token and time.time() < self.expires:
                return self.token
            cfg = self.config
            body = urlencode({"grant_type": "client_credentials", "client_id": cfg.client_id,
                              "client_secret": cfg.secret(),
                              "scope": cfg.graph_url.rsplit("/", 1)[0] + "/.default"}).encode()
            url = cfg.authority_url + "/" + segment(cfg.tenant_id) + "/oauth2/v2.0/token"
            with self._open(Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})) as response:
                payload = self._read_json(response, "token")
            self.token = payload["access_token"]
            self.expires = time.time() + max(0, int(payload["expires_in"]) - 120)
            return self.token

    def get(self, url):
        if url.startswith("/"):
            url = self.config.graph_url + url
        base, target = urlsplit(self.config.graph_url), urlsplit(url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc) or not target.path.startswith(base.path + "/"):
            raise GraphError(0, "untrusted_graph_link")
        for attempt in range(2):
            request = Request(url, headers={"Authorization": "Bearer " + self.access_token()})
            try:
                with self._open(request) as response:
                    return self._read_json(response, "metadata")
            except GraphError as exc:
                if exc.status != 401 or attempt:
                    raise
                self.token = None

    def post_json(self, url, payload):
        if url.startswith("/"):
            url = self.config.graph_url + url
        base, target = urlsplit(self.config.graph_url), urlsplit(url)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc) or not target.path.startswith(base.path + "/"):
            raise GraphError(0, "untrusted_graph_link")
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        for attempt in range(2):
            request = Request(url, data=encoded, method="POST", headers={
                "Authorization": "Bearer " + self.access_token(),
                "Content-Type": "application/json",
            })
            request.throttle_cost = max(1, len(payload.get("requests", [])))
            try:
                with self._open(request) as response:
                    return self._read_json(response, "batch")
            except GraphError as exc:
                if exc.status != 401 or attempt:
                    raise
                self.token = None

    def pages(self, url):
        while url:
            page = self.get(url)
            yield page
            url = page.get("@odata.nextLink")

    def metadata(self, drive_id, item_id):
        return self.get(f"/drives/{segment(drive_id)}/items/{segment(item_id)}")

    def metadata_batch(self, identities):
        """Fetch up to 20 driveItem resources while preserving per-item failures."""
        if not identities:
            return []
        if len(identities) > 20:
            raise ValueError("graph_batch_limit_exceeded")
        if identities:
            log.debug("Graph 메타데이터 배치 조회 시작 items=%s", len(identities))
        results = [None] * len(identities)
        pending = {str(index): (index, drive_id, item_id)
                   for index, (drive_id, item_id) in enumerate(identities)}
        for attempt in range(self.config.max_http_attempts):
            requests = [{"id": request_id, "method": "GET",
                         "url": "/drives/{}/items/{}".format(segment(drive_id), segment(item_id))}
                        for request_id, (_, drive_id, item_id) in pending.items()]
            try:
                payload = self.post_json("/$batch", {"requests": requests})
            except GraphError as exc:
                return [exc if result is None else result for result in results]
            responses = {str(response.get("id")): response for response in payload.get("responses", [])
                         if isinstance(response, dict)}
            retry = {}
            retry_after = None
            throttled = False
            for request_id, identity in pending.items():
                index, drive_id, item_id = identity
                response = responses.get(request_id)
                if not response:
                    results[index] = GraphError(0, "batch_response_missing")
                    continue
                status = response.get("status")
                body = response.get("body", {})
                self.governor.headers(response.get("headers"))
                if isinstance(status, int) and 200 <= status < 300 and isinstance(body, dict):
                    results[index] = body
                    continue
                if status in (429, 503):
                    throttled = True
                headers = response.get("headers", {})
                if isinstance(headers, dict):
                    value = next((v for k, v in headers.items() if str(k).casefold() == "retry-after"), None)
                    if value is not None:
                        retry_after = max(self.delay(attempt, str(value)), retry_after or 0)
                if status in (429, 500, 502, 503, 504) and attempt + 1 < self.config.max_http_attempts:
                    retry[request_id] = (index, drive_id, item_id)
                    continue
                results[index] = GraphError(status if isinstance(status, int) else 0,
                                            safe_error_payload_code(body))
            wait_seconds = self.delay(attempt, str(retry_after) if retry_after is not None else None)
            if throttled:
                self.governor.backoff(wait_seconds)
            if not retry:
                break
            log.warning("Graph 배치 요청 재시도 items=%s attempt=%s/%s wait_seconds=%.1f",
                        len(retry), attempt + 1, self.config.max_http_attempts, wait_seconds)
            if not throttled:
                time.sleep(wait_seconds)
            pending = retry
        for request_id, (index, _, _) in pending.items():
            if results[index] is None:
                results[index] = GraphError(0, "batch_retry_exhausted")
        if identities:
            failed = sum(isinstance(result, Exception) for result in results)
            log.debug("Graph 메타데이터 배치 조회 완료 items=%s failed=%s", len(identities), failed)
        return results

    def download(self, metadata):
        url = metadata.get("@microsoft.graph.downloadUrl")
        if not url or urlsplit(url).scheme != "https":
            raise GraphError(0, "download_url_missing")
        # Pre-authenticated URL: deliberately no Graph Authorization header.
        return ResumableBody(self, metadata)

    def close(self):
        self.opener.close()
        self.download_opener.close()
