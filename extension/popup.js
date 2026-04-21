const BACKEND = "http://127.0.0.1:8765";
const POLL_MS = 2000;
const REQ_TIMEOUT_MS = 1500;

function fmtMs(ms) {
  if (!ms) return "0m";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

async function fetchWithTimeout(url, ms) {
  const ctrl = new AbortController();
  const id = setTimeout(() => ctrl.abort(), ms);
  try {
    return await fetch(url, { signal: ctrl.signal });
  } finally {
    clearTimeout(id);
  }
}

let lastBackendState = null; // "ok" | "bad" | null

function setBackendState(state) {
  if (state === lastBackendState) return;
  lastBackendState = state;
  const backend = document.getElementById("backend");
  const hint = document.getElementById("hint");
  if (state === "ok") {
    backend.textContent = "ok";
    backend.className = "status-ok";
    hint.style.display = "none";
  } else {
    backend.textContent = "unreachable";
    backend.className = "status-bad";
    hint.style.display = "block";
  }
}

async function refresh() {
  try {
    const [statsRes, cfgRes, dqRes] = await Promise.all([
      fetchWithTimeout(`${BACKEND}/stats`, REQ_TIMEOUT_MS),
      fetchWithTimeout(`${BACKEND}/debug/config`, REQ_TIMEOUT_MS).catch(() => null),
      fetchWithTimeout(`${BACKEND}/debug/data-quality`, REQ_TIMEOUT_MS).catch(() => null),
    ]);
    const s = await statsRes.json();
    setBackendState("ok");
    document.getElementById("tweets").textContent = s.tweets_today;
    document.getElementById("sessions").textContent = s.sessions_today;
    document.getElementById("dwell").textContent = fmtMs(s.total_dwell_ms_today);
    if (cfgRes && cfgRes.ok) {
      const cfg = await cfgRes.json();
      document.getElementById("data-dir").textContent = cfg.data_dir || "—";
    }
    if (dqRes && dqRes.ok) {
      const dq = await dqRes.json();
      document.getElementById("dq-stubs").textContent = dq.tweets_without_text;
      document.getElementById("dq-queue").textContent = dq.enrichment_pending;
      document.getElementById("dq-templates").textContent = dq.graphql_templates;
    }
  } catch (e) {
    setBackendState("bad");
  }
  const sync = await chrome.storage.sync.get(["captureEnabled", "enrichmentEnabled"]);
  const captureEnabled = sync.captureEnabled !== false;
  const enrichmentEnabled = sync.enrichmentEnabled === true;
  document.getElementById("cap").textContent = captureEnabled ? "on" : "off";
  document.getElementById("enrich-state").textContent = enrichmentEnabled ? "on" : "off";
}

document.getElementById("toggle").addEventListener("click", async () => {
  const { captureEnabled = true } = await chrome.storage.sync.get("captureEnabled");
  await chrome.storage.sync.set({ captureEnabled: !captureEnabled });
  refresh();
});

document.getElementById("toggle-enrich").addEventListener("click", async () => {
  const { enrichmentEnabled = false } = await chrome.storage.sync.get("enrichmentEnabled");
  await chrome.storage.sync.set({ enrichmentEnabled: !enrichmentEnabled });
  refresh();
});

document.getElementById("force-enrich").addEventListener("click", async () => {
  const btn = document.getElementById("force-enrich");
  const status = document.getElementById("enrich-status");
  btn.disabled = true;
  status.textContent = "running…";
  try {
    const r = await chrome.runtime.sendMessage({ kind: "force_enrichment" });
    if (!r) {
      status.textContent = "no response from service worker";
    } else if (r.reason === "ok") {
      status.textContent = `✓ ${r.op} → ${r.target_id}`;
    } else {
      status.textContent = `${r.reason}${r.op ? " (" + r.op + ")" : ""}`;
    }
    refresh();
  } catch (e) {
    status.textContent = `error: ${e.message || e}`;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("export").addEventListener("click", async () => {
  const btn = document.getElementById("export");
  const status = document.getElementById("export-status");
  btn.disabled = true;
  status.textContent = "exporting…";
  try {
    const res = await fetch(`${BACKEND}/export/day`, { method: "POST" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    status.textContent = `${body.tweet_count} tweets → ${body.file_path}`;
  } catch (e) {
    status.textContent = `export failed: ${e.message || e}`;
  } finally {
    btn.disabled = false;
  }
});

refresh();
const pollId = setInterval(refresh, POLL_MS);
window.addEventListener("unload", () => clearInterval(pollId));
