const builtin_engine = "singlefile";

/**
 * Reddit snapshot before hook (runs inside SingleFile's browser process).
 *
 * Reddit's web components re-render during the 12s browser-wait-delay and
 * clear any text/images injected into their existing elements. Therefore, the
 * before hook does NOT inject visible text — that is handled by the after hook
 * which runs on the static saved HTML.
 *
 * What this hook does:
 *   1. Waits for shreddit-post to appear.
 *   2. Fetches comment avatar images as base64 data URIs and stores them as
 *      data-ld-avatar-b64 attributes on shreddit-comment elements. These
 *      attributes survive Reddit's re-rendering because custom element
 *      attributes are preserved by the framework.
 */

async function before(url, config) {
  await waitForSelector("shreddit-post", 10000);
  // If the comments toggle is off, shreddit-comment-tree is in remove_elements
  // and the entire comment tree will be removed during cleanup. Skip the
  // expensive score API fetch and per-comment avatar fetches entirely — these
  // are heavy HTTP operations that scale with comment count and would be
  // wasted work since the results are discarded.
  var removeElements = (config && config.remove_elements) || [];
  var commentsDisabled = removeElements.indexOf("shreddit-comment-tree") !== -1;
  if (commentsDisabled) return;

  // Wait for comments to render before reading scores/avatars.
  await waitForSelector("shreddit-comment[author]", 10000);
  // Give Reddit's JS a moment to hydrate comment scores.
  await sleep(2000);
  // Run concurrently; each task enforces its own per-request timeout so a
  // single stuck image/API request cannot block the entire before hook.
  await Promise.allSettled([fetchCommentScores(url), fetchCommentAvatars()]);
}

function sleep(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

/**
 * Fetch comment avatar images and store as base64 data attributes.
 * Uses canvas toDataURL to convert images to base64.
 * Falls back to storing the original URL if CORS prevents canvas export.
 */
async function fetchCommentAvatars() {
  var comments = document.querySelectorAll("shreddit-comment");
  var promises = [];

  comments.forEach(function (comment) {
    var avatarUrl = comment.getAttribute("avatar");
    if (!avatarUrl) return;
    if (comment.getAttribute("data-ld-avatar-b64") || comment.getAttribute("data-ld-avatar-url")) return;

    promises.push(fetchImageAsBase64(avatarUrl).then(function (b64) {
      if (b64) {
        comment.setAttribute("data-ld-avatar-b64", b64);
      } else {
        comment.setAttribute("data-ld-avatar-url", avatarUrl);
      }
    }).catch(function () {
      comment.setAttribute("data-ld-avatar-url", avatarUrl);
    }));
  });

  if (promises.length) {
    await Promise.allSettled(promises);
  }

  // Re-scan in case new comments loaded during fetch.
  var newComments = document.querySelectorAll(
    "shreddit-comment:not([data-ld-avatar-b64]):not([data-ld-avatar-url])"
  );
  if (newComments.length > 0) {
    var morePromises = [];
    newComments.forEach(function (comment) {
      var avatarUrl = comment.getAttribute("avatar");
      if (!avatarUrl) return;
      morePromises.push(fetchImageAsBase64(avatarUrl).then(function (b64) {
        if (b64) {
          comment.setAttribute("data-ld-avatar-b64", b64);
        } else {
          comment.setAttribute("data-ld-avatar-url", avatarUrl);
        }
      }).catch(function () {
        comment.setAttribute("data-ld-avatar-url", avatarUrl);
      }));
    });
    if (morePromises.length) {
      await Promise.allSettled(morePromises);
    }
  }
}

/**
 * Fetch real comment scores from Reddit's JSON API.
 * The score attribute on shreddit-comment includes the commenter's own
 * auto-upvote (so it's always >= 1), but the displayed score can be 0.
 * We fetch the actual score from the API and store it as
 * data-ld-display-score for the after hook to use.
 */
async function fetchCommentScores(pageUrl) {
  // Strategy 1: Try Reddit's JSON API for real scores.
  // The score attribute on shreddit-comment is the SSR value which includes
  // the commenter's own auto-upvote (always >= 1). The actual displayed
  // score can be 0 if no one else upvoted.
  var jsonUrl = pageUrl.replace(/\/$/, "") + ".json?raw_json=1";
  var apiScores = null;
  try {
    var controller = new AbortController();
    var fetchTimer = setTimeout(function () { controller.abort(); }, 15000);
    var resp = await fetch(jsonUrl, {
      credentials: "same-origin",
      signal: controller.signal,
    });
    clearTimeout(fetchTimer);
    if (resp.ok) {
      var data = await resp.json();
      apiScores = {};
      collectCommentScores(data, apiScores);
    }
  } catch (e) {
    // CORS/rate-limit — fall through to DOM-based approach.
  }

  // Strategy 2: Read scores from the live DOM.
  // After hydration, Reddit may update the score attribute, or the
  // CommentActionRow may have rendered the real score in the DOM.
  var els = document.querySelectorAll("shreddit-comment");
  els.forEach(function (el) {
    var thingid = el.getAttribute("thingid");

    // Prefer API score.
    if (apiScores && thingid && apiScores[thingid] !== undefined) {
      el.setAttribute("data-ld-display-score", String(apiScores[thingid]));
      return;
    }

    // Fall back to the current DOM score attribute (may have been
    // updated by Reddit's JS after hydration).
    var liveScore = el.getAttribute("score");
    if (liveScore !== null) {
      el.setAttribute("data-ld-display-score", liveScore);
    }
  });
}

function collectCommentScores(node, out) {
  if (!node || typeof node !== "object") return;
  if (node.kind === "t1" && node.data && node.data.id) {
    out[node.data.id] = node.data.score;
    if (node.data.replies && typeof node.data.replies === "object") {
      var children = node.data.replies.data && node.data.replies.data.children;
      if (Array.isArray(children)) {
        children.forEach(function (c) { collectCommentScores(c, out); });
      }
    }
  } else if (Array.isArray(node)) {
    node.forEach(function (c) { collectCommentScores(c, out); });
  }
}

function fetchImageAsBase64(url) {
  return new Promise(function (resolve) {
    var decodedUrl = url.replace(/&amp;/g, "&");
    var img = new Image();
    img.crossOrigin = "anonymous";
    // Timeout: if the image hasn't loaded in 8s, give up so a single stuck
    // request cannot block the entire before hook (and exhaust the snapshot
    // timeout). Reddit CDN rate-limiting or network hiccups can stall loads
    // indefinitely without ever firing onerror.
    var timer = setTimeout(function () {
      img.onload = img.onerror = null;
      img.src = "";
      resolve(null);
    }, 8000);
    img.onload = function () {
      clearTimeout(timer);
      try {
        var canvas = document.createElement("canvas");
        canvas.width = img.naturalWidth || 64;
        canvas.height = img.naturalHeight || 64;
        var ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL("image/png"));
      } catch (e) {
        resolve(null);
      }
    };
    img.onerror = function () { clearTimeout(timer); resolve(null); };
    img.src = decodedUrl;
  });
}

function waitForSelector(selector, timeoutMs) {
  return new Promise(function (resolve) {
    if (document.querySelector(selector)) return resolve();
    var observer = new MutationObserver(function () {
      if (document.querySelector(selector)) {
        observer.disconnect();
        resolve();
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(function () { observer.disconnect(); resolve(); }, timeoutMs);
  });
}
