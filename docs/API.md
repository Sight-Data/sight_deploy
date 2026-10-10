# Sight 部署助手 —— 前后端接口约定

本地程序（Python）在 `127.0.0.1:<port>` 起 HTTP 服务并自动打开浏览器：`http://127.0.0.1:<port>/?t=<token>`。
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
  "settings": {"keep_backups": 3, "health_timeout": 300},
  "hosts": [
    {"name": "生产-1", "host": "10.0.0.5", "port": 22, "user": "root", "auth": "password",
     "last_used": "2026-10-10 01:16",
     "products": {"sight-report": {"install_dir": "/opt/sight-report", "port": 9091, "version": "2.0.71"}}}
  ],
  "products": {"sight-report": {"name": "SightReport", "port": 9091}, "sight-wall": {"name": "SightWall", "port": 9090}},
  "log_path": "/Users/x/.sight-deploy/logs/sight-deploy-20261010.log"
}
```
`hosts[].products` 是**上次查到的缓存**，可能过时；真实状态以 `status` 任务结果为准。

`POST /api/settings` `{"keep_backups": 3, "health_timeout": 300}` → `{"ok": true}`

`POST /api/hosts/delete` `{"name": "生产-1"}` → `{"ok": true}`（只删本机保存的信息）

`POST /api/shutdown` → `{"ok": true}`，随后进程退出（前端显示「已退出，可以关闭此页面」）。有任务在跑时返回 409。

## 本地文件

`GET /api/packages` → 自动发现的安装包（当前目录 / 程序目录 / 下载 / 桌面）：
```json
[{"path": "/Users/x/Downloads/sight-report-2.0.71.zip", "name": "sight-report-2.0.71.zip",
  "size": 258824866, "mtime": 1791518682, "dir": "/Users/x/Downloads",
  "product": "sight-report", "version": "2.0.71"}]
```
（`product` / `version` 由文件名猜，可能为 null）

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
| `deploy` | `hosts: [名字...], package: 本机路径` | `{"results": [{"host": "生产-1", "status": "完成/跳过/失败/取消", "url": "http://..."}]}` |
| `service` | `host, product, action("start"/"stop"/"restart")` | `{}` |
| `logs` | `host, product, lines(默认200)` | `{"text": "日志文本"}` |
| `follow_logs` | `host, product` | 不会自己结束；通过 `log` 事件推送新行，用户点「停止」→ cancel |
| `list_backups` | `host, product` | `{"backup_dir": "...", "current_version": "2.0.71", "db_kind": "h2/external/none", "backups": [{"file": "...", "version": "2.0.69", "time": "2026-10-10 01:16", "size": 30000000}]}` |
| `rollback` | `host, product, file, full(bool)` | `{}` |

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
  {"product": "sight-wall", "name": "SightWall", "installed": false}
 ]}
```

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
| `form` | `fields: [{"key","label","default","placeholder","help","type":"text/password/number"}]` | `{"key": "值", ...}` |
| `file` | `file_kind`("jdk"/"package"/"key"), `label` | 本机文件路径（复用安装包选择器：自动发现列表 + 浏览 + 拖入上传） |

`POST /api/jobs/<id>/answer` `{"qid": "q7", "value": ...}`；用户点「取消 / 返回」时 `{"qid": "q7", "cancel": true}`（任务会中止）。
校验失败时后端会**再发同一类问题**（新 qid，`error` 有值），前端照常显示即可。

`POST /api/jobs/<id>/cancel` → 请求中止。说明：升级的关键步骤在服务器后台执行，取消只是「停止等待」，后端会在时间线里说明。

## 页面要做的事（产品需求）

1. **服务器列表**（左栏）：名字、user@host、已知产品徽标；「+ 添加服务器」。选中一台就跑 `status` 任务，结果显示在右侧。
2. **服务器详情**（右侧）：顶部名字/地址 +「刷新」「修改密码」「删除」；两张产品卡（SightReport、SightWall）：
   - 未安装：灰卡 +「安装」按钮。
   - 已安装：状态徽标（运行中 / 启动中或无响应 / 未运行）、版本、访问地址（可点击，新窗口打开）、目录、端口、服务方式、Java、数据库、健康（未初始化时提示去 /setup）、备份数、磁盘剩余。
     按钮：「升级」「重启」「停止 / 启动」「日志」「回滚」。
3. **安装 / 升级**：打开「选择安装包」对话框（自动发现列表，按时间倒序，显示产品+版本+大小+目录；「浏览…」文件浏览器；拖入区域，拖进来先上传再选中）；
   然后「选择服务器」（默认当前服务器，可多选做滚动升级，说明“多台会依次处理，某台失败会询问是否继续”）→ 开始 `deploy` 任务。
   从产品卡点「安装/升级」时，安装包列表优先显示该产品的包。
4. **任务面板**：任务开始后弹出大面板（或右侧抽屉）展示时间线：step 为节点，其下 ok/warn/fail/info 行、进度条、live 行、可折叠的 log。
   顶部显示任务标题与耗时；运行中有「停止」按钮（cancel，需二次确认）。结束后显示成功/失败横幅（失败显示 error + hint）、「关闭」按钮；
   deploy 成功且 result 里有 url 时显示「打开 SightReport」按钮。关闭后自动刷新当前服务器状态。
5. **日志**：对话框，等宽字体显示 `logs` 结果，「刷新」「实时跟踪」开关（开=`follow_logs` 任务，把 log 事件追加到底部并自动滚动；关=cancel）。
6. **回滚**：先跑 `list_backups`，列出备份（时间、版本、大小），选一份 + 选范围（程序+数据 / 仅程序，附说明；外部数据库时提示数据库不在备份里）→ 二次确认 → `rollback` 任务。
7. **添加服务器**：表单（地址、SSH 端口 22、账号 root、登录方式 密码/密钥文件、密码 / 私钥路径（带「浏览…」）+ 私钥密码、名称（默认=地址））→ `add_host` 任务（会做连接测试；任务里可能还会问 sudo 密码）。成功后列表刷新并选中它。
8. **设置**：升级备份保留份数、等待服务就绪秒数。页脚显示本地日志路径（可复制）与「退出程序」。
9. 首次打开且没有服务器时，右侧显示引导：三步说明（添加服务器 → 选择安装包 → 一键安装/升级）+ 大号「添加服务器」按钮。
