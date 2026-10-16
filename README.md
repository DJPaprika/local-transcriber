# local-transcriber

> 一条命令，把在线视频或本地媒体文件，变成纯文本和 SRT 字幕。
> 全程本地运行 —— 不调用任何云端 API，音频不出本机。

```
yt-dlp 取流  →  ffmpeg 抽 16kHz 单声道 PCM  →  whisper.cpp 转写  →  输出 txt + srt
```

---

## 为什么做这个

现成的转写方案通常是两种：要么用在线服务（按分钟计费、音频要上传），要么手动敲三四条命令串工具链（每次都要记得参数）。

这个脚本解决的是后者：**把已经验证过的参数固化成一条命令，并且把耗时统计出来**——因为本地转写的瓶颈在哪，一开始并不明显。

---

## 快速开始

### 依赖

| 组件 | 用途 | 是否必需 |
|---|---|---|
| Python 3.9+ | 运行脚本（**只用标准库，不需要 pip 安装任何东西**） | 必需 |
| [whisper.cpp](https://github.com/ggerganov/whisper.cpp) 的 `whisper-cli` | 转写引擎 | 必需 |
| ggml 模型文件 | 例如 `ggml-large-v3-turbo.bin` | 必需 |
| ffmpeg | 抽取并重采样音频 | 必需 |
| yt-dlp | 下载在线视频（仅输入为 URL 时需要） | 可选 |

模型可从 whisper.cpp 仓库获取：

```bash
bash models/download-ggml-model.sh large-v3-turbo
```

### 运行

```bash
# 本地文件
python transcribe.py video.mp4 -m /path/to/ggml-large-v3-turbo.bin

# 在线视频
python transcribe.py "https://youtu.be/xxxx" -m /path/to/ggml-large-v3-turbo.bin -l zh

# 工具不在 PATH 里时显式指定
python transcribe.py video.mp4 \
    -m models/ggml-large-v3-turbo.bin \
    --whisper /path/to/whisper-cli \
    --ffmpeg  /path/to/ffmpeg
```

模型路径也可以通过环境变量给出：

```bash
export WHISPER_MODEL=/path/to/ggml-large-v3-turbo.bin
python transcribe.py video.mp4
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `-o, --outdir` | 输出目录（默认 `output/`） |
| `-n, --name` | 输出文件名前缀（默认由源文件名推导） |
| `-l, --language` | 语言代码，`auto` 为自动识别（默认 `zh`） |
| `-t, --threads` | 转写线程数（默认 CPU 核数 − 4） |
| `--prompt` | 初始提示词，用于纠正专有名词 |
| `--keep-audio` | 保留中间产出的 16k wav |
| `--verbose` | 实时显示 whisper 输出（默认写入日志文件） |

### 产物

| 文件 | 内容 |
|---|---|
| `<name>.txt` | 纯文本转写 |
| `<name>.srt` | 带时间轴的字幕 |
| `<name>.stats.json` | 本次运行的完整耗时统计（便于横向对比） |
| `<name>.whisper.log` | whisper.cpp 的原始输出 |

---

## 实测性能

测试环境：AMD Ryzen（16 逻辑核）· 32 GB 内存 · Windows 11 · `ggml-large-v3-turbo` · 12 线程 · CPU 推理（无 GPU 加速）。

| 样本 | 音频时长 | 模型加载 | 净转写 | encode 占比 | 处理速度 |
|---|---|---|---|---|---|
| 片段 A | 1:00 | 1.0 s（热缓存） | 32.7 s | **77 %** | 1.83× 实时 |
| 片段 B | 5:00 | 1.0 s（热缓存） | 141.0 s | **73 %** | 2.13× 实时 |
| 完整视频 | 31:16 | 冷启动 ~80 s | — | — | 1.87× 实时 |

> 「处理速度」= 音频时长 ÷ 转写耗时，大于 1 表示比实时更快。

### 从数据里读出的三件事

**1. 编码器（encoder）是唯一的主要瓶颈**

whisper.cpp 会打印各阶段耗时，300 秒样本的构成是：

```
encode   104.06 s   ████████████████████████████████  73%
batchd    25.81 s   ████████                          18%
sample     7.19 s   ██                                 5%
prompt     3.50 s   █                                  2%
mel        0.12 s
decode     0.10 s
```

解码（decode）几乎不花时间，**全部成本压在 encoder 上**。原因在于 Whisper 的架构：音频先被切成 30 秒窗口，每个窗口都要完整跑一遍编码器，无论音频里是否有语音。

**所以提速的正确方向只有一个：让 encoder 跑在 GPU 上。** 调线程数、换更小的模型、剪掉静音段，收益都远不如这一条。

**2. 模型加载是一次性成本，且强烈依赖系统文件缓存**

同一个模型文件：

| 场景 | 加载耗时 |
|---|---|
| 冷启动（首次读取，绕不过磁盘） | **~80 s** |
| 热启动（文件已在系统缓存中） | **~1 s** |

对 31 分钟的视频，80 秒加载可以忽略；但对 1 分钟的短音频，加载能占到总耗时的 **70 %**，会让实测速度严重失真。

**如果只转一个短视频，别用"含加载"的倍率去评价性能。** 同理，批量转写多个文件时，真正该做的是让模型常驻内存（见下方优化方向），而不是反复启动进程。

**3. 长音频的单位成本更低**

60 秒样本每音频秒耗 0.56 秒，300 秒样本降到 0.47 秒。因为窗口越多，启动阶段和首窗口的固定开销越被摊薄。

---

## 设计取舍

**为什么强制转成 16kHz / 单声道 / 16-bit PCM？**

这是 Whisper 系列模型的输入规格。如果直接喂 44.1kHz 立体声，whisper.cpp 会自己做一遍重采样——白白多花一次 CPU，而且要先解码整段音频。在 ffmpeg 这一步转好，转写阶段就是纯粹的推理。

**为什么不用 ffprobe 取音频时长？**

没必要多起一个进程。转成 16kHz 单声道 16-bit PCM 之后，每秒固定是 `16000 × 2 = 32000` 字节，`文件大小 ÷ 32000` 就是时长，误差在采样精度以内。

**为什么把子进程输出写文件而不是管道？**

早期版本用管道捕获 whisper 的输出以解析耗时，但在受限的执行环境（例如带沙箱的终端）下，创建管道可能被拒绝。写文件同样能拿到完整输出，且不依赖管道。需要实时看进度时用 `--verbose` 切回直接输出。

**为什么只用标准库？**

这是个一次跑完就退出的命令行工具，引入依赖只会让"换台机器就得先装环境"这件事变得更烦。

---

## 已知限制与优化方向

| 限制 | 说明 | 可行的解法 |
|---|---|---|
| **encoder 跑在 CPU 上** | 本机显卡是 AMD，whisper.cpp 默认构建不含其加速后端，日志里明确写着 `no GPU found` | 自行编译 whisper.cpp 并启用 **Vulkan** 后端（AMD 可用），或换 NVIDIA + CUDA |
| **模型每次重新加载** | 转一个文件加载一次，批量场景浪费严重 | 改用 whisper.cpp 自带的 **`whisper-server`**：模型常驻内存，通过 HTTP 提交任务 |
| 长音频不切分 | 目前整段送入，单文件失败就得重跑 | 按静音点切段后并行转写，最后按时间轴拼接 |
| 专有名词识别错误 | 人名、专业术语易被同音字替换 | 用 `--prompt` 提供词表；或转写后做一次术语校正 |
| 依赖外部工具 | 需要用户自行安装 ffmpeg / whisper.cpp | 后续可加启动自检，缺什么直接给出安装指引 |

---

## 目录结构

```
local-transcriber/
├── transcribe.py          # 主脚本
├── README.md
├── samples/               # 测试用片段（不入库）
└── output/                # 运行产物（不入库）
    ├── .work/             # 中间文件：下载的媒体、16k wav
    ├── <name>.txt
    ├── <name>.srt
    ├── <name>.stats.json
    └── <name>.whisper.log
```
