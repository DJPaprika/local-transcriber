# local-transcriber

> 一条命令，把在线视频或本地媒体变成字幕；可选本地大模型做翻译、摘要、问答。
> **全程离线**——音频不出本机。

```bash
python transcribe.py "https://www.bilibili.com/video/BVxxxx"
```

产出：

```
output/transcript.srt          带时间轴的字幕
output/transcript.txt          纯文本
output/transcript.stats.json   本次运行的完整耗时
```

---

## 它解决什么问题

做本地转写，现成的方案通常两种：在线服务（按分钟计费、音频要上传），或者手动串三四条命令（`yt-dlp` → `ffmpeg` → 引擎 → 整理输出）。

这个项目把后者固化下来，并且**把"慢在哪"测清楚**——因为本地转写的瓶颈并不在任何文档里写着。

跑完之后还能接本地 LLM，把非中文内容翻成中文，或者直接问它"这视频讲了什么"。

---

## 特性

| 特性 | 说明 |
|---|---|
| **双引擎** | SenseVoice（快，中文最优）/ Whisper（多语种，时间戳更精确） |
| **自动取流** | 给 URL 就自己下载，给本地文件就直接用 |
| **统一结构** | 两个引擎产出同一种 `Transcript`，上层不用区分 |
| **本地 LLM** | 翻译 / 摘要 / 问答，llama.cpp + Vulkan，AMD 显卡可用 |
| **惰性启停** | LLM 按需拉起、用完释放显存；已有服务则直接复用，也可设为常驻 |
| **Web UI** | 零依赖的浏览器界面：拖拽上传、实时进度、任务管理、一键摘要/翻译 |
| **耗时拆分** | 把模型加载这种一次性成本单独拆出来，避免短样本测出误导性数字 |
| **零 pip 依赖** | 引擎编排与 LLM 调用全部只用 Python 标准库 |

---

## 快速开始

### 依赖总览

| 组件 | 用途 | 必需 |
|---|---|---|
| Python 3.9+ | 主程序（只用标准库） | ✅ |
| [ffmpeg](https://ffmpeg.org/) | 转码成 16kHz 单声道 PCM | ✅ |
| whisper.cpp 的 `whisper-cli` + ggml 模型 | Whisper 引擎 | 二选一 |
| [sherpa-onnx-node](https://github.com/k2-fsa/sherpa-onnx) + SenseVoice 模型 | SenseVoice 引擎 | 二选一 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 下载在线视频 | 输入是 URL 时 |
| llama.cpp（Vulkan 构建）+ Qwen GGUF | 翻译 / 摘要 / 问答 | 可选 |

### 最小可用（只要转写）

```bash
# 1. 装 ffmpeg 并确保在 PATH 里
# 2. 准备 SenseVoice 模型（目录结构见下）
# 3. 安装 sherpa-onnx-node（装在项目里或全局都行，程序会自动找）
npm install sherpa-onnx-node

# 4. 检查依赖是否都找得到
python tools/setup-sherpa.py

# 5. 跑
python transcribe.py video.mp4
```

`sherpa-onnx-node` 的探测顺序是：环境变量 `SHERPA_ONNX_NODE` → 项目内
`node_modules` → 全局 npm 根 → DSH 的依赖树。想把它固化成用户级环境变量：

```bash
python tools/setup-sherpa.py --persist
```

SenseVoice 模型目录要求：

```
models/
├── sensevoice-onnx/
│   ├── model.int8.onnx
│   └── tokens.txt
└── silero/
    └── silero_vad.onnx
```

用环境变量指定位置：

```bash
set SENSEVOICE_MODEL_DIR=D:\path\to\models      # Windows cmd
$env:SENSEVOICE_MODEL_DIR="D:\path\to\models"   # PowerShell
export SENSEVOICE_MODEL_DIR=/path/to/models     # bash
```

### 换成 Whisper 引擎

```bash
python transcribe.py video.mp4 -e whisper -m models/ggml-large-v3-turbo.bin
```

Whisper 支持的语种远多于 SenseVoice，且时间戳更精确，代价是慢。

### 启用 LLM（翻译 / 摘要 / 问答）

```bash
# 先转写（假设是英文素材）
python transcribe.py talk.mp4 -l en

# 翻成中文，输出 talk.zh.srt（保留时间轴）
python translate.py output/talk.srt

# 摘要
python translate.py output/talk.txt --task summary

# 针对内容提问
python translate.py output/talk.txt --task ask -q "作者的核心论点是什么"
```

llama.cpp 从[官方 releases](https://github.com/ggml-org/llama.cpp/releases) 下载 `llama-*-bin-win-vulkan-x64.zip`
（Vulkan 版对 AMD/NVIDIA/Intel 通用，解压即用，无需装 ROCm）。

懒得手动找版本、下模型的话：

```bash
python tools/prepare-llm.py            # 先检查缺什么
python tools/prepare-llm.py --download # 缺什么下什么（llama.cpp + Qwen2.5-7B q4_k_m）
```

国内网络可以加 `--proxy http://127.0.0.1:7890`。

---

## Web UI

不想敲命令的话，有一个浏览器界面，**零新增依赖**（标准库 `http.server`）：

```bash
python web/serve.py
```

它会从 8081 开始找一个空闲端口，然后自动打开浏览器。页面上可以：

- 贴视频链接、填本机路径，或直接把音视频文件拖进页面（自动上传并排队）
- 实时看进度条和完整日志
- 完成后直接下载 `.srt` / `.txt`，或一键「生成摘要」「翻译成中文」
- 任务可以取消、也可以从列表删除（只删记录，不删产物）
- 本地模型服务可以设成常驻，批量跑摘要/翻译时不用反复加载模型

产物命名和 CLI 完全一致：走 UI 也是「视频标题」，直接落在 `output/` 下。

任务是**串行**的（SenseVoice 会吃满 CPU，显存也只够一个 llama-server），
其余排队显示「排队中」。

底层调用的还是 `transcribe.py` / `translate.py`，所以 CLI 能跑通的 UI 一定能跑通。
详见 [`web/README.md`](web/README.md)，方案取舍见 [`docs/webui-plan.md`](docs/webui-plan.md)。

---

## 架构

```
                    ┌───────────────────────────┐
   输入（URL / 文件）│  transcribe.py            │
                    │  ├ 取流（yt-dlp，可选）    │
                    │  ├ 转码（ffmpeg → 16k）    │
                    │  └ 调用引擎                │
                    └─────────────┬─────────────┘
                                  │
                 ┌────────────────┴─────────────────┐
         ┌───────▼────────┐                ┌────────▼────────┐
         │ WhisperEngine  │                │ SenseVoice      │
         │ 调 whisper-cli │                │ Engine          │
         │ 解析 SRT       │                │ 调 Node 桥      │
         └───────┬────────┘                └────────┬────────┘
                 │                          ┌───────▼────────┐
                 │                          │ sv-bridge.js   │
                 │                          │ sherpa-onnx    │
                 │                          │ 吐字符级时间戳  │
                 │                          └────────────────┘
         ┌───────▼──────────────────────────────┐
         │ Transcript（统一结构）                │
         │   segments[] / text / lang / stats   │
         └───────┬──────────────────────────────┘
                 │
      ┌──────────▼─────────┐      ┌──────────────────────────┐
      │ SRT / TXT 输出     │      │ translate.py → 本地 LLM  │
      └────────────────────┘      │ 翻译 / 摘要 / 问答        │
                                  └──────────────────────────┘
```

| 目录 | 放什么 | 详细说明 |
|---|---|---|
| `engines/` | 两个引擎的实现与统一接口 | [engines/README.md](engines/README.md) |
| `bridge/` | SenseVoice 的 Node 桥 | [bridge/README.md](bridge/README.md) |
| `llm/` | 本地 LLM 客户端、启停管理、任务 | [llm/README.md](llm/README.md) |

---

## 实测性能

测试环境：Intel i5-12600KF（6 P-core + 4 E-core）· 32 GB · Windows 11 ·
AMD Radeon RX 6750 GRE 10GB（**仅 LLM 使用**）·
`SenseVoice-small int8` / `whisper large-v3-turbo`，均为 **CPU 推理**。

### SenseVoice

| 音频 | 耗时 | 速度 |
|---|---|---|
| 1:00 | 1.8 s | **19.9× 实时** |
| 5:00 | 49.9 s | 6.0× |
| 24:20 | 370.7 s | 3.9× |
| 23:14 | 304.6 s | 4.6× |
| 31:16 | 373.6 s | 5.0× |

### Whisper（large-v3-turbo，12 线程）

| 音频 | 耗时 | 速度 |
|---|---|---|
| 1:00 | 33.8 s | 1.8× |
| 5:00 | 142.1 s | 2.1× |
| 31:16 | 1001 s | 1.9× |

### 从数据里读出的三件事

**① Whisper 的成本压在编码器上（73~77%）**

```
encode  104.06 s (73%) │ batchd 25.81 s │ sample 7.19 s │ decode 0.10 s
```

Whisper 把音频切成 30 秒窗口，**每个窗口都要完整跑一遍 encoder**，不管里面有没有人声。

⇒ 提速的唯一大杠杆是让 encoder 上 GPU；调线程、换小模型都是小修小补。

**② SenseVoice 快，但代价落在精度上**

同一段音频，两个引擎对同一处的判断：

| 原文 | Whisper | SenseVoice |
|---|---|---|
| 研报 | 宏观**言报** ❌ | 宏观**研报** ✅ |
| 闪辉（人名） | **闪辉** ✅ | **闪回** ❌ |

这不是"谁更好"——SenseVoice 用约 1/3.5 的参数量 + int8 量化换来 3~10 倍速度，代价就是细节会掉。

**③ 短样本的倍率会骗人**

模型加载是一次性成本：冷启动（首次读盘）约 80 秒，热启动约 1 秒。
拿 1 分钟音频去测，这个成本能占到总耗时 70%，测出来的"速度"毫无参考价值。

`stats.json` 同时记录 `realtime_factor_total`（含加载）与 `realtime_factor_net`（净转写），就是防这个陷阱。

---

## 设计取舍

**为什么强制 16kHz / 单声道 / 16-bit PCM**

这是 Whisper 与 SenseVoice 共同要求的输入规格。不转的话引擎会自己重采样一遍，白白多花一次 CPU，而且要先解码整段音频。

**为什么不用 ffprobe 取时长**

转成 16kHz 单声道 16-bit PCM 之后，每秒固定 `16000 × 2 = 32000` 字节，`文件大小 ÷ 32000` 就是时长，误差在采样精度内。少起一个进程。

**为什么子进程输出写文件而不是管道**

受限环境（带沙箱的终端）可能拒绝创建管道。写文件同样能拿到完整输出，再由主进程轮询增量转发到终端——既能看到实时进度，又能事后解析耗时。

**为什么 SenseVoice 的时间戳要加段偏移**

它的 VAD 会先把音频切成若干语音段，每段单独识别，时间戳是**段内相对**的。而 `seg.start` 给的是**采样点编号**，不是秒。所以：

```python
绝对时间 = seg.start / 16000 + token.timestamp
```

漏掉任何一半，整条时间轴都会错位。

**为什么翻译不逐条调模型**

一个字幕文件动辄几百条，逐条翻译意味着几百次请求。所以按批发送（默认每批 20 条 / 1200 字），并要求模型**按编号逐条返回**；条数对不上时先重试一次，仍不行就整批退回原文——**宁可保留原文，也不让时间轴错位**。

**为什么 LLM 用惰性启停**

模型常驻要占 4 GB 以上显存，忘了关就一直占着。所以默认"按需拉起、用完释放"，同时保留复用：已经开着的服务不去动它，批量处理时也能共用同一个实例。

---

## 已知限制

| 限制 | 影响 | 可行方向 |
|---|---|---|
| **断句会切在词中间** | 长句被切成一端悬空 | 接入中文分词，只在词边界断 |
| **偶发噪声字符** | 出现孤立的英文字母串 | 用 token 置信度过滤 |
| **SenseVoice 只支持 5 种语言** | 中/粤/英/日/韩 之外无法识别 | 换用 Whisper 引擎 |
| **Whisper 的 encoder 仍在 CPU** | 长音频慢 | 自行编译启用 Vulkan 后端 |
| **翻译依赖 LLM 输出格式** | 偶尔少回或合并条目 | 已有重试与兜底 |
| **摘要会误判正常表达为错字** | 7B 能力边界，`temperature 0` 下稳定复现 | 示例里原样标注，人工过一眼 |
| **多人对话会串说话人** | 转写稿无说话人标记，靠上下文推断 | 提示词要求标注归属，仍偶有串位 |
| **llama-server 需自行准备** | 翻译前要先装 llama.cpp 与模型 | 已提供 `tools/prepare-llm.py --download` |

---

## 真实运行示例

[`docs/examples/`](docs/examples/) 里放了两次真实运行的完整产物：

| 素材 | 时长 | 转写耗时 | 产物 |
|---|---|---|---|
| B 站案件纪录片 | 24:20 | 370.66 s（3.94×） | 391 条字幕 / 7150 字 + 摘要 |
| YouTube 观点访谈 | 23:14 | 304.6 s（4.58×） | 410 条字幕 / 8628 字 + 摘要 |

两份摘要都带了「它哪里写错了」的标注：B 站那份附提示词新旧两版对比，
YouTube 那份标出了两处误报（把正常表达判成错字）。**包括它暴露出的问题**——
断在词中间的切句、偶发噪声字符、多人对话的说话人串位，都原样留在里面，不是挑好的结果放上去。

---

## 目录结构

```
local-transcriber/
├── transcribe.py            # 转写入口
├── translate.py             # 翻译 / 摘要 / 问答入口
├── engines/                 # 引擎实现（见各自 README）
├── bridge/sv-bridge.js      # SenseVoice 的 Node 桥
├── llm/                     # 本地大模型能力
├── web/                     # 浏览器界面（零新增依赖，见 web/README.md）
├── tools/                   # 依赖检查 / 一键准备（见 tools/README.md）
├── docs/examples/           # 真实运行的示例产物
├── output/                  # 运行产物（不入库）
└── models/                  # 模型文件（不入库，也可用环境变量指向外部目录）
```

---

## 许可

本项目代码采用 MIT 许可。

模型权重各随其原始许可：SenseVoice 遵循 FunASR Model License（官方澄清允许商用，需保留归属与模型名），
Qwen 遵循其模型卡，whisper.cpp 的 ggml 模型由 OpenAI 权重转换而来。
