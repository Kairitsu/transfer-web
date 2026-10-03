# Transfer Web

在浏览器里并排浏览两台服务器的目录，勾选文件后用 rsync 直接在服务器之间传输（数据不经过你的电脑）。

- 左右两侧显示哪台服务器、叫什么名字，都在配置文件里定义
- 传输前先预检：列出新增和**将被覆盖**的文件，可选择跳过已存在的文件
- 显示整体进度、速度和剩余时间；中断后再传会自动续传
- 任务排队、可停止，保留任务历史和每次的传输日志
- 支持亮色和暗色主题，手机上也能用

> **安全提示**：能打开这个页面的人，就能读写配置里各个根目录下的所有文件（包括 `.ssh`、`.bashrc` 这类文件），这基本等于拥有这两台机器的 shell 权限。**不要把它直接暴露在公网上**，请按下文用 Tailscale 只在自己的设备之间访问。

## 要求

- 运行本服务的机器：Python 3.10+、`rsync`、`ssh`
- 远端服务器：`rsync`、`python3`（列目录用），以及可以用密钥登录的 SSH 账号
- 不需要安装任何 Python 第三方包

## 快速开始

```bash
cp config.example.ini config.ini   # 按需修改
python3 app.py --config config.ini
```

然后打开 `http://127.0.0.1:8756`。配置文件的查找顺序是：`--config` 参数 → 环境变量 `TRANSFER_WEB_CONFIG` → `/etc/transfer-web/config.ini` → 仓库根目录的 `config.ini`。

所有选项及说明见 [`config.example.ini`](config.example.ini)。要点如下：

- 每个 `[endpoint:<id>]` 是一台服务器，`name` 是页面上显示的名字。
- `type = local` 表示运行本服务的这台机器，`type = ssh` 表示远端。
- rsync 不能直接在两个远端之间传输，所以每次传输至少有一端必须是 `local`。
- 配置超过两台服务器时，页面上可以用下拉框切换每一侧显示哪台。

## 部署（systemd）

```bash
sudo git clone <本仓库> /opt/transfer-web
sudo mkdir -p /etc/transfer-web
sudo cp /opt/transfer-web/config.example.ini /etc/transfer-web/config.ini   # 然后编辑
sudo cp /opt/transfer-web/deploy/transfer-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now transfer-web
```

示例 unit 里写的运行用户是 `azureuser`，请改成你实际传输文件所属的那个用户。如果 local 端点的根目录不在 `/home/azureuser` 下，还要同步修改 `ReadWritePaths`。**不建议用 root 运行**；如果必须用 root，请给 local 端点配置 `owner`，否则传过来的文件会沿用远端的属主。

## 通过 Tailscale 访问（推荐）

目标是让页面和 SSH 都不再暴露在公网上，安全全部交给外层，平时使用时不需要任何额外操作。

1. 在两台服务器和你自己的设备上安装 Tailscale，登录同一个 tailnet。
2. 本服务保持 `listen = 127.0.0.1`。在运行本服务的机器上执行：

   ```bash
   sudo tailscale serve --bg 8756
   tailscale serve status        # 会显示 https://<机器名>.<tailnet>.ts.net
   ```

   之后在任意已登录 Tailscale 的设备上打开这个 `https://…ts.net` 地址即可，证书由 Tailscale 自动签发。
3. （可选）只允许自己的账号访问：在 `[server]` 里设置 `allowed_users = 你的 Tailscale 登录邮箱`。`tailscale serve` 会在请求中带上访问者的身份（`Tailscale-User-Login`），本服务据此放行或拒绝。
4. 把远端的 `host` 改成它的 Tailscale 机器名或 `100.x.y.z` 地址，并按下一节固定主机指纹。确认页面能正常列目录、传输之后：
   - 关闭原来对公网开放的反向代理（及其账号密码）；
   - 在云服务商的安全组里关闭远端服务器公网的 22 端口。

   **关端口之前，先确认能通过云控制台（VNC 或串口）登录服务器**，以免规则配错把自己锁在外面。
5. （可选）在 Tailscale 管理后台的 Access controls 里进一步限制：只有你的设备能访问本服务所在机器的 443 端口，只有本服务所在机器能访问远端的 22 端口。
6. （可选）在远端 `~/.ssh/authorized_keys` 中这把密钥的那一行前面加上 `from="100.64.0.0/10"`，即使密钥泄露，也只能从 tailnet 内使用。

## 固定主机指纹

为防止中间人攻击，本服务连接远端时使用 `StrictHostKeyChecking=yes`：指纹未知或不匹配时直接拒绝连接，页面上会提示“主机指纹未知或已变化”。首次配置时按下面的步骤操作：

```bash
# 1. 在远端服务器上（例如通过云控制台）查看真实指纹
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub

# 2. 在运行本服务的机器上获取指纹，与上一步的结果核对一致
ssh-keyscan -t ed25519 <host> > /tmp/remote.keys        # 非 22 端口加 -p <port>
ssh-keygen -lf /tmp/remote.keys

# 3. 核对无误后写入配置里 known_hosts 指定的文件
sudo install -m 644 /tmp/remote.keys /etc/transfer-web/known_hosts
```

`known_hosts` 中记录的主机名必须与配置里的 `host` 完全一致。改用 Tailscale 机器名后，需要重新做一遍。

## 传输行为

- **续传**：中断的文件保存在目标目录下隐藏的 `.rsync-partial/` 里，下次传输同一个文件时会从已传部分继续，传完后自动清理。
- **覆盖**：源和目标的文件大小或修改时间不同，就会用源文件覆盖目标文件。预检会列出这些文件；勾选“跳过目标端已存在的文件”后，已存在的文件一律不动。
- **不会删除**目标端多出来的文件。
- 符号链接不能作为传输项目单独选中。目录里指向目录内部的链接会原样复制，指向目录外部的链接会被忽略（rsync `--safe-links`）。
- 列目录时每个目录最多显示 5000 项，超出时页面会提示。
- 远端的 SSH 连接会复用 5 分钟，所以连续操作时列目录很快。

## 从旧版本迁移

旧版本把服务器信息写死在 `app.py` 里。升级后需要建立配置文件，对应关系如下：

| 旧代码 | 新配置 |
|---|---|
| `KR_HOME = /home/azureuser` | `[endpoint:kr]`：`type = local`，`root = /home/azureuser` |
| `REMOTE = ubuntu@43.130.17.25` | `[endpoint:us]`：`type = ssh`，`host = 43.130.17.25`（建议改为 Tailscale 机器名），`user = ubuntu` |
| `US_HOME = /home/ubuntu` | `[endpoint:us]`：`root = /home/ubuntu` |
| `SSH_KEY = /home/azureuser/Tencent.pem` | `[endpoint:us]`：`identity_file = /home/azureuser/Tencent.pem` |
| 页面上的“美国”“韩国” | 各端点的 `name`；左右位置用 `[ui] left/right` 设置 |

其他说明：

- 旧版本使用 `StrictHostKeyChecking=accept-new`，远端指纹已经记录在服务运行用户的 `~/.ssh/known_hosts` 里。因此 `host` 不变、`known_hosts` 留空时可以直接连上；换成 Tailscale 机器名后，需要按上一节重新固定指纹。
- 旧的 `state.json` 不会导入历史记录；旧日志仍保留在 `/var/log/transfer-web/transfers/`，会按 `log_retention_days` 清理。
- 旧版本的 OliveTin 兼容跳转已经移除。

## 开发与测试

```bash
python3 -m unittest discover -s tests -t .
```

测试会用两个本地目录作为端点，调用真实的 rsync（未安装 rsync 时会跳过相关用例）。

## 许可证

AGPL-3.0，见 [LICENSE](LICENSE)。
