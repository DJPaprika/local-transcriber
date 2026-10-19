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

## 三个设计决定

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

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/jobs` | `{kind, params}` → 创建任务 |
| GET | `/api/jobs` | 任务列表 |
| GET | `/api/jobs/{id}?offset=N` | 状态 + 增量日志 + 进度 |
| POST | `/api/jobs/{id}/cancel` | 取消 |
| GET | `/api/files?path=...` | 下载 output 下的文件（有路径校验） |

`kind` 有四类：

```json
{"kind": "transcribe", "params": {"source": "...", "name": "...", "engine": "sensevoice", "language": "auto"}}
{"kind": "summary",    "params": {"input": "...", "name": "..."}}
{"kind": "translate",  "params": {"input": "...", "name": "...", "target": "简体中文"}}
{"kind": "ask",        "params": {"input": "...", "question": "...", "name": "ask"}}
```

## 文件

```
web/
├── serve.py            # HTTP 路由 + 静态文件
├── jobs.py             # 任务队列、日志、进程管理
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
- **不能上传文件**，只能填本机绝对路径或 URL。浏览器沙箱拿不到真实路径，
  要做拖拽上传得再加一个 multipart 接收端。
- **没有鉴权**，只监听 `127.0.0.1`，只给本机用。别改成 `0.0.0.0`。
