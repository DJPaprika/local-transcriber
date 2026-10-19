# web/

本地 Web UI。**零新增依赖**——只用标准库 `http.server`。

## 启动

```bash
python web/serve.py                 # 从 8081 找空闲端口，自动开浏览器
python web/serve.py --no-browser    # 不自动开浏览器
python web/serve.py --port 9000     # 指定起始端口（被占则递增）
```

打开后就是一个页面：贴链接或本地路径 → 看进度和日志 → 下载 srt / txt / 摘要。

## 依赖

和 CLI 完全一样，没有多装任何东西：

```
python web/serve.py   →  transcribe.py / translate.py  →  engines/ + llm/
```

子进程用的就是同一套脚本，所以 CLI 能跑通的，UI 一定能跑通。

## 几个设计决定

### ① 串行，一次只跑一个任务

SenseVoice 会吃满 CPU（实测 4.6× 实时），同时跑两个只会互相拖慢；
显存也只够一个 llama-server。所以队列并发上限是 1，其余任务显示「排队中」。

### ② 不用管道，靠文件通信

```
子进程 stdout/stderr ──→ output/.jobs/<id>/job.log   （父进程按 offset 增量读）
转写进度           ──→ output/.jobs/<id>/progress.json
```

父子之间没有 pipe。好处是父进程崩了子进程不受影响，日志也能事后翻。

`transcribe.py` 为此加了 `--progress <path>`：不传就完全不写，CLI 行为不变。

### ③ 取消要杀整棵进程树

Windows 上用 `taskkill /T /F`，否则 node bridge、ffmpeg 会变成孤儿继续占 CPU。

若 `taskkill` 被环境拒绝（受限沙箱），会退回到只杀直接子进程——至少保证
任务状态能落定，不留一个"永远在跑"的条目。

### ④ 输出命名交给 transcribe.py

Web 层不再用 BV 号之类的「推导名」传给 `-n`。不填输出名时，`transcribe.py`
自己按「视频标题」命名，所以 UI 产物和 CLI 产物落在同一个 `output/` 下、叫同一个名字：

```
output/苏联卫国战争胜利纪念口粮.srt
output/苏联卫国战争胜利纪念口粮.txt
output/苏联卫国战争胜利纪念口粮.stats.json
```

摘要/翻译也沿用 `translate.py` 的默认命名（和源文件同目录同前缀），Web 层不再拼
`-o`。任务结果里带一个 `result.dir`（相对 `output/`，空串表示根目录），
前端按它拼下载地址。

### ⑤ 拖拽上传用原始字节，不用 multipart

浏览器把文件直接作为 `fetch/XHR` 的 body 发过来，文件名放在 query
（`POST /api/upload?name=xxx.mp4`）。这样服务端只要边读 `Content-Length` 边写盘，
不需要解析 multipart，也不用 `cgi`（Python 3.13 起已移除）。

每个文件落在一个独立目录：`output/.uploads/<时间戳>-<随机>/<原文件名>`，
保证转写出来的产物用原文件名命名，且同名文件不会互相覆盖。单文件上限 8 GB。

### ⑥ 常驻开关 + 可停止

`output/.jobs/settings.json` 里存 `keep_server`。打开后，摘要/翻译任务会带
`--keep-server`；`translate.py` 启动的 llama-server 会把 pid 写进
`output/.jobs/llama-server.pid`，页面上的「停掉服务」按这个 pid 收拾它。
外部自己启动的服务不写 pid，也不会被误杀。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/jobs` | `{kind, params}` → 创建任务 |
| GET | `/api/jobs` | 任务列表 |
| GET | `/api/jobs/{id}?offset=N` | 状态 + 增量日志 + 进度 |
| POST | `/api/jobs/{id}/cancel` | 取消 |
| DELETE | `/api/jobs/{id}` | 删除一条已结束的任务（只删记录，不删产物） |
| POST | `/api/upload?name=...` | 上传文件，body 是原始字节 |
| GET | `/api/files?path=...` | 下载 output 下的文件（有路径校验） |
| GET/POST | `/api/settings` | 读写 Web 设置（`keep_server`） |
| GET | `/api/llm` | 本地 llama-server 状态 |
| POST | `/api/llm/stop` | 停掉常驻的 llama-server |

`kind` 有四类：

```json
{"kind": "transcribe", "params": {"source": "...", "name": "", "engine": "sensevoice", "language": "auto"}}
{"kind": "summary",    "params": {"input": "xxx.txt"}}
{"kind": "translate",  "params": {"input": "xxx.srt", "target": "简体中文"}}
{"kind": "ask",        "params": {"input": "xxx.txt", "question": "..."}}
```

`name` 留空 = 用视频标题；显式填了才按填的来。`input` 是相对 `output/`
的路径（也接受绝对路径）。

## 文件

```
web/
├── serve.py            # HTTP 路由 + 静态文件 + 上传/设置/LLM 接口
├── jobs.py             # 任务队列、日志、进程管理、产物定位
├── run.cmd             # 双击启动（CRLF + 纯 ASCII，别改坏）
└── static/
    ├── index.html
    ├── app.js          # 原生 JS，无构建
    └── style.css
```

任务数据落在 `output/.jobs/<id>/`：

```
job.json       任务状态（重启后还能看到历史）
job.log        完整日志
progress.json  当前进度（转写时每 2 秒刷新）
```

`output/` 已经在 `.gitignore` 里，不会进仓库。

## 已知限制

- **进度只在阶段间更新**：转写阶段是"已用时间 + 预估剩余"，因为 SenseVoice
  是一次性返回结果的，中途没有真实百分比。Whisper 引擎同理。
- **上传上限 8 GB**：超出直接 413。上传是同步读取，大文件期间页面显示百分比，
  但没有断点续传，传一半断了要重来。
- **删除任务不删产物**：只移除列表条目和 `.jobs/<id>/` 日志目录，字幕文件原地保留。
- **没有鉴权**，只监听 `127.0.0.1`，只给本机用。别改成 `0.0.0.0`。
