/**
 * SenseVoice → SRT / TXT
 *
 * 用 DSH speech-to-text 插件已下载的 sherpa-onnx 栈与 SenseVoice 模型，
 * 把 16kHz 单声道 WAV 转成字幕。核心工作是把「段内相对、字符级」的时间戳
 * 还原成「全局、句级」的字幕条目。
 *
 * 用法：node sv-to-srt.js <16k.wav> [输出前缀]
 */

const fs = require("fs");
const os = require("os");
const path = require("path");

// ---------------------------------------------------------------- 依赖定位
// 这里原本写死了作者机器上的两条路径，换台机器就跑不起来。改成按顺序探测：
//   1. 环境变量显式指定
//   2. 项目本地 node_modules / models
//   3. 已知的默认位置

function resolveSherpa() {
  const candidates = [
    process.env.SHERPA_ONNX_NODE,
    path.join(__dirname, "node_modules", "sherpa-onnx-node"),
    path.join(__dirname, "..", "node_modules", "sherpa-onnx-node"),
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
      "    请在项目里执行 npm i sherpa-onnx-node，\n" +
      "    或用环境变量 SHERPA_ONNX_NODE 指定它的目录。"
  );
  process.exit(1);
}

function resolveModelFiles() {
  const candidates = [
    process.env.SENSEVOICE_MODEL_DIR,
    path.join(os.homedir(), ".dsh", "speech-to-text", "sensevoice", "models"),
    path.join(__dirname, "models"),
  ].filter(Boolean);

  for (const dir of candidates) {
    const files = {
      dir,
      onnx: path.join(dir, "sensevoice-onnx", "model.int8.onnx"),
      tokens: path.join(dir, "sensevoice-onnx", "tokens.txt"),
      vad: path.join(dir, "silero", "silero_vad.onnx"),
    };
    if (fs.existsSync(files.onnx) && fs.existsSync(files.tokens) && fs.existsSync(files.vad)) {
      return files;
    }
  }
  console.error(
    "[x] 找不到 SenseVoice 模型文件。期望的目录结构：\n" +
      "      <MODEL_DIR>/sensevoice-onnx/model.int8.onnx\n" +
      "      <MODEL_DIR>/sensevoice-onnx/tokens.txt\n" +
      "      <MODEL_DIR>/silero/silero_vad.onnx\n" +
      "    用环境变量 SENSEVOICE_MODEL_DIR 指定该目录。"
  );
  process.exit(1);
}

const sherpa = resolveSherpa();
const modelFiles = resolveModelFiles();

// ---------------------------------------------------------------- 字幕切分规则

const SENTENCE_END = /[。！？!?…]/; // 句末：一定断
const CLAUSE_END = /[，,；;、]/; // 句中停顿：够长才断
const MAX_CHARS = 24; // 单条字幕最长字数
const MAX_SECONDS = 8.0; // 单条字幕最长时长
const MIN_SECONDS = 1.0; // 单条字幕最短显示时长
const BREAK_AFTER = 0.45; // 字间静默超过这个值就断句

/**
 * 把字符级时间戳合并成字幕条目。
 * @param {{char: string, t: number}[]} chars 已换算为全局秒的时间戳序列
 */
function toCues(chars) {
  const cues = [];
  let cur = [];

  const flush = (end) => {
    if (!cur.length) return;
    const start = cur[0].t;
    const text = cur.map((c) => c.char).join("").trim();
    if (text) cues.push({ start, end: Math.max(end, start + 0.2), text });
    cur = [];
  };

  for (let i = 0; i < chars.length; i++) {
    const c = chars[i];
    cur.push(c);

    const next = chars[i + 1];
    const span = c.t - cur[0].t;
    const gap = next ? next.t - c.t : 99;

    if (SENTENCE_END.test(c.char)) flush(c.t + 0.35);
    else if (gap >= BREAK_AFTER) flush(c.t + 0.25);
    else if (span >= MAX_SECONDS) flush(c.t + 0.2);
    else if (cur.length >= MAX_CHARS && (CLAUSE_END.test(c.char) || cur.length >= MAX_CHARS + 6))
      flush(c.t + 0.2);
  }
  flush(chars.length ? chars[chars.length - 1].t + 0.5 : 0);

  // 收尾整理：保证最短显示时长、且不与下一条重叠
  for (let i = 0; i < cues.length; i++) {
    if (cues[i].end - cues[i].start < MIN_SECONDS) cues[i].end = cues[i].start + MIN_SECONDS;
    const nextStart = cues[i + 1]?.start;
    if (nextStart !== undefined && cues[i].end > nextStart) {
      cues[i].end = Math.max(cues[i].start + 0.2, nextStart - 0.05);
    }
  }
  return cues;
}

function srtTime(sec) {
  const ms = Math.max(0, Math.round(sec * 1000));
  const h = String(Math.floor(ms / 3600000)).padStart(2, "0");
  const m = String(Math.floor((ms % 3600000) / 60000)).padStart(2, "0");
  const s = String(Math.floor((ms % 60000) / 1000)).padStart(2, "0");
  return `${h}:${m}:${s},${String(ms % 1000).padStart(3, "0")}`;
}

// ---------------------------------------------------------------- 主流程

const wavPath = process.argv[2];
if (!wavPath) {
  console.error("用法: node sv-to-srt.js <16k.wav> [输出前缀]");
  process.exit(1);
}
const stem = process.argv[3] || path.basename(wavPath, path.extname(wavPath));
const outDir = path.dirname(path.resolve(wavPath));

const buf = fs.readFileSync(wavPath);
const pcm = new DataView(buf.buffer, buf.byteOffset + 44, buf.byteLength - 44);
const samples = Float32Array.from(
  { length: pcm.byteLength / 2 },
  (_, i) => pcm.getInt16(i * 2, true) / 32768
);
const duration = samples.length / 16000;

const tLoad = performance.now();
const recognizer = new sherpa.OfflineRecognizer({
  featConfig: { sampleRate: 16000, featureDim: 80 },
  modelConfig: {
    senseVoice: {
      model: modelFiles.onnx,
      language: "auto",
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
  // 环形缓冲容量（秒）。注意：它不控制分段长度，分段由上面的 maxSpeechDuration 决定。
  // 但容量必须大于实际最长段，否则会触发 circular-buffer 溢出警告（数据不丢，只是多一次拷贝）。
  // 实测段长可到约 32 秒，取 64 留足余量（float32 占用约 4 MB）。
  64
);

const tInfer = performance.now();
const chars = []; // { char, t } 全局秒
const segTexts = [];
let segCount = 0;

const drain = () => {
  while (!detector.isEmpty()) {
    const seg = detector.front(false);
    const segOffset = seg.start / 16000; // ← 采样点转秒，关键一步
    const stream = recognizer.createStream();
    stream.acceptWaveform({ sampleRate: 16000, samples: seg.samples });
    recognizer.decode(stream);
    const r = recognizer.getResult(stream);
    segCount++;

    const tokens = r.tokens || [];
    const stamps = r.timestamps || [];
    for (let i = 0; i < tokens.length; i++) {
      chars.push({ char: tokens[i], t: segOffset + (stamps[i] ?? 0) });
    }
    segTexts.push((r.text || "").trim());
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

const cues = toCues(chars);

const srt = cues
  .map((c, i) => `${i + 1}\n${srtTime(c.start)} --> ${srtTime(c.end)}\n${c.text}\n`)
  .join("\n");
fs.writeFileSync(path.join(outDir, `${stem}.srt`), srt, "utf-8");
fs.writeFileSync(path.join(outDir, `${stem}.txt`), segTexts.filter(Boolean).join("\n"), "utf-8");

const stats = {
  engine: "sensevoice-small-int8-onnx",
  wav: path.basename(wavPath),
  duration_seconds: +duration.toFixed(2),
  vad_segments: segCount,
  load_seconds: +loadSec.toFixed(2),
  infer_seconds: +inferSec.toFixed(2),
  realtime_factor: +(duration / inferSec).toFixed(2),
  cues: cues.length,
  avg_cue_seconds: cues.length ? +(duration / cues.length).toFixed(2) : 0,
};
fs.writeFileSync(path.join(outDir, `${stem}.svstats.json`), JSON.stringify(stats, null, 2), "utf-8");

console.log("=".repeat(48));
console.log("  SenseVoice 转写完成");
console.log("=".repeat(48));
console.log(`  音频时长    : ${duration.toFixed(1)} s`);
console.log(`  模型加载    : ${loadSec.toFixed(2)} s`);
console.log(`  推理耗时    : ${inferSec.toFixed(2)} s`);
console.log(`  处理速度    : ${(duration / inferSec).toFixed(2)}× 实时`);
console.log(`  VAD 分段    : ${segCount}`);
console.log(`  字幕条数    : ${cues.length}`);
console.log("-".repeat(48));
cues.slice(0, 6).forEach((c, i) => {
  console.log(`  ${i + 1}. [${srtTime(c.start)} → ${srtTime(c.end)}] ${c.text}`);
});
console.log("=".repeat(48));
console.log(`  输出: ${stem}.srt / ${stem}.txt / ${stem}.svstats.json`);
