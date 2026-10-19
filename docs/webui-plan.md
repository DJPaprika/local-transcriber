# Web UI 方案

> 状态：**已实现**，代码在 [`web/`](../web/)。本文保留当初的选型和理由，
> 实现后的偏差见文末「实现时改了什么」。

## 目标

给现有 CLI 加一层浏览器界面，**不改动 `engines/` 和 `llm/` 的任何一行**。

定位是「本机工具」，不是「分发给别人的软件」——所以不追求单文件打包，
也不为了跨平台牺牲依赖约束。

## 选型结论

**用标准库的 `http.server` + 原生前端，浏览器访问。**

### 排除的方案

| 方案 | 排除原因 |
|---|---|
| Gradio / Streamlit | 需要 pip 装一堆依赖。本机 Python 3.14，带 Rust 扩展的包（pydantic-core 等）可能没有预编译轮子——这和当初排除 `llama-cpp-python` 是同一个坑 |
| Electron / Tauri | 引入第二个运行时（Node / Rust 工具链），打包体积上百 MB，违反「解压即用、零 pip 依赖」的项目原则 |
| FastAPI + uvicorn | 本身是纯 Python，但依赖 pydantic v2（Rust 扩展），3.14 上有轮子风险。**如果以后装得上，可以换，接口设计不用变** |

### 为什么浏览器是对的

1. **长任务天然适合**。转写要 5 分钟，命令行只能滚屏；Web 可以开任务列表、后台跑
2. **不用处理 Windows 桌面细节**（高 DPI、字体、窗口缩放）
3. **能截图**，作品集需要「看得见的成果」

## 结构

```
web/
├── app.py          # 路由 + 静态文件服务（ThreadingHTTPServer）
├── jobs.py         # 任务队列（并发 1）+ 状态 + 日志环形缓冲
└── static/
    ├── index.html
    ├── app.js
    └── style.css
```

零新增依赖：`http.server` / `json` / `threading` / `subprocess` / `pathlib`。

## 接口

```
POST /api/jobs              {source, engine, lang}     → {id}
GET  /api/jobs              (可选 ?status=running)    → [job]
GET  /api/jobs/{id}         → {status, stage, percent, logs[], result}
POST /api/jobs/{id}/cancel  → {ok}
GET  /api/files/{path}      → 下载 srt / txt / summary
```

前端每 1 秒轮询 `/api/jobs/{id}`。**不用 SSE**——轮询够用，少一层复杂度。

## 核心设计：进度状态文件

这是整个方案里最关键的一点。

不给 `transcribe.py` 加任何 Web 相关代码，而是让它**在关键节点写一个 JSON 状态文件**：

```
output/<name>/.progress.json
{
  "stage": "transcribe",        // extract | transcribe | post
  "percent": 42,
  "elapsed": 123.4,
  "detail": "已处理 00:09:48 / 00:23:14",
  "updated_at": 1792...
}
```

Web 层只读这个文件，不解析日志文本。

**好处**：

- CLI 和 Web 完全解耦，CLI 还能单独用
- 前端不需要理解日志格式
- 进程意外退出时，`updated_at` 停住就是判断依据

## 任务队列

- **并发上限 1**。SenseVoice 会吃满 CPU（4.6× 实时），同时跑两个只会互相拖慢；
  显存也只够一个 llama-server
- 状态机：`queued → running → done | failed | cancelled`
- UI 上诚实显示「排队中（前面还有 1 个任务）」

## 取消要杀干净

Windows 上点「取消」必须用 `taskkill /T /F` 杀掉整个进程树，
否则 node bridge、ffmpeg 会变成孤儿进程继续占 CPU。

**这是必须处理项**，不处理第一次用就会踩到。

## 待讨论

| # | 问题 | 我的倾向 |
|---|---|---|
| 1 | 要不要支持拖拽本地文件 | 要，比贴 URL 更常用 |
| 2 | 摘要/翻译做成独立任务还是转写完成后一键触发 | 一键触发，转写完成才有意义 |
| 3 | 历史记录存哪（内存 / 文件 / SQLite） | JSON 文件，重启不丢 |
| 4 | llama-server 要不要常驻开关 | 要，现在是惰性启停，批量处理时希望它别关 |
| 5 | 要不要用 pywebview 套成桌面窗口 | 先不做，浏览器打开够用；多一个 pip 依赖不值 |
| 6 | 端口冲突怎么办 | 8081 起，被占就递增，启动时打印真实地址 |
| 7 | 完成后自动打开浏览器 | 要，`webbrowser.open` 一行 |

## 工作量

| 部分 | 估计 |
|---|---|
| `serve.py` | ~150 行 |
| `jobs.py` | ~150 行 |
| 前端（含样式） | ~500 行 |
| 改动 `transcribe.py`（加进度写入） | ~30 行 |

两轮能出可用版本。

---

## 实现时改了什么

### 七个待定项的落地情况

| # | 问题 | 结果 |
|---|---|---|
| 1 | 拖拽本地文件 | ✅ **已做**（2026-10-20）。不做 multipart，直接 POST 原始字节 + `?name=`，服务端边读边写盘 |
| 2 | 摘要一键触发 | ✅ 转写卡片上多了「生成摘要」「翻译成中文」两个按钮 |
| 3 | 历史记录 | ✅ 每个任务落一个 `output/.jobs/<id>/job.json`，重启后列表还在 |
| 4 | llama-server 常驻 | ✅ **已做**（2026-10-20）。Web 设置里开关 + pid 文件 + 「停掉服务」按钮 |
| 5 | pywebview 套壳 | ❌ 按计划不做 |
| 6 | 端口自动递增 | ✅ `find_port()` 从 8081 往上找 |
| 7 | 自动开浏览器 | ✅ `webbrowser.open`，`--no-browser` 可关 |

### 后续补上的三项（2026-10-20）

- **删除任务**：`DELETE /api/jobs/{id}`，只删 `.jobs/<id>/` 和列表条目，产物保留
- **输出命名统一**：Web 层不再推导名字，交给 `transcribe.py`（URL 用视频标题），
  UI 与 CLI 产出同名同目录；摘要/翻译也用 `translate.py` 的默认命名
- **中文文件名的下载**：`Content-Disposition` 只能是 latin-1，先用 ASCII 兜底名 +
  `filename*=UTF-8''...`，否则标题里带中文时下载会 500

### 没预料到的三件事

**① `transcribe.py` 的进度参数不能是必选**

原计划让它往固定位置写进度，实现时改成 `--progress <path>` 可选参数——
不传就一行都不写，`_PROGRESS` 保持 `None`。这样 CLI 的行为完全没变，
也没有"CLI 也要建 .jobs 目录"的耦合。

**② `taskkill /T` 不一定杀得动**

在受限沙箱里 `taskkill` 会报 `Access is denied`（打不开进程句柄），
结果是取消后任务状态永远停在 `running`，node bridge 变孤儿继续吃 CPU。

加了一层兜底：`taskkill` 失败就退回 `proc.kill()`，至少让子进程退出、
状态能落定。正常桌面环境下 `taskkill /T /F` 是能连孙进程一起清理的。

**③ 中文文件名会打断下载**

输出名从 BV 号改成视频标题之后，`Content-Disposition: filename="..."` 直接抛
`UnicodeEncodeError`——HTTP 头只能用 latin-1。后来改成 ASCII 兜底 + RFC 5987 的
`filename*=UTF-8''...`，浏览器才认得。
