import json
import os
import posixpath
import shlex
import subprocess
import tempfile
import threading
from http import HTTPStatus
from pathlib import Path

from . import helper
from .errors import AppError

# Unix socket paths are limited to ~104 bytes; %C expands to 40 hex characters.
MAX_CONTROL_DIR_LENGTH = 60

SSH_ERROR_HINTS = (
    ("Host key verification failed", "主机指纹未知或已变化，已拒绝连接。请按 README 的“固定主机指纹”一节核对并写入 known_hosts。"),
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "主机指纹与 known_hosts 中记录的不一致，已拒绝连接。请确认服务器是否重装过。"),
    ("Permission denied", "SSH 认证失败，请检查 user 和 identity_file 配置。"),
    ("Could not resolve hostname", "无法解析主机名，请检查 host 配置和 Tailscale 是否在线。"),
    ("Connection timed out", "连接超时，请检查网络和 Tailscale 是否在线。"),
    ("Connection refused", "连接被拒绝，请检查端口和 SSH 服务。"),
    ("command not found", "远端缺少 python3，请安装或在配置中设置 python 路径。"),
)


def control_dir_for(state_dir):
    preferred = Path(state_dir) / "ssh"
    if len(str(preferred)) <= MAX_CONTROL_DIR_LENGTH:
        return preferred
    return Path(tempfile.gettempdir()) / f"transfer-web-{os.getuid()}"


def prepare_control_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    if path.stat().st_uid != os.getuid():
        raise RuntimeError(f"SSH 控制目录不属于当前用户：{path}")
    return path


class EndpointClient:
    """Directory operations and rsync arguments for one configured endpoint."""

    def __init__(self, endpoint, control_dir=None):
        self.endpoint = endpoint
        self.control_dir = control_dir
        self._master_lock = threading.Lock()

    @property
    def id(self):
        return self.endpoint.id

    @property
    def name(self):
        return self.endpoint.name

    @property
    def is_local(self):
        return self.endpoint.is_local

    def display_path(self, rel=""):
        root = self.endpoint.root
        if not rel:
            return root if root.endswith("/") else root + "/"
        return posixpath.join(root, rel)

    # ---- SSH -----------------------------------------------------------

    def ssh_command(self, control_master="no"):
        ep = self.endpoint
        cmd = [
            "ssh",
            "-p", str(ep.port),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=6",
        ]
        if ep.identity_file:
            cmd += ["-i", ep.identity_file, "-o", "IdentitiesOnly=yes"]
        if ep.known_hosts:
            cmd += ["-o", f"UserKnownHostsFile={ep.known_hosts}"]
        if self.control_dir:
            cmd += ["-o", f"ControlPath={self.control_dir}/%C", "-o", f"ControlMaster={control_master}"]
        return cmd

    def ensure_master(self):
        """Start a shared background SSH connection so later calls skip the handshake.

        Clients use ControlMaster=no: when no master is running they connect
        directly instead of spawning a persistent master that would inherit
        (and hold open) our output pipes.
        """
        if not self.control_dir:
            return
        with self._master_lock:
            check = subprocess.run(
                self.ssh_command() + ["-O", "check", self.endpoint.ssh_target],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
            if check.returncode == 0:
                return
            try:
                subprocess.run(
                    self.ssh_command("yes") + ["-o", "ControlPersist=300", "-N", self.endpoint.ssh_target],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30, check=False,
                )
            except subprocess.TimeoutExpired:
                pass  # the direct connection below reports the real error

    def close_master(self):
        if self.is_local or not self.control_dir:
            return
        subprocess.run(
            self.ssh_command() + ["-O", "exit", self.endpoint.ssh_target],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )

    def explain_ssh_error(self, stderr, returncode):
        text = (stderr or "").strip()
        for needle, hint in SSH_ERROR_HINTS:
            if needle in text:
                return f"{self.name}：{hint}"
        last = text.splitlines()[-1] if text else f"退出码 {returncode}"
        return f"{self.name}：SSH 失败：{last}"

    # ---- helper calls ----------------------------------------------------

    def call(self, request, timeout=40):
        request = dict(request, root=self.endpoint.root)
        if self.is_local:
            try:
                return helper.run(request)
            except helper.HelperError as exc:
                raise AppError(f"{self.name}：{exc}") from exc
            except OSError as exc:
                raise AppError(f"{self.name}：{exc.strerror or exc}") from exc

        self.ensure_master()
        cmd = self.ssh_command() + [self.endpoint.ssh_target, shlex.quote(self.endpoint.python), "-"]
        try:
            proc = subprocess.run(
                cmd, input=helper.remote_script(request), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError(f"{self.name}：连接超时。", HTTPStatus.GATEWAY_TIMEOUT) from exc
        lines = proc.stdout.strip().splitlines()
        if not lines:
            raise AppError(self.explain_ssh_error(proc.stderr, proc.returncode), HTTPStatus.BAD_GATEWAY)
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise AppError(f"{self.name}：无法解析服务器响应。", HTTPStatus.BAD_GATEWAY) from exc
        if not payload.get("ok"):
            raise AppError(f"{self.name}：{payload.get('error', '操作失败。')}")
        return payload

    def list(self, rel, show_hidden=False):
        payload = self.call({"op": "list", "path": rel, "showHidden": show_hidden})
        return {
            "endpoint": self.id,
            "path": payload["path"],
            "root": self.endpoint.root,
            "entries": payload["entries"],
            "limited": payload["limited"],
        }

    def check_sources(self, paths):
        results = self.call({"op": "check", "paths": paths}, timeout=60)["results"]
        errors = [item["error"] for item in results if not item["ok"]]
        if errors:
            raise AppError(f"{self.name}：" + "；".join(errors[:5]))
        return results

    def stat(self, rel):
        return self.call({"op": "stat", "path": rel})

    def mkdir(self, rel):
        return self.call({"op": "mkdir", "path": rel})

    def ping(self):
        self.call({"op": "ping"}, timeout=20)

    # ---- rsync -------------------------------------------------------------

    def rsync_path(self, rel, directory=False):
        """The rsync argument for a path under this endpoint's root."""
        if self.is_local:
            base = os.path.realpath(self.endpoint.root)
        else:
            base = self.endpoint.root
        path = posixpath.join(base, rel) if rel else base
        if directory and not path.endswith("/"):
            path += "/"
        return path if self.is_local else f"{self.endpoint.ssh_target}:{path}"

    def rsync_shell(self):
        return shlex.join(self.ssh_command())
