// MV3 service worker. Batches events from all Twitter tabs, tracks session
// boundaries, flushes to 127.0.0.1:8765/ingest every 3s or at 50 events.
// Persists queue to chrome.storage.local so a backend outage is recoverable.

const BACKEND_URL = "http://127.0.0.1:8765/ingest";
const FLUSH_INTERVAL_MS = 3000;
const BATCH_SIZE = 50;
const SESSION_IDLE_MS = 5 * 60 * 1000;
const QUEUE_STORAGE_KEY = "__tm_queue__";
const MAX_QUEUE_SIZE = 5000;

let queue = [];
let flushTimer = null;
let backoffUntil = 0;
let backoffMs = 1000;
let captureEnabled = true;

chrome.storage.sync.get("captureEnabled").then((r) => {
  if (typeof r.captureEnabled === "boolean") captureEnabled = r.captureEnabled;
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "sync" && changes.captureEnabled) {
    captureEnabled = changes.captureEnabled.newValue !== false;
  }
});

// --- Session tracking ---------------------------------------------------
let currentSessionId = null;
let sessionStartedAt = null;
let lastActivityAt = 0;
let sessionTweetCount = 0;
let sessionFeeds = new Set();

function uuid() {
  return crypto.randomUUID();
}

async function loadQueue() {
  const r = await chrome.storage.local.get(QUEUE_STORAGE_KEY);
  if (Array.isArray(r[QUEUE_STORAGE_KEY])) {
    queue = r[QUEUE_STORAGE_KEY].slice(-MAX_QUEUE_SIZE);
  }
}

async function persistQueue() {
  try {
    await chrome.storage.local.set({ [QUEUE_STORAGE_KEY]: queue.slice(-MAX_QUEUE_SIZE) });
  } catch (e) {
    // quota issues — drop oldest
    queue = queue.slice(-1000);
    try {
      await chrome.storage.local.set({ [QUEUE_STORAGE_KEY]: queue });
    } catch (_) {}
  }
}

function enqueue(event) {
  if (!event.event_id) event.event_id = crypto.randomUUID();
  queue.push(event);
  if (queue.length >= BATCH_SIZE) void flushNow();
}

async function flushNow() {
  if (queue.length === 0) return;
  if (Date.now() < backoffUntil) return;
  const batch = queue.splice(0, queue.length);
  try {
    const res = await fetch(BACKEND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: batch }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    backoffMs = 1000;
    backoffUntil = 0;
    await persistQueue();
  } catch (e) {
    // put back and back off
    queue = batch.concat(queue).slice(-MAX_QUEUE_SIZE);
    await persistQueue();
    backoffUntil = Date.now() + backoffMs;
    backoffMs = Math.min(backoffMs * 2, 5 * 60 * 1000);
  }
}

// chrome.alarms is the only reliable periodic timer in MV3 SW. Use it for flush.
chrome.alarms.create("tm-flush", { periodInMinutes: 1 / 20 }); // ~3s — capped by platform to 30s
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "tm-flush") void flushNow();
});

// --- Session management ---------------------------------------------------
// Sessions are global across all tabs, not per-tab. A "session" is an attention
// stretch, not a browser tab — two tabs open on x.com is still one session.
// tabId is accepted for future per-tab analytics but not used for session keying.
function ensureSession(tabId) {
  const now = Date.now();
  if (!currentSessionId || now - lastActivityAt > SESSION_IDLE_MS) {
    if (currentSessionId) endSession(now);
    currentSessionId = uuid();
    sessionStartedAt = now;
    sessionTweetCount = 0;
    sessionFeeds = new Set();
    enqueue({
      type: "session_start",
      session_id: currentSessionId,
      timestamp: new Date(now).toISOString(),
    });
  }
  lastActivityAt = now;
}

function endSession(now = Date.now()) {
  if (!currentSessionId) return;
  enqueue({
    type: "session_end",
    session_id: currentSessionId,
    timestamp: new Date(now).toISOString(),
    total_dwell_ms: now - sessionStartedAt,
    tweet_count: sessionTweetCount,
    feeds_visited: [...sessionFeeds],
  });
  currentSessionId = null;
}

// Idle check as a fallback.
chrome.alarms.create("tm-session-idle", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== "tm-session-idle") return;
  if (currentSessionId && Date.now() - lastActivityAt > SESSION_IDLE_MS) {
    endSession();
    void flushNow();
  }
});

// --- Messaging ------------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.kind !== "event") return;
  if (!captureEnabled) return;
  const ev = msg.event;
  if (!ev || !ev.type) return;

  if (sender.tab?.id) {
    ensureSession(sender.tab.id);
    ev.tab_id = sender.tab.id;
  }

  // Attach session + track per-session counts.
  if (!ev.session_id) ev.session_id = currentSessionId;
  if (ev.type === "impression_end") {
    sessionTweetCount += 1;
    if (ev.feed_source) sessionFeeds.add(ev.feed_source);
  }
  enqueue(ev);
});

async function injectIntoOpenTabs() {
  const tabs = await chrome.tabs.query({
    url: ["https://x.com/*", "https://twitter.com/*"],
  });
  for (const t of tabs) {
    if (typeof t.id !== "number") continue;
    try {
      await chrome.scripting.executeScript({
        target: { tabId: t.id },
        files: ["content-script.js"],
      });
    } catch (_) {
      // incognito-without-access, chrome:// pages, discarded tabs — ignore
    }
  }
}

// Drop session on startup of SW after a cold restart to avoid reusing stale IDs.
chrome.runtime.onStartup.addListener(async () => {
  await loadQueue();
});
chrome.runtime.onInstalled.addListener(async (details) => {
  await loadQueue();
  if (details.reason === "install" || details.reason === "update") {
    await injectIntoOpenTabs();
  }
});

// Kick off: load any persisted queue and try a flush.
void loadQueue().then(() => flushNow());
