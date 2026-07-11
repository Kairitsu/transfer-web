#!/usr/bin/env python3
import base64
import json
import mimetypes
import os
import posixpath
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


HOST = "127.0.0.1"
PORT = 8756

KR_HOME = Path("/home/azureuser").resolve()
US_HOME = "/home/ubuntu"
REMOTE = "ubuntu@43.130.17.25"
SSH_KEY = "/home/azureuser/Tencent.pem"

APP_DIR = Path("/opt/transfer-web")
STATIC_DIR = APP_DIR / "static"
STATE_DIR = Path("/var/lib/transfer-web")
LOG_DIR = Path("/var/log/transfer-web")
TRANSFER_LOG_DIR = LOG_DIR / "transfers"
STATE_FILE = STATE_DIR / "state.json"

MAX_ITEMS = 100
MAX_LIST_ENTRIES = 5000
MAX_LOG_BYTES = 120_000
BLOCKED_SNIPPETS = ("\x00", "\n", "\r", ";", "`", "|", "$(", "\\")
CACHE_BUST_PATH = "/?v=transfer-web-20260709-2"

SSH_OPTS = [
    "ssh",
    "-i",
    SSH_KEY,
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=6",
]

SSH_CMD = (
    f"ssh -i {shlex.quote(SSH_KEY)} "
    "-o BatchMode=yes "
    "-o StrictHostKeyChecking=accept-new "
    "-o ConnectTimeout=15 "
    "-o ServerAliveInterval=30 "
    "-o ServerAliveCountMax=6"
)

RSYNC_BASE = [
    "rsync",
    "-a",
    "--human-readable",
    "--info=progress2,stats2",
    "--partial",
    "--append-verify",
    "--secluded-args",
    "--safe-links",
    "-e",
    SSH_CMD,
]

REMOTE_HELPER = r'''
import base64
import json
import os
import posixpath
import stat
import sys

BASE = "/home/ubuntu"
MAX_LIST_ENTRIES = 5000
BLOCKED_SNIPPETS = ("\x00", "\n", "\r", ";", "`", "|", "$(", "\\")

def fail(message, code=1):
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    raise SystemExit(code)

def validate_rel(value):
    value = value or ""
    if len(value) > 4096:
        fail("路径过长。")
    if value in ("", "."):
        return ""
    if value.startswith("/"):
        fail("只能使用相对路径。")
    if ".." in value:
        fail("路径不能包含 ..。")
    for snippet in BLOCKED_SNIPPETS:
        if snippet in value:
            fail("路径包含危险字符或片段。")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        fail("路径包含非法路径段。")
    return posixpath.normpath(value)

def is_safe_rel(value):
    try:
        if value in ("", ".") or value.startswith("/") or ".." in value:
            return False
        for snippet in BLOCKED_SNIPPETS:
            if snippet in value:
                return False
        parts = value.split("/")
        return not any(part in ("", ".", "..") for part in parts)
    except Exception:
        return False

def inside(path):
    real = os.path.realpath(path)
    return real == BASE or real.startswith(BASE + "/")

def abs_path(rel, must_exist=False):
    rel = validate_rel(rel)
    target = os.path.join(BASE, rel)
    if must_exist and not os.path.exists(target):
        fail(f"美国服务器路径不存在：{target}")
    real = os.path.realpath(target)
    if not (real == BASE or real.startswith(BASE + "/")):
        fail(f"美国服务器路径跳出了允许目录：{target}")
    return target, real, rel

def entry_to_dict(parent_rel, entry):
    path = entry.path
    st = os.lstat(path)
    mode = st.st_mode
    is_dir = stat.S_ISDIR(mode)
    is_symlink = stat.S_ISLNK(mode)
    rel = posixpath.join(parent_rel, entry.name) if parent_rel else entry.name
    safe = True
    reason = ""
    if not is_safe_rel(rel):
        safe = False
        reason = "路径名包含被禁用的字符"
    if is_symlink:
        safe = False
        reason = "符号链接不可传输"
    return {
        "name": entry.name,
        "path": rel,
        "type": "dir" if is_dir else "symlink" if is_symlink else "file",
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "safe": safe,
        "reason": reason,
    }

op = sys.argv[1]
rel = base64.b64decode(sys.argv[2].encode("ascii")).decode("utf-8")

try:
    if op == "list":
        target, real, rel = abs_path(rel, must_exist=True)
        if not os.path.isdir(real):
            fail("目标不是目录。")
        entries = []
        with os.scandir(real) as it:
            for entry in it:
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
                entries.append(entry_to_dict(rel, entry))
        entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
        print(json.dumps({
            "ok": True,
            "path": rel,
            "root": BASE,
            "entries": entries,
            "limited": len(entries) >= MAX_LIST_ENTRIES,
        }, ensure_ascii=False))
    elif op == "stat":
        target, real, rel = abs_path(rel, must_exist=True)
        st = os.lstat(real)
        if stat.S_ISLNK(st.st_mode):
            fail("符号链接不可传输。")
        print(json.dumps({
            "ok": True,
            "path": rel,
            "type": "dir" if stat.S_ISDIR(st.st_mode) else "file",
        }, ensure_ascii=False))
    elif op == "mkdir":
        target, real, rel = abs_path(rel, must_exist=False)
        os.makedirs(real, exist_ok=True)
        real_after = os.path.realpath(target)
        if not (real_after == BASE or real_after.startswith(BASE + "/")):
            fail("美国服务器目标目录跳出了允许目录。")
        print(json.dumps({"ok": True, "path": rel}, ensure_ascii=False))
    else:
        fail("未知远端操作。", code=2)
except Exception as exc:
    fail(str(exc))
'''


STATE_DIR.mkdir(parents=True, exist_ok=True)
TRANSFER_LOG_DIR.mkdir(parents=True, exist_ok=True)

task_lock = threading.Lock()
current_task = None


class AppError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_rel(value):
    value = value or ""
    if not isinstance(value, str):
        raise AppError("路径必须是字符串。")
    if len(value) > 4096:
        raise AppError("路径过长。")
    if value in ("", "."):
        return ""
    if value.startswith("/"):
        raise AppError("只能使用相对路径。")
    if ".." in value:
        raise AppError("路径不能包含 ..。")
    for snippet in BLOCKED_SNIPPETS:
        if snippet in value:
            raise AppError("路径包含危险字符或片段。")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise AppError("路径包含非法路径段。")
    return posixpath.normpath(value)


def join_rel(directory, name):
    directory = validate_rel(directory)
    name = validate_rel(name)
    return validate_rel(posixpath.join(directory, name) if directory else name)


def local_abs(rel, must_exist=True):
    rel = validate_rel(rel)
    target = KR_HOME / rel if rel else KR_HOME
    if must_exist and not target.exists():
        raise AppError(f"韩国服务器路径不存在：{target}")
    real = target.resolve(strict=must_exist)
    if os.path.commonpath([str(KR_HOME), str(real)]) != str(KR_HOME):
        raise AppError(f"韩国服务器路径跳出了允许目录：{target}")
    return target, real, rel


def local_prepare_dir(rel):
    target, _, rel = local_abs(rel, must_exist=False)
    target.mkdir(parents=True, exist_ok=True)
    real = target.resolve(strict=True)
    if os.path.commonpath([str(KR_HOME), str(real)]) != str(KR_HOME):
        raise AppError(f"韩国服务器目标目录跳出了允许目录：{target}")
    return target, real, rel


def ensure_local_source(rel):
    target, real, rel = local_abs(rel, must_exist=True)
    if target.is_symlink():
        raise AppError("符号链接不可传输。")
    return target, real, rel


def encode_rel(rel):
    safe_rel = validate_rel(rel) or "."
    return base64.b64encode(safe_rel.encode("utf-8")).decode("ascii")


def run_remote(op, rel, timeout=30):
    rel_b64 = encode_rel(rel)
    proc = subprocess.run(
        SSH_OPTS + [REMOTE, "python3", "-", op, rel_b64],
        input=REMOTE_HELPER,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    stdout = proc.stdout.strip()
    if not stdout:
        raise AppError(f"美国服务器无响应：{proc.stderr.strip() or proc.returncode}")
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise AppError(f"无法解析美国服务器响应：{stdout}") from exc
    if not payload.get("ok"):
        raise AppError(payload.get("error", "美国服务器操作失败。"))
    return payload


def remote_abs(rel):
    rel = validate_rel(rel)
    return f"{US_HOME}/{rel}" if rel else US_HOME


def remote_spec(rel):
    return f"{REMOTE}:{remote_abs(rel)}"


def local_entry_to_dict(parent_rel, entry):
    st = entry.stat(follow_symlinks=False)
    is_dir = entry.is_dir(follow_symlinks=False)
    is_symlink = entry.is_symlink()
    rel = join_rel(parent_rel, entry.name)
    safe = True
    reason = ""
    try:
        validate_rel(rel)
    except AppError:
        safe = False
        reason = "路径名包含被禁用的字符"
    if is_symlink:
        safe = False
        reason = "符号链接不可传输"
    return {
        "name": entry.name,
        "path": rel,
        "type": "dir" if is_dir else "symlink" if is_symlink else "file",
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "safe": safe,
        "reason": reason,
    }


def list_local(rel):
    target, real, rel = local_abs(rel, must_exist=True)
    if not real.is_dir():
        raise AppError("目标不是目录。")
    entries = []
    with os.scandir(real) as it:
        for entry in it:
            if len(entries) >= MAX_LIST_ENTRIES:
                break
            try:
                entries.append(local_entry_to_dict(rel, entry))
            except AppError:
                st = entry.stat(follow_symlinks=False)
                entries.append({
                    "name": entry.name,
                    "path": "",
                    "type": "dir" if entry.is_dir(follow_symlinks=False) else "file",
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "safe": False,
                    "reason": "路径名包含被禁用的字符",
                })
    entries.sort(key=lambda item: (item["type"] != "dir", item["name"].lower()))
    return {
        "side": "kr",
        "root": str(KR_HOME),
        "path": rel,
        "entries": entries,
        "limited": len(entries) >= MAX_LIST_ENTRIES,
    }


def list_remote(rel):
    payload = run_remote("list", rel, timeout=40)
    payload["side"] = "us"
    return payload


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length > 1_000_000:
        raise AppError("请求过大。", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise AppError("JSON 请求格式错误。") from exc


def write_state(task):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(task.public(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def append_log(task, text):
    with task.log_file.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
        fh.flush()


def parse_progress(task, text):
    matches = re.findall(r"\s(\d{1,3})%", text)
    if matches:
        value = min(100, int(matches[-1]))
        with task_lock:
            task.progress = value


class TransferTask:
    def __init__(self, direction, items, dest_path):
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.direction = direction
        self.items = items
        self.dest_path = dest_path
        self.status = "running"
        self.started_at = now_iso()
        self.ended_at = ""
        self.exit_code = None
        self.error = ""
        self.current_item = ""
        self.current_pid = None
        self.progress = 0
        self.stop_requested = False
        self.log_file = TRANSFER_LOG_DIR / f"{self.id}.log"

    def public(self):
        return {
            "id": self.id,
            "direction": self.direction,
            "items": self.items,
            "destPath": self.dest_path,
            "status": self.status,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "exitCode": self.exit_code,
            "error": self.error,
            "currentItem": self.current_item,
            "progress": self.progress,
            "logFile": str(self.log_file),
        }


def run_rsync(task, cmd):
    append_log(task, "\n$ " + shlex.join(cmd) + "\n")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=False,
        preexec_fn=os.setsid,
    )
    with task_lock:
        task.current_pid = proc.pid
        write_state(task)

    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        append_log(task, text)
        parse_progress(task, text)
        with task_lock:
            write_state(task)

    rc = proc.wait()
    with task_lock:
        task.current_pid = None
        write_state(task)
    return rc


def run_transfer(task):
    append_log(task, f"==== {now_iso()} transfer started ====\n")
    append_log(task, f"direction: {task.direction}\n")
    append_log(task, f"destination path: {task.dest_path or '/'}\n")
    append_log(task, "items:\n" + "\n".join(f"  - {item}" for item in task.items) + "\n")

    rc = 0
    try:
        for index, item in enumerate(task.items, start=1):
            with task_lock:
                if task.stop_requested:
                    task.status = "stopped"
                    task.error = "用户停止任务。"
                    break
                task.current_item = item
                task.progress = 0
                write_state(task)

            append_log(task, f"\n==== item {index}/{len(task.items)}: {item} ====\n")
            if task.direction == "us_to_kr":
                run_remote("stat", item)
                local_prepare_dir(task.dest_path)
                cmd = RSYNC_BASE + [remote_spec(item), f"{local_abs(task.dest_path, must_exist=True)[1]}/"]
            elif task.direction == "kr_to_us":
                src_target, src_real, _ = ensure_local_source(item)
                run_remote("mkdir", task.dest_path)
                cmd = RSYNC_BASE + [str(src_real), f"{REMOTE}:{remote_abs(task.dest_path)}/"]
            else:
                raise AppError("未知传输方向。")

            rc = run_rsync(task, cmd)
            if rc != 0:
                with task_lock:
                    if task.stop_requested:
                        task.status = "stopped"
                        task.error = "用户停止任务。"
                    else:
                        task.status = "failed"
                        task.error = f"rsync 退出码：{rc}"
                    task.exit_code = rc
                    task.ended_at = now_iso()
                    write_state(task)
                append_log(task, f"\n==== {now_iso()} transfer ended, exit={rc} ====\n")
                return

        with task_lock:
            if task.status == "running":
                task.status = "completed"
                task.progress = 100
            task.exit_code = 0 if task.status == "completed" else rc
            task.ended_at = now_iso()
            write_state(task)
        append_log(task, f"\n==== {now_iso()} transfer {task.status}, exit={task.exit_code} ====\n")
    except Exception as exc:
        with task_lock:
            task.status = "failed" if not task.stop_requested else "stopped"
            task.error = str(exc)
            task.exit_code = 1
            task.ended_at = now_iso()
            write_state(task)
        append_log(task, f"\nERROR: {exc}\n==== {now_iso()} transfer {task.status} ====\n")


def start_transfer(payload):
    direction = payload.get("direction")
    if direction not in ("us_to_kr", "kr_to_us"):
        raise AppError("未知传输方向。")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise AppError("请选择要传输的文件或文件夹。")
    if len(items) > MAX_ITEMS:
        raise AppError(f"一次最多传输 {MAX_ITEMS} 个项目。")
    items = [validate_rel(item) for item in items]
    if any(item == "" for item in items):
        raise AppError("不能直接选择根目录传输。")
    dest_path = validate_rel(payload.get("destPath", ""))

    with task_lock:
        global current_task
        if current_task and current_task.status == "running":
            raise AppError("已有传输任务正在运行。", HTTPStatus.CONFLICT)
        task = TransferTask(direction, items, dest_path)
        current_task = task
        write_state(task)

    thread = threading.Thread(target=run_transfer, args=(task,), daemon=False)
    thread.start()
    return task.public()


def stop_transfer():
    with task_lock:
        task = current_task
        if not task or task.status != "running":
            return {"stopped": False, "message": "当前没有运行中的传输任务。"}
        task.stop_requested = True
        pid = task.current_pid
        write_state(task)

    if pid:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {"stopped": True, "message": "已请求停止当前传输任务。"}


def current_status():
    with task_lock:
        if current_task:
            return {"task": current_task.public()}
    if STATE_FILE.exists():
        try:
            return {"task": json.loads(STATE_FILE.read_text(encoding="utf-8"))}
        except json.JSONDecodeError:
            pass
    return {"task": None}


def read_log(task_id=None):
    task = None
    with task_lock:
        if current_task and (task_id in (None, "", current_task.id)):
            task = current_task
    if task:
        path = task.log_file
    elif task_id:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "", task_id)
        path = TRANSFER_LOG_DIR / f"{safe_id}.log"
    else:
        state = current_status().get("task")
        path = Path(state["logFile"]) if state and state.get("logFile") else None
    if not path or not path.exists():
        return {"log": ""}
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > MAX_LOG_BYTES:
            fh.seek(size - MAX_LOG_BYTES)
        data = fh.read().decode("utf-8", errors="replace")
    return {"log": data, "truncated": size > MAX_LOG_BYTES, "logFile": str(path)}


class Handler(BaseHTTPRequestHandler):
    server_version = "TransferWeb/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, exc):
        status = exc.status if isinstance(exc, AppError) else HTTPStatus.INTERNAL_SERVER_ERROR
        self.send_json({"ok": False, "error": str(exc)}, status)

    def serve_static(self, rel_path):
        if rel_path in ("", "/"):
            rel_path = "index.html"
        else:
            rel_path = rel_path.lstrip("/")
        path = (STATIC_DIR / rel_path).resolve()
        if os.path.commonpath([str(STATIC_DIR.resolve()), str(path)]) != str(STATIC_DIR.resolve()):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if path.name == "index.html":
            self.send_header("Clear-Site-Data", '"cache", "storage"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def serve_legacy_olivetin_path(self, path):
        if path.startswith("/assets/") and path.endswith(".js"):
            body = f"window.location.replace({json.dumps(CACHE_BUST_PATH)});\n".encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Clear-Site-Data", '"cache", "storage"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/assets/") and path.endswith(".css"):
            body = b""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Clear-Site-Data", '"cache", "storage"')
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", CACHE_BUST_PATH)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Clear-Site-Data", '"cache", "storage"')
        self.end_headers()

    def discard_request_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 0:
            self.rfile.read(length)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/list":
                qs = parse_qs(parsed.query)
                side = (qs.get("side") or [""])[0]
                rel = (qs.get("path") or [""])[0]
                if side == "kr":
                    self.send_json({"ok": True, **list_local(rel)})
                elif side == "us":
                    self.send_json({"ok": True, **list_remote(rel)})
                else:
                    raise AppError("未知面板。")
            elif parsed.path == "/api/status":
                self.send_json({"ok": True, **current_status()})
            elif parsed.path == "/api/logs":
                qs = parse_qs(parsed.query)
                task_id = (qs.get("task") or [""])[0]
                self.send_json({"ok": True, **read_log(task_id)})
            elif (
                parsed.path.startswith("/assets/")
                or parsed.path.startswith("/olivetin.api.v1.")
                or parsed.path.startswith("/api/olivetin.api.v1.")
            ):
                self.serve_legacy_olivetin_path(parsed.path)
            elif parsed.path == "/" or parsed.path.startswith("/static/"):
                self.serve_static(parsed.path.removeprefix("/static/") if parsed.path.startswith("/static/") else "index.html")
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_error_json(exc)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/olivetin.api.v1.") or parsed.path.startswith("/olivetin.api.v1."):
                self.discard_request_body()
                self.serve_legacy_olivetin_path(parsed.path)
            elif parsed.path == "/api/transfer":
                self.send_json({"ok": True, "task": start_transfer(read_json(self))}, HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/stop":
                self.send_json({"ok": True, **stop_transfer()})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_error_json(exc)


def main():
    os.chdir(str(APP_DIR))
    print(f"{now_iso()} transfer-web starting on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
