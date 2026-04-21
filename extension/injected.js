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
  const origFetch = window.fetch;
  const OrigXHR = window.XMLHttpRequest;

  const GRAPHQL_RE = /\/i\/api\/graphql\/[^/]+\/([A-Za-z0-9_]+)/;

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
    return p.then(
      (response) => {
        // Clone so the app still consumes the body normally.
        response
          .clone()
          .json()
          .then((body) => postToContentScript(opName, url, body))
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
    const origOpen = xhr.open;
    const origSetHeader = xhr.setRequestHeader;
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
    xhr.addEventListener("load", () => {
      if (!opName) return;
      // Belt-and-braces: template may not have fired if authorization wasn't
      // explicitly set (e.g. some Twitter endpoints rely on cookies alone).
      postTemplate(opName, url, authHeader);
      try {
        const body = JSON.parse(xhr.responseText);
        postToContentScript(opName, url, body);
      } catch (e) {
        // not JSON — ignore
      }
    });
    return xhr;
  }
  PatchedXHR.prototype = OrigXHR.prototype;
  window.XMLHttpRequest = PatchedXHR;
})();
