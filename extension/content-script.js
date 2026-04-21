// Runs in ISOLATED world. Relays GraphQL payloads from injected.js to the
// service worker, and emits DOM-observed events: impressions (dwell),
// interactions (clicks on like/rt/reply/bookmark/profile/expand), and searches.

(() => {
  // Guard against double-injection (manifest + retroactive chrome.scripting).
  if (window.__tm_content_loaded__) return;
  window.__tm_content_loaded__ = true;

  const GRAPHQL_TAG = "__tm_graphql__";
  const TEMPLATE_TAG = "__tm_graphql_template__";

  // Dev hook: a custom DOM event on x.com pages that flips enrichmentEnabled
  // in chrome.storage.sync. Only reachable from x.com origins (matches
  // manifest host_permissions), which are first-party to the user anyway.
  window.addEventListener("__tm_dev_set_enrichment__", (ev) => {
    const on = !!(ev && ev.detail && ev.detail.on);
    try {
      chrome.storage.sync.set({ enrichmentEnabled: on });
    } catch (_) {}
  });

  // --- GraphQL relay -------------------------------------------------------
  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    const data = ev.data;
    if (!data) return;
    if (data.type === GRAPHQL_TAG) {
      send({
        type: "graphql_payload",
        operation_name: data.operation_name,
        url: data.url,
        payload: data.payload,
      });
    } else if (data.type === TEMPLATE_TAG) {
      send({
        type: "graphql_template",
        operation_name: data.operation_name,
        url: data.url,
        auth: data.auth,
      });
    }
  });

  // --- Helpers -------------------------------------------------------------
  function send(event) {
    if (!event.event_id) event.event_id = crypto.randomUUID();
    try {
      chrome.runtime.sendMessage({ kind: "event", event });
    } catch (e) {
      // service worker may be restarting — drop silently; impressions are replayable
    }
  }

  function nowIso() {
    return new Date().toISOString();
  }

  function closestTweetId(el) {
    const art = el.closest('article[data-testid="tweet"]');
    if (!art) return null;
    const link = art.querySelector('a[href*="/status/"]');
    if (!link) return null;
    const m = link.getAttribute("href").match(/\/status\/(\d+)/);
    return m ? m[1] : null;
  }

  function feedSourceFromPath() {
    const path = location.pathname;
    if (path === "/home") return "for_you"; // refined client-side with tab state later
    if (path.startsWith("/search")) return "search";
    if (path.startsWith("/i/bookmarks")) return "bookmarks";
    if (path.startsWith("/notifications")) return "notifications";
    const parts = path.split("/").filter(Boolean);
    if (parts.length >= 3 && parts[1] === "status") return "thread";
    if (parts.length === 1) return "profile";
    return "other";
  }

  // --- Impression tracking -------------------------------------------------
  const seenTweets = new WeakMap(); // article -> {id, firstSeenAt, visibleSince, totalDwell}

  const io = new IntersectionObserver(
    (entries) => {
      const now = performance.now();
      for (const e of entries) {
        const art = e.target;
        let rec = seenTweets.get(art);
        const tweet_id = rec?.id || closestTweetId(art);
        if (!tweet_id) continue;
        const visible =
          e.isIntersecting &&
          e.intersectionRatio >= 0.5 &&
          document.visibilityState === "visible";
        if (!rec) {
          rec = {
            id: tweet_id,
            firstSeenAt: nowIso(),
            visibleSince: visible ? now : 0,
            totalDwell: 0,
            feed: feedSourceFromPath(),
          };
          seenTweets.set(art, rec);
          if (visible) {
            send({
              type: "impression_start",
              tweet_id,
              first_seen_at: rec.firstSeenAt,
              feed_source: rec.feed,
            });
          }
        }
        if (visible && rec.visibleSince === 0) {
          rec.visibleSince = now;
        } else if (!visible && rec.visibleSince > 0) {
          rec.totalDwell += now - rec.visibleSince;
          rec.visibleSince = 0;
        }
      }
    },
    { threshold: [0, 0.25, 0.5, 0.75, 1.0] },
  );

  function observeTweet(art) {
    if (art.dataset.__tm_observed) return;
    art.dataset.__tm_observed = "1";
    io.observe(art);
  }

  // Flush dwell when a tweet is removed from the DOM.
  const removeObserver = new MutationObserver((muts) => {
    for (const m of muts) {
      for (const n of m.removedNodes) {
        if (n.nodeType !== 1) continue;
        const arts = n.matches?.('article[data-testid="tweet"]')
          ? [n]
          : n.querySelectorAll?.('article[data-testid="tweet"]') || [];
        for (const art of arts) {
          const rec = seenTweets.get(art);
          if (!rec) continue;
          if (rec.visibleSince > 0) {
            rec.totalDwell += performance.now() - rec.visibleSince;
            rec.visibleSince = 0;
          }
          send({
            type: "impression_end",
            tweet_id: rec.id,
            first_seen_at: rec.firstSeenAt,
            dwell_ms: Math.round(rec.totalDwell),
            feed_source: rec.feed,
          });
          io.unobserve(art);
        }
      }
      for (const n of m.addedNodes) {
        if (n.nodeType !== 1) continue;
        const arts = n.matches?.('article[data-testid="tweet"]')
          ? [n]
          : n.querySelectorAll?.('article[data-testid="tweet"]') || [];
        arts.forEach(observeTweet);
      }
    }
  });
  removeObserver.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  // Flush all in-flight dwell on tab hide / unload.
  function flushAll() {
    const nodes = document.querySelectorAll('article[data-testid="tweet"]');
    const now = performance.now();
    nodes.forEach((art) => {
      const rec = seenTweets.get(art);
      if (!rec) return;
      if (rec.visibleSince > 0) {
        rec.totalDwell += now - rec.visibleSince;
        rec.visibleSince = 0;
      }
      send({
        type: "impression_end",
        tweet_id: rec.id,
        first_seen_at: rec.firstSeenAt,
        dwell_ms: Math.round(rec.totalDwell),
        feed_source: rec.feed,
      });
      // Prevent double-flush by marking as flushed.
      rec.totalDwell = 0;
    });
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushAll();
  });
  window.addEventListener("pagehide", flushAll);

  // --- Interactions --------------------------------------------------------
  const INTERACTION_MAP = [
    ["like", '[data-testid="like"], [data-testid="unlike"]'],
    ["retweet", '[data-testid="retweet"], [data-testid="unretweet"]'],
    ["reply", '[data-testid="reply"]'],
    ["bookmark", '[data-testid="bookmark"], [data-testid="removeBookmark"]'],
    ["profile_click", 'a[data-testid^="UserAvatar"], a[data-testid="User-Name"]'],
    ["expand", '[data-testid="caret"]'],
  ];

  document.addEventListener(
    "click",
    (ev) => {
      const t = ev.target;
      if (!(t instanceof Element)) return;
      for (const [action, sel] of INTERACTION_MAP) {
        const hit = t.closest(sel);
        if (!hit) continue;
        const tweet_id = closestTweetId(hit);
        if (!tweet_id) continue;
        send({
          type: "interaction",
          action,
          tweet_id,
          timestamp: nowIso(),
        });
        break;
      }
    },
    true, // capture phase — some X elements stopPropagation on bubbles
  );

  // --- Search URL detection ------------------------------------------------
  let lastSearch = "";
  function checkSearch() {
    if (location.pathname !== "/search") {
      lastSearch = "";
      return;
    }
    const q = new URLSearchParams(location.search).get("q") || "";
    if (q && q !== lastSearch) {
      lastSearch = q;
      send({ type: "search", query: q, timestamp: nowIso() });
    }
  }
  // Monkey-patch history API to catch SPA navigation.
  const origPush = history.pushState;
  const origReplace = history.replaceState;
  history.pushState = function () {
    const r = origPush.apply(this, arguments);
    setTimeout(checkSearch, 10);
    return r;
  };
  history.replaceState = function () {
    const r = origReplace.apply(this, arguments);
    setTimeout(checkSearch, 10);
    return r;
  };
  window.addEventListener("popstate", () => setTimeout(checkSearch, 10));
  window.addEventListener("load", checkSearch);
  checkSearch();
})();
