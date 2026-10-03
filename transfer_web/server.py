import argparse
import json
import mimetypes
import re
import signal
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ConfigError, find_config_path, load_config
from .endpoints import EndpointClient, control_dir_for, prepare_control_dir
from .errors import AppError
from .tasks import TaskManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
MAX_BODY_BYTES = 1_000_000
HEALTH_TTL_SECONDS = 15
TASK_PATH_RE = re.compile(r"^/api/tasks/([A-Za-z0-9_.-]+)/(log|cancel)$")
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
QUIET_PATHS = ("/api/status", "/api/health", "/api/list", "/static/")


class App:
    def __init__(self, config):
        self.config = config
        control_dir = None
        if any(not endpoint.is_local for endpoint in config.endpoints.values()):
            control_dir = prepare_control_dir(control_dir_for(config.state_dir))
        self.clients = {
            endpoint_id: EndpointClient(endpoint, None if endpoint.is_local else control_dir)
            for endpoint_id, endpoint in config.endpoints.items()
        }
        self.tasks = TaskManager(config, self.clients)
        self._health = {}
        self._health_lock = threading.Lock()
        self._health_pool = ThreadPoolExecutor(max_workers=max(2, len(self.clients)))

    def client(self, endpoint_id):
        client = self.clients.get(endpoint_id)
        if not client:
            raise AppError(f"未知端点：{endpoint_id}" if endpoint_id else "未指定端点。")
        return client

    def public_config(self, user):
        # rsync needs one side of every transfer to be this machine.
        transferable = {
            a.id: [b.id for b in self.clients.values() if b.id != a.id and (a.is_local or b.is_local)]
            for a in self.clients.values()
        }
        return {
            "title": self.config.title,
            "endpoints": [client.endpoint.public() for client in self.clients.values()],
            "layout": {"left": self.config.left, "right": self.config.right},
            "transferable": transferable,
            "user": user,
        }

    def _check_one(self, client):
        started = time.monotonic()
        try:
            client.ping()
            result = {"ok": True, "error": ""}
        except AppError as exc:
            result = {"ok": False, "error": str(exc)}
        result["latencyMs"] = round((time.monotonic() - started) * 1000)
        result["checkedAt"] = time.time()
        return result

    def health(self, force=False):
        now = time.time()
        with self._health_lock:
            stale = [
                client for client in self.clients.values()
                if force or now - self._health.get(client.id, {}).get("checkedAt", 0) > HEALTH_TTL_SECONDS
            ]
        if stale:
            results = dict(zip((c.id for c in stale), self._health_pool.map(self._check_one, stale)))
            with self._health_lock:
                self._health.update(results)
        with self._health_lock:
            return {"endpoints": dict(self._health)}

    def shutdown(self):
        self.tasks.shutdown()
        for client in self.clients.values():
            try:
                client.close_master()
            except Exception:
                pass


class Handler(BaseHTTPRequestHandler):
    server_version = "TransferWeb/2.0"
    app = None  # set by make_server

    # ---- plumbing ------------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def log_request(self, code="-", size="-"):
        path = urlparse(self.path).path
        try:
            ok = int(code) < 400
        except (TypeError, ValueError):
            ok = False
        if ok and self.command == "GET" and path.startswith(QUIET_PATHS):
            return  # polling noise
        super().log_request(code, size)

    def end_headers(self):
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, exc):
        if isinstance(exc, AppError):
            self.send_json({"ok": False, "error": str(exc)}, exc.status)
            return
        traceback.print_exc()
        self.send_json({"ok": False, "error": "服务器内部错误，详见服务日志。"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise AppError("Content-Length 无效。") from exc
        if length > MAX_BODY_BYTES:
            raise AppError("请求过大。", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError("JSON 请求格式错误。") from exc
        if not isinstance(payload, dict):
            raise AppError("JSON 请求必须是对象。")
        return payload

    # ---- access control ------------------------------------------------------

    def tailscale_user(self):
        return (self.headers.get("Tailscale-User-Login") or "").strip()

    def check_user(self):
        allowed = self.app.config.allowed_users
        if allowed and self.tailscale_user() not in allowed:
            raise AppError("没有访问权限。", HTTPStatus.FORBIDDEN)

    def check_same_origin(self):
        """Reject cross-site writes (CSRF).

        Requiring application/json already forces a CORS preflight that this
        server never approves; the fetch-metadata and Origin checks back it up.
        """
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise AppError("请求必须是 application/json。", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            raise AppError("拒绝跨站请求。", HTTPStatus.FORBIDDEN)
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            origin_host = urlparse(origin).netloc.lower()
            hosts = {
                (self.headers.get(name) or "").split(",")[0].strip().lower()
                for name in ("Host", "X-Forwarded-Host")
            }
            if origin_host not in hosts:
                raise AppError("拒绝跨站请求。", HTTPStatus.FORBIDDEN)
        elif origin == "null":
            raise AppError("拒绝跨站请求。", HTTPStatus.FORBIDDEN)

    # ---- static files --------------------------------------------------------

    def serve_static(self, name):
        path = (STATIC_DIR / name).resolve()
        if path.parent != STATIC_DIR or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        st = path.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Always revalidate so a redeploy shows up on the next load.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    # ---- routes ---------------------------------------------------------------

    def do_GET(self):
        try:
            self.check_user()
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            def arg(name, default=""):
                return (qs.get(name) or [default])[0]

            app = self.app
            if path == "/":
                self.serve_static("index.html")
            elif path.startswith("/static/"):
                self.serve_static(path[len("/static/"):])
            elif path == "/api/config":
                self.send_json({"ok": True, **app.public_config(self.tailscale_user())})
            elif path == "/api/list":
                show_hidden = arg("showHidden").lower() in ("1", "true", "yes", "on")
                listing = app.client(arg("endpoint")).list(arg("path"), show_hidden=show_hidden)
                self.send_json({"ok": True, **listing})
            elif path == "/api/health":
                self.send_json({"ok": True, **app.health(force=arg("force") == "1")})
            elif path == "/api/status":
                self.send_json({"ok": True, **app.tasks.status()})
            elif path == "/api/tasks":
                limit = int(arg("limit", "0")) if arg("limit", "0").isdigit() else 0
                self.send_json({"ok": True, **app.tasks.history(limit or None)})
            elif TASK_PATH_RE.match(path) and path.endswith("/log"):
                task_id = TASK_PATH_RE.match(path).group(1)
                offset = int(arg("offset")) if arg("offset").isdigit() else None
                self.send_json({"ok": True, **app.tasks.read_log(task_id, offset)})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_error_json(exc)

    def do_POST(self):
        try:
            self.check_user()
            self.check_same_origin()
            path = urlparse(self.path).path
            payload = self.read_json()
            app = self.app
            if path == "/api/preview":
                self.send_json({"ok": True, "preview": app.tasks.preview(payload)})
            elif path == "/api/transfer":
                self.send_json({"ok": True, "task": app.tasks.submit(payload)}, HTTPStatus.ACCEPTED)
            elif TASK_PATH_RE.match(path) and path.endswith("/cancel"):
                task_id = TASK_PATH_RE.match(path).group(1)
                self.send_json({"ok": True, **app.tasks.cancel(task_id)})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_error_json(exc)


def make_server(app):
    handler = type("BoundHandler", (Handler,), {"app": app})
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((app.config.listen, app.config.port), handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Transfer Web")
    parser.add_argument("--config", help="配置文件路径（默认 /etc/transfer-web/config.ini 或仓库内 config.ini）")
    args = parser.parse_args(argv)
    try:
        config = load_config(find_config_path(args.config))
    except ConfigError as exc:
        sys.stderr.write(f"配置错误：{exc}\n")
        return 2

    app = App(config)
    server = make_server(app)

    def on_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    print(f"transfer-web listening on {config.listen}:{config.port} (config: {config.path})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("transfer-web shutting down", flush=True)
        app.shutdown()
        server.server_close()
    return 0
