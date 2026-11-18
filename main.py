"""Entrypoint: a GitHub-compatible git forge.

Trial lifecycle (from the starter README):
  * bind port 0 (never 8080), POST {"port": N} to $MAKEFUN_ENV_URL/register
  * the environment then GETs /healthz (twice) and runs the checks
  * poll GET $MAKEFUN_ENV_URL/done until {"done": true}, then exit 0
  * without MAKEFUN_ENV_URL, serve standalone on a free port when
    FORGE_STANDALONE=1 (local testing), else exit 1
"""
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from forge.server import make_server  # noqa: E402


def _env_call(env_url, path, method="GET", payload=None):
    url = env_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"{}")
        except Exception:
            return err.code, {}
    except Exception:
        return None, {}


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "")
    server = make_server(0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    stop = threading.Event()

    def _shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if env_url:
        deadline = time.time() + 60
        registered = False
        while time.time() < deadline and not stop.is_set():
            status, _ = _env_call(env_url, "/register", "POST",
                                  {"port": port})
            if status in (200, 201, 409):
                registered = True
                break
            time.sleep(1)
        if not registered:
            server.shutdown()
            return 1
        # poll /done until the environment is finished
        deadline = time.time() + 1800
        while time.time() < deadline and not stop.is_set():
            status, body = _env_call(env_url, "/done")
            if status == 200 and body.get("done") is True:
                break
            time.sleep(1)
    elif os.environ.get("FORGE_STANDALONE") == "1":
        print("github-forge listening on http://127.0.0.1:%d" % port,
              flush=True)
        while not stop.is_set():
            time.sleep(0.2)
    else:
        server.shutdown()
        print("MAKEFUN_ENV_URL is not set; nothing to do", file=sys.stderr)
        return 1

    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
