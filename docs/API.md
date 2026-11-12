# Sight 部署助手 —— 前后端接口约定

本地程序（Python）在 `127.0.0.1:<port>` 起 HTTP 服务，并用 Edge / Chrome 的应用窗口（没有则默认浏览器）打开：`http://127.0.0.1:<port>/?t=<token>`。
前端是单个离线页面 `web/index.html`（**不能引用任何外部资源**：无 CDN、无外链字体，客户多在内网）。

## 通用

- 页面加载时从 URL 取 `t`，存 `sessionStorage`，之后所有 `/api/*` 请求带请求头 `X-Token: <token>`。
  **接口只认这个请求头**（cookie 只用于刷新页面本身），并拒绝跨站来源（Origin / Sec-Fetch-Site），防止浏览器里其他网页冒用。
  （同时用 `history.replaceState` 把 `?t=` 从地址栏去掉。）
- 返回 403 → 显示全屏提示「会话已失效，请重新打开 Sight 部署助手」。
- 所有请求/响应都是 JSON（`/api/upload` 除外）。出错时 HTTP 4xx/5xx，body `{"error": "中文说明"}`。
- 前端每 5 秒 `POST /api/heartbeat`（无 body）。后端超过 10 分钟收不到心跳、且没有任务在跑，就自动退出。
- 同一时间只有一个任务在跑（`state.busy` 非空时，所有会发起任务的按钮置灰）。

## 状态与设置

`GET /api/state` →
```json
{
  "version": "2.0.0",
  "busy": "job-id 或 null",
  "settings": {"keep_backups": 3, "health_timeout": 300, "auto_check": true},
  "hosts": [
    {"name": "生产-1", "host": "10.0.0.5", "port": 22, "user": "root", "auth": "password",
     "last_used": "2026-10-10 01:16",
     "products": {"sight-report": {"install_dir": "/opt/sight-report", "port": 9091, "version": "2.0.71"}}}
  ],
  "products": {"sight-report": {"name": "SightReport", "port": 9091, "dir": "/opt/sight-report", "zip_prefix": "sight-report",
                               "setup_path": "/setup", "setup_token": false, "rolling_upgrade": true, "schema_versioned": false,
                               "kind": "java", "instances": true, "cluster": true, "service": "sight-report"},
               "sight-wall": {...},
               "sight-datalink": {"name": "Sight DataLink", "port": 18680, "dir": "/opt/sight-datalink", "zip_prefix": "sight-datalink",
                              "setup_path": "/", "setup_token": true, "rolling_upgrade": false, "schema_versioned": true},
               "sight-mobile-gateway": {"name": "Sight 移动网关", "port": 8080, "tunnel_port": 7443, "dir": "/opt/sight-mobile-gateway",
                              "zip_prefix": "sight-mobile-gateway", "setup_path": null, "setup_token": false,
                              "rolling_upgrade": true, "schema_versioned": false, "kind": "gateway"}},
  "log_path": "/Users/x/.sight-deploy/logs/sight-deploy-20261010.log"
}
```
`hosts[].products` 是**上次查到的缓存**，可能过时；真实状态以 `status` 任务结果为准。

`products` 是产品描述（后端 `PRODUCTS`），界面上的产品卡、默认端口、安装包文件名识别（`<zip_prefix>-<版本>.zip`）都按它来，不要在页面里写死产品：
- `setup_path`：未初始化时让实施人员打开的地址（SightReport / SightWall 是 `/setup`，Sight DataLink 的安装向导在根路径 `/`）。
- `setup_token`：初始化要用服务日志里的一次性令牌（Sight DataLink）。
- `rolling_upgrade=false`：多选服务器时不做自动滚动升级（Sight DataLink 集群共用外部库），要提示先全部停止、再逐台升级。
- `kind`：`"gateway"` = 非 Java 产品（Sight 移动网关：Go 单文件程序，部署在 DMZ 前置机上；没有 Java / 数据库 / `/api/health`，安装包名是 `sight-mobile-gateway-<版本>-linux-<amd64|arm64|loong64>.zip`）。界面按它换产品卡和安装完成页；其余产品没有这个字段（当作 Java 产品）。`tunnel_port` 是它的默认隧道端口，`port` 是默认手机侧端口。
- `schema_versioned=true`：健康检查报告数据库结构版本（Flyway `schemaVersion`），升级时记进备份元数据，回滚前核对。

`POST /api/settings` `{"keep_backups": 3, "health_timeout": 300, "auto_check": true}` → `{"ok": true}`（`auto_check` 可省略：是否在启动时自动联网检查更新，默认开）

`POST /api/hosts/delete` `{"name": "生产-1"}` → `{"ok": true}`（只删本机保存的信息）

`POST /api/shutdown` → `{"ok": true}`，随后进程退出（前端显示「已退出，可以关闭此页面」）。有任务在跑时返回 409。

## 在线检查（官网下载中心）

数据源：官网 `GET <官网>/api/downloads`（只读版本信息，不上传任何数据）；查不到部署助手本身的版本时，再退到 GitHub Release。
官网地址默认 `https://www.sightdata.top`，环境变量 `SIGHT_DEPLOY_SITE` 可改（调试用假官网）。结果在内存里缓存 10 分钟（失败只缓存 1 分钟）。

`GET /api/online[?refresh=1]` → 网络不通也返回 200，错误放在 `error`：
```json
{"checked_at": 1791766419, "source": "site", "error": null, "site": "https://www.sightdata.top",
 "tool": {"current": "2.1.0", "latest": "2.2.0", "update": true, "can_download": true, "platform": "windows-x64",
          "file_name": "Sight部署助手-2.2.0-windows-x64.exe", "size_text": "14.4 MB", "page": "https://www.sightdata.top/downloads"},
 "products": {"sight-report": {"latest": "2.0.73", "file_name": "sight-report-2.0.73.zip", "size_text": "258M",
                               "pan_url": "", "pan_code": ""},
              "sight-wall": {"latest": "2.0.70", "...": "..."}}}
```
- `tool` 可能为 null（官网和 GitHub 都没连上）；`can_download=false` 表示这个系统没有现成的安装包（如 Linux 源码运行），界面只给「打开下载中心」。
- Intel Mac 的部署助手只在 GitHub Release 里，下载地址会指向 GitHub（可用环境变量 `GH_DL_PREFIX` 加速）。
- `products` 里没有的产品，说明官网没有可下载的包。
- `sight-mobile-gateway` 按 CPU 架构分三个官网条目（`dl-gateway-linux-amd64` / `-arm64` / `-loong64`）：顶层 `latest` 等字段是版本最高的一个，另有 `variants: {"amd64": {"latest", "file_name", "size_text", "url", ...}, "arm64": {...}}`，只含官网上有安装包文件的架构。界面选包时按服务器架构列出对应条目。

`POST /api/reveal` → `{"ok": true}`：在文件管理器里显示刚下载的新版部署助手（只认 `download_tool` 保存的那个文件）。

## 本地文件

`GET /api/packages` → 自动发现的安装包（当前目录 / 程序目录 / 下载 / 桌面）：
```json
[{"path": "/Users/x/Downloads/sight-report-2.0.71.zip", "name": "sight-report-2.0.71.zip",
  "size": 258824866, "mtime": 1791518682, "dir": "/Users/x/Downloads",
  "product": "sight-report", "version": "2.0.71"}]
```
（`product` / `version` 由文件名猜，可能为 null；移动网关的包另有 `"arch": "amd64|arm64|loong64"`）

`GET /api/jdks` → 同上结构（无 product/version），自动发现的 JDK 压缩包。

`GET /api/fs?dir=<绝对路径，可省略>&kind=package|jdk|key|any` → 本机文件浏览（用于「浏览…」）：
```json
{"dir": "/Users/x/Downloads", "parent": "/Users/x",
 "shortcuts": [{"name": "下载", "path": "/Users/x/Downloads"}, {"name": "桌面", "path": "..."}, {"name": "主目录", "path": "..."}],
 "entries": [{"name": "abc", "path": "...", "is_dir": true},
             {"name": "sight-report-2.0.71.zip", "path": "...", "is_dir": false, "size": 123, "mtime": 1791518682}]}
```
后端已按 kind 过滤文件（目录总是返回）。`dir` 省略时返回「下载」目录（没有则主目录）。Windows 下 `dir=::drives` 返回盘符列表（快捷入口「此电脑」），盘符根目录的 `parent` 也是 `::drives`。

`POST /api/upload?name=<文件名>`，body 为文件原始字节（`fetch(url, {method:'POST', body: file})`）→ `{"path": "本机保存路径"}`。
用于把安装包 / JDK 包**拖进页面**：浏览器拿不到本地路径，只能先传给本地程序（走本机回环，很快）。显示上传进度（用 XHR 的 upload.onprogress）。

## 任务（Job）

`POST /api/jobs` `{"kind": "...", "params": {...}}` → `{"job_id": "..."}`；有任务在跑时 409。

| kind | params | result（任务成功结束时） |
|---|---|---|
| `add_host` | `host, port, user, auth("password"/"key"), password, key_file, key_passphrase, name` | `{"name": "保存后的名字"}` |
| `edit_host` | `name, password` 或 `name, key_passphrase` | `{"name": ...}` |
| `test_host` | `name` | `{"root": true}` |
| `status` | `host` | 见下 |
| `deploy` | `hosts: [名字...], package: 本机路径, product`（可选：从产品卡点的是哪个产品，包不对时直接失败；可以是实例键 `sight-report@b`），`instance`（可选：装 / 升级同机的哪个实例，见下「同机多实例」） | `{"results": [{"host": "生产-1", "status": "完成/跳过/失败/取消", "url": "http://..."}]}`；Sight DataLink 升级后持续 60 秒处于 `mode=waiting`（连不上元数据库）按未就绪处理：`fail` 事件「服务已启动但连不上元数据库（等待模式…）」+ `info`「最近一次连接错误：…」（健康检查的 `lastError`，口令打码），随后照常显示日志并 `confirm` 是否回滚；首次安装还可能有 `mode`（`setup` 安装向导 / `waiting` 等元数据库）、`setup_token`（从服务日志取到的初始化令牌）、`token_hint`（没取到令牌时的说明，如「令牌为部署时设置的 FDL_SETUP_TOKEN」）；移动网关的结果项另有 `gateway`（见下「Sight 移动网关」），升级 SightReport 后若助手里登记的某台服务器的移动网关版本不同，结果项有 `gateway_notices: [{"host", "gateway", "target", "text"}]`（`text` 形如「移动网关需同步升级到 2.0.96：前置机 上的移动网关现在是 2.0.95 …」），同时时间线上有 `warn` |
| `service` | `host, product, action("start"/"stop"/"restart")` | `{}` |
| `logs` | `host, product, lines(默认200)` | `{"text": "日志文本"}` |
| `follow_logs` | `host, product` | 不会自己结束；通过 `log` 事件推送新行，用户点「停止」→ cancel |
| `list_backups` | `host, product` | `{"backup_dir": "...", "current_version": "2.0.71", "db_kind": "h2/external/none", "backups": [{"file": "...", "version": "2.0.69", "time": "2026-10-10 01:16", "size": 30000000}]}`；`schema_versioned` 的产品（Sight DataLink）另有 `current_schema_version` 和每份备份的 `schema_version`（备份时记录的数据库结构版本，旧备份为 null） |
| `rollback` | `host, product, file, full(bool)` | `{}`。Sight DataLink：外部库且备份时记录的结构版本与当前不同 → 直接失败（不论 `full`），提示先用数据库备份恢复；读不到版本时会用 `confirm` 再问一次 |
| `gateway_show` | `host` | `{"gateway": {...}}`：再次查看移动网关的证书指纹和接入令牌（服务器上执行 `show`），字段同下「Sight 移动网关」的 `gateway`；令牌只在结果里，不进本地日志 |
| `download_package` | `product`（`sight-report` / `sight-wall` / `sight-datalink` / `sight-mobile-gateway`；官网条目 id 分别是 `dl-report` / `dl-wall` / `dl-datalink` / `dl-gateway-linux-<arch>`），移动网关另需 `arch`（`amd64` / `arm64` / `loong64`），结果里也带 `arch` | `{"path", "name", "size", "product", "version", "mtime", "dir", "reused"}`：从官网下载该产品最新版到 `~/.sight-deploy/uploads/`（断点续传、zip 完整性校验）；本机常用位置已有同名同大小的包就直接复用（`reused=true`）。拿到的 `path` 直接用于 `deploy` |
| `download_tool` | 无 | `{"path", "name", "dir", "version", "howto": [...], "can_apply"}`：下载新版部署助手到「下载」目录；已是最新时失败。下载本身不会替换正在运行的程序；Windows 打包版、macOS 上从 .app 里运行的打包版 `can_apply=true`，可再调 `POST /api/apply_update` 自更新（文件放在 `~/.sight-deploy/updates/`，不是「下载」；Mac 在「下载」里直接打开、被系统放进只读沙盒的 .app 不行，要先拖到「应用程序」） |

`status` 结果：
```json
{"host": "生产-1", "address": "root@10.0.0.5", "os": "Linux x86_64 / CentOS 7.9 / up 3 days",
 "products": [
  {"product": "sight-report", "name": "SightReport", "installed": true,
   "version": "2.0.71", "dir": "/opt/sight-report", "owner": "root",
   "service": "systemd", "active": "active",          // service: "systemd" | "process"
   "running": true, "healthy": true, "bootstrapped": true, "db_reachable": true,
   "port": 9091, "url": "http://10.0.0.5:9091/", "reachable": true,
   "java": "21.0.4（/usr/lib/jvm/.../java）", "db": "内置 H2 数据库（…）", "db_kind": "h2",
   "disk_free": 31876000000, "backups": 2, "last_backup": "2026-10-10 01:16（2.0.69）"},
  {"product": "sight-wall", "name": "SightWall", "installed": false},
  {"product": "sight-datalink", "name": "Sight DataLink", "installed": true, "version": "0.2.0", "port": 18680,
   "running": true, "healthy": true, "bootstrapped": false, "db_reachable": null,
   "mode": "setup",                                    // 健康检查的 mode：setup（安装向导）/ waiting（等元数据库）/ normal
   "admin_initialized": false,                         // 健康检查有 adminInitialized 时才有：false = 还没「初始化管理员」
   "setup_url": "http://10.0.0.7:18680/",             // setup_path 不是 /setup 的产品才有
   "db": "尚未配置（还没走安装向导）", "db_kind": "none", ...}
 ]}
```
`status` 结果顶层另有 `arch`（服务器 CPU 对应的安装包架构：`amd64` / `arm64` / `loong64`，不认识为 null），移动网关选安装包用。

Sight 移动网关（`kind: "gateway"`）的 `status` 项没有 Java / 数据库 / 初始化相关字段，而是：
```json
{"product": "sight-mobile-gateway", "name": "Sight 移动网关", "kind": "gateway", "installed": true, "version": "2.0.96",
 "dir": "/opt/sight-mobile-gateway", "service": "systemd", "active": "active", "running": true,
 "healthy": true,                      // 隧道端口 /healthz 返回了 {"agents":n}
 "agents": 1,                          // SightReport 连进来的数量；0 = 「SightReport 还没有连上来」；null = 服务器没有 curl / wget 读不到
 "port": 8080, "mobile_port": 8080, "tunnel_port": 7443, "tunnel_tls": true,
 "tunnel_url": "wss://gw.example.com:7443/tunnel",   // 证书里登记的第一个域名 / IP（没有就用 SSH 地址）
 "hostnames": ["gw.example.com"], "has_config": true, "disk_free": 18400000000, "backups": 1, "last_backup": "2026-10-20 14:02（2.0.68）",
 "registered_version": "2.0.96"}
```
`url` 为 null（没有给人打开的页面）。

Sight DataLink 的数据库描述来自 `data/datalink.properties` 的 `DB_URL`（Java properties 转义），部署方在 systemd 环境、进程环境或 `sight-datalink.env` 里给的 `DB_URL` 优先。

### 任务事件

`GET /api/jobs/<id>/events?since=<n>` → 长轮询（最多等 1 秒有新事件就返回）：
```json
{"events": [...], "next": 42, "done": false, "ok": null, "result": null, "error": null, "hint": null,
 "question": null}
```
- `next`：下次请求的 `since`。
- `done=true` 时 `ok`（bool）、`result`、`error`（失败原因）、`hint`（建议）有值。用户取消的任务 `ok=false, error="已取消"`。
- `question`：当前待回答的问题（见下），没有为 null。

事件（按顺序渲染成时间线）：
| t | 字段 | 说明 |
|---|---|---|
| `step` | `text` | 阶段标题（如「上传安装包（28.6 MB）」），时间线上的大节点 |
| `ok` / `warn` / `fail` | `text` | √ 绿 / ! 琥珀 / × 红 的一行 |
| `info` | `text` | 普通说明行 |
| `log` | `text` | 服务器原始输出（等宽、灰色、默认折叠在所属 step 下，可展开） |
| `progress` | `id, label, done, total, speed`（字节/秒）`, eta`（秒） | 同一 `id` 是同一个进度条，原地更新 |
| `live` | `id, text` | 同一 `id` 原地更新的一行（如「等待服务启动 … 0:12」）；`text` 为空表示移除 |
| `kv` | `title, items: [[标签, 值], ...]` | 一组键值（安装计划、环境信息等），渲染成小表格 |

### 任务中途的问题

任务执行中可能需要用户决定，`question` 字段非空时弹出模态框（不能点遮罩关闭）：
```json
{"qid": "q7", "kind": "...", "title": "问题标题", "detail": ["可选的说明行", "..."], "error": "上次输入的校验错误或 null", ...}
```

| kind | 额外字段 | 回答 value |
|---|---|---|
| `ask` | `label, default, placeholder` | 字符串 |
| `secret` | `label` | 字符串（密码框） |
| `confirm` | `default`(bool), `danger`(bool) | `true` / `false`（按钮「确定」「取消」；default 那个按钮为主按钮；danger 时主按钮红色） |
| `typed` | `word` | 用户输入的字符串（提示「请输入 <word> 确认」，输入一致才能点确定） |
| `choose` | `options: [{"label": "...", "desc": "可选副标题"}], default`(0 起的下标) | 选中的下标（整数） |
| `form` | `fields: [{"key","label","default","placeholder","help","type":"text/password/number/checkbox"}]` | `{"key": "值", ...}`（`checkbox` 的值是 `true` / `false`，`default` 为 `"1"` 时默认勾选） |
| `file` | `file_kind`("jdk"/"package"/"key"), `label` | 本机文件路径（复用安装包选择器：自动发现列表 + 浏览 + 拖入上传） |

`POST /api/jobs/<id>/answer` `{"qid": "q7", "value": ...}`；用户点「取消 / 返回」时 `{"qid": "q7", "cancel": true}`（任务会中止）。
校验失败时后端会**再发同一类问题**（新 qid，`error` 有值），前端照常显示即可。

`POST /api/jobs/<id>/cancel` → 请求中止。说明：升级的关键步骤在服务器后台执行，取消只是「停止等待」，后端会在时间线里说明。

### 同机多实例

`instances=true` 的产品（只有 SightReport）可以在同一台服务器上装几套，用于同机集群（配合 nginx 入口不停机升级）。

- **产品键**：实例 `b` 的键是 `<产品>@b`，如 `sight-report@b`；基础实例（老的那套）键不变。`service` / `logs` / `follow_logs` / `list_backups` / `rollback` 的 `product` 直接传实例键。
- **实例名**：`^[a-z0-9][a-z0-9-]{0,15}$`。实例 `b` 的默认安装目录 `<dir>-b`、系统服务 `<service>-b`，新装时默认端口 = 基础端口 + 1（端口被占会照常询问）。
- **探测**：`status` 列出 `/etc/systemd/system/<service>-*.service` 找到的实例（以及 `hosts.json` 里登记过的），结果的 `products` 里实例项紧跟在基础产品后面，另带 `base`（基础产品键）、`instance`（实例名）。安装目录下的 `.sight-instance` 记着实例名，定位已有安装时核对它，不会把实例 b 的目录当成基础实例。
- **安装包要求**：需要 2.0.117 起的完整安装包（`.zip`，带 `bin/sight-common.sh`，`install.sh --instance`）；旧包传 `instance` 直接失败：「同机多实例需要 2.0.117 及以上版本的完整安装包（.zip）」。
- 安装时助手在安装目录写 `.sight-instance`，并给 `install.sh` 加 `--instance <名字>`。

### 集群

几个实例连同一个外部数据库、前面一个 nginx 入口。只有 `/api/state` 里 `cluster=true` 的产品能组集群（只有 SightReport；SightWall 只做单实例部署）。见 sight-data 仓库 `docs/集群部署与集群入口-设计.md` §4、§6、§8。

`GET /api/state` 另有：
```json
"clusters": {"report-prod": {"product": "sight-report", "db_fp": "库地址指纹",
   "entry": {"host": "入口服务器名", "port": 80, "server_name": "", "public_url": ""},
   "instances": [{"host": "node-01", "product": "sight-report", "port": 9091, "install_dir": "/opt/sight-report", "down": false}],
   "shared_dir": "/mnt/sight-shared", "gateway": "dmz-01", "created": "2026-11-12 10:00"}},
"cluster_suggestions": [{"product": "sight-report", "db_fp": "…", "cluster_mode": true,
   "instances": [{"host": "node-01", "product": "sight-report", "port": 9091, "version": "2.0.117", "cluster_mode": true}]}]
```
- 实例 = 服务器名 + 产品键（带实例名，如 `sight-report@b`），在接口参数里写成 `"<服务器>|<产品键>"`。
- `status` 结果的产品项另有 `cluster`（所属集群名）；hosts.json 里的产品记录另有 `db_fp`（外部库地址规范化后的 sha256 前 16 位）、`cluster_mode`。`cluster_suggestions` = 同一产品、同一 `db_fp`、还没归组的实例 ≥2 个。
- 删除服务器时，它若是某个集群的实例或入口，返回 400「请先在集群里把它移除」。

| 接口 | 参数 | 说明 |
|---|---|---|
| `POST /api/clusters/save` | `name, old_name?（改名 / 编辑时）, product, instances: [{host, product, port?, addr?}], entry: {host, port, server_name, public_url} 或 null, shared_dir, gateway` | 只改本地记录。校验：集群名 `^[a-z0-9][a-z0-9-]{0,31}$`；实例属于该产品、不在别的集群；`db_fp` 不能不同；入口端口不能与同机实例端口相同；`public_url` 要 http(s)。编辑时保留各实例的 `down`。返回 `{"name"}` |
| `POST /api/clusters/delete` | `name` | 只删记录，不动服务器和 nginx |

| 任务 kind | params | result |
|---|---|---|
| `cluster_status` | `cluster` | `{"cluster", "instances": [{host, product, port, dir, version, running, healthy, ready, state, reasons, mode, leader, node_id, active_nodes, crypto_fp, crypto_ok, down, error}], "crypto_consistent", "versions", "entry": {host, port, nginx, version, test, test_out, confd, wst, running, conf, upstream: [{addr, port, down}], probe, error}}`。`crypto_fp` 是本机对各实例加密配置算的散列（只用于比较），`probe` 是在入口机上经入口访问 `/api/health/live` 的状态码 |
| `cluster_entry` | `cluster, up?: [实例], down?: [实例]` | 先改实例的 `down`，再：检查 / 安装 nginx（没有就 `confirm` 后用 dnf / yum / apt-get / zypper 安装；版本 <1.9.13 拒绝，<1.18 提醒；主配置没 include `conf.d/*.conf` 拒绝；现有配置 `nginx -t` 不过拒绝；没设 `worker_shutdown_timeout` 提示）→ 写两个文件 → `nginx -t` → 通过才重载（没在跑就启动），不通过恢复 `.bak` 并失败 → 防火墙放行入口端口（`confirm`）→ 经入口探测。返回 `{"url"}` |
| `cluster_expand` | `cluster, host, instance?, package` | 从集群里第一个没摘下的实例读数据库连接与加密配置（`secret_out`，不进日志）；来源不是集群模式 / 包版本不同会 `confirm`；照常 `do_install`（会问目录、端口），在 `install.sh` 之前把机密经 stdin 写进新实例的 `sight.env` 与 `data/work/database.config`（600）；等就绪 → 拷插件（有的话，重启一次）→ 新机器跑 `install.sh identity` → 记进集群 → 挂进入口。返回 `{"host", "product", "url", "identity"}` |
| `cluster_upgrade` | `cluster, package` | 预检读各实例版本（有实例比包新 → 失败，不做降级；都已是这个版本 → 直接成功）。`rolling_decision(rollingFrom, 各实例版本)`；滚动且没有入口时 `confirm`；集群层面一次 `confirm`（已备份数据库），之后各实例的升级不再逐台问。滚动：每个实例 `down` → 重载 → `do_upgrade` → 等 `/api/health/ready` 200 且版本对上 → 放回 → 重载；失败停在该实例、不放回，错误里列出已升级的实例。整体停机：全停 → 逐个升级 → 原本已是新版本的实例重新启动。返回 `{"upgraded": [...], "rolling": true/false/null}`；集群登记了移动网关时只提醒那一台同步升级 |

另外：`deploy` 单独升级一个属于多实例集群的实例时，先 `warn` 再 `confirm`（建议用集群升级）；升级 SightReport 后的移动网关提醒（`gateway_notices`），该实例属于登记了 `gateway` 的集群时只针对那一台。

### Sight 移动网关

非 Java 产品，`deploy` / `service` / `logs` / `follow_logs` / `list_backups` / `rollback` / `status` 都按 `product: "sight-mobile-gateway"` 走同一批接口，差别：

- **安装**（服务器上没有网关）：问一个 `form`（安装目录；`hostnames` = SightReport 访问网关用的域名或 IP，可多个，逗号分隔；`port` = 手机访问端口，默认 8080），再 `confirm` 安装计划。环境检查有阻止安装的项（CPU 架构与包不符、没有 systemd、端口被占用、磁盘不足、目标目录里已有 `gateway.yaml` / 程序）时任务失败，不做任何改动；手机侧端口被占用时会再问一个新端口。之后：上传 → 在服务器上试跑 `version` → `init -hostnames …`（输出里的指纹和令牌**不进本地日志、不进时间线**）→ 需要时把 `listen` 改成所填端口 → `install` → `start` → 等 `/healthz`。结果项：
  ```json
  {"host": "前置机", "status": "完成", "url": null,
   "gateway": {"version": "2.0.96", "mobile_port": 8080, "tunnel_port": 7443, "hostnames": ["gw.example.com"],
               "tunnel_url": "wss://gw.example.com:7443/tunnel", "agents": 0,
               "fingerprint": "BA:DE:…（SHA-256，冒号分隔）", "token": "接入令牌（机密）"}}
  ```
  `fingerprint` / `token` 只在**首次安装**和 `gateway_show` 的结果里有（只存在内存里，不写本地日志和 `hosts.json`）；升级结果的 `gateway` 没有这两项（令牌和指纹没变）。隧道没启用 TLS（`tunnel.tls: false`）时没有 `fingerprint`。
- **升级**：`confirm` 升级计划 →（服务仍在运行时）上传 → 试跑新程序 → 后台脚本「停止 → 备份旧程序 → 替换 → 启动」→ 核对 `version` 与 `/healthz`；失败时 `confirm` 是否回滚。**不会**碰 `gateway.yaml` 和 `data/`。降级要输入「降级」确认。
- **`list_backups`**：备份是 `<备份目录>/sight-mobile-gateway-<版本>-<时间>.bin`（`db_kind` 恒为 `"none"`）；**`rollback`**：`file` 必须是这样的 `.bin` 文件，`full` 被忽略（只换回旧程序；当前程序会先另存一份，回滚也能撤销）。
- **`service`**：用程序自己的 `start` / `stop`，启动后等 `/healthz`。

## 页面要做的事（产品需求）

1. **服务器列表**（左栏）：名字、user@host、已知产品徽标；「+ 添加服务器」。选中一台就跑 `status` 任务，结果显示在右侧。
2. **服务器详情**（右侧）：顶部名字/地址 +「刷新」「修改密码」「删除」；每个产品一张卡（SightReport、SightWall、Sight DataLink，按 `/api/state` 的 `products`）：
   - 左栏服务器列表上方是「集群」：每个集群一项（产品、实例数、入口、已摘下几个）+「新建集群」；有 `cluster_suggestions` 时显示「N 个实例连着同一个数据库 · 归为一个集群」（打开新建对话框并预选这些实例）。
     集群页：「刷新」（`cluster_status`）「编辑」「配置入口」（`cluster_entry`）「扩容」（选服务器与实例名 → 选包 → `cluster_expand`，结果里有 `identity` 时醒目显示并可复制）「删除」，主按钮「集群升级」（选包 → `cluster_upgrade`）；入口卡片（nginx 状态、访问地址、配置文件）；实例表（版本、就绪状态、leader、加密配置一致性、是否在入口，「摘下 / 放回入口」）。
   - 未安装：灰卡 +「安装」按钮。
   - 已安装：状态徽标（运行中 / 启动中或无响应 / 未运行）、版本、访问地址（可点击，新窗口打开）、目录、端口、服务方式、Java、数据库、健康（未初始化时提示去 `setup_url` 或 `/setup`；`mode=setup` 徽标「安装向导」并提示需要初始化令牌；`mode=waiting` 徽标「等待数据库」并说明服务在自动重试；`admin_initialized=false` 提示去「初始化管理员」）、备份数、磁盘剩余。
     按钮：「升级」「重启」「停止 / 启动」「日志」「回滚」；`instances=true` 的产品另有「再装一个实例」（先问实例名，默认第一个没用过的字母 b、c…，校验格式与重名，再走「安装 / 升级」流程，`deploy` 带 `instance`）。同机多实例各一张卡，标题「SightReport（实例 b）」，按 `status` 结果的顺序排在基础产品后面。
3. **安装 / 升级**：打开「选择安装包」对话框（顶部是「官网最新版」条目——选中后点「下一步」才跑 `download_package`，下完再进第 2 步；本机已有同版本时不再显示官网条目；下面是自动发现列表，按时间倒序，显示产品+版本+大小+目录；「浏览…」文件浏览器；拖入区域，拖进来先上传再选中）；
   然后「选择服务器」（默认当前服务器，可多选做滚动升级，说明“多台会依次处理，某台失败会询问是否继续”；`rolling_upgrade=false` 的产品多选时显示集群升级顺序的提醒）→ 开始 `deploy` 任务。
   从产品卡点「安装/升级」时，安装包列表优先显示该产品的包。
4. **任务面板**：任务开始后弹出大面板（或右侧抽屉）展示时间线：step 为节点，其下 ok/warn/fail/info 行、进度条、live 行、可折叠的 log。
   顶部显示任务标题与耗时；运行中有「停止」按钮（cancel，需二次确认）。结束后显示成功/失败横幅（失败显示 error + hint）、「关闭」按钮；
   deploy 成功且 result 里有 url 时显示「打开 SightReport」按钮（`mode=setup` 时为「打开安装向导」）。results 里有 `setup_token` 时在结果横幅里醒目显示令牌 +「复制令牌」，并说明安装向导和之后的「初始化管理员」都要用同一个令牌；`mode=waiting` 时说明元数据库连不上、服务在自动重试。关闭后自动刷新当前服务器状态。
5. **日志**：对话框，等宽字体显示 `logs` 结果，「刷新」「实时跟踪」开关（开=`follow_logs` 任务，把 log 事件追加到底部并自动滚动；关=cancel）。
6. **回滚**：先跑 `list_backups`，列出备份（时间、版本、大小；`schema_versioned` 的产品另显示结构版本），选一份 + 选范围（程序+数据 / 仅程序，附说明；外部数据库时提示数据库不在备份里；外部库结构版本不同时红色说明并禁用「回滚…」）→ 二次确认 → `rollback` 任务。
7. **添加服务器**：表单（地址、SSH 端口 22、账号 root、登录方式 密码/密钥文件、密码 / 私钥路径（带「浏览…」）+ 私钥密码、名称（默认=地址））→ `add_host` 任务（会做连接测试；任务里可能还会问 sudo 密码）。成功后列表刷新并选中它。
8. **设置**：升级备份保留份数（Sight DataLink 升级前做的 pg_dump 元数据库备份同样按它清理）、等待服务就绪秒数。页脚显示本地日志路径（可复制）与「退出程序」。
9. 首次打开且没有服务器时，右侧显示引导：三步说明（添加服务器 → 选择安装包 → 一键安装/升级）+ 大号「添加服务器」按钮。
10. **Sight 移动网关**（`products[id].kind === "gateway"`）：
    - 产品卡：版本、服务状态、「SightReport 连接数」（0 时黄色提示「SightReport 还没有连上来」并说明去 SightReport「移动 › 设置 › 网络与部署」启用）、网关地址（`tunnel_url`）、手机端口 / 隧道端口（隧道端口提示只放行 SightReport 所在机器）；按钮多一个「查看接入信息」（`gateway_show`）。未安装卡提示要装在手机能访问到的前置机（DMZ）上，不是 SightReport 所在的机器。
    - 版本配套：助手里知道的 SightReport 版本（所有服务器里最高的）与网关版本不一致时，卡片上提示「移动网关需同步升级到 x.y.z」。
    - 选安装包：官网条目按架构各一条（`variants`）；已知服务器架构（`status.arch`）时只列匹配的，本机包架构不符的打「与服务器架构不符」标，点「下一步」时直接拦下；`download_package` 要带 `arch`。
    - 安装完成页：`results[].gateway.token` 存在时醒目显示「网关地址 / 证书指纹 / 接入令牌」三项（各有复制按钮）、下一步三步说明和端口放行提醒；升级完成页只显示版本和「令牌与指纹没变」。回滚对话框没有「恢复范围」，只换回旧程序。
11. **在线更新提示**：启动后（设置里没关「启动时自动检查更新」）后台调 `GET /api/online`：
    - 部署助手有新版本 → 页面顶部横幅「下载新版本 / 忽略此版本」，侧栏版本号旁出现「有新版本」；点「下载新版本」跑 `download_tool`，成功后提供「在文件夹中显示」；Windows 打包版和 Mac 的 .app 还提供「立即更新并重启」（`POST /api/apply_update`）：Windows 把当前 exe 改名为 `.exe.old`、换上新 exe，再带 `--after-update <旧进程号>` 拉起它（新进程等旧进程退出才开始，并删掉 `.old`），复制失败会改回原样，换不上就直接运行下载的 exe；Mac 写 `~/.sight-deploy/update.sh`，等本进程退出后解压新 zip，把旧 .app 挪开、换上新 .app 并打开，换不上就放回旧 .app，把新 .app 放进「下载」打开。每一步记在 `~/.sight-deploy/update.log`。覆盖前会校验下载的文件（exe 要有 `MZ` 头，zip 里要有 .app），能自更新的平台把新版下载到 `~/.sight-deploy/updates/`。
    - 产品卡：已安装版本低于官网最新时，版本旁出现「官网有 X」标签和一条提示；点「升级」自动预选版本最高的包（本机包与官网最新版里选高的，同版本优先用本机的）。
    - 侧栏「检查更新」：强制刷新（`?refresh=1`）并弹出汇总（部署助手、两个产品的最新版本、哪些已添加的服务器可升级）；连不上官网时说明原因。
    - 联网失败一律不打断使用：自动检查失败时静默，选包对话框里提示「只能使用本机上的安装包」。
