import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus

from . import rsync
from .config import Endpoint
from .endpoints import EndpointClient
from .errors import AppError
from .paths import validate_rel

MAX_ITEMS = 100
LOG_TAIL_BYTES = 120_000
LOG_CHUNK_BYTES = 256_000
PREVIEW_TIMEOUT = 180
ACTIVE_STATUSES = ("queued", "running")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_utf8_cut(data):
    """Drop a trailing incomplete UTF-8 sequence so chunks never split a character."""
    end = len(data)
    start = end - 1
    while start >= 0 and end - start < 4 and data[start] & 0xC0 == 0x80:
        start -= 1  # skip continuation bytes back to the lead byte
    if start < 0:
        return data
    lead = data[start]
    if lead < 0x80:
        length = 1
    elif lead >= 0xF0:
        length = 4
    elif lead >= 0xE0:
        length = 3
    elif lead >= 0xC0:
        length = 2
    else:
        return data
    return data if end - start >= length else data[:start]


class Task:
    def __init__(self, src, dst, items, dest_path, skip_existing):
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.src = src
        self.dst = dst
        self.items = items
        self.dest_path = dest_path
        self.skip_existing = skip_existing
        self.status = "queued"
        self.created_at = now_iso()
        self.started_at = ""
        self.ended_at = ""
        self.exit_code = None
        self.error = ""
        self.progress = None
        self.current_file = ""
        self.stats = {}
        self.pid = None
        self.stop_requested = False

    @classmethod
    def from_record(cls, record):
        task = cls.__new__(cls)
        task.id = record["id"]
        task.src = record.get("from", "")
        task.dst = record.get("to", "")
        task.items = record.get("items", [])
        task.dest_path = record.get("destPath", "")
        task.skip_existing = record.get("skipExisting", False)
        task.status = record.get("status", "interrupted")
        task.created_at = record.get("createdAt", "")
        task.started_at = record.get("startedAt", "")
        task.ended_at = record.get("endedAt", "")
        task.exit_code = record.get("exitCode")
        task.error = record.get("error", "")
        task.progress = record.get("progress")
        task.current_file = record.get("currentFile", "")
        task.stats = record.get("stats", {})
        task.pid = None
        task.stop_requested = False
        return task

    def public(self):
        return {
            "id": self.id,
            "from": self.src,
            "to": self.dst,
            "items": self.items,
            "destPath": self.dest_path,
            "skipExisting": self.skip_existing,
            "status": self.status,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "exitCode": self.exit_code,
            "error": self.error,
            "progress": self.progress,
            "currentFile": self.current_file,
            "stats": self.stats,
        }


class TaskManager:
    """Runs transfers one at a time from a queue and keeps a bounded history."""

    def __init__(self, config, clients):
        self.config = config
        self.clients = clients
        self.history_file = config.state_dir / "tasks.json"
        self.log_dir = config.log_dir / "transfers"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Condition()
        self.tasks = []  # newest first
        self.queue = deque()
        self.active = None
        self.shutting_down = False
        self._load_history()
        self.cleanup_logs()
        self.worker = threading.Thread(target=self._worker, name="transfer-worker", daemon=True)
        self.worker.start()

    # ---- persistence -------------------------------------------------------

    def _load_history(self):
        records = []
        if self.history_file.exists():
            try:
                records = json.loads(self.history_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                records = []
        changed = False
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or not TASK_ID_RE.match(str(record.get("id", ""))):
                continue
            task = Task.from_record(record)
            if task.status in ACTIVE_STATUSES:
                # The previous process died with this task unfinished.
                task.status = "interrupted"
                task.error = task.error or "服务重启，任务被中断。"
                task.ended_at = task.ended_at or now_iso()
                changed = True
            self.tasks.append(task)
        if changed:
            self._save()

    def _save(self):
        records = [task.public() for task in self.tasks[: self.config.history_limit]]
        tmp = self.history_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.history_file)

    def log_path(self, task_id):
        if not TASK_ID_RE.match(task_id or ""):
            raise AppError("任务 ID 无效。")
        return self.log_dir / f"{task_id}.log"

    def cleanup_logs(self):
        """Trim history to its limit and delete logs past the retention period."""
        with self.lock:
            dropped = self.tasks[self.config.history_limit:]
            del self.tasks[self.config.history_limit:]
            active_id = self.active.id if self.active else None
        for task in dropped:
            self.log_path(task.id).unlink(missing_ok=True)
        if self.config.log_retention_days <= 0:
            return
        cutoff = time.time() - self.config.log_retention_days * 86400
        for path in self.log_dir.glob("*.log"):
            try:
                if path.stem != active_id and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    def append_log(self, task, text):
        with self.log_path(task.id).open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(text)

    # ---- public API --------------------------------------------------------

    def client(self, endpoint_id):
        client = self.clients.get(endpoint_id)
        if not client:
            raise AppError(f"未知端点：{endpoint_id}" if endpoint_id else "未指定端点。")
        return client

    def parse_request(self, payload):
        src = self.client(payload.get("from"))
        dst = self.client(payload.get("to"))
        rsync.endpoints_for_transfer(src, dst)
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise AppError("请选择要传输的文件或文件夹。")
        if len(items) > MAX_ITEMS:
            raise AppError(f"一次最多传输 {MAX_ITEMS} 个项目。")
        items = [validate_rel(item) for item in items]
        if any(item == "" for item in items):
            raise AppError("不能直接传输根目录。")
        if len(set(items)) != len(items):
            raise AppError("选择的项目有重复。")
        dest_path = validate_rel(payload.get("destPath", ""))
        return src, dst, items, dest_path, bool(payload.get("skipExisting"))

    def preview(self, payload):
        src, dst, items, dest_path, skip_existing = self.parse_request(payload)
        src.check_sources(items)
        dest = dst.stat(dest_path)
        if dest["exists"] and dest["type"] != "dir":
            raise AppError(f"{dst.name}：目标不是目录：{dst.display_path(dest_path)}")
        dest_exists = dest["exists"]
        result = {
            "from": src.id,
            "to": dst.id,
            "items": items,
            "destPath": dest_path,
            "destDisplay": dst.display_path(dest_path),
            "destExists": dest_exists,
        }
        for client in (src, dst):
            if not client.is_local:
                client.ensure_master()
        with tempfile.TemporaryDirectory(prefix="transfer-web-preview-") as empty_dir:
            if dest_exists:
                cmd = rsync.build_command(src, dst, items, dest_path, skip_existing, dry_run=True)
            else:
                # Nothing exists at the destination yet, so every file is new:
                # dry-run into an empty local directory to count and size them.
                empty = EndpointClient(Endpoint(id="__preview__", name="", type="local", root=empty_dir))
                cmd = rsync.build_command(src, empty, items, "", dry_run=True)
            result.update(self._dry_run(cmd))
        return result

    def _dry_run(self, cmd):
        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=PREVIEW_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError("预检超时（文件太多？），可以跳过预检直接开始。", HTTPStatus.GATEWAY_TIMEOUT) from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise AppError(f"预检失败（rsync 退出码 {proc.returncode}）：{stderr.splitlines()[-1] if stderr else ''}")
        return rsync.parse_preview(proc.stdout.decode("utf-8", errors="replace"))

    def submit(self, payload):
        src, dst, items, dest_path, skip_existing = self.parse_request(payload)
        task = Task(src.id, dst.id, items, dest_path, skip_existing)
        with self.lock:
            if self.shutting_down:
                raise AppError("服务正在关闭。", HTTPStatus.SERVICE_UNAVAILABLE)
            self.tasks.insert(0, task)
            self.queue.append(task)
            self._save()
            self.lock.notify_all()
        return task.public()

    def cancel(self, task_id):
        with self.lock:
            task = self._find(task_id)
            if task.status == "queued":
                self.queue.remove(task)
                task.status = "cancelled"
                task.ended_at = now_iso()
                self._save()
                return {"message": "已取消排队中的任务。", "task": task.public()}
            if task.status != "running":
                return {"message": "任务已经结束。", "task": task.public()}
            task.stop_requested = True
            pid = task.pid
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return {"message": "已请求停止任务。", "task": task.public()}

    def status(self):
        """Small payload for frequent polling: active, queued and last finished task."""
        with self.lock:
            last = next((task for task in self.tasks if task.status not in ACTIVE_STATUSES), None)
            return {
                "active": self.active.public() if self.active else None,
                "queued": [task.public() for task in self.queue],
                "last": last.public() if last else None,
            }

    def history(self, limit=None):
        with self.lock:
            tasks = self.tasks[:limit] if limit else list(self.tasks)
            return {"tasks": [task.public() for task in tasks]}

    def read_log(self, task_id, offset=None):
        path = self.log_path(task_id)
        if not path.exists():
            return {"data": "", "offset": 0, "size": 0, "truncated": False}
        size = path.stat().st_size
        truncated = False
        if offset is None or offset < 0 or offset > size:
            # First read: only the tail of a large log.
            offset = max(0, size - LOG_TAIL_BYTES)
            truncated = offset > 0
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(LOG_CHUNK_BYTES)
        data = safe_utf8_cut(data)
        return {
            "data": data.decode("utf-8", errors="replace"),
            "offset": offset + len(data),
            "size": size,
            "truncated": truncated,
        }

    def shutdown(self):
        with self.lock:
            self.shutting_down = True
            for task in self.queue:
                task.status = "interrupted"
                task.error = "服务关闭，任务未执行。"
                task.ended_at = now_iso()
            self.queue.clear()
            active = self.active
            if active:
                active.stop_requested = True
                active.error = "服务关闭，任务被中断。"
            self._save()
            self.lock.notify_all()
        if active and active.pid:
            try:
                os.killpg(active.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.worker.join(timeout=10)

    # ---- worker ------------------------------------------------------------

    def _find(self, task_id):
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise AppError("找不到该任务。", HTTPStatus.NOT_FOUND)

    def _worker(self):
        while True:
            with self.lock:
                while not self.queue and not self.shutting_down:
                    self.lock.wait()
                if self.shutting_down:
                    return
                task = self.queue.popleft()
                task.status = "running"
                task.started_at = now_iso()
                self.active = task
                self._save()
            try:
                self._run(task)
            except AppError as exc:
                with self.lock:
                    task.status = "failed"
                    task.error = str(exc)
                self.append_log(task, f"\n错误：{exc}\n")
            except Exception as exc:  # keep the worker alive whatever happens
                with self.lock:
                    task.status = "failed"
                    task.error = f"内部错误：{exc}"
                self.append_log(task, "\n" + traceback.format_exc())
            finally:
                with self.lock:
                    if task.stop_requested and task.status != "completed":
                        task.status = "interrupted" if self.shutting_down else "stopped"
                        task.error = task.error if self.shutting_down else "用户停止了任务。"
                    elif task.status == "running":
                        task.status = "failed"
                    task.ended_at = now_iso()
                    task.pid = None
                    self.active = None
                    self._save()
                self.append_log(task, f"\n==== {task.ended_at} {task.status} ====\n")
                self.cleanup_logs()

    def _run(self, task):
        src = self.client(task.src)
        dst = self.client(task.dst)
        self.append_log(task, (
            f"==== {task.started_at} 开始 ====\n"
            f"从：{src.name}  {', '.join(src.display_path(item) for item in task.items)}\n"
            f"到：{dst.name}  {dst.display_path(task.dest_path)}\n"
        ))
        src.check_sources(task.items)
        dst.mkdir(task.dest_path)
        cmd = rsync.build_command(src, dst, task.items, task.dest_path, task.skip_existing)
        for client in (src, dst):
            if not client.is_local:
                client.ensure_master()
        self.append_log(task, "$ " + shlex.join(cmd) + "\n\n")

        with self.lock:
            if task.stop_requested:
                return
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            task.pid = proc.pid

        # Progress updates end in \r and only feed the progress bar; every
        # other line (file names, errors, final stats) goes to the log.
        last_progress_line = ""
        tail = deque(maxlen=200)
        buffer = ""
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            *lines, buffer = re.split(r"[\r\n]", buffer)
            log_lines = []
            for line in lines:
                progress = rsync.parse_progress(line)
                if progress:
                    last_progress_line = line.strip()
                    with self.lock:
                        task.progress = progress
                elif line.strip():
                    log_lines.append(line)
            if log_lines:
                tail.extend(log_lines)
                self.append_log(task, "\n".join(log_lines) + "\n")
                with self.lock:
                    task.current_file = log_lines[-1]
        proc.stdout.close()
        if buffer.strip():
            tail.append(buffer)
            self.append_log(task, buffer + "\n")
        rc = proc.wait()
        if last_progress_line:
            self.append_log(task, f"\n最终进度：{last_progress_line}\n")
        with self.lock:
            task.exit_code = rc
            task.stats = rsync.parse_stats("\n".join(tail))
            task.current_file = ""
            if rc == 0 and not task.stop_requested:
                task.status = "completed"
                if task.progress:
                    task.progress = dict(task.progress, percent=100, eta="0:00:00")
            elif not task.stop_requested:
                task.status = "failed"
                task.error = f"rsync 退出码 {rc}，详见日志。"
