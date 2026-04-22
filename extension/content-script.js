// Runs in ISOLATED world. Pure DOM-only capture: no page-global mutation,
// no fetch/XHR patching, no outbound Twitter traffic. Emits impressions
// (dwell), dom_tweet (synthesized from the rendered DOM when a tweet first
// becomes visible), interactions (like/rt/reply/bookmark/profile/expand),
// searches, link_clicks, media opens, text selections, scroll bursts, nav
// changes, and relationship changes (observed via DOM state flip).

(() => {
  if (window.__tm_content_loaded__) return;
  window.__tm_content_loaded__ = true;

  const SELECTION_DEBOUNCE_MS = 1000;
  const SELECTION_MAX_CHARS = 500;
  const SCROLL_QUIESCENT_MS = 1500;
  const SCROLL_REVERSAL_THRESHOLD_PX = 400;
  const LINK_FALLBACK_KEY = "__tm_pending_link__";
  const NAV_POLL_MS = 250;
  const RELATIONSHIP_CONFIRM_MS = 2000;

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

  // Tweet text is composed of text nodes + inline <img alt="..."> for custom
  // emoji. innerText drops the alts; walk nodes to preserve them.
  function tweetTextFromNode(root) {
    if (!root) return null;
    let out = "";
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (n.nodeType === 3) {
        out += n.nodeValue;
      } else if (n.nodeType === 1) {
        if (n.tagName === "IMG" && n.alt) out += n.alt;
        else if (n.tagName === "BR") out += "\n";
      }
    }
    return out.trim() || null;
  }

  // Twitter's aria-labels embed exact integer counts, e.g. "1,234 Likes. Like".
  // Abbreviated display text ("1.2K") would lose precision; always prefer aria.
  function countFromAria(el) {
    if (!el) return null;
    const label = el.getAttribute("aria-label") || "";
    const m = label.match(/([\d,]+)/);
    if (!m) return null;
    const n = parseInt(m[1].replace(/,/g, ""), 10);
    return Number.isFinite(n) ? n : null;
  }

  function extractAuthorFromArticle(art) {
    // [data-testid="User-Name"] holds both display name and @handle links.
    // The handle link's href is "/handle" (single path segment, no /status).
    const userNameBlock = art.querySelector('[data-testid="User-Name"]');
    let handle = null;
    let displayName = null;
    if (userNameBlock) {
      const anchors = userNameBlock.querySelectorAll('a[role="link"]');
      for (const a of anchors) {
        const href = a.getAttribute("href") || "";
        const m = href.match(/^\/([A-Za-z0-9_]{1,15})$/);
        if (m) {
          handle = m[1];
          break;
        }
      }
      // Display name — the first span group inside the User-Name block that's
      // not the @handle.
      const firstSpan = userNameBlock.querySelector("span");
      if (firstSpan) displayName = (firstSpan.innerText || "").trim() || null;
    }
    return { handle, displayName };
  }

  // Parse a numeric token from Twitter's aria-labels. Handles exact integers
  // ("12,345"), abbreviations ("12.3K", "1.4M", "2B"), and plain digits.
  function parseCount(token) {
    if (!token) return null;
    const s = token.replace(/,/g, "").trim();
    const m = s.match(/^(\d+(?:\.\d+)?)([KMB])?$/i);
    if (!m) {
      const plain = parseInt(s, 10);
      return Number.isFinite(plain) ? plain : null;
    }
    const base = parseFloat(m[1]);
    const suf = (m[2] || "").toUpperCase();
    const mult = suf === "K" ? 1000 : suf === "M" ? 1_000_000 : suf === "B" ? 1_000_000_000 : 1;
    return Math.round(base * mult);
  }

  // View count lives in one of three places on Twitter/X's current UI:
  // 1. A dedicated analytics link `a[href*="/analytics"]` (author view).
  // 2. The action-bar `role="group"` aria-label that sometimes concatenates all
  //    engagement: "N replies, N reposts, N likes, N views".
  // 3. An `aria-label` on a standalone element ending in "Views" / "View" —
  //    Twitter often emits `aria-label="12.3K Views"` on the view-count chip.
  function extractViewCount(art) {
    const analyticsLink = art.querySelector('a[href*="/analytics"]');
    if (analyticsLink) {
      const label = analyticsLink.getAttribute("aria-label") || "";
      const m = label.match(/([\d.,]+[KMB]?)\s*[Vv]iew/);
      if (m) {
        const n = parseCount(m[1]);
        if (n !== null) return n;
      }
    }
    const group = art.querySelector('[role="group"][aria-label]');
    if (group) {
      const label = group.getAttribute("aria-label") || "";
      const m = label.match(/([\d.,]+[KMB]?)\s*[Vv]iew/);
      if (m) {
        const n = parseCount(m[1]);
        if (n !== null) return n;
      }
    }
    // Last-ditch scan: any descendant whose aria-label ends in "Views".
    const all = art.querySelectorAll("[aria-label]");
    for (const el of all) {
      const label = el.getAttribute("aria-label") || "";
      const m = label.match(/^([\d.,]+[KMB]?)\s*[Vv]iews?$/);
      if (m) {
        const n = parseCount(m[1]);
        if (n !== null) return n;
      }
    }
    return null;
  }

  function extractMediaFromArticle(art) {
    const media = [];
    art.querySelectorAll('[data-testid="tweetPhoto"] img[src]').forEach((img) => {
      const src = img.getAttribute("src");
      if (src && src.includes("pbs.twimg.com")) media.push({ type: "image", url: src });
    });
    art.querySelectorAll("video").forEach((v) => {
      const poster = v.getAttribute("poster");
      if (poster) media.push({ type: "video", url: poster });
    });
    return media.length ? media : null;
  }

  // Emit a dom_tweet event carrying everything we can pull off the rendered
  // article. Safe to call multiple times per tweet — backend upsert is idempotent.
  function emitDomTweet(art) {
    const tweet_id = closestTweetId(art);
    if (!tweet_id) return;
    const { handle, displayName } = extractAuthorFromArticle(art);
    if (!handle) return; // backend requires author_handle
    const timeEl = art.querySelector("time[datetime]");
    const createdAt = timeEl ? timeEl.getAttribute("datetime") : null;
    const textEl = art.querySelector('[data-testid="tweetText"]');
    const text = tweetTextFromNode(textEl);
    const likes = countFromAria(art.querySelector('[data-testid="like"], [data-testid="unlike"]'));
    const retweets = countFromAria(art.querySelector('[data-testid="retweet"], [data-testid="unretweet"]'));
    const replies = countFromAria(art.querySelector('[data-testid="reply"]'));
    const views = extractViewCount(art);
    const media = extractMediaFromArticle(art);
    // Quoted tweet: a nested article inside this one (Twitter wraps quoted
    // tweets in a role="link" container containing their own tweetText).
    let quoted_tweet_id = null;
    const quoted = art.querySelector('[role="link"] article, div[role="link"] [data-testid="tweetText"]');
    if (quoted) {
      const qLink = quoted.closest("a[href*='/status/']") ||
                    art.querySelectorAll("a[href*='/status/']")[1];
      if (qLink) {
        const qm = (qLink.getAttribute("href") || "").match(/\/status\/(\d+)/);
        if (qm && qm[1] !== tweet_id) quoted_tweet_id = qm[1];
      }
    }
    // Retweet socialContext indicator: "<user> reposted" above the article.
    const social = art.querySelector('[data-testid="socialContext"]');
    const is_retweet = !!(social && /reposted|retweeted/i.test(social.textContent || ""));
    send({
      type: "dom_tweet",
      tweet_id,
      author_handle: handle,
      author_display: displayName,
      text,
      created_at_iso: createdAt,
      like_count: likes,
      retweet_count: retweets,
      reply_count: replies,
      view_count: views,
      media_json: media ? JSON.stringify(media) : null,
      quoted_tweet_id,
      is_retweet,
      timestamp: nowIso(),
    });
  }

  function feedSourceFromPath(path) {
    path = path || location.pathname;
    if (path === "/home") return "for_you"; // refined client-side with tab state later
    if (path.startsWith("/search")) return "search";
    if (path.startsWith("/i/bookmarks")) return "bookmarks";
    if (path.startsWith("/notifications")) return "notifications";
    const parts = path.split("/").filter(Boolean);
    if (parts.length >= 3 && parts[1] === "status") return "thread";
    if (parts.length === 1) return "profile";
    return "other";
  }

  function domainOf(url) {
    try {
      return new URL(url, location.href).hostname || null;
    } catch (_) {
      return null;
    }
  }

  // Classify a link as internal_tweet / internal_profile / hashtag / mention /
  // external. Used so agents can distinguish "read another tweet" from
  // "left the platform entirely to research something".
  function classifyLink(href) {
    let u;
    try {
      u = new URL(href, location.href);
    } catch (_) {
      return "external";
    }
    const host = u.hostname;
    const isX = host === "x.com" || host === "twitter.com" ||
                host.endsWith(".x.com") || host.endsWith(".twitter.com");
    if (!isX) return "external";
    const p = u.pathname || "/";
    if (/\/status\/\d+/.test(p)) return "internal_tweet";
    if (p.startsWith("/hashtag/")) return "hashtag";
    if (p.startsWith("/search")) {
      // Mentions and hashtags land in /search?q=... as well — classify by q prefix.
      const q = u.searchParams.get("q") || "";
      if (q.startsWith("@")) return "mention";
      if (q.startsWith("#")) return "hashtag";
      return "internal_tweet"; // search result link — treated as internal
    }
    const parts = p.split("/").filter(Boolean);
    if (parts.length === 1) return "internal_profile";
    return "internal_tweet";
  }

  function modifiersFromEvent(ev) {
    const m = [];
    if (ev.shiftKey) m.push("shift");
    if (ev.ctrlKey) m.push("ctrl");
    if (ev.metaKey) m.push("meta");
    if (ev.altKey) m.push("alt");
    if (ev.button === 1) m.push("middle");
    return m.join(",");
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
            domEmitted: false,
          };
          seenTweets.set(art, rec);
          // Tweet is in the DOM as soon as we observe it — emit the DOM
          // synthesis immediately, regardless of first-frame visibility.
          emitDomTweet(art);
          rec.domEmitted = true;
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
    // v2 buffered state: close any open scroll burst, fire any debounced
    // selection, and drain sessionStorage link fallback.
    if (typeof closeBurst === "function") closeBurst();
    if (selectionTimer) {
      clearTimeout(selectionTimer);
      selectionTimer = null;
      emitSelection("select");
    }
    drainPendingLinks();
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

  // Shared click listener: first checks for a link (link_click event with
  // sessionStorage fallback for same-tab navigation), then falls through to
  // the interaction selector map. Single listener, capture phase, no
  // preventDefault / stopPropagation — pure observation.
  function handleLinkClick(ev, linkEl) {
    const href = linkEl.getAttribute("href") || "";
    if (!href || href.startsWith("javascript:") || href.startsWith("#")) return;
    const absolute = new URL(href, location.href).toString();
    const tweet_id = closestTweetId(linkEl); // may be null (click outside any article)
    const link_kind = classifyLink(absolute);
    const modifiers = modifiersFromEvent(ev);
    const event_id = crypto.randomUUID();
    const payload = {
      type: "link_click",
      event_id,
      tweet_id,
      url: absolute,
      domain: domainOf(absolute),
      link_kind,
      modifiers,
      timestamp: nowIso(),
    };
    // Same-tab navigations race chrome.runtime.sendMessage. Stash a copy in
    // sessionStorage *before* the async send — drained on next content-script
    // load or pagehide. Dedup by event_id at the backend keeps retries safe.
    const sameTab = link_kind === "external" &&
                    !modifiers.includes("middle") &&
                    !modifiers.includes("meta") &&
                    !modifiers.includes("ctrl") &&
                    linkEl.target !== "_blank";
    if (sameTab) {
      try {
        const pending = JSON.parse(sessionStorage.getItem(LINK_FALLBACK_KEY) || "[]");
        pending.push(payload);
        sessionStorage.setItem(LINK_FALLBACK_KEY, JSON.stringify(pending.slice(-50)));
      } catch (_) {}
    }
    send(payload);
  }

  function drainPendingLinks() {
    let pending;
    try {
      pending = JSON.parse(sessionStorage.getItem(LINK_FALLBACK_KEY) || "[]");
    } catch (_) {
      pending = [];
    }
    if (!pending.length) return;
    try {
      sessionStorage.removeItem(LINK_FALLBACK_KEY);
    } catch (_) {}
    for (const ev of pending) send(ev); // event_id preserved → backend dedup
  }

  document.addEventListener(
    "click",
    (ev) => {
      const t = ev.target;
      if (!(t instanceof Element)) return;
      // 1) Link click? (Must run before interaction map — e.g. user-name is
      // both an anchor and a profile_click. Link_click wins because it carries
      // strictly more information; profile_click remains derivable from URL.)
      const link = t.closest("a[href]");
      if (link) handleLinkClick(ev, link);
      // 2) Interaction selectors (like / retweet / reply / ...).
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
  // Middle-click emits `auxclick`, not `click`. Capture those too so
  // "open in new tab via middle button" is recorded.
  document.addEventListener(
    "auxclick",
    (ev) => {
      if (ev.button !== 1) return;
      const t = ev.target;
      if (!(t instanceof Element)) return;
      const link = t.closest("a[href]");
      if (link) handleLinkClick(ev, link);
    },
    true,
  );

  // --- Text selections -----------------------------------------------------
  let selectionTimer = null;
  let lastEmittedSelection = "";

  function currentSelectionTweetAndText() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return null;
    const text = sel.toString();
    if (!text || text.trim().length < 10) return null;
    const anchor = sel.anchorNode;
    if (!anchor) return null;
    const el = anchor.nodeType === 1 ? anchor : anchor.parentElement;
    if (!el) return null;
    const article = el.closest('article[data-testid="tweet"]');
    if (!article) return null;
    const link = article.querySelector('a[href*="/status/"]');
    let tweet_id = null;
    if (link) {
      const m = (link.getAttribute("href") || "").match(/\/status\/(\d+)/);
      tweet_id = m ? m[1] : null;
    }
    return { tweet_id, text: text.slice(0, SELECTION_MAX_CHARS) };
  }

  function emitSelection(via) {
    const data = currentSelectionTweetAndText();
    if (!data) return;
    if (data.text === lastEmittedSelection) return;
    lastEmittedSelection = data.text;
    send({
      type: "text_selection",
      tweet_id: data.tweet_id,
      text: data.text,
      via,
      timestamp: nowIso(),
    });
  }

  document.addEventListener("selectionchange", () => {
    if (selectionTimer) clearTimeout(selectionTimer);
    selectionTimer = setTimeout(() => {
      selectionTimer = null;
      emitSelection("select");
    }, SELECTION_DEBOUNCE_MS);
  });
  // Copy is the higher-signal event; emit immediately (and dedup against
  // a pending selectionchange via lastEmittedSelection).
  document.addEventListener("copy", () => {
    if (selectionTimer) {
      clearTimeout(selectionTimer);
      selectionTimer = null;
    }
    emitSelection("copy");
  });

  // --- Scroll bursts -------------------------------------------------------
  // Event-driven aggregation (NOT 1 Hz sampling). One row per burst, capturing
  // start/end y, duration, and direction reversals. See plan §Performance.
  let burst = null;
  let burstTimer = null;

  function closeBurst() {
    if (!burst) return;
    const b = burst;
    burst = null;
    if (burstTimer) {
      clearTimeout(burstTimer);
      burstTimer = null;
    }
    // Only emit if there was meaningful displacement.
    if (Math.abs(b.endY - b.startY) < 50) return;
    send({
      type: "scroll_burst",
      feed_source: b.feed,
      started_at: new Date(b.startT).toISOString(),
      ended_at: new Date(b.endT).toISOString(),
      duration_ms: Math.max(0, Math.round(b.endT - b.startT)),
      start_y: b.startY,
      end_y: b.endY,
      delta_y: b.endY - b.startY,
      reversals_count: b.reversals,
    });
  }

  window.addEventListener(
    "scroll",
    () => {
      const y = window.scrollY;
      const now = Date.now();
      if (!burst) {
        burst = {
          startY: y,
          endY: y,
          startT: now,
          endT: now,
          lastY: y,
          dir: 0,
          reversals: 0,
          feed: feedSourceFromPath(),
        };
      } else {
        const dy = y - burst.lastY;
        const d = dy === 0 ? burst.dir : (dy > 0 ? 1 : -1);
        if (burst.dir !== 0 && d !== 0 && d !== burst.dir) {
          burst.reversals += 1;
          // Emit early on a large reversal — treat scroll-back as a distinct burst.
          if (Math.abs(y - burst.startY) >= SCROLL_REVERSAL_THRESHOLD_PX) {
            burst.endY = y;
            burst.endT = now;
            closeBurst();
            return;
          }
        }
        burst.dir = d || burst.dir;
        burst.lastY = y;
        burst.endY = y;
        burst.endT = now;
      }
      if (burstTimer) clearTimeout(burstTimer);
      burstTimer = setTimeout(closeBurst, SCROLL_QUIESCENT_MS);
    },
    { passive: true },
  );

  // --- Search + nav + media (SPA URL observation) --------------------------
  // One handler, zero new listeners on X's DOM. Piggybacks on the history API
  // patch below to catch SPA navigation. Three event types derived from the
  // path change: search (existing), nav_change (new), media_open (new).
  let lastSearch = "";
  let lastPath = location.pathname + location.search;
  const MEDIA_RE = /^\/[^/]+\/status\/(\d+)\/(photo|video)\/(\d+)/;

  function checkSearchAndNav() {
    const pathBefore = lastPath;
    const newPath = location.pathname + location.search;
    // 1) Search queries (existing behavior, preserved verbatim).
    if (location.pathname !== "/search") {
      lastSearch = "";
    } else {
      const q = new URLSearchParams(location.search).get("q") || "";
      if (q && q !== lastSearch) {
        lastSearch = q;
        send({ type: "search", query: q, timestamp: nowIso() });
      }
    }
    // 2) Nav changes — any path change within X.
    if (newPath !== pathBefore) {
      // Parse the non-query path for feed_source classification.
      const feedBefore = feedSourceFromPath(pathBefore.split("?")[0]);
      const feedAfter = feedSourceFromPath(location.pathname);
      send({
        type: "nav_change",
        from_path: pathBefore,
        to_path: newPath,
        feed_source_before: feedBefore,
        feed_source_after: feedAfter,
        timestamp: nowIso(),
      });
      lastPath = newPath;
    }
    // 3) Media lightbox opens — /status/{id}/photo|video/{n}.
    const m = location.pathname.match(MEDIA_RE);
    if (m) {
      send({
        type: "media_open",
        tweet_id: m[1],
        media_kind: m[2] === "photo" ? "image" : "video",
        media_index: parseInt(m[3], 10),
        timestamp: nowIso(),
      });
    }
  }
  // Isolated-world SPA nav detection: polling location.href every 250ms is
  // cheaper than monkey-patching history.pushState and leaves zero page-global
  // side effects. checkSearchAndNav is a no-op when the path hasn't changed.
  window.addEventListener("popstate", () => setTimeout(checkSearchAndNav, 10));
  window.addEventListener("load", () => {
    drainPendingLinks();
    checkSearchAndNav();
  });
  setInterval(checkSearchAndNav, NAV_POLL_MS);
  drainPendingLinks();
  checkSearchAndNav();

  // --- Relationship changes (follow / unfollow / mute / block) -------------
  // Twitter follow buttons carry data-testid of the form `{user_id}-follow`
  // or `{user_id}-unfollow`. On click we start a short-lived MutationObserver
  // on the button and emit only if the label flips within ~2s — that confirms
  // the server-side accepted the action (and filters out double-clicks /
  // rate-limited attempts).
  const FOLLOW_TESTID_RE = /^(\d+)-(follow|unfollow)$/;

  function findFollowButton(el) {
    let cur = el;
    for (let i = 0; i < 6 && cur; i++) {
      if (cur.dataset && cur.dataset.testid) {
        const m = cur.dataset.testid.match(FOLLOW_TESTID_RE);
        if (m) return { btn: cur, user_id: m[1], action: m[2] };
      }
      cur = cur.parentElement;
    }
    return null;
  }

  function watchButtonFlip(btn, initialLabel, action, user_id) {
    const start = Date.now();
    const obs = new MutationObserver(() => {
      const current = (btn.textContent || "").trim();
      if (current && current !== initialLabel) {
        obs.disconnect();
        // Label flipped: Follow→Following (confirmed follow),
        // Following→Follow (confirmed unfollow). Action reflects the click.
        send({
          type: "relationship_change",
          action,
          target_user_id: user_id,
          timestamp: nowIso(),
        });
      } else if (Date.now() - start > RELATIONSHIP_CONFIRM_MS) {
        obs.disconnect();
      }
    });
    obs.observe(btn, { childList: true, characterData: true, subtree: true });
    // Hard timeout — MutationObserver won't fire if nothing changes.
    setTimeout(() => obs.disconnect(), RELATIONSHIP_CONFIRM_MS + 50);
  }

  document.addEventListener(
    "click",
    (ev) => {
      const t = ev.target;
      if (!(t instanceof Element)) return;
      const hit = findFollowButton(t);
      if (!hit) return;
      const initial = (hit.btn.textContent || "").trim();
      watchButtonFlip(hit.btn, initial, hit.action, hit.user_id);
    },
    true,
  );
})();
