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

**六项待办已全部实现，改动还在工作区，没有提交、也没有推送**（这一轮约定不提交）。

已用真实链路验证：38 项自动化断言全过（上传/转写/下载/删除/取消/设置/pid/CLI 自动探测），
外加一次 YouTube URL 端到端——CLI 与 UI 产出同名同目录。

| 提交 / 改动 | 内容 |
|---|---|
| `681ab98` | 交接文档（已提交，未推送） |
| 本工作区改动 | 下表六项 + 文档同步 |

| # | 原来的问题 | 现在 |
|---|---|---|
| 1 | 跑 CLI 必须手设 `SHERPA_ONNX_NODE` | 自动探测（项目 / 全局 npm / DSH 依赖树），`tools/setup-sherpa.py` 可检查、可 `--persist` |
| 2 | UI 产物叫 `BV1SRnJ6fEzE`，CLI 叫视频标题 | 命名交给 `transcribe.py`，UI 与 CLI 同名同目录 |
| 3 | 只能取消，不能从列表删除 | `DELETE /api/jobs/{id}` + 卡片上的「删除」 |
| 4 | 只能贴链接或填路径 | 拖拽 / 点选上传，8 GB 上限，自动排队 |
| 5 | 每次摘要/翻译都重新加载模型 | `keep_server` 开关 + pid 文件 + 「停掉服务」 |
| 6 | 要自己装 llama.cpp 和模型 | `tools/prepare-llm.py --download` 一条命令 |

**能用的**：CLI 转写 / 翻译 / 摘要 / 问答，Web UI 全流程（上传、进度、下载、删除、常驻）。
**产物**：`output/` 下四组成品（案件解说、经济评论、开箱视频、40 秒片段），都带 summary。

---

## 三、怎么跑起来

### 3.1 依赖自检（最容易漏的一步）

```powershell
python tools/setup-sherpa.py
```

它检查 node、`sherpa-onnx-node`、SenseVoice 模型三样东西，并打印探测到的路径。
**探测顺序**：`SHERPA_ONNX_NODE` → 项目 `node_modules` → 全局 npm 根 →
DSH 的依赖树（`%APPDATA%\npm\node_modules\@deepseek-ai\dsh\node_modules`）。

所以正常情况下**什么都不用设**。想固化下来：

```powershell
python tools/setup-sherpa.py --persist   # setx，写用户级环境变量
```

`transcribe.py` 启动时也会自己找一次，找到就写回 `SHERPA_ONNX_NODE`；
`bridge/sv-bridge.js` 里有同样的候选列表，直接跑 Node 桥时也能命中。

> `web/run.cmd` 里**不再硬编码**任何绝对路径，只负责 `cd` 到项目根再起服务。

### 3.2 常用命令

```powershell
cd D:\workspace\local-transcriber

# 转写（URL 或本地文件都可以）
python transcribe.py "https://www.bilibili.com/video/BVxxxx"
python transcribe.py "D:\path\to\video.mp4" -o output -n 自定义名字

# 摘要 / 翻译 / 问答（默认输出在源文件旁边）
python translate.py output\xxx.txt --task summary
python translate.py output\xxx.srt                  # → xxx.zh.srt
python translate.py output\xxx.txt --task ask -q "作者的核心观点是什么"

# 依赖准备
python tools/setup-sherpa.py            # SenseVoice 三件套
python tools/prepare-llm.py             # llama.cpp + GGUF 检查
python tools/prepare-llm.py --download  # 缺什么下什么（~4.5 GB）
```

### 3.3 起 Web UI

双击 `web\run.cmd`，或：

```powershell
python web\serve.py          # 从 8081 开始找空闲端口，自动开浏览器
python web\serve.py --no-browser
```

页面上可以：拖文件或贴链接 → 看实时进度和日志 → 下载 srt/txt → 一键摘要/翻译；
任务可取消、可删除；「本地模型服务」面板能设常驻、能停服务。任务是串行的。

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
  server.py            llama-server 惰性启停 + pid 文件
  tasks.py             三套提示词 + 翻译/摘要/问答实现
web/
  serve.py             http.server 路由 + 静态文件 + 上传/设置/LLM 接口
  jobs.py              串行任务队列、产物定位、删除
  static/              原生 JS 单页（拖拽上传、删除、常驻开关）
  run.cmd              一键启动
tools/
  setup-sherpa.py      SenseVoice 依赖检查 / 持久化环境变量
  prepare-llm.py       llama.cpp + 模型检查 / 下载
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

Web UI 走的是同一条路：不填输出名时，`jobs.py` 不传 `-n`，产物直接落在
`output/` 根目录，和 CLI 完全一致。中间文件都放 `output\.work\`，跑完自动删
（`--keep-audio` 可保留）。

---

## 五、关键设计（改动前先看这里）

| 决策 | 原因 |
|---|---|
| 强制 16kHz / 单声道 / PCM | Whisper 和 SenseVoice 的共同要求，不转引擎会自己重采样，白花一次 CPU |
| 不用 ffprobe 取时长 | 16k 单声道 16bit 每秒固定 32000 字节，文件大小除一下就是时长，少起一个进程 |
| 子进程输出写文件不写管道 | 受限环境可能拒绝创建管道；写文件照样能增量读 |
| SenseVoice 时间戳要加段偏移 | VAD 分段识别，`seg.start` 是**采样点编号**不是秒，`绝对时间 = seg.start/16000 + token.timestamp`，漏一半整条时间轴错位 |
| 翻译按批不逐条 | 几百条字幕逐条调模型就是几百次请求；按编号批处理，条数对不上先重试、再不行整批退回原文——宁可原文，不能让时间轴错位 |
| LLM 惰性启停 | 常驻占 4 GB+ 显存；已有服务则复用不动它。需要批量处理时用 Web 的常驻开关 |
| 任务队列串行 | SenseVoice 吃满 CPU，显存只够一个 llama-server |
| Web 不推导输出名 | 名字由 `transcribe.py` 决定，UI 和 CLI 才不会各叫各的 |
| 上传发原始字节，不用 multipart | 标准库就能写，不需要 `cgi`（Python 3.13 已移除）；边读边写盘，不把大文件整个读进内存 |
| 下载用 RFC 5987 文件名 | HTTP 头只能 latin-1，中文标题必须 `filename*=UTF-8''...`，否则直接 500 |

---

## 六、已知的坑

### 6.1 `SHERPA_ONNX_NODE` 已经不是硬要求
自动探测覆盖了项目内、全局 npm、DSH 依赖树三种情况。只有装在奇怪位置时才需要
`--persist` 或手动设环境变量。

### 6.2 `yt-dlp` 在受限沙箱里起不来
它是 PyInstaller 单文件程序，解包要写临时目录，沙箱下报：

```
[PYI-26024:ERROR] Failed to create parent directory structure.
```

需要提权/放宽沙箱才能下载。纯本地文件输入不受影响。

2026-10-20 用 DSH 自带的 `windows-acl` runner 在 `workspace-write` 下复测，同样的报错
原样复现，不是版本问题。同一次测试里 `node`、`ffmpeg`、`python` 都能在受限模式下
正常启动——只有「自解包到 temp」这一类程序不行。

### 6.3 `git push` 需要 `-c http.sslBackend=openssl`
这台机器上 schannel 后端连不上 GitHub：

```powershell
git -c http.sslBackend=openssl push origin main
```

### 6.4 `.cmd` 批处理必须是 CRLF + ASCII
用 LF 换行或中文注释，cmd 会把整个文件当一行读，报出莫名其妙的
`'p0。"' 不是内部或外部命令`。`web\run.cmd` 已经是 CRLF + 纯 ASCII，改的时候别破坏。

### 6.5 Web UI 下输出名和 CLI 不一致 —— 已修复
现在 `serve.py` 不再推导名字，`jobs.py` 也不传 `-n`；产物由 `transcribe.py`
按标题命名，直接落在 `output/` 根目录。摘要/翻译也改用 `translate.py` 的默认命名，
和源文件同目录同前缀，不会再出现 `xxx.zh.zh.srt` 这种重复后缀。

### 6.6 网络不稳
GitHub、B 站、YouTube 都会间歇性超时（`Failed to connect ... after 21s`、
`Recv failure: Connection was reset`），重试即可。YouTube 需要代理：

```powershell
yt-dlp --no-check-certificates --proxy http://127.0.0.1:7890 <url>
```

机器上 Clash 监听 `127.0.0.1:7890`（系统 ProxyEnable 是关的）。
`tools/prepare-llm.py` 下载时用 `--proxy http://127.0.0.1:7890`。

**B 站不登录也能下**（2026-10-20 实测）：普通视频最高到 1080p，4K / 1080P60 才需要
大会员 cookies。偶尔会报 `No video formats found!`，那是风控瞬时拒绝，
**重试通常就好**——同一个 BV 号第一次失败、第二次列出全部格式，完整跑通
8 分 37 秒视频：下载 3 s + 转写 114 s（4.53× 实时）。

**YouTube 走代理要设环境变量**，因为 `transcribe.py` 调 yt-dlp 时不传 `--proxy`：

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
python transcribe.py "https://www.youtube.com/watch?v=jNQXAC9IVRw"
```

实测 19 秒视频：下载 7.8 s + 转写 1.5 s → `output\Me_at_the_zoo.srt`
（标题 `Me at the zoo`，空格被 safe_stem 换成下划线）。

### 6.7 取消任务时可能杀不干净
`jobs.py` 的 `_kill_tree()` 先 `taskkill /T /F`，被拒（低完整性沙箱会 Access denied）
再退回 `proc.kill()`。后者杀不掉孙进程，node bridge 和 ffmpeg 可能变孤儿。

Web 的「停掉服务」同理：`_kill_pid()` 先 `taskkill`、再 `os.kill`，两者都失败会返回
`ok: false` 并在页面上弹出来，不会假装已经停掉。

2026-10-20 实测（`workspace-write`）：`taskkill /T /F` 返回码 1，`os.kill(pid, SIGTERM)`
抛 `WinError 5`，子进程仍然活着。同一模式下的管道也一样：`os.pipe()`、
`subprocess(..., capture_output=True)`、`Popen(stdout=PIPE)` 全部 `WinError 5`。
完整对照见 6.10。

### 6.8 中文文件名曾让下载 500（已修复，但要知道原因）
`Content-Disposition` 只能用 latin-1。输出名变成视频标题后，直接塞中文会抛
`UnicodeEncodeError`。现在的写法是 ASCII 兜底名 + `filename*=UTF-8''<percent-encoded>`，
新增下载接口时别退回去。

### 6.9 上传是同步读取，没有断点续传
`POST /api/upload` 边读 `Content-Length` 边写盘，超过 8 GB 直接 413。
传到一半断开，服务端会把半截文件删掉，但前端要重新传。
文件落在 `output\.uploads\<时间戳>-<随机>\`，不会自动清理。

### 6.10 沙箱实测（2026-10-20）

用 DSH 自带的 `dsh-sandbox-windows-acl` runner 手动拉起受限进程，跑同一套探针，
三组对照。`全权限` 的进程完整性是 High，两种受限模式都是 Low。

| 能力 | 全权限 | workspace-write | read-only |
|---|---|---|---|
| 写 workspace | ✅ | ✅ | ❌ |
| 写 workspace 外 | ✅ | ❌ | ❌ |
| 写私有 temp | ✅ | ✅ | ❌ |
| 写 `C:\Windows\Temp` | ✅ | ❌ | ❌ |
| 读系统文件 | ✅ | ✅ | ✅ |
| `os.pipe` / `subprocess(capture_output)` | ✅ | ❌ `WinError 5` | ✅ |
| 命名管道 listener | ✅ | ✅ | ✅ |
| 外网 TCP | ✅ | ✅ | ✅ |
| `taskkill /T /F` | ✅ rc=0 | ❌ rc=1 | ❌ rc=1 |
| `os.kill(SIGTERM)` | ✅ | ❌ `WinError 5` | ❌ |
| 删除 workspace 内 | ✅ | ✅ | ❌ |
| 符号链接 | ✅ | ❌ `WinError 1314` | ❌ |
| 注册表 HKCU 写 | ✅ | ❌ `WinError 5` | ❌ |
| `yt-dlp`（PyInstaller） | ✅ | ❌ `PYI: Failed to create parent directory` | 未测 |

全权限下唯一失败的是 **>260 字符长路径**，那是 Windows `MAX_PATH`，与沙箱无关。

结论：沙箱只拦写/删，不拦读、网络、进程可见性。这解释了项目里三处看起来「过于保守」的写法——
子进程输出写文件不写管道、取消要 `taskkill` + `proc.kill` 双保险、停 LLM 服务可能返回失败——
它们都是被这个边界逼出来的。

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
- 问答（`ask`）的结果只写进任务日志，UI 里没有单独的结果卡片

---

## 八、待办

这一轮的六项已经做完。往后可以做的，按价值排：

1. **上传断点续传 / 上传取消**：现在大文件传一半断了只能重传
2. **删除任务时可选删产物**：目前只删记录，字幕留在 `output/`
3. **问答结果进 UI**：`ask` 现在只把答案打在日志里
4. `.uploads` 定期清理：上传的原文件会一直堆着
5. **llama-server 日志落盘**：现在是 `DEVNULL`，启动失败时只有「立刻退出」一句
6. **llama-server 常驻的持久化**：服务重启后 pid 文件还在，但进程可能已经不在；
   状态探测按端口判断，这点已经兼容

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
