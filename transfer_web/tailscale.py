"""Best-effort startup checks for the Tailscale front door (`tailscale serve`).

Nothing here is required for the server to run; it only tells the operator
how the page is reached, or what is still missing.
"""
import json
import shutil
import subprocess
from urllib.parse import urlparse

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
UNKNOWN = object()


def _cli_json(*args):
    """Run `tailscale <args> --json`; UNKNOWN if the CLI is missing or fails."""
    exe = shutil.which("tailscale")
    if not exe:
        return UNKNOWN
    try:
        result = subprocess.run(
            [exe, *args, "--json"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if result.returncode != 0:
        return UNKNOWN
    try:
        return json.loads(result.stdout or "null")
    except ValueError:
        return UNKNOWN


def serve_urls(serve_config, port):
    """HTTPS URLs whose root `tailscale serve` proxies to 127.0.0.1:<port>."""
    urls = []
    for host_port, web in ((serve_config or {}).get("Web") or {}).items():
        proxy = (((web or {}).get("Handlers") or {}).get("/") or {}).get("Proxy") or ""
        target = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        try:
            target_port = target.port
        except ValueError:
            continue
        if target_port == port and (target.hostname or "") in LOOPBACK_HOSTS:
            host, _, https_port = host_port.rpartition(":")
            urls.append(f"https://{host}" if https_port == "443" else f"https://{host_port}")
    return sorted(urls)


def startup_notes(port):
    """Lines to print at startup when require_tailscale is on."""
    status = _cli_json("status")
    if status is UNKNOWN or not isinstance(status, dict):
        return [
            "警告：无法读取 Tailscale 状态（没装 tailscale 或 tailscaled 没在运行）。",
            "      本服务只接受经 tailscale serve 转发的请求，请先运行 sudo ./install.sh 或安装 Tailscale。",
        ]
    if status.get("BackendState") != "Running":
        return [f"警告：Tailscale 当前状态为 {status.get('BackendState') or '未知'}，请先执行 sudo tailscale up 登录。"]
    serve_config = _cli_json("serve", "status")
    if serve_config is UNKNOWN:
        return [f"提示：请确认已执行 sudo tailscale serve --bg {port}，然后通过 Tailscale 给出的 https 地址访问。"]
    urls = serve_urls(serve_config, port)
    if not urls:
        return [f"警告：tailscale serve 还没有转发到本服务，请执行 sudo tailscale serve --bg {port}"]
    return [f"通过 Tailscale 访问：{url}" for url in urls]
