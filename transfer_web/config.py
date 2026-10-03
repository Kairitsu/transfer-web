import configparser
import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATHS = (
    Path("/etc/transfer-web/config.ini"),
    Path(__file__).resolve().parent.parent / "config.ini",
)
ENDPOINT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class ConfigError(Exception):
    pass


@dataclass
class Endpoint:
    id: str
    name: str
    type: str  # "local" or "ssh"
    root: str
    host: str = ""
    user: str = ""
    port: int = 22
    identity_file: str = ""
    known_hosts: str = ""
    python: str = "python3"
    owner: str = ""

    @property
    def is_local(self):
        return self.type == "local"

    @property
    def ssh_target(self):
        return f"{self.user}@{self.host}" if self.user else self.host

    def public(self):
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "root": self.root,
            "host": self.host if not self.is_local else "",
        }


@dataclass
class Config:
    path: Path
    listen: str = "127.0.0.1"
    port: int = 8756
    state_dir: Path = Path("/var/lib/transfer-web")
    log_dir: Path = Path("/var/log/transfer-web")
    allowed_users: list = field(default_factory=list)
    # Only accept requests forwarded by `tailscale serve` (they carry the
    # Tailscale-User-Login header, which serve strips from client input).
    require_tailscale: bool = True
    history_limit: int = 200
    log_retention_days: int = 30
    title: str = "Transfer"
    endpoints: dict = field(default_factory=dict)
    left: str = ""
    right: str = ""


def find_config_path(explicit=None):
    if explicit:
        return Path(explicit)
    env = os.environ.get("TRANSFER_WEB_CONFIG")
    if env:
        return Path(env)
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    searched = "、".join(str(path) for path in DEFAULT_CONFIG_PATHS)
    raise ConfigError(
        f"找不到配置文件（已查找：{searched}）。"
        "请复制 config.example.ini 修改后使用，或用 --config / TRANSFER_WEB_CONFIG 指定路径。"
    )


def _int(section, key, default, minimum=0):
    try:
        value = section.getint(key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {key} 必须是整数。") from exc
    if value < minimum:
        raise ConfigError(f"[{section.name}] {key} 不能小于 {minimum}。")
    return value


def _bool(section, key, default):
    try:
        return section.getboolean(key, fallback=default)
    except ValueError as exc:
        raise ConfigError(f"[{section.name}] {key} 只能是 yes 或 no。") from exc


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _dir(base, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _endpoint(section_name, section):
    endpoint_id = section_name.split(":", 1)[1].strip()
    if not ENDPOINT_ID_RE.match(endpoint_id):
        raise ConfigError(f"[{section_name}] 端点 ID 只能包含字母、数字、下划线和短横线。")
    kind = section.get("type", "local").strip()
    if kind not in ("local", "ssh"):
        raise ConfigError(f"[{section_name}] type 只能是 local 或 ssh。")
    root = section.get("root", "").strip()
    if not root.startswith("/"):
        raise ConfigError(f"[{section_name}] root 必须是绝对路径。")
    root = root.rstrip("/") or "/"
    endpoint = Endpoint(
        id=endpoint_id,
        name=section.get("name", endpoint_id).strip() or endpoint_id,
        type=kind,
        root=root,
        host=section.get("host", "").strip(),
        user=section.get("user", "").strip(),
        port=_int(section, "port", 22, minimum=1),
        identity_file=section.get("identity_file", "").strip(),
        known_hosts=section.get("known_hosts", "").strip(),
        python=section.get("python", "python3").strip() or "python3",
        owner=section.get("owner", "").strip(),
    )
    if endpoint.type == "ssh" and not endpoint.host:
        raise ConfigError(f"[{section_name}] ssh 端点必须填写 host。")
    if endpoint.owner and endpoint.type != "local":
        raise ConfigError(f"[{section_name}] owner 只对 local 端点有效（远端文件属于 SSH 登录用户）。")
    if endpoint.owner and not re.match(r"^[A-Za-z0-9_.-]+(:[A-Za-z0-9_.-]+)?$", endpoint.owner):
        raise ConfigError(f"[{section_name}] owner 格式应为 用户 或 用户:组。")
    return endpoint


def load_config(path):
    path = Path(path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as fh:
            parser.read_file(fh)
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc
    except configparser.Error as exc:
        raise ConfigError(f"配置文件格式错误：{exc}") from exc

    base = path.resolve().parent
    config = Config(path=path)
    if parser.has_section("server"):
        server = parser["server"]
        config.listen = server.get("listen", config.listen).strip()
        config.port = _int(server, "port", config.port, minimum=1)
        config.state_dir = _dir(base, server.get("state_dir", str(config.state_dir)))
        config.log_dir = _dir(base, server.get("log_dir", str(config.log_dir)))
        config.allowed_users = [
            user.strip() for user in re.split(r"[,\s]+", server.get("allowed_users", "")) if user.strip()
        ]
        config.require_tailscale = _bool(server, "require_tailscale", config.require_tailscale)
        config.history_limit = _int(server, "history_limit", config.history_limit, minimum=1)
        config.log_retention_days = _int(server, "log_retention_days", config.log_retention_days)
    if config.require_tailscale and not _is_loopback(config.listen):
        # Anyone who can reach a non-loopback port could forge the identity header.
        raise ConfigError(
            "[server] require_tailscale 开启时 listen 只能是本机地址（如 127.0.0.1），"
            "对外访问交给 tailscale serve。"
        )

    for section_name in parser.sections():
        if section_name.startswith("endpoint:"):
            endpoint = _endpoint(section_name, parser[section_name])
            if endpoint.id in config.endpoints:
                raise ConfigError(f"端点 ID 重复：{endpoint.id}")
            config.endpoints[endpoint.id] = endpoint
    if len(config.endpoints) < 2:
        raise ConfigError("至少需要配置两个 [endpoint:<id>] 端点。")

    ids = list(config.endpoints)
    ui = parser["ui"] if parser.has_section("ui") else {}
    config.title = (ui.get("title", config.title) or config.title).strip()
    config.left = (ui.get("left", ids[0]) or ids[0]).strip()
    config.right = (ui.get("right", ids[1]) or ids[1]).strip()
    for side in ("left", "right"):
        if getattr(config, side) not in config.endpoints:
            raise ConfigError(f"[ui] {side} 指向了不存在的端点：{getattr(config, side)}")
    if config.left == config.right:
        raise ConfigError("[ui] left 和 right 不能是同一个端点。")
    return config
