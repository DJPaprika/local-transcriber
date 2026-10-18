/**
 * SenseVoice 可行性验证
 *
 * 复用 DSH speech-to-text 插件已下载的 sherpa-onnx 栈与模型，
 * 直接转写一个 16kHz / 单声道 / 16-bit WAV 文件。
 *
 * 目的：确认三件事
 *   1. 不改任何东西、不装 Python，能不能跑通
 *   2. CPU 推理比 whisper.cpp 快多少
 *   3. getResult() 里到底有没有时间戳（决定能不能做 SRT）
 *
 * 用法：node sensevoice-test.js <16k.wav>
 */

const fs = require("fs");
const path = require("path");

const SHERPA = "C:/Users/13414/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh/node_modules/sherpa-onnx-node";
const MODEL_DIR = "C:/Users/13414/.dsh/speech-to-text/sensevoice/models";

const sherpa = require(SHERPA);

const wavPath = process.argv[2];
if (!wavPath) {
  console.error("用法: node sensevoice-test.js <16k.wav>");
  process.exit(1);
}

// ---- 读 WAV：跳过 44 字节头，Int16 -> Float32 ----
const buf = fs.readFileSync(wavPath);
const pcm = new DataView(buf.buffer, buf.byteOffset + 44, buf.byteLength - 44);
const samples = Float32Array.from(
  { length: pcm.byteLength / 2 },
  (_, i) => pcm.getInt16(i * 2, true) / 32768
);
const durationSec = samples.length / 16000;
console.log(`音频: ${path.basename(wavPath)}  时长 ${durationSec.toFixed(2)}s  采样点 ${samples.length}`);

// ---- 加载模型 ----
const tLoad = performance.now();
const recognizer = new sherpa.OfflineRecognizer({
  featConfig: { sampleRate: 16000, featureDim: 80 },
  modelConfig: {
    senseVoice: {
      model: `${MODEL_DIR}/sensevoice-onnx/model.int8.onnx`,
      language: "zh",
      useInverseTextNormalization: 1,
    },
    tokens: `${MODEL_DIR}/sensevoice-onnx/tokens.txt`,
    numThreads: 8,
    provider: "cpu",
    debug: 0,
  },
});
const loadMs = performance.now() - tLoad;

// ---- VAD ----
const detector = new sherpa.Vad(
  {
    sileroVad: {
      model: `${MODEL_DIR}/silero/silero_vad.onnx`,
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
  64 // 缓冲容量（秒）；不控制分段，只需大于实际最长段（约 32 秒）
);

// ---- 推理 ----
const tInfer = performance.now();
const segments = [];
const drain = () => {
  while (!detector.isEmpty()) {
    const seg = detector.front(false);
    const stream = recognizer.createStream();
    stream.acceptWaveform({ sampleRate: 16000, samples: seg.samples });
    recognizer.decode(stream);
    const result = recognizer.getResult(stream);
    segments.push({ seg, result });
    detector.pop();
  }
};
for (let off = 0; off < samples.length; off += 512) {
  detector.acceptWaveform(samples.subarray(off, off + 512));
  drain();
}
detector.flush();
drain();
const inferMs = performance.now() - tInfer;

// ---- 报告 ----
console.log(`\n模型加载: ${(loadMs / 1000).toFixed(2)} s`);
console.log(`VAD+推理: ${(inferMs / 1000).toFixed(2)} s`);
console.log(`处理速度: ${(durationSec / (inferMs / 1000)).toFixed(2)}x 实时（不含加载）`);

console.log(`\nVAD 分段数: ${segments.length}`);
if (segments.length > 0) {
  console.log("segment 对象字段:", Object.keys(segments[0].seg).join(", "));
  console.log("result   对象字段:", Object.keys(segments[0].result).join(", "));
  const first = segments[0].result;

  // 关键：tokens / timestamps / durations / words 怎么对齐
  console.log(
    `\n token 数=${first.tokens?.length}  timestamps 数=${first.timestamps?.length}` +
      `  durations 数=${first.durations?.length}  words 数=${first.words?.length}`
  );

  console.log("\n 前 15 个 token（含时间与时长）:");
  for (let i = 0; i < Math.min(15, first.tokens?.length ?? 0); i++) {
    console.log(
      `   ${String(i).padStart(3)}  "${first.tokens[i]}"  t=${first.timestamps?.[i]}` +
        `  dur=${first.durations?.[i]}`
    );
  }

  console.log("\n 前 8 个 word:");
  console.log(JSON.stringify(first.words?.slice(0, 8), null, 1));

  console.log("\n VAD 段信息:");
  segments.forEach((s, i) => {
    const sec = s.seg.samples.length / 16000;
    console.log(`   段${i}: start=${s.seg.start}  采样=${s.seg.samples.length}  约 ${sec.toFixed(2)}s`);
  });
}

console.log("\n---- 全文 ----");
console.log(segments.map((s) => (s.result.text || "").trim()).filter(Boolean).join(" / "));
