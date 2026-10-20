"""Bounded HTTPS keep-alive pool; urllib remains the proxy transport."""
import ssl
import threading
from collections import OrderedDict
from http.client import HTTPSConnection
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, getproxies, proxy_bypass


class PooledResponse:
    def __init__(self, response, connection, release):
        self.response, self.connection, self.release = response, connection, release
        self.headers, self.status = response.headers, response.status
        self.reason = response.reason
        self.finished = False

    def getcode(self):
        return self.status

    def _finish(self, reusable):
        if not self.finished:
            self.finished = True
            self.release(self.connection, reusable)

    def read(self, size=-1):
        try:
            # HTTPResponse.read() uses None for "read all". Passing -1 into
            # chunked decoding treats it as a literal chunk length and can read
            # framing bytes into JSON or raise ValueError on buffered sockets.
            block = self.response.read() if size is None or size < 0 else self.response.read(size)
        except Exception:
            self._finish(False)
            raise
        if self.response.isclosed():
            self._finish(not self.response.will_close)
        return block

    def close(self):
        self._finish(self.response.isclosed() and not self.response.will_close)
        self.response.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class PersistentHTTPSOpener:
    def __init__(self, fallback, allow_redirects=False, max_idle=32):
        self.fallback, self.allow_redirects = fallback, allow_redirects
        self.context = ssl.create_default_context()
        self.max_idle = max_idle
        self.idle = OrderedDict()
        self.lock = threading.Lock()

    def _take(self, origin, timeout):
        with self.lock:
            connections = self.idle.get(origin)
            connection = connections.pop() if connections else None
            if connections == []:
                self.idle.pop(origin, None)
        if connection is None:
            connection = HTTPSConnection(origin[0], origin[1], timeout=timeout, context=self.context)
        connection.timeout = timeout
        if connection.sock is not None:
            try:
                connection.sock.settimeout(timeout)
            except OSError:
                connection.close()
                raise
        return connection

    def _release(self, origin, connection, reusable):
        if not reusable:
            connection.close()
            return
        with self.lock:
            self.idle.setdefault(origin, []).append(connection)
            self.idle.move_to_end(origin)
            while sum(len(values) for values in self.idle.values()) > self.max_idle:
                _, connections = self.idle.popitem(last=False)
                for old in connections:
                    old.close()

    def open(self, request, timeout=None):
        current = request
        for redirect in range(11):
            target = urlsplit(current.full_url)
            if target.scheme != "https" or not target.hostname or target.username or target.password:
                raise OSError("invalid_https_destination")
            # Preserve urllib's existing proxy handling, including proxy TLS
            # tunneling and authentication. Never silently bypass a configured proxy.
            proxies = getproxies()
            if (proxies.get("https") or proxies.get("all")) and not proxy_bypass(target.hostname):
                return self.fallback.open(current, timeout=timeout)
            origin = (target.hostname, target.port or 443)
            connection = self._take(origin, timeout)
            try:
                path = target.path or "/"
                if target.query:
                    path += "?" + target.query
                connection.request(current.get_method(), path, body=current.data,
                                   headers=dict(current.header_items()))
                response = connection.getresponse()
            except Exception:
                connection.close()
                raise
            wrapped = PooledResponse(response, connection,
                                     lambda conn, reusable: self._release(origin, conn, reusable))
            if self.allow_redirects and response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if location and redirect < 10 and current.get_method() == "GET":
                    destination = urljoin(current.full_url, location)
                    wrapped.close()  # unread redirect body: discard this connection
                    if urlsplit(destination).scheme != "https":
                        raise OSError("insecure_download_redirect")
                    # Signed download URLs never carry the Graph bearer token.
                    safe_headers = {key: value for key, value in current.header_items()
                                    if key.lower() in ("range", "if-range", "accept-encoding", "user-agent")}
                    current = Request(destination, headers=safe_headers)
                    continue
            if response.status >= 300:
                raise HTTPError(current.full_url, response.status, response.reason, response.headers, wrapped)
            return wrapped
        raise OSError("too_many_download_redirects")

    def close(self):
        with self.lock:
            for connections in self.idle.values():
                for connection in connections:
                    connection.close()
            self.idle.clear()
