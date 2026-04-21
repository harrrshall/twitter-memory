// Active enrichment worker. Replays captured GraphQL queries against x.com
// using the user's own session cookies. Throttled, visibility-gated, opt-in.
//
// Imported by service-worker.js. Exposes noteOrganicEvent() and forceTick().

const BACKEND = "http://127.0.0.1:8765";
const ALARM_NAME = "tm-enrichment";
const PERIOD_MIN = 1;              // alarm period (floor ~30s in MV3)
const MIN_INTERVAL_MS = 12_000;    // min gap between replays
const JITTER_MS = 8_000;           // random pre-fire delay
const ACTIVITY_GATE_MS = 120_000;  // only fire if organic activity within last 2 min
const BACKOFF_429_MS = 30 * 60 * 1000;
const MAX_ATTEMPTS_PER_HOUR = {
  TweetDetail: 20,
  TweetResultByRestId: 40,
  UserByScreenName: 10,
  UserByRestId: 10,
  UserTweets: 5,
};

let enrichmentEnabled = false;
let authBroken = false;
let nextOkAt = 0;
let lastFireAt = 0;
let lastOrganicEventAt = 0;
const perEndpointWindow = new Map();

chrome.storage.sync.get("enrichmentEnabled").then((r) => {
  enrichmentEnabled = r.enrichmentEnabled === true;
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "sync") return;
  if (changes.enrichmentEnabled) {
    enrichmentEnabled = changes.enrichmentEnabled.newValue === true;
    if (enrichmentEnabled) authBroken = false; // user toggled on → give auth another shot
  }
});

export function noteOrganicEvent() {
  lastOrganicEventAt = Date.now();
}

async function isXcomTabActive() {
  const tabs = await chrome.tabs.query({
    active: true,
    windowType: "normal",
    url: ["https://x.com/*", "https://twitter.com/*"],
  });
  return tabs.length > 0;
}

function checkAndBumpRate(op) {
  const cap = MAX_ATTEMPTS_PER_HOUR[op] || 5;
  const now = Date.now();
  const arr = (perEndpointWindow.get(op) || []).filter((t) => now - t < 3_600_000);
  if (arr.length >= cap) {
    perEndpointWindow.set(op, arr);
    return false;
  }
  arr.push(now);
  perEndpointWindow.set(op, arr);
  return true;
}

function mutateVariables(op, targetId, templateVars) {
  let vars;
  try {
    vars = JSON.parse(templateVars || "{}");
  } catch (_) {
    vars = {};
  }
  if (op === "TweetDetail") {
    vars.focalTweetId = targetId;
    // Keep other TweetDetail flags from the captured template (with_rux_injections,
    // includePromotedContent, etc.) — those travel with the template.
  } else if (op === "TweetResultByRestId") {
    vars.tweetId = targetId;
  } else if (op === "UserByRestId") {
    vars.userId = targetId;
  } else if (op === "UserByScreenName") {
    vars.screen_name = targetId;
  } else if (op === "UserTweets") {
    vars.userId = targetId;
  } else {
    return null;
  }
  return vars;
}

async function getCsrf() {
  try {
    const c = await chrome.cookies.get({ url: "https://x.com", name: "ct0" });
    return c?.value || "";
  } catch (_) {
    return "";
  }
}

async function replay(work) {
  const { target_id, template } = work;
  const op = template.operation_name;
  const vars = mutateVariables(op, target_id, template.variables_json);
  if (!vars) return { kind: "error", error: `unsupported op ${op}` };

  const csrf = await getCsrf();
  if (!template.bearer || !csrf) {
    return { kind: "auth_failed", error: "missing bearer or ct0" };
  }

  const url = new URL(`https://x.com${template.url_path}`);
  url.searchParams.set("variables", JSON.stringify(vars));
  url.searchParams.set("features", template.features_json || "{}");

  let res;
  try {
    res = await fetch(url.toString(), {
      method: "GET",
      credentials: "include",
      headers: {
        authorization: template.bearer,
        "x-csrf-token": csrf,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "x-twitter-active-user": "yes",
      },
    });
  } catch (e) {
    return { kind: "error", error: String(e) };
  }

  if (res.status === 429) {
    nextOkAt = Date.now() + BACKOFF_429_MS;
    return { kind: "rate_limited" };
  }
  if (res.status === 401 || res.status === 403) {
    authBroken = true;
    return { kind: "auth_failed", error: `HTTP ${res.status}` };
  }
  if (!res.ok) return { kind: "error", error: `HTTP ${res.status}` };

  let body;
  try {
    body = await res.json();
  } catch (_) {
    return { kind: "error", error: "invalid JSON" };
  }

  try {
    await fetch(`${BACKEND}/ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        events: [{
          type: "graphql_payload",
          event_id: crypto.randomUUID(),
          operation_name: op,
          url: url.pathname,
          payload: body,
        }],
      }),
    });
  } catch (_) {
    // Even if backend ingest fails, we successfully re-fetched from Twitter.
    // Reporting "ok" would leave the queue entry marked done without data
    // landing, so report as error and let it retry next alarm.
    return { kind: "error", error: "backend ingest unreachable" };
  }

  return { kind: "ok" };
}

async function reportComplete(id, kind, error) {
  try {
    await fetch(`${BACKEND}/enrichment/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, result: kind, error: error || null }),
    });
  } catch (_) {}
}

async function tick({ force = false } = {}) {
  // Force path (popup button click) bypasses the toggle — the click itself
  // is the opt-in signal for that single replay. Still honors rate limits
  // and auth state, because those are about safety / detection, not consent.
  if (!force && !enrichmentEnabled) return { reason: "disabled" };
  if (authBroken) return { reason: "auth_broken" };
  const now = Date.now();
  if (now < nextOkAt) return { reason: "rate_limited" };
  if (!force && now - lastFireAt < MIN_INTERVAL_MS) return { reason: "too_soon" };
  if (!force && now - lastOrganicEventAt > ACTIVITY_GATE_MS) return { reason: "user_idle" };
  if (!force && !(await isXcomTabActive())) return { reason: "no_active_tab" };

  let items;
  try {
    const res = await fetch(`${BACKEND}/enrichment/next?limit=1`);
    if (!res.ok) return { reason: "next_failed" };
    items = (await res.json()).items;
  } catch (_) {
    return { reason: "backend_unreachable" };
  }
  if (!items || items.length === 0) return { reason: "empty_queue" };

  const work = items[0];
  const op = work.template.operation_name;
  if (!checkAndBumpRate(op)) {
    await reportComplete(work.id, "rate_limited", "per-endpoint cap");
    return { reason: "endpoint_cap" };
  }

  if (!force) {
    await new Promise((r) => setTimeout(r, Math.random() * JITTER_MS));
  }
  lastFireAt = Date.now();
  const result = await replay(work);
  await reportComplete(work.id, result.kind, result.error);
  return { reason: result.kind, op, target_id: work.target_id };
}

export async function forceTick() {
  return tick({ force: true });
}

chrome.alarms.create(ALARM_NAME, { periodInMinutes: PERIOD_MIN });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === ALARM_NAME) void tick();
});
