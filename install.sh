#!/usr/bin/env bash
# Transfer Web 一键安装。
#
#   sudo ./install.sh            在运行本服务的机器上执行：装好 Tailscale 并登录，
#                                把服务部署成 systemd 服务，再用 tailscale serve
#                                把页面发布到 tailnet 里（只能经 Tailscale 访问）。
#   sudo ./install.sh --remote   在另一台服务器上执行：只装 Tailscale、rsync 和
#                                python3，让主机器能经 Tailscale 用 SSH 连过来。
#
# 选项：
#   --user 用户名      服务以哪个用户运行（默认是执行 sudo 的那个用户）
#   --https-port 端口  tailscale serve 使用的 HTTPS 端口（默认 443）
#   --remote           见上
#
# 无人值守安装时，可以把 Tailscale auth key 放在环境变量 TS_AUTHKEY 里：
#   sudo TS_AUTHKEY=tskey-auth-... ./install.sh
#
# 重复执行是安全的：已有的配置文件不会被覆盖，可以用来升级代码，
# 或者在改了配置里 local 端点的 root 之后刷新 systemd 的可写目录。
set -euo pipefail

PREFIX=/opt/transfer-web
CONF_DIR=/etc/transfer-web
CONFIG=$CONF_DIR/config.ini
UNIT=/etc/systemd/system/transfer-web.service
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

MODE=server
RUN_USER=${SUDO_USER:-}
HTTPS_PORT=443

say() { printf '==> %s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,20s/^# \{0,1\}//p' "$0"; }

ARGS="$*"
while [ $# -gt 0 ]; do
  case $1 in
    --remote) MODE=remote ;;
    --user) [ $# -ge 2 ] || die "--user 后面要跟用户名"; RUN_USER=$2; shift ;;
    --user=*) RUN_USER=${1#*=} ;;
    --https-port) [ $# -ge 2 ] || die "--https-port 后面要跟端口号"; HTTPS_PORT=$2; shift ;;
    --https-port=*) HTTPS_PORT=${1#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) die "不认识的参数：$1（用 --help 查看用法）" ;;
  esac
  shift
done

[ "$(uname -s)" = Linux ] || die "只支持 Linux。"
[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo $0 $ARGS"
case $HTTPS_PORT in ''|*[!0-9]*) die "--https-port 必须是数字。" ;; esac

# ---- 系统软件包 ----------------------------------------------------------------

pkg_install() {  # 参数是命令名：rsync python3 ssh curl
  local names=() cmd
  if command -v apt-get >/dev/null 2>&1; then
    for cmd in "$@"; do case $cmd in ssh) names+=(openssh-client) ;; *) names+=("$cmd") ;; esac; done
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${names[@]}"
  elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    for cmd in "$@"; do case $cmd in ssh) names+=(openssh-clients) ;; *) names+=("$cmd") ;; esac; done
    if command -v dnf >/dev/null 2>&1; then dnf install -y -q "${names[@]}"; else yum install -y -q "${names[@]}"; fi
  elif command -v zypper >/dev/null 2>&1; then
    for cmd in "$@"; do case $cmd in ssh) names+=(openssh-clients) ;; *) names+=("$cmd") ;; esac; done
    zypper --non-interactive install "${names[@]}"
  elif command -v pacman >/dev/null 2>&1; then
    for cmd in "$@"; do case $cmd in ssh) names+=(openssh) ;; python3) names+=(python) ;; *) names+=("$cmd") ;; esac; done
    pacman -Sy --noconfirm --needed "${names[@]}"
  else
    die "不认识这个系统的包管理器，请先手动安装：$*"
  fi
}

ensure_commands() {
  local missing=() cmd
  for cmd in "$@"; do command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  say "安装 ${missing[*]}"
  pkg_install "${missing[@]}"
}

# ---- Tailscale -----------------------------------------------------------------

# 从 `tailscale status --json` 里取一个字段：state / dns / login / certs
ts_field() {
  tailscale status --json 2>/dev/null | python3 -c '
import json, sys
status = json.load(sys.stdin)
me = status.get("Self") or {}
field = sys.argv[1]
if field == "state":
    print(status.get("BackendState") or "")
elif field == "dns":
    print((me.get("DNSName") or "").rstrip("."))
elif field == "login":
    user = (status.get("User") or {}).get(str(me.get("UserID"))) or {}
    print("" if me.get("Tags") else user.get("LoginName") or "")
elif field == "certs":
    print(" ".join(status.get("CertDomains") or []))
' "$1" 2>/dev/null || true
}

ensure_tailscale() {
  if ! command -v tailscale >/dev/null 2>&1; then
    ensure_commands curl
    say "安装 Tailscale（使用 Tailscale 官方安装源，之后随系统更新一起升级）"
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now tailscaled >/dev/null 2>&1 || warn "无法启动 tailscaled，请检查 systemctl status tailscaled。"
  fi
  if [ "$(ts_field state)" != Running ]; then
    if [ -n "${TS_AUTHKEY:-}" ]; then
      say "用 TS_AUTHKEY 登录 Tailscale"
      tailscale up --auth-key="$TS_AUTHKEY"
    else
      say "登录 Tailscale：请在浏览器里打开下面出现的链接，用你的 Tailscale 账号登录"
      tailscale up
    fi
  fi
  [ "$(ts_field state)" = Running ] || die "Tailscale 没有登录成功，请执行 sudo tailscale up 后重新运行本脚本。"
}

# ---- 另一台服务器 --------------------------------------------------------------

install_remote() {
  ensure_commands rsync python3
  ensure_tailscale
  local dns ip
  dns=$(ts_field dns)
  ip=$(tailscale ip -4 2>/dev/null | head -n1 || true)
  cat <<EOF

完成。这台服务器在 Tailscale 里的地址是：
  ${dns:-（未开启 MagicDNS）}  ${ip}

在运行 Transfer Web 的那台机器上，把配置里对应端点的 host 写成 ${dns:-$ip}，
再按 README“关于主机指纹”记录主机指纹（ssh-keyscan 时也用这个名字）。
确认一切正常后，就可以在云服务商的安全组里关掉这台服务器公网的 22 端口。
EOF
}

# ---- 运行本服务的机器 ----------------------------------------------------------

# 在已安装的代码目录里运行一段 Python（stdin），参数原样传入。
in_app() { (cd "$PREFIX" && PYTHONDONTWRITEBYTECODE=1 python3 - "$@"); }

install_code() {
  local dest
  dest=$(cd "$PREFIX" 2>/dev/null && pwd -P || true)
  if [ "$dest" = "$SRC" ]; then
    say "代码已经在 $PREFIX，跳过复制"
    return
  fi
  say "复制程序到 $PREFIX"
  install -d -m 755 "$PREFIX"
  rm -rf "${PREFIX:?}/transfer_web" "${PREFIX:?}/static"
  cp -R "$SRC/transfer_web" "$SRC/static" "$PREFIX/"
  install -m 755 "$SRC/app.py" "$PREFIX/app.py"
  install -m 644 "$SRC/LICENSE" "$SRC/config.example.ini" "$PREFIX/"
  find "$PREFIX" -name __pycache__ -prune -exec rm -rf {} +
  chown -R root:root "$PREFIX"
  chmod -R u=rwX,go=rX "$PREFIX"
}

CREATED_CONFIG=0
install_config() {
  install -d -m 755 "$CONF_DIR"
  if [ -e "$CONFIG" ]; then
    say "保留现有配置 $CONFIG"
    return
  fi
  say "生成配置 $CONFIG"
  in_app "$CONFIG" "$RUN_HOME" "$(ts_field login)" <<'PY'
import re
import sys
from pathlib import Path

dest, home, login = sys.argv[1:]
text = Path("config.example.ini").read_text(encoding="utf-8")
text = text.replace("root = /home/me", f"root = {home}", 1)
if login:
    text = re.sub(r"^allowed_users =.*$", lambda m: f"allowed_users = {login}", text, count=1, flags=re.M)
Path(dest).write_text(text, encoding="utf-8")
PY
  chown "root:$RUN_GROUP" "$CONFIG"
  chmod 640 "$CONFIG"
  CREATED_CONFIG=1
}

# 读配置，输出：端口、是否强制 Tailscale、local 端点的 root（每行一个）。
read_config() {
  in_app "$CONFIG" <<'PY'
import sys
from transfer_web.config import ConfigError, load_config

try:
    config = load_config(sys.argv[1])
except ConfigError as exc:
    sys.exit(f"配置有误：{exc}")
print(config.port)
print("yes" if config.require_tailscale else "no")
for endpoint in config.endpoints.values():
    if endpoint.is_local:
        print(endpoint.root)
PY
}

install_unit() {
  say "安装 systemd 服务（以 $RUN_USER 用户运行）"
  in_app "$SRC/deploy/transfer-web.service" "$UNIT" "$RUN_USER" "$RUN_GROUP" "${LOCAL_ROOTS[@]}" <<'PY'
import re
import sys
from pathlib import Path

src, dest, user, group, *roots = sys.argv[1:]
# "-" 前缀：目录不存在时服务照样启动，由页面提示列目录失败。
paths = " ".join('"-%s"' % root for root in dict.fromkeys(roots))
text = Path(src).read_text(encoding="utf-8")
text = re.sub(r"^User=.*$", lambda m: f"User={user}", text, flags=re.M)
text = re.sub(r"^Group=.*$", lambda m: f"Group={group}", text, flags=re.M)
text = re.sub(r"^ReadWritePaths=.*$", lambda m: f"ReadWritePaths={paths}", text, flags=re.M)
Path(dest).write_text(text, encoding="utf-8")
PY
  chmod 644 "$UNIT"
  systemctl daemon-reload
  systemctl enable transfer-web >/dev/null 2>&1
  systemctl restart transfer-web
  sleep 1
  systemctl is-active --quiet transfer-web || die "服务没能启动，请查看：journalctl -u transfer-web -n 50"
}

setup_serve() {
  local port=$1 current
  # 这个 HTTPS 端口的根路径现在转发到哪里（没有则为空）。
  current=$(tailscale serve status --json 2>/dev/null | python3 -c '
import json, sys
config = json.load(sys.stdin) or {}
for host_port, web in (config.get("Web") or {}).items():
    if host_port.rpartition(":")[2] == sys.argv[1]:
        handler = ((web or {}).get("Handlers") or {}).get("/") or {}
        print(handler.get("Proxy") or handler.get("Path") or handler.get("Text") or "(其他)")
' "$HTTPS_PORT" 2>/dev/null || true)
  case $current in
    "")
      ;;
    "http://127.0.0.1:$port"|"http://localhost:$port"|"127.0.0.1:$port"|"localhost:$port")
      say "tailscale serve 已经指向本服务"
      return
      ;;
    *)
      die "tailscale serve 的 HTTPS $HTTPS_PORT 端口已经用于 $current。换个端口重新运行：sudo $0 --https-port 8443"
      ;;
  esac
  if [ -z "$(ts_field certs)" ]; then
    say "你的 tailnet 还没开启 HTTPS 证书：下面 tailscale serve 会给出一个链接，打开后点 Enable 即可（只需一次）"
  fi
  say "用 tailscale serve 把页面发布到 tailnet（HTTPS $HTTPS_PORT 端口）"
  tailscale serve --bg --https="$HTTPS_PORT" "http://127.0.0.1:$port"
}

install_server() {
  command -v systemctl >/dev/null 2>&1 || die "需要 systemd。"
  ensure_commands rsync python3 ssh
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "需要 Python 3.10 以上。"
  [ -n "$RUN_USER" ] || die "无法判断用哪个用户运行服务，请加上 --user 用户名。"
  id "$RUN_USER" >/dev/null 2>&1 || die "用户 $RUN_USER 不存在。"
  [ "$RUN_USER" != root ] || warn "以 root 运行时，传过来的文件会沿用远端的属主，建议换成普通用户（见 README“长期运行”）。"
  RUN_GROUP=$(id -gn "$RUN_USER")
  RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)

  ensure_tailscale
  install_code
  install_config

  local settings port require
  settings=$(read_config) || exit 1
  port=$(sed -n 1p <<<"$settings")
  require=$(sed -n 2p <<<"$settings")
  mapfile -t LOCAL_ROOTS < <(sed -n '3,$p' <<<"$settings")
  [ ${#LOCAL_ROOTS[@]} -gt 0 ] || LOCAL_ROOTS=("$RUN_HOME")
  [ "$require" = yes ] || warn "配置里 require_tailscale = no：页面不再强制经 Tailscale 访问，请确保你有别的认证手段。"

  setup_serve "$port"  # 先做：HTTPS 端口被占用时，什么都还没改就退出
  install_unit

  local dns login
  dns=$(ts_field dns)
  login=$(ts_field login)
  local url="https://$dns"
  [ "$HTTPS_PORT" = 443 ] || url="$url:$HTTPS_PORT"
  echo
  echo "完成。在任何登录了 Tailscale 的设备上打开：$url"
  if [ "$CREATED_CONFIG" = 1 ]; then
    if [ -n "$login" ]; then
      echo "配置里只允许 $login 访问（[server] allowed_users）。"
    else
      warn "这台机器是用打了 tag 的身份登录 Tailscale 的，没法自动限定账号；tailnet 里所有用户都能打开页面，可以在 $CONFIG 的 allowed_users 里手动填写。"
    fi
    cat <<EOF

接下来：
  1. 在另一台服务器上运行 sudo ./install.sh --remote，让它也加入 tailnet。
  2. 编辑 $CONFIG：把 [endpoint:remote] 的 host 写成它在 Tailscale 里的机器名，
     再填好 user、root、identity_file。
  3. 按 README“关于主机指纹”把远端的主机指纹写进 known_hosts。
  4. 重新运行 sudo ./install.sh（或 sudo systemctl restart transfer-web）让配置生效。
EOF
  fi
}

case $MODE in
  remote) install_remote ;;
  *) install_server ;;
esac
