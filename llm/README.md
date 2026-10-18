# llm/

本地大模型能力：翻译、摘要、问答。跑在自己的机器上，**不调任何云端 API**。

## 三个模块

| 文件 | 职责 |
|---|---|
| [`client.py`](client.py) | llama-server 的 HTTP 客户端（只用标准库） |
| [`server.py`](server.py) | llama-server 的惰性启停管理 |
| [`tasks.py`](tasks.py) | 三个任务的具体实现：翻译 / 摘要 / 问答 |

## 为什么用 llama.cpp 而不是别的

目标机器是 **AMD 显卡 + Windows**。可选路线里：

| 方案 | AMD 支持 | 门槛 |
|---|---|---|
| **llama.cpp（Vulkan 构建）** | ✅ 通用 | **解压即用**，官方提供预编译 zip |
| llama.cpp（ROCm 构建） | ✅ 原生 | 要装 ROCm 运行时，折腾 |
| llama-cpp-python | 需自行编译 | Python 3.14 没有预编译轮子 |
| vLLM | ❌ 主要 NVIDIA | — |

官方 releases 里的 `llama-*-bin-win-vulkan-x64.zip` 解压后直接可用，
启动日志会打印 `Vulkan0: AMD Radeon RX 6750 GRE 10GB (10224 MiB)`。

## 惰性启停（server.py）

```python
with LlamaServer(exe=..., model=..., ngl=24) as server:
    client = ChatClient(port=8080)
    ...
```

行为：

```
启动前探测 127.0.0.1:8080
├─ 已有服务 → 直接用，不接管（退出时也不动它）
└─ 没有服务 → 自己拉起，轮询 /health 等就绪
      └─ 退出时：只关自己拉起的那个
```

**为什么要这样**：

- **偶尔翻一次**：跑完自动释放 4 GB+ 显存，不影响你打游戏
- **批量翻 20 个**：第一次拉起后，后面共用同一个实例，不会加载 20 次模型
- 你已经手动开着服务时，脚本不会把它抢走或关掉

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `port` | 8080 | 监听端口 |
| `ngl` | 24 | 卸载到 GPU 的层数，**留余量用 24**（全上 GPU 用 99） |
| `ctx` | 8192 | 上下文长度 |
| `ready_timeout` | 180 秒 | 等待就绪的上限 |

## 客户端（client.py）

只用 `urllib.request` 调 `POST /v1/chat/completions`，非流式。

```python
client = ChatClient(port=8080)
client.ask("用一句话概括这段内容", system="你是内容分析助手")
```

**为什么不用 `openai` 库**：

- 只连本地一个端点，格式固定，不需要那层"抹平厂商差异"的抽象
- 保持整个项目零 pip 依赖
- 流式输出我们用不上——翻译是批处理，按批打印进度就够

将来如果要做逐字流式、或同时接云端 API，只需要改 `client.chat()` 一个函数。

## 任务（tasks.py）

### 翻译：按批发送 + 编号对号

一个字幕文件动辄几百条，逐条调模型意味着几百次请求。所以：

```
按「每批 20 条 / 1200 字」切批
  → 拼成 "1. 文本\n2. 文本\n..." 发给模型
  → 要求按编号逐条返回
  → 按编号对回原来的时间轴
```

**条数对不上怎么办**：先重试一次（加严提示"必须输出 N 条"），
仍然不行就**整批退回原文**。

> 宁可保留原文，也不让时间轴错位——错位的字幕比没翻译的字幕更糟。

### 摘要：三段式输出

```
一句话结论
→ 不超过 8 个要点
→ 文中提到但没说清的问题
```

最后一段是刻意的：它把"模型的不确定"显式暴露出来，而不是让模型硬编。

### 问答：只依据给定文本

system prompt 里写死"若文中没有依据，直接说「文中没有提到」"，防止 7B 模型自由发挥。

## 实测效果

24 分钟中文视频转写（7150 字）→ 摘要：

> 史蒂芬·麦卡洛因精心策划的谋杀案被判处终身监禁，但其假直播和精心设计的不在场证明使得案件扑朔迷离。
>
> 1. 史蒂芬在2022年世界杯决赛夜直播长达6小时，但警方怀疑他就是杀害女友娜塔丽的凶手
> 2. 监控显示案发当晚有一名男子从史蒂芬住所附近前往娜塔丽家
> 3. 史蒂芬的电脑记录显示直播期间没有实际操作，暗示直播是提前录制好的

人名、案情逻辑、"电脑记录显示直播期间没有实操"这类细节都抓到了。

60 秒中文视频 → 英文翻译（节选）：

| 原文 | 译文 |
|---|---|
| 欢迎大家回到老厉害的YouTube频道 | Hello everyone, welcome back to the old and awesome YouTube channel |
| 小伙伴们的投稿 | submissions from our friends |
| 高盛的宏观研报 | a macro research report from Goldman Sachs |
| 清华五道口的首席经济学家论坛 | the Chief Economist Forum at Tsinghua University |

## 脚本用法

```bash
# 翻译（输出 xxx.zh.srt，保留时间轴）
python translate.py output/talk.srt

# 只翻前 12 条（调试用）
python translate.py output/talk.srt --limit 12

# 摘要
python translate.py output/talk.txt --task summary

# 问答
python translate.py output/talk.txt --task ask -q "作者的核心论点是什么"

# 服务已经开着了，别自己启
python translate.py output/talk.srt --no-start

# 跑完别关服务（批量处理时用）
python translate.py output/talk.srt --keep-server
```
