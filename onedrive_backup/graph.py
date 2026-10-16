"""Small stdlib Graph client; credentials never follow download redirects."""
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


log = logging.getLogger(__name__)


class GraphError(RuntimeError):
    def __init__(self, status, code="request_failed"):
        self.status = status
        self.code = code
        # Never include response bodies, tokens, or signed URLs in exception text.
        super().__init__(f"Graph HTTP {status}: {code}")


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


class GraphClient:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(NoRedirect())
        self.download_opener = build_opener(HTTPSRedirect())
        self.token = None
        self.expires = 0
        self.token_lock = threading.Lock()

    def delay(self, attempt, retry_after=None):
        if retry_after:
            try:
                return max(0, float(retry_after))
            except ValueError:
                try:
                    return max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError):
                    pass
        return min(300, self.config.retry_base_seconds * 2 ** min(attempt, 10)) + random.random()

    def _open(self, request, opener=None):
        for attempt in range(self.config.max_http_attempts):
            try:
                return (opener or self.opener).open(request, timeout=self.config.http_timeout_seconds)
            except HTTPError as exc:
                status, retry_after = exc.code, exc.headers.get("Retry-After")
                error_code = safe_http_error_code(exc)
                exc.close()
                if status not in (429, 500, 502, 503, 504) or attempt + 1 == self.config.max_http_attempts:
                    raise GraphError(status, error_code) from None
                wait_seconds = self.delay(attempt, retry_after)
                log.warning("Graph 요청 재시도 status=%s code=%s attempt=%s/%s wait_seconds=%.1f",
                            status, error_code, attempt + 1, self.config.max_http_attempts, wait_seconds)
                time.sleep(wait_seconds)
            except (URLError, TimeoutError, OSError):
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
                payload = json.load(response)
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
                    return json.load(response)
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

    def download(self, metadata):
        url = metadata.get("@microsoft.graph.downloadUrl")
        if not url or urlsplit(url).scheme != "https":
            raise GraphError(0, "download_url_missing")
        # Pre-authenticated URL: deliberately no Graph Authorization header.
        return self._open(Request(url), self.download_opener)
