# bridge/

SenseVoice 的 Node 桥。

## 为什么要一个 Node 桥

SenseVoice 的推理靠 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)。
它的 Python 绑定需要 `pip install sherpa-onnx`，而 **Node 绑定（`sherpa-onnx-node`）在 Windows 上带预编译二进制，开箱即用**，不必编译、不必装 onnxruntime。

所以这一层的作用是：**用最省事的方式拿到模型输出，然后把结果交给 Python。**

## 职责边界（很重要）

| 它做 | 它不做 |
|---|---|
| 读 WAV、跑 VAD、跑识别 | 断句 |
| 把段内时间戳换算成全局秒 | 生成 SRT |
| 输出 JSON | 格式化、写文件命名策略 |

**断句放在 Python 那边**（`engines/sensevoice.py` 的 `build_segments`），
这样 Whisper 和 SenseVoice 走的是同一套字幕合并逻辑。

## 用法

```bash
node sv-bridge.js <16k.wav> <out.json> [language]
```

`language` 可取 `auto` / `zh` / `en` / `yue` / `ja` / `ko`，默认 `auto`。

## 输出协议

```json
{
  "engine": "sensevoice-small-int8-onnx",
  "lang": "zh",
  "duration": 1459.7,
  "stats": {
    "vad_segments": 161,
    "load_seconds": 1.03,
    "infer_seconds": 369.7,
    "realtime_factor": 3.95
  },
  "chars": [["大", 1.314], ["家", 1.374], ["好", 1.434]]
}
```

`chars` 是**字符级**时间戳，单位秒，**已经是全局时间**（段偏移已加回）。

## 两个容易踩的坑

**① `seg.start` 是采样点，不是秒**

```js
const segOffset = seg.start / 16000;   // ← 必须除
```

**② 时间戳是段内相对的**

VAD 把音频切成若干段，每段单独识别，时间戳都从自己段的 0 开始。所以：

```js
chars.push([token, segOffset + timestamp]);
```

漏掉任何一半，整条时间轴都会错位。

**③ VAD 的缓冲容量不控制分段长度**

`new sherpa.Vad(config, bufferSizeInSeconds)` 的第二个参数**只是缓冲区大小**，
分段长度由 `maxSpeechDuration`（30 秒）决定。

但缓冲区必须比实际最长段大——实测段长能到约 32 秒，所以给 64。给 31 会触发：

```
circular-buffer.cc: Overflow! (Original data is copied. No data loss!)
```

数据不会丢，但每次都要多做一次内存拷贝。

## 依赖定位

按这个顺序找，前面命中就停：

| 依赖 | 查找顺序 |
|---|---|
| `sherpa-onnx-node` | `SHERPA_ONNX_NODE` → `../node_modules/` → `./node_modules/` → `%APPDATA%\\npm\\node_modules\\sherpa-onnx-node` → DSH 依赖树 → 常规解析 |

`transcribe.py` 会把找到的路径写回 `SHERPA_ONNX_NODE`，所以从 Python 调过来时
通常第一步就命中；直接 `node sv-bridge.js` 时靠后面的兜底。
| 模型 | `SENSEVOICE_MODEL_DIR` → `~/.dsh/speech-to-text/sensevoice/models` → `../models/` |

模型目录结构：

```
<MODEL_DIR>/
├── sensevoice-onnx/
│   ├── model.int8.onnx
│   └── tokens.txt
└── silero/
    └── silero_vad.onnx
```

找不到时会打印**期望的目录结构**和该设哪个环境变量，而不是甩一句 `Cannot find module`。
