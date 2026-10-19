/**
 * SenseVoice 桥：WAV → 字符级时间戳 JSON
 *
 * 用法：node sv-bridge.js <16k.wav> <out.json> [language]
 *
 * 输出结构（JSON）：
 *   {
 *     engine, lang, duration,
 *     stats: { vad_segments, load_seconds, infer_seconds },
 *     chars: [["大", 1.314], ["家", 1.374], ...]
 *   }
 *
 * 注意：chars 的时间是「全局秒」——已经把 VAD 段的起点偏移加回去了。
 * 断句合并成字幕这件事交给 Python，桥只负责吐原始数据。
 */

const fs = require("fs");
const os = require("os");
const path = require("path");

// ---------------------------------------------------------------- 依赖定位
// 探测顺序：环境变量 → 项目本地 → 全局 npm 根 / DSH 依赖树 → 常规解析

function knownInstallLocations() {
  const out = [];
  if (process.env.APPDATA) {
    const npmRoot = path.join(process.env.APPDATA, "npm", "node_modules");
    // DSH 把 sherpa-onnx-node 装在自己的依赖树里
    out.push(path.join(npmRoot, "@deepseek-ai", "dsh", "node_modules", "sherpa-onnx-node"));
    out.push(path.join(npmRoot, "sherpa-onnx-node"));
  }
  if (process.env.DSH_HOME) {
    out.push(path.join(process.env.DSH_HOME, "node_modules", "sherpa-onnx-node"));
  }
  return out;
}

function resolveSherpa() {
  const candidates = [
    process.env.SHERPA_ONNX_NODE,
    path.join(__dirname, "..", "node_modules", "sherpa-onnx-node"),
    path.join(__dirname, "node_modules", "sherpa-onnx-node"),
    ...knownInstallLocations(),
    "sherpa-onnx-node",
  ].filter(Boolean);

  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch {
      /* 换下一个 */
    }
  }
  console.error(
    "[x] 找不到 sherpa-onnx-node。\n" +
      "    请执行 npm i sherpa-onnx-node，\n" +
      "    或用环境变量 SHERPA_ONNX_NODE 指定它的目录。\n" +
      "    也可以跑 python tools/setup-sherpa.py 看看都探测了哪些位置。"
  );
  process.exit(1);
}

function resolveModelFiles() {
  const candidates = [
    process.env.SENSEVOICE_MODEL_DIR,
    path.join(os.homedir(), ".dsh", "speech-to-text", "sensevoice", "models"),
    path.join(__dirname, "..", "models"),
  ].filter(Boolean);

  for (const dir of candidates) {
    const files = {
      onnx: path.join(dir, "sensevoice-onnx", "model.int8.onnx"),
      tokens: path.join(dir, "sensevoice-onnx", "tokens.txt"),
      vad: path.join(dir, "silero", "silero_vad.onnx"),
    };
    if (fs.existsSync(files.onnx) && fs.existsSync(files.tokens) && fs.existsSync(files.vad)) {
      return files;
    }
  }
  console.error(
    "[x] 找不到 SenseVoice 模型文件。期望结构：\n" +
      "      <MODEL_DIR>/sensevoice-onnx/model.int8.onnx\n" +
      "      <MODEL_DIR>/sensevoice-onnx/tokens.txt\n" +
      "      <MODEL_DIR>/silero/silero_vad.onnx\n" +
      "    用环境变量 SENSEVOICE_MODEL_DIR 指定。"
  );
  process.exit(1);
}

const sherpa = resolveSherpa();
const modelFiles = resolveModelFiles();

// ---------------------------------------------------------------- 入参

const wavPath = process.argv[2];
const outPath = process.argv[3];
const language = process.argv[4] || "auto";

if (!wavPath || !outPath) {
  console.error("用法: node sv-bridge.js <16k.wav> <out.json> [language]");
  process.exit(1);
}

// ---------------------------------------------------------------- 读 WAV

const buf = fs.readFileSync(wavPath);
const pcm = new DataView(buf.buffer, buf.byteOffset + 44, buf.byteLength - 44);
const samples = Float32Array.from(
  { length: pcm.byteLength / 2 },
  (_, i) => pcm.getInt16(i * 2, true) / 32768
);
const duration = samples.length / 16000;

// ---------------------------------------------------------------- 模型

const tLoad = performance.now();
const recognizer = new sherpa.OfflineRecognizer({
  featConfig: { sampleRate: 16000, featureDim: 80 },
  modelConfig: {
    senseVoice: {
      model: modelFiles.onnx,
      language: language,
      useInverseTextNormalization: 1,
    },
    tokens: modelFiles.tokens,
    numThreads: 8,
    provider: "cpu",
    debug: 0,
  },
});
const loadSec = (performance.now() - tLoad) / 1000;

const detector = new sherpa.Vad(
  {
    sileroVad: {
      model: modelFiles.vad,
      threshold: 0.5,
      minSilenceDuration: 0.5,
      minSpeechDuration: 0.25,
      maxSpeechDuration: 30,
      windowSize: 512,
    },
    sampleRate: 16000,
    numThreads: 2,
    provider: "cpu",
    debug: 0,
  },
  // 缓冲容量（秒）：不控制分段长度，只需大于实际最长段（约 32 秒）
  64
);

// ---------------------------------------------------------------- 推理

const tInfer = performance.now();
const chars = [];
let segCount = 0;
let detectedLang = "";

const drain = () => {
  while (!detector.isEmpty()) {
    const seg = detector.front(false);
    const segOffset = seg.start / 16000; // 采样点 → 秒，关键一步
    const stream = recognizer.createStream();
    stream.acceptWaveform({ sampleRate: 16000, samples: seg.samples });
    recognizer.decode(stream);
    const r = recognizer.getResult(stream);
    segCount++;
    if (!detectedLang && r.lang) detectedLang = r.lang.replace(/[<|>]/g, "");

    const tokens = r.tokens || [];
    const stamps = r.timestamps || [];
    for (let i = 0; i < tokens.length; i++) {
      chars.push([tokens[i], +(segOffset + (stamps[i] ?? 0)).toFixed(3)]);
    }
    detector.pop();
  }
};

for (let off = 0; off < samples.length; off += 512) {
  detector.acceptWaveform(samples.subarray(off, off + 512));
  drain();
}
detector.flush();
drain();

const inferSec = (performance.now() - tInfer) / 1000;

// ---------------------------------------------------------------- 输出

fs.writeFileSync(
  outPath,
  JSON.stringify({
    engine: "sensevoice-small-int8-onnx",
    lang: detectedLang || language,
    duration: +duration.toFixed(2),
    stats: {
      vad_segments: segCount,
      load_seconds: +loadSec.toFixed(2),
      infer_seconds: +inferSec.toFixed(2),
      realtime_factor: +(duration / inferSec).toFixed(2),
    },
    chars,
  }),
  "utf-8"
);

console.log(
  `[bridge] ${chars.length} 字 / ${segCount} 段 / 推理 ${inferSec.toFixed(2)}s ` +
    `(${(duration / inferSec).toFixed(2)}× 实时) → ${outPath}`
);
