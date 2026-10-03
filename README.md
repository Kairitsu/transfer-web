# Transfer Web

我有两台服务器，经常要在它们之间搬文件。每次都 ssh 上去敲 rsync 有点烦，用 WinSCP 这类工具又要先下载到本地再上传，绕了一大圈。所以写了这个小工具：在浏览器里左右两栏分别显示两台服务器的目录，勾选文件、点一下按钮，文件就由 rsync 直接在两台服务器之间传过去，不经过我自己的电脑。

![传输进行中](docs/screenshots/transfer.png)

## 能做什么

左右两栏各对应一台服务器，叫什么名字、显示哪个目录都写在配置文件里。勾选文件或文件夹，点中间的“复制到某某”，就会把它们复制到另一侧当前所在的目录。

开始之前会先做一次预检，告诉你这次要传多少东西、哪些文件会覆盖目标端已有的同名文件。不想覆盖的话，勾上“跳过目标端已存在的文件”就行。

![传输前的确认窗口](docs/screenshots/confirm.png)

传输过程中能看到整体进度、速度、预计剩余时间和正在传的文件。中途停掉或者断网都没关系，下次再传同一个文件会从断开的地方接着传。前一个任务还没传完时再提交新任务，新任务会排队等着。历史记录里能查到以前每次传了什么，以及当时的完整日志。

![任务历史（暗色主题）](docs/screenshots/history-dark.png)

在手机上也能用，两栏会改成上下排列，按钮上的箭头也跟着变成上下方向。

<img src="docs/screenshots/mobile.png" alt="手机上的界面" width="320">

## 先说安全

能打开这个页面的人，就能读写配置里那几个目录下的任何文件，包括 `.ssh/authorized_keys`、`.bashrc` 这种。说白了，这跟把两台机器的 shell 交出去差不多。所以千万别把它直接挂到公网上。

我自己的做法是让它只监听 `127.0.0.1`，再通过 Tailscale 只让自己的设备访问，具体做法见后面的“用 Tailscale 访问”。

## 准备工作

运行这个服务的那台机器需要 Python 3.10 以上、`rsync` 和 `ssh`，不用装任何 Python 第三方包。另一台服务器需要有 `rsync` 和 `python3`（列目录时会用到），再准备一个能用密钥登录的 SSH 账号。

rsync 有个限制：它不能直接在两台远端机器之间传文件，所以两台服务器里有一台必须是运行本服务的机器。

## 跑起来

先复制一份配置：

```bash
cp config.example.ini config.ini
```

一个最简单的配置大概长这样：

```ini
[ui]
left = us
right = kr

[endpoint:us]
name = 美国
type = ssh
host = us-server
user = ubuntu
root = /home/ubuntu
identity_file = /home/azureuser/.ssh/us_server_key

[endpoint:kr]
name = 韩国
type = local
root = /home/azureuser
```

每个 `[endpoint:…]` 就是一台服务器：`type = local` 是运行本服务的这台，`type = ssh` 是需要连过去的那台，`name` 是页面上显示的名字，`root` 决定了页面里能看到哪个目录。如果配置了不止两台，页面上可以用下拉框切换每一栏显示哪台。其他可选项（端口、`known_hosts`、历史条数、日志保留天数等）在 [`config.example.ini`](config.example.ini) 里都有注释。

然后启动：

```bash
python3 app.py --config config.ini
```

打开 <http://127.0.0.1:8756> 就能用了。不加 `--config` 时，程序会依次查找环境变量 `TRANSFER_WEB_CONFIG`、`/etc/transfer-web/config.ini` 和仓库根目录下的 `config.ini`。

## 长期运行

仓库里有一份 systemd 配置 [`deploy/transfer-web.service`](deploy/transfer-web.service)，默认假设代码放在 `/opt/transfer-web`、配置放在 `/etc/transfer-web/config.ini`：

```bash
sudo cp deploy/transfer-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now transfer-web
```

里面的 `User=azureuser` 要改成你自己的用户名，最好就是平时拥有这些文件的那个账号。尽量别用 root 跑：root 收到的文件会沿用远端机器上的属主，之后你可能改不动它们。实在要用 root，就在 local 端点里加一行 `owner = 你的用户名`。

## 用 Tailscale 访问

思路是让页面和 SSH 都只在 Tailscale 组成的私有网络里可见，公网上一个端口都不开。配好之后平时用起来没有任何额外步骤，打开网址就行。

在两台服务器和自己的电脑、手机上都装好 Tailscale，登录同一个账号。然后在运行本服务的机器上执行：

```bash
sudo tailscale serve --bg 8756
tailscale serve status
```

`status` 会显示一个 `https://<机器名>.<tailnet>.ts.net` 的地址，在任何登录了 Tailscale 的设备上打开它就行，HTTPS 证书 Tailscale 会自动处理。

如果 tailnet 里不止你一个人，可以在配置的 `[server]` 里写上 `allowed_users = 你的Tailscale登录邮箱`，这样别人即使能连到这台机器，也打不开页面。

把远端服务器的 `host` 换成它在 Tailscale 里的机器名，确认能正常列目录和传文件之后，就可以去云服务商的安全组里把远端服务器公网的 22 端口关掉了。关之前，先确认自己能从云控制台（VNC 或串口）登录那台机器，万一规则写错了还有路可退。

## 关于主机指纹

本服务连远端时会严格核对 SSH 主机指纹，对不上就拒绝连接，防止被中间人冒充。第一次配置、或者换了 `host` 之后，需要把指纹记下来：

```bash
# 在远端服务器上（比如通过云控制台）看一下它真正的指纹
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub

# 在本服务所在的机器上抓取指纹，和上面的结果对一下
ssh-keyscan -t ed25519 us-server > /tmp/us.keys
ssh-keygen -lf /tmp/us.keys

# 对得上的话，存到配置里 known_hosts 指向的文件
sudo install -m 644 /tmp/us.keys /etc/transfer-web/known_hosts
```

`known_hosts` 里记录的主机名必须和配置里的 `host` 一字不差；远端 SSH 不在 22 端口的话，`ssh-keyscan` 要加 `-p 端口号`。页面上提示“主机指纹未知或已变化”时，多半就是这一步没做或者没对上。

## 传输时的一些细节

- 判断要不要传一个文件，看的是大小和修改时间：只要有一项不一样，就用源文件覆盖目标文件。
- 目标端多出来的文件不会被删除。
- 传到一半中断的文件，会暂存在目标目录下一个隐藏的 `.rsync-partial` 文件夹里，下次续传完成后会自动清掉。
- 符号链接不能单独勾选。文件夹里指向文件夹内部的链接会原样复制，指向外部的会被跳过。
- 一个目录最多显示 5000 项，超过时页面会提示。

## 测试

```bash
python3 -m unittest discover -s tests -t .
```

测试会拿两个本地临时目录当作两台服务器，调用真实的 rsync 来跑（没装 rsync 的话，相关用例会自动跳过）。

## 许可证

AGPL-3.0，见 [LICENSE](LICENSE)。
