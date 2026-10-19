# 交接文档

> 写给接手的人（或另一个 agent）。看完这份就能直接跑起来，也知道哪里埋着坑。
> 最后更新：2026-10-20

---

## 一、这是什么

把在线视频或本地媒体变成字幕的本地工具，全程离线，音频不出本机。
跑完还能接本地 LLM 做翻译、摘要、问答。

仓库：`https://github.com/DJPaprika/local-transcriber`（public，MIT）
本地：`D:\workspace\local-transcriber`

技术栈刻意保守：**Python 只用标准库**，没有 requirements.txt、没有虚拟环境。
唯一的运行时要装的东西是 `sherpa-onnx-node`（npm）和几个外部可执行文件。

---

## 二、现在什么状态

代码已全部提交并推送，`main` 与远端同步。

| 提交 | 内容 |
|---|---|
| `557500a` | 摘要提示词补细节与防幻觉约束；超长素材分段兜底；附调优记录 |
| `7f8835a` | 输出名改用视频标题；修复清 `.work` 后再 stat 崩溃；UI 相对路径按 `output/` 解析 |
| `7a4f255` | 输出名推导支持 Windows 反斜杠路径 |
| `8cf98bb` | `run.cmd` 改用 CRLF + ASCII |

**能用的**：CLI 转写 / 翻译 / 摘要 / 问答，Web UI 全流程。
**产物**：`output/` 下四组成品（案件解说、经济评论、开箱视频、40 秒片段），都带 summary。
**测试素材已删**：`samples/` 里的 `yt-demo.webm`、`clip40.wav` 已清理（19.7 MB），要测自己找素材。

---

## 三、怎么跑起来

### 3.1 先设一个环境变量（最容易漏的一步）

```powershell
$env:SHERPA_ONNX_NODE = "C:\Users\13414\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\node_modules\sherpa-onnx-node"
```

**不设这个，SenseVoice 引擎直接报「找不到 sherpa-onnx-node」**。项目里没有 `node_modules`，
`require("sherpa-onnx-node")` 也找不到它——它现在挂在 DSH 的依赖树里。

`web\run.cmd` 里已经把这一行写进去了，所以走 UI 不受影响；**走 CLI 必须自己设**。

> 长期建议：把 `SHERPA_ONNX_NODE` 设为用户级环境变量，或者 `npm i sherpa-onnx-node` 装到项目里。
> 这是当前可用性最差的一处。

### 3.2 常用命令

```powershell
cd D:\workspace\local-transcriber

# 转写（URL 或本地文件都可以）
python transcribe.py "https://www.bilibili.com/video/BVxxxx"
python transcribe.py "D:\path\to\video.mp4" -o output -n 自定义名字

# 摘要
python translate.py output\xxx\xxx.txt --task summary

# 翻译字幕（保留时间轴，输出 xxx.zh.srt）
python translate.py output\xxx\xxx.srt

# 针对内容提问
python translate.py output\xxx\xxx.txt --task ask -q "作者的核心观点是什么"
```

### 3.3 起 Web UI

双击 `web\run.cmd`，或：

```powershell
python web\serve.py          # 从 8081 开始找空闲端口，自动开浏览器
python web\serve.py --no-browser
```

页面上可以贴链接转写、看实时进度和日志、下载 srt/txt、一键摘要/翻译。任务是串行的。

---

## 四、代码地图

```
transcribe.py          转写入口：取流 → 抽音频 → 调引擎 → 写 srt/txt/stats
translate.py           翻译 / 摘要 / 问答入口
engines/               两个引擎的统一接口
  base.py              Transcript / Segment 数据结构
  whisper.py           调 whisper-cli
  sensevoice.py        调 bridge/sv-bridge.js
bridge/sv-bridge.js    Node 桥：sherpa-onnx → 字符级时间戳 JSON
llm/
  client.py            llama-server 的 OpenAI 兼容客户端（标准库 urllib）
  server.py            llama-server 惰性启停
  tasks.py             三套提示词 + 翻译/摘要/问答实现
web/
  serve.py             http.server 路由 + 静态文件
  jobs.py              串行任务队列
  static/              原生 JS 单页
  run.cmd              一键启动
docs/
  webui-plan.md        Web UI 选型与接口设计
  summary-experiments.md  摘要调优记录（很有参考价值，见第七节）
```

### 数据流

```
URL/文件 ─→ acquire_source() ─→ media 文件 + 标题
         ─→ extract_audio()  ─→ 16kHz 单声道 PCM wav
         ─→ engine.transcribe() → Transcript(segments, text, lang, stats)
         ─→ 写 {stem}.srt / .txt / .stats.json
```

`{stem}` 的取法：**视频标题**（从 yt-dlp 的 `--write-info-json` 里读 `title`），
拿不到才退回文件名 / URL 推导。所以 B 站视频会存成
`output\苏联卫国战争胜利纪念口粮.srt` 而不是 `output\BV1HVnn62ELQ.srt`。

中间文件都放 `output\.work\`，跑完自动删（`--keep-audio` 可保留）。

---

## 五、关键设计（改动前先看这里）

| 决策 | 原因 |
|---|---|
| 强制 16kHz / 单声道 / PCM | Whisper 和 SenseVoice 的共同要求，不转引擎会自己重采样，白花一次 CPU |
| 不用 ffprobe 取时长 | 16k 单声道 16bit 每秒固定 32000 字节，文件大小除一下就是时长，少起一个进程 |
| 子进程输出写文件不写管道 | 受限环境可能拒绝创建管道；写文件照样能增量读 |
| SenseVoice 时间戳要加段偏移 | VAD 分段识别，`seg.start` 是**采样点编号**不是秒，`绝对时间 = seg.start/16000 + token.timestamp`，漏一半整条时间轴错位 |
| 翻译按批不逐条 | 几百条字幕逐条调模型就是几百次请求；按编号批处理，条数对不上先重试、再不行整批退回原文——宁可原文，不能让时间轴错位 |
| LLM 惰性启停 | 常驻占 4 GB+ 显存；已有服务则复用不动它 |
| 任务队列串行 | SenseVoice 吃满 CPU，显存只够一个 llama-server |

---

## 六、已知的坑

### 6.1 跑 CLI 前必须设 `SHERPA_ONNX_NODE`
见 3.1。不设就是硬失败。

### 6.2 `yt-dlp` 在受限沙箱里起不来
它是 PyInstaller 单文件程序，解包要写临时目录，沙箱下报：

```
[PYI-26024:ERROR] Failed to create parent directory structure.
```

需要提权/放宽沙箱才能下载。纯本地文件输入不受影响。

### 6.3 `git push` 需要 `-c http.sslBackend=openssl`
这台机器上 schannel 后端连不上 GitHub：

```powershell
git -c http.sslBackend=openssl push origin main
```

### 6.4 `.cmd` 批处理必须是 CRLF + ASCII
用 LF 换行或中文注释，cmd 会把整个文件当一行读，报出莫名其妙的
`'p0。"' 不是内部或外部命令`。`web\run.cmd` 已经是 CRLF + 纯 ASCII，改的时候别破坏。

### 6.5 Web UI 下输出名和 CLI 不一致
`web/jobs.py` 的 `_build_cmd()` 给 `transcribe.py` 传了 `-n p["name"]`，而 `name` 是
`serve.py` 的 `_derive_name(URL)` 从链接里抠出来的（BV 号之类）。**所以走 UI 出来的目录
叫 `BV1SRnJ6fEzE`，不是视频标题**——标题命名只在 CLI 路径生效。

要修的话：让 `serve.py` 不传 `-n`，或者把推导逻辑改成"转完之后再用标题重命名"。

### 6.6 网络不稳
GitHub、B 站、YouTube 都会间歇性超时（`Failed to connect ... after 21s`、
`Recv failure: Connection was reset`），重试即可。YouTube 需要代理：

```powershell
yt-dlp --no-check-certificates --proxy http://127.0.0.1:7890 <url>
```

机器上 Clash 监听 `127.0.0.1:7890`（系统 ProxyEnable 是关的）。

### 6.7 取消任务时可能杀不干净
`jobs.py` 的 `_kill_tree()` 先 `taskkill /T /F`，被拒（低完整性沙箱会 Access denied）
再退回 `proc.kill()`。后者杀不掉孙进程，node bridge 和 ffmpeg 可能变孤儿。

---

## 七、已知限制（7B 的能力边界）

这些是花了时间试出来的，**不要再重复试**。详细记录在
[`docs/summary-experiments.md`](summary-experiments.md)。

**有效的提示词改动**（别删）：
- 通用角色，不写死领域
- 「原文点了名的东西要点名，不要用"各种""一些"笼统带过」
- 「不要替原文做统计：原文没数过就不许写"共 N 种"」——不加这句会凭空编数字
- 双人对话必须写明谁说的
- 「每条存疑附上对应原文」

**试过、更差的**：

| 做法 | 结果 |
|---|---|
| 温度 0.1 | 说话人归属 1/3 正确（两次把双方立场搞反），存疑检测 3/3 丢 |
| 分段摘要（先笔记再合并） | 细节在两次压缩里掉光，存疑从 3 处变「无」 |
| 存疑检测拆成独立调用 | 分段→全是同音瞎猜；整篇→陷入重复循环直到截断 |

**结论**：7B 只有在「先读懂全文、顺手校对」的模式下才偶尔可靠。
存疑检测当参考，别当保证——实测抓得到「三支箭→三支件」这种同音错字，
但「朗母→兰博」「双泰发黎→某广告产品名」这类会漏掉。

其他限制：
- 断句会切在词中间（长句一端悬空）
- SenseVoice 只支持中/粤/英/日/韩
- Whisper 的 encoder 跑在 CPU 上，长音频慢
- 短素材（<1 分钟）容易过度解读，会补原文没有的推断

---

## 八、待办

按优先级：

1. **`SHERPA_ONNX_NODE` 落地**：设为用户级环境变量，或写进项目 README 的快速开始——现在是最大的上手障碍。
2. **统一 UI 和 CLI 的输出命名**：见 6.5，让 UI 也走标题命名。
3. **删除任务功能**：UI 现在只能取消，不能从列表里删。
4. **拖拽上传**：现在只能贴链接或手填路径。
5. **llama-server 常驻开关**：批量处理时反复启停模型，加载成本浪费。
6. **llama-server 一键准备**：现在要用户自己装 llama.cpp 和模型。

---

## 九、需要知道的环境细节

| 项 | 值 |
|---|---|
| Python | 3.14.7（`C:\Program Files\Python314`） |
| Node | v24.21.0 |
| SenseVoice 模型 | `C:\Users\13414\.dsh\speech-to-text\sensevoice\models`（默认探测路径，239 MB） |
| sherpa-onnx-node | `C:\Users\13414\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\node_modules\sherpa-onnx-node` |
| Whisper 模型 | `D:\workspace\tools\whisper\models\ggml-large-v3-turbo.bin`（环境变量 `WHISPER_MODEL` 已设） |
| llama-server | `D:\toolchains\llama.cpp\llama-server.exe`（Vulkan 构建，`-ngl 24 --ctx 32768`） |
| LLM 模型 | `D:\workspace\tools\llm\models\qwen2.5-7b-instruct-q4_k_m-0000{1,2}-of-00002.gguf` |
| ffmpeg | `D:\toolchains\ffmpeg\bin\ffmpeg.EXE` |
| yt-dlp | `D:\toolchains\yt-dlp\yt-dlp.EXE` |
| 硬件 | i5-12600KF · 32 GB · RX 6750 GRE 10 GB（只给 LLM 用） |

实测速度（CPU 推理）：SenseVoice 约 3.5~4.5× 实时（长音频），Whisper large-v3-turbo 约 1.9×。
