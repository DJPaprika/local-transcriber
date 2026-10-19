const $ = (sel) => document.querySelector(sel);
const jobsEl = $("#jobs");
const tpl = $("#job-tpl");
const formError = $("#form-error");
const dropzone = $("#dropzone");
const uploadNote = $("#upload-note");
const filepicker = $("#filepicker");

const cache = new Map(); // id -> { job, offset, nodes }
const KIND_LABEL = { transcribe: "转写", summary: "摘要", translate: "翻译", ask: "问答" };
const STATUS_LABEL = {
  queued: "排队中", running: "进行中", done: "完成",
  failed: "失败", cancelled: "已取消",
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtSize(n) {
  if (!n) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

// 产物目录（相对 output/）。老任务没有 result.dir，退回用提交时的名字。
function jobDir(job) {
  const dir = job.result && job.result.dir;
  if (typeof dir === "string") return dir;
  return (job.params && job.params.name) || "";
}

function relPath(job, name) {
  const dir = jobDir(job);
  return dir ? dir + "/" + name : name;
}

function fileUrl(job, name) {
  return "/api/files?path=" + encodeURIComponent(relPath(job, name));
}

function ensureCard(job) {
  let c = cache.get(job.id);
  if (c) return c;

  const node = tpl.content.firstElementChild.cloneNode(true);
  const nodes = {
    root: node,
    badge: node.querySelector(".badge"),
    kind: node.querySelector(".job-kind"),
    name: node.querySelector(".job-name"),
    time: node.querySelector(".job-time"),
    cancel: node.querySelector(".cancel"),
    del: node.querySelector(".delete"),
    bar: node.querySelector(".bar"),
    fill: node.querySelector(".bar > i"),
    detail: node.querySelector(".job-detail"),
    log: node.querySelector(".log"),
    actions: node.querySelector(".job-actions"),
  };
  nodes.kind.textContent = KIND_LABEL[job.kind] || job.kind;
  nodes.name.textContent = job.params?.name || job.params?.source || job.id;
  nodes.cancel.onclick = async () => {
    nodes.cancel.disabled = true;
    try { await api("/api/jobs/" + job.id + "/cancel", { method: "POST" }); } catch {}
    poll();
  };
  nodes.del.onclick = async () => {
    if (!confirm("从列表删除这条任务记录？\n字幕等产物文件不会被删除。")) return;
    nodes.del.disabled = true;
    try {
      await api("/api/jobs/" + job.id, { method: "DELETE" });
    } catch (err) {
      alert(err.message);
      nodes.del.disabled = false;
      return;
    }
    cache.delete(job.id);
    nodes.root.remove();
    poll();
  };
  jobsEl.prepend(node);

  c = { job, offset: 0, nodes, detailLoaded: false };
  cache.set(job.id, c);
  return c;
}

function paint(c) {
  const { job, nodes } = c;
  const st = job.status;
  const p = job.progress || {};

  nodes.badge.textContent = STATUS_LABEL[st] || st;
  nodes.badge.className = "badge " + st;
  nodes.time.textContent = fmtTime(job.started_at);

  if (st === "queued") {
    nodes.bar.classList.add("indeterminate");
    nodes.detail.textContent = "等待前一个任务完成……";
  } else if (st === "running") {
    if (p.percent == null) {
      nodes.bar.classList.add("indeterminate");
    } else {
      nodes.bar.classList.remove("indeterminate");
      nodes.fill.style.width = p.percent + "%";
    }
    nodes.detail.textContent = p.detail || "启动中……";
  } else {
    nodes.bar.classList.remove("indeterminate");
    nodes.fill.style.width = st === "done" ? "100%" : "0";
    nodes.fill.style.background =
      st === "failed" ? "var(--bad)" : st === "cancelled" ? "var(--warn)" : "";
    nodes.detail.textContent =
      st === "done"
        ? (p.detail || "已完成")
        : (job.error || p.detail || "").split("\n").slice(-2).join(" · ");
  }

  nodes.cancel.style.display = st === "queued" || st === "running" ? "" : "none";
  nodes.del.style.display = st === "queued" || st === "running" ? "none" : "";

  if (st === "done") renderActions(c);
}

function renderActions(c) {
  const { job, nodes } = c;
  nodes.actions.replaceChildren();
  const files = job.result?.files || [];

  for (const f of files) {
    const a = document.createElement("a");
    a.href = fileUrl(job, f.name);
    a.textContent = "↓ " + f.name + " " + fmtSize(f.size);
    nodes.actions.appendChild(a);
  }

  // 转写完成后可以一键摘要 / 翻译（产物和源文件同目录、同前缀）
  if (job.kind === "transcribe") {
    const txt = files.find((f) => f.name.endsWith(".txt"));
    const srt = files.find((f) => f.name.endsWith(".srt"));
    if (txt) {
      const b = document.createElement("button");
      b.textContent = "生成摘要";
      b.onclick = () => submitFollowUp("summary", relPath(job, txt.name));
      nodes.actions.appendChild(b);
    }
    if (srt) {
      const b = document.createElement("button");
      b.textContent = "翻译成中文";
      b.onclick = () => submitFollowUp("translate", relPath(job, srt.name));
      nodes.actions.appendChild(b);
    }
  }
}

async function submitFollowUp(kind, input) {
  try {
    await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, params: { input } }),
    });
    poll();
  } catch (err) {
    alert(err.message);
  }
}

async function render(list) {
  if (!list.length) {
    if (!cache.size) {
      jobsEl.innerHTML = '<p class="empty">还没有任务</p>';
    }
    return;
  }
  const empty = jobsEl.querySelector(".empty");
  if (empty) empty.remove();

  for (const job of list) {
    const c = ensureCard(job);
    c.job = { ...c.job, ...job };
    paint(c);
    if (job.status === "running") await pullDetail(c);
  }
}

async function pullDetail(c) {
  try {
    const snap = await api("/api/jobs/" + c.job.id + "?offset=" + c.offset);
    c.offset = snap.log_offset || c.offset;
    if (snap.log) {
      const stick =
        c.nodes.log.scrollTop + c.nodes.log.clientHeight >= c.nodes.log.scrollHeight - 20;
      c.nodes.log.textContent += snap.log;
      if (stick) c.nodes.log.scrollTop = c.nodes.log.scrollHeight;
    }
    c.job = { ...c.job, ...snap };
    paint(c);
  } catch {}
}

// ---------------------------------------------------------------- 上传 & 拖拽

function uploadOne(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload?name=" + encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.error || "上传失败（HTTP " + xhr.status + "）"));
    };
    xhr.onerror = () => reject(new Error("上传失败：网络错误"));
    xhr.send(file);
  });
}

async function handleFiles(files) {
  if (!files.length) return;
  formError.textContent = "";
  const engine = $("#engine").value;
  const language = $("#language").value;

  for (const file of files) {
    uploadNote.textContent = "上传中：" + file.name;
    try {
      const data = await uploadOne(file, (p) => {
        uploadNote.textContent = "上传中：" + file.name + " " + Math.round(p * 100) + "%";
      });
      uploadNote.textContent = "已上传：" + file.name + "（" + fmtSize(data.size) + "），开始转写";
      const job = await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "transcribe",
          params: { source: data.path, name: "", engine: engine, language: language },
        }),
      });
      const c = ensureCard(job);
      paint(c);
      poll();
    } catch (err) {
      formError.textContent = file.name + "：" + err.message;
      uploadNote.textContent = "";
      return;
    }
  }
  uploadNote.textContent = "";
}

document.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropzone.classList.add("over");
});
document.addEventListener("dragleave", (e) => {
  if (!e.relatedTarget) dropzone.classList.remove("over");
});
document.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("over");
  if (e.dataTransfer?.files?.length) handleFiles([...e.dataTransfer.files]);
});
dropzone.addEventListener("click", (e) => {
  if (e.target.closest("input")) return;
  filepicker.click();
});
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); filepicker.click(); }
});
filepicker.addEventListener("change", () => {
  handleFiles([...filepicker.files]);
  filepicker.value = "";
});

// ---------------------------------------------------------------- 设置 & LLM

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    $("#keep-server").checked = !!s.keep_server;
  } catch {}
}

$("#keep-server").onchange = async (e) => {
  const want = e.target.checked;
  try {
    const s = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_server: want }),
    });
    e.target.checked = !!s.keep_server;
  } catch (err) {
    e.target.checked = !want;
    alert(err.message);
  }
};

async function refreshLlm() {
  try {
    const s = await api("/api/llm");
    $("#llm-state").textContent = s.running
      ? "运行中" + (s.pid ? " · pid " + s.pid : " · 外部启动")
      : (s.stale ? "未运行（有残留 pid，可点「停掉服务」清理）" : "未运行");
  } catch {
    $("#llm-state").textContent = "状态未知";
  }
}

$("#stop-llm").onclick = async () => {
  try {
    const r = await api("/api/llm/stop", { method: "POST" });
    if (!r.ok) throw new Error(r.error || "停止失败");
  } catch (err) {
    alert(err.message);
    return;
  }
  setTimeout(refreshLlm, 1200);
  refreshLlm();
};

// ---------------------------------------------------------------- 轮询

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const list = await api("/api/jobs");
    await render(list);
  } catch (e) {
    // 服务没起来时安静重试
  } finally {
    polling = false;
  }
}

$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  formError.textContent = "";
  const btn = $("#submit");
  btn.disabled = true;
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: "transcribe",
        params: {
          source: $("#source").value.trim(),
          name: $("#name").value.trim(),
          engine: $("#engine").value,
          language: $("#language").value,
        },
      }),
    });
    $("#source").value = "";
    $("#name").value = "";
    const c = ensureCard(job);
    paint(c);
    poll();
  } catch (err) {
    formError.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
});

$("#refresh").onclick = poll;

// 打开页面先把历史任务拉出来（不展开日志）
(async function init() {
  loadSettings();
  refreshLlm();
  setInterval(refreshLlm, 5000);
  try {
    const list = await api("/api/jobs");
    await render(list);
  } catch {}
  setInterval(poll, 1500);
})();
