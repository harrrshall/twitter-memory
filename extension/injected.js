// Runs in MAIN world at document_start. Patches fetch + XHR before Twitter's
// bundle wraps them, so we see every GraphQL response body.
//
// Cached early so a later re-wrap by Twitter's code can't clobber us. Forwards
// parsed responses to the content script via window.postMessage with a tagged
// type so the content script can filter.

(() => {
  // Guard against double-injection (manifest + retroactive chrome.scripting).
  if (window.__tm_injected_loaded__) return;
  window.__tm_injected_loaded__ = true;

  const TAG = "__tm_graphql__";
  const TEMPLATE_TAG = "__tm_graphql_template__";
  const MUTATION_TAG = "__tm_mutation__";
  const NAV_TAG = "__tm_nav__";

  // X.com's SPA router calls history.pushState from MAIN world. An isolated-world
  // monkey-patch on history.pushState doesn't intercept MAIN-world calls — each
  // world has its own Window prototype chain. So the nav/search/media detection
  // must patch here (MAIN world) and signal the content-script via postMessage.
  const origPushState = history.pushState;
  const origReplaceState = history.replaceState;
  function notifyNav() {
    try {
      window.postMessage({ type: NAV_TAG }, window.location.origin);
    } catch (_) {}
  }
  history.pushState = function () {
    const r = origPushState.apply(this, arguments);
    // Defer one tick so any synchronous DOM updates that accompany the route
    // change are in place before the content-script inspects location.
    setTimeout(notifyNav, 0);
    return r;
  };
  history.replaceState = function () {
    const r = origReplaceState.apply(this, arguments);
    setTimeout(notifyNav, 0);
    return r;
  };
  const origFetch = window.fetch;
  const OrigXHR = window.XMLHttpRequest;

  const GRAPHQL_RE = /\/i\/api\/graphql\/[^/]+\/([A-Za-z0-9_]+)/;

  // Relationship-change GraphQL mutations. Each entry: opName -> action string.
  // We only emit on a 2xx + errors==null response, so failed/rate-limited
  // attempts don't appear in the DB.
  const MUTATION_OP_TO_ACTION = {
    FollowUser: "follow",
    UnfollowUser: "unfollow",
    MuteUser: "mute",
    UnmuteUser: "unmute",
    BlockUser: "block",
    UnblockUser: "unblock",
  };

  // Extract the target user id from GraphQL request body variables. Twitter
  // uses either `user_id` or `userId` depending on the operation. Accept both.
  function extractTargetUserId(requestBody) {
    if (!requestBody) return null;
    let obj = requestBody;
    if (typeof obj === "string") {
      try { obj = JSON.parse(obj); } catch (_) { return null; }
    }
    if (obj && typeof obj === "object") {
      const vars = obj.variables || obj;
      return vars && (vars.user_id || vars.userId || vars.target_id) || null;
    }
    return null;
  }

  function postMutation(opName, targetUserId) {
    const action = MUTATION_OP_TO_ACTION[opName];
    if (!action || !targetUserId) return;
    try {
      window.postMessage(
        {
          type: MUTATION_TAG,
          operation_name: opName,
          action,
          target_user_id: String(targetUserId),
          timestamp: new Date().toISOString(),
        },
        window.location.origin,
      );
    } catch (_) {}
  }

  function postToContentScript(operation_name, url, payload) {
    try {
      window.postMessage(
        { type: TAG, operation_name, url, payload },
        window.location.origin,
      );
    } catch (e) {
      // postMessage can throw on unserializable payloads; drop quietly.
    }
  }

  // De-dupe template emissions per operation. We only need one template per
  // operation per session — re-emitting on every call pollutes the ingest path.
  const seenTemplates = new Set();

  function readAuthHeader(init, input) {
    try {
      // init.headers may be Headers, plain object, or array of tuples.
      if (init && init.headers) {
        const h = init.headers;
        if (h instanceof Headers) return h.get("authorization") || "";
        if (Array.isArray(h)) {
          const hit = h.find((p) => (p[0] || "").toLowerCase() === "authorization");
          return hit ? hit[1] : "";
        }
        for (const k of Object.keys(h)) {
          if (k.toLowerCase() === "authorization") return h[k];
        }
      }
      // Some callers build a Request() object and pass it as input.
      if (input && typeof input === "object" && input.headers instanceof Headers) {
        return input.headers.get("authorization") || "";
      }
    } catch (_) {}
    return "";
  }

  function postTemplate(opName, url, auth) {
    if (seenTemplates.has(opName)) return;
    seenTemplates.add(opName);
    try {
      window.postMessage(
        { type: TEMPLATE_TAG, operation_name: opName, url, auth },
        window.location.origin,
      );
    } catch (_) {}
  }

  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input && input.url;
    const p = origFetch.apply(this, arguments);
    if (!url || !GRAPHQL_RE.test(url)) return p;
    const m = url.match(GRAPHQL_RE);
    const opName = m ? m[1] : "unknown";
    postTemplate(opName, url, readAuthHeader(init, input));
    // Capture outbound body once — mutation detection needs variables.user_id,
    // which isn't available on the response side. Kept local to this call.
    let reqBody = null;
    if (MUTATION_OP_TO_ACTION[opName]) {
      try {
        if (init && init.body) reqBody = init.body;
        else if (input && typeof input === "object" && "body" in input) reqBody = input.body;
      } catch (_) {}
    }
    return p.then(
      (response) => {
        // Clone so the app still consumes the body normally.
        response
          .clone()
          .json()
          .then((body) => {
            postToContentScript(opName, url, body);
            // Success-gated relationship mutation emit.
            if (
              MUTATION_OP_TO_ACTION[opName] &&
              response.ok &&
              (!body || !body.errors)
            ) {
              postMutation(opName, extractTargetUserId(reqBody));
            }
          })
          .catch(() => {});
        return response;
      },
      (err) => {
        throw err;
      },
    );
  };

  function PatchedXHR() {
    const xhr = new OrigXHR();
    let url = "";
    let opName = "";
    let authHeader = "";
    let reqBody = null;
    const origOpen = xhr.open;
    const origSetHeader = xhr.setRequestHeader;
    const origSend = xhr.send;
    xhr.open = function (method, u) {
      url = u;
      const m = (u || "").match(GRAPHQL_RE);
      opName = m ? m[1] : "";
      return origOpen.apply(xhr, arguments);
    };
    xhr.setRequestHeader = function (name, value) {
      if (typeof name === "string" && name.toLowerCase() === "authorization") {
        authHeader = value;
        // Fire the template once per op as soon as we know auth. Waiting for
        // the load event would miss the capture on aborted / long-pending requests.
        if (opName) postTemplate(opName, url, authHeader);
      }
      return origSetHeader.apply(xhr, arguments);
    };
    xhr.send = function (body) {
      if (opName && MUTATION_OP_TO_ACTION[opName]) reqBody = body;
      return origSend.apply(xhr, arguments);
    };
    xhr.addEventListener("load", () => {
      if (!opName) return;
      // Belt-and-braces: template may not have fired if authorization wasn't
      // explicitly set (e.g. some Twitter endpoints rely on cookies alone).
      postTemplate(opName, url, authHeader);
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
        postToContentScript(opName, url, body);
      } catch (e) {
        // not JSON — ignore
      }
      // Success-gated relationship mutation emit.
      if (
        MUTATION_OP_TO_ACTION[opName] &&
        xhr.status >= 200 &&
        xhr.status < 300 &&
        (!body || !body.errors)
      ) {
        postMutation(opName, extractTargetUserId(reqBody));
      }
    });
    return xhr;
  }
  PatchedXHR.prototype = OrigXHR.prototype;
  window.XMLHttpRequest = PatchedXHR;
})();
