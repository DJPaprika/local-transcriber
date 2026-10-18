# Web UI 方案（待讨论）

> 状态：方案已定方向，技术细节待 2026-10-19 之后讨论。
> 本文只记录决策和待定项，不含实现。

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
| `app.py` | ~150 行 |
| `jobs.py` | ~150 行 |
| 前端（含样式） | ~500 行 |
| 改动 `transcribe.py`（加进度写入） | ~30 行 |

两轮能出可用版本。
