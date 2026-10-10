# Sight 部署助手

[![Build](https://github.com/Sight-Data/sight_deploy/actions/workflows/build.yml/badge.svg)](https://github.com/Sight-Data/sight_deploy/actions/workflows/build.yml)
[![Release](https://img.shields.io/github/v/release/Sight-Data/sight_deploy?label=release)](https://github.com/Sight-Data/sight_deploy/releases)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Sight Data](https://img.shields.io/badge/官网-www.sightdata.top-2f6fed.svg)](https://www.sightdata.top)

[**Sight Data**](https://www.sightdata.top) 旗下 **SightReport（报表）/ SightWall（数据大屏）** 的远程安装与升级工具。

双击打开，操作界面在浏览器里。通过 SSH 连到 Linux 服务器，三步完成部署：添加服务器 → 选择安装包 → 一键安装 / 升级。
不用敲命令，客户电脑上也不用装 Python。

<table>
<tr><td width="50%">

**服务器状态一目了然**

<img src="docs/screenshots/status.png" alt="服务器状态页" width="100%">

</td><td width="50%">

**升级过程全程可见，失败可一键回滚**

<img src="docs/screenshots/upgrade.png" alt="升级时间线" width="100%">

</td></tr>
</table>

> 截图中的地址、目录均为测试环境数据。

## 下载

| 系统 | 文件 | 说明 |
|---|---|---|
| Windows 10 / 11 | `sight-deploy-<版本>-windows-x64.exe` | 单文件，双击运行 |
| macOS（Apple 芯片） | `sight-deploy-<版本>-mac-arm64.zip` | 解压后打开「Sight部署助手.app」 |
| macOS（Intel） | `sight-deploy-<版本>-mac-x86_64.zip` | 同上 |

- **GitHub**：[Releases](https://github.com/Sight-Data/sight_deploy/releases)
- **国内下载**：[Sight Data 官网下载中心](https://www.sightdata.top/downloads)

程序本身没有窗口，操作界面在浏览器里：
- **Windows**：运行后任务栏右下角通知区会出现它的图标（可能收在「^」里）。双击图标重新打开界面，右键「退出」关闭程序。
- 界面左下角也有「退出程序」按钮；关掉浏览器 10 分钟后，程序在没有任务进行时会自动退出。

程序没有做代码签名：
- **Mac**：第一次打开会提示「无法验证开发者」，右键 →「打开」即可。macOS 15 起要到「系统设置 › 隐私与安全性」点「仍要打开」。
- **Windows**：个别杀毒软件可能误报，加入信任即可。

SightReport / SightWall 的产品安装包同样在[官网下载中心](https://www.sightdata.top/downloads)下载。

## 能做什么

- **服务器管理**：支持 root 账号，或可以 sudo 的普通账号；支持密码或密钥登录。
  - 密码加密保存在本机，密钥与本机绑定。
  - 首次连接记录服务器指纹，下次指纹变了会提醒。
- **首次安装**：先检查环境，包括系统与 CPU 架构、systemd、Java 21、字体库、内存、磁盘、端口、防火墙。
  - 缺 Java 时可以上传本机的 JDK 包，自动核对版本和架构。服务器能联网的话，也可以用 yum/apt 在线安装。
  - 调用产品包里的 `install.sh` 注册成 systemd 服务，开机自启、崩溃自动拉起。
  - 健康检查通过后给出 `/setup` 初始化地址，需要的话顺手放行防火墙端口。
- **升级**：
  - 服务还在运行时先上传安装包，支持断点续传和 sha256 校验，尽量缩短停机时间。
  - 「停服 → 备份程序和 `data/` → 替换程序 → 启动」这一段在服务器后台执行，网络断了也会执行完。
  - 启动后检查健康状态并核对版本号，失败时一键回滚，程序和数据一起恢复。
  - 用外部数据库时会提醒先备份数据库。
  - 可以多选服务器依次滚动升级，某台失败时会询问是否继续处理剩下的。
- **日常运维**：启动、停止、重启；查看日志（可实时跟踪）；回滚到任意一份升级备份。回滚前会先把当前状态另存一份，回滚本身也能撤销。

## 安全

- 网页服务只监听本机 `127.0.0.1`，每次启动生成一个随机令牌。接口只认请求头里的令牌，并拒绝跨站请求：浏览器里的其他网页用不了它。
- 本机保存的数据都在 `~/.sight-deploy/`：
  - `hosts.json`：服务器列表，敏感字段用 Fernet 加密。
  - `.key`：加密密钥，与本机绑定，配置文件拷到别的电脑上解不开。设环境变量 `SIGHT_DEPLOY_PASSPHRASE` 可以再加一层口令。
  - `logs/`：操作日志，不含任何密码，出问题时发给技术支持即可。
- sudo 密码通过标准输入传给 sudo，不出现在命令行、进程列表或日志里。

## 服务器端约定

与产品安装包里的 `install.sh` / `start.sh` 一致：

| 项 | 约定 |
|---|---|
| 安装目录 | 默认 `/opt/sight-report`、`/opt/sight-wall` |
| 系统服务 | systemd 服务 `sight-report` / `sight-wall` |
| 升级备份 | `<安装目录上级>/sight-backups/<产品>/`，含程序文件与 `data/`；不含 logs、data/cache*、data/temp、data/backups；默认保留 3 份 |
| 非默认端口 | `/etc/systemd/system/<服务>.service.d/10-sight-deploy.conf` |
| 安装记录 | 安装目录下的 `.sight-deploy.json`（版本、端口、启动参数） |

支持的服务器：x86_64 / aarch64 的 Linux，有无 systemd 都可以（没有 systemd 时以后台进程方式运行）。

## 从源码运行

```bash
pip install paramiko
python3 sight_deploy.py                                 # 起服务并自动打开浏览器
python3 sight_deploy.py --no-browser --port 17880       # 只起服务，地址打印在控制台
```

- 只改界面时用假后端：`python3 web/mock_server.py 8765`，令牌是 `mocktoken123`。
- 前后端接口约定见 [`docs/API.md`](docs/API.md)。改接口时 `docs/API.md`、`sight_deploy.py`、`web/index.html` 三处一起改。

## 打包与发版

平时不用自己打包。改好代码后：

1. 把 `sight_deploy.py` 里的 `TOOL_VERSION` 改成新版本号，提交。
2. 打同名 tag 并推送（如 `git tag v2.0.1 && git push origin v2.0.1`）。

GitHub Actions 会在 Windows / macOS 真机上打包、冒烟自检，然后发布到 Releases。tag 和 `TOOL_VERSION` 对不上时构建会直接失败。

本地打包（PyInstaller 不能跨平台：Windows 版在 Windows 上打，Mac 版在 Mac 上打）：

```bash
bash build-mac.sh          # → dist/sight-deploy-<版本>-mac-<arch>.zip
build-windows.bat          # → dist\sight-deploy-<版本>-windows-x64.exe
```

- `PIP_INDEX_URL`：换国内 pip 源。
- `MAC_TARGET_ARCH=universal2`：打 Mac 通用包，需要 python.org 的通用版 Python。

## 许可

[Apache License 2.0](LICENSE) © [Sight Data](https://www.sightdata.top)
