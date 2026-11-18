"""HTTP server: routes /healthz, git smart-HTTP, REST v3, GraphQL and MCP.
"""
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import git_http, gitobj, graphql, mcp, rest
from .store import STORE, ZERO


def _expected_token():
    import os
    return os.environ.get("MAKEFUN_SHOP_TOKEN", "")


def _authorized(handler):
    expected = _expected_token()
    auth = handler.headers.get("Authorization") or ""
    if expected:
        return auth == "Bearer " + expected
    return auth.startswith("Bearer ") and len(auth) > 7


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GithubForge/1.0"

    # -- plumbing -------------------------------------------------------
    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        return self.rfile.read(n)

    def _send(self, status, body, content_type="application/json",
              extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(self, status, payload, request_id=True, extra_headers=None):
        extra = dict(extra_headers or {})
        if request_id:
            extra["X-GitHub-Request-Id"] = uuid.uuid4().hex
        self._send(status, json.dumps(payload), "application/json", extra)

    def _unauthorized(self, restish=True):
        self._send_json(401, {"message": "Bad credentials"})

    # -- verbs ----------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    # -- routing --------------------------------------------------------
    def _dispatch(self):
        from urllib.parse import urlsplit, parse_qs
        parts = urlsplit(self.path)
        path = parts.path
        query = parts.query
        # Consume the request body up front, even for routes that answer
        # 401/404 before using it: on a keep-alive connection any unread
        # body bytes are parsed as the next request line and the client
        # gets an HTML "Bad request syntax" page instead of JSON.
        body = self._read_body() if self.command in (
            "POST", "PUT", "PATCH", "DELETE") else b""

        if path == "/healthz":
            if self.command == "GET":
                self._send_json(200, {"ok": True}, request_id=False)
            else:
                self._send_json(404, {"message": "Not Found"})
            return

        if path.startswith("/api/v3"):
            if not _authorized(self):
                self._unauthorized()
                return
            sub = path[len("/api/v3"):]
            segments = [s for s in sub.split("/") if s != ""]
            query_dict = parse_qs(query)
            base_url = "http://%s" % (
                self.headers.get("Host") or "127.0.0.1")
            try:
                status, payload, extra = rest.handle_rest(
                    STORE, self.command, segments, query_dict,
                    self.headers, body, base_url)
            except rest.RestError as err:
                status, payload, extra = err.status, {"message": err.message}, {}
            self._send_json(status, payload, extra_headers=extra)
            return

        if path == "/api/graphql":
            if self.command != "POST":
                self._send_json(404, {"message": "Not Found"})
                return
            if not _authorized(self):
                self._unauthorized()
                return
            try:
                parsed = json.loads(body) if body else {}
            except Exception:
                self._send_json(400, {"errors": [{"message": "Parse error"}]},
                                request_id=False)
                return
            payload = graphql.handle_graphql(STORE, parsed)
            self._send_json(200, payload, request_id=False)
            return

        if path == "/mcp":
            if self.command != "POST":
                self._send_json(404, {"message": "Not Found"})
                return
            if not _authorized(self):
                self._unauthorized()
                return
            status, payload = mcp.handle_mcp(body)
            if payload is None:
                self._send(202, b"", "application/json")
            else:
                self._send_json(status, payload, request_id=False)
            return

        # git smart HTTP
        git_path = self._git_path(path)
        if git_path is not None:
            owner, repo_name, action = git_path
            repo = STORE.get_repo(owner, repo_name)
            if repo is None:
                self._send_json(404, {"message": "Not Found"})
                return
            self._handle_git(repo, action, body)
            return

        self._send_json(404, {"message": "Not Found"})

    def _git_path(self, path):
        """(owner, repo, action) for a git smart-HTTP path or None."""
        if path.endswith("/info/refs"):
            base = path[:-len("/info/refs")]
            action = "info/refs"
        elif path.endswith("/git-upload-pack"):
            base = path[:-len("/git-upload-pack")]
            action = "git-upload-pack"
        elif path.endswith("/git-receive-pack"):
            base = path[:-len("/git-receive-pack")]
            action = "git-receive-pack"
        else:
            return None
        if not base.startswith("/"):
            return None
        parts = base[1:].split("/")
        if len(parts) != 2 or not parts[1].endswith(".git"):
            return None
        return parts[0], parts[1][:-4], action

    def _handle_git(self, repo, action, body):
        from urllib.parse import urlsplit, parse_qs
        query = parse_qs(urlsplit(self.path).query)
        service = (query.get("service", [""])[0]
                   if action == "info/refs" else None)
        version = self.headers.get("Git-Protocol", "")

        if action == "info/refs":
            if self.command != "GET" or service not in (
                    "git-upload-pack", "git-receive-pack"):
                self._send_json(404, {"message": "Not Found"})
                return
            v2 = "version=2" in version
            ct, body = git_http.advertisement(
                repo, service, 2 if (v2 and service == "git-upload-pack")
                else 0)
            self._send(200, body, ct)
            return

        if self.command != "POST":
            self._send_json(404, {"message": "Not Found"})
            return
        if action == "git-upload-pack":
            if "version=2" in version:
                req = git_http.parse_fetch_request_v2(
                    gitobj.decode_pkt_lines(body))
                if req is None:
                    self._send_json(400, {"message": "Bad request"})
                    return
                if req["command"] == "ls-refs":
                    parsed = git_http.parse_ls_refs_request(
                        gitobj.decode_pkt_lines(body))
                    if parsed is None:
                        self._send_json(400, {"message": "Bad request"})
                        return
                    prefixes, peel, symrefs, unborn = parsed
                    out = git_http.ls_refs(repo, prefixes, peel, symrefs,
                                           unborn)
                    self._send(200, out,
                               "application/x-git-upload-pack-result")
                    return
                out = git_http.fetch_v2(repo, req["wants"], req["haves"],
                                        req["shallow"], req["depth"],
                                        req["done"])
                self._send(200, out, "application/x-git-upload-pack-result")
            else:
                req = git_http.parse_fetch_request_v0(
                    gitobj.decode_pkt_lines(body))
                if req is None:
                    self._send_json(400, {"message": "Bad request"})
                    return
                out = git_http.fetch_v0(repo, req["wants"], req["haves"],
                                        req["shallow"], req["depth"])
                self._send(200, out, "application/x-git-upload-pack-result")
            return

        # receive-pack (v0 only)
        parsed = git_http.parse_push_request(body)
        if parsed is None:
            self._send_json(400, {"message": "Bad request"})
            return
        commands, pack = parsed
        lines, _results = git_http.apply_push(repo, STORE, commands, pack)
        out = git_http.push_report_body(lines)
        self._send(200, out, "application/x-git-receive-pack-result")


def make_server(port=0):
    import socket

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        # 50 concurrent clones in the benchmark open ~150 connections at
        # once; the default backlog of 5 drops/refuses the excess and the
        # client retries with backoff (round times swung 4.4 s -> 0.14 s).
        request_queue_size = 128

        def get_request(self):
            sock, addr = super().get_request()
            # Disable Nagle: without this, a keep-alive response whose
            # headers and body go out as separate segments stalls ~40 ms
            # per request waiting for the client's delayed ACK. The read
            # benchmark (2,000 sequential GETs on one connection) times
            # out at 41 ms/req without it and runs at <1 ms/req with it.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock, addr

    server = _Server(("127.0.0.1", port), _Handler)
    return server
