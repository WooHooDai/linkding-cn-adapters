/**
 * Reddit snapshot after hook (runs on saved HTML via Linkedom).
 *
 * builtin_engine = "singlefile" — this script runs in the snapshot_browser_after.js
 * runner, which parses the saved HTML with Linkedom and writes it back.
 *
 * Reddit's web components re-render during the 12s browser-wait-delay and clear
 * any text/images injected into their existing elements. The before hook can
 * only preserve data by storing it as attributes on the custom elements (which
 * survive re-rendering). This after hook runs on the static saved HTML where
 * Reddit's JS can no longer interfere, and injects the missing text/images.
 *
 * What this hook does:
 *   1. Removes legacy ld-* injected elements from the old before hook.
 *   2. Injects subreddit name, author name, and post score from shreddit-post
 *      attributes into empty containers.
 *   3. Injects comment author names, scores, and avatars from shreddit-comment
 *      attributes / data attributes into empty containers.
 */

const builtin_engine = "singlefile";

async function after(url, config) {
  // 1. Remove legacy ld-* injected elements from the old before hook.
  // These may be present if the snapshot was created with an older before hook.
  document.querySelectorAll(
    "div.ld-avatar, div.ld-comment-avatar, span.ld-comment-score, " +
    "span.ld-score, a.ld-author-name, a.ld-comment-author"
  ).forEach(function (el) { el.remove(); });

  // 2. Post info
  var post = document.querySelector("shreddit-post");
  if (post) {
    injectSubredditName(post);
    injectPostAuthor(post);
    // Note: post score is NOT injected here. The shreddit-post element's
    // shadow DOM already contains a rpl-vote-button-group with a
    // <faceplate-number> that displays the score. Linkedom cannot see
    // shadow DOM content, but the browser renders it correctly from
    // the serialized <template shadowrootmode="open">.
  }

  // 3. Comment info
  document.querySelectorAll("shreddit-comment").forEach(function (comment) {
    injectCommentAuthor(comment);
    injectCommentScore(comment);
    injectCommentAvatar(comment);
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function hasVisibleText(el) {
  for (var i = 0; i < el.childNodes.length; i++) {
    var child = el.childNodes[i];
    if (child.nodeType === 3 && child.textContent.trim()) return true;
    if (child.nodeType === 1 && child.tagName !== "TEMPLATE" && hasVisibleText(child)) return true;
  }
  return false;
}

function createAuthorLink(author) {
  var a = document.createElement("a");
  a.setAttribute("href", "/user/" + author + "/");
  a.textContent = "u/" + author;
  a.setAttribute("style", "color:inherit;text-decoration:none;font-weight:bold");
  return a;
}

// ---------------------------------------------------------------------------
// Post injections
// ---------------------------------------------------------------------------

function injectSubredditName(post) {
  var subName = post.getAttribute("subreddit-prefixed-name");
  if (!subName) return;
  // The outer span.subreddit-name may exist but be empty if the inner
  // faceplate-tracker was cleared by Reddit's JS re-rendering.
  var spans = post.querySelectorAll("span.subreddit-name");
  for (var i = 0; i < spans.length; i++) {
    var span = spans[i];
    if (!hasVisibleText(span)) {
      span.textContent = subName;
    }
  }
}

function injectPostAuthor(post) {
  var author = post.getAttribute("author");
  if (!author) return;
  var slot = post.querySelector('[slot="authorName"]');
  if (!slot) return;
  if (hasVisibleText(slot)) return;

  // Try to find faceplate-hovercard first, then fall back to the slot itself.
  var hovercard = slot.querySelector("faceplate-hovercard");
  var target = hovercard || slot;
  if (hasVisibleText(target)) return;

  target.appendChild(createAuthorLink(author));
}

function injectPostScore(post) {
  var score = post.getAttribute("score");
  if (!score) return;

  // Check if there's already a visible score display.
  var existing = post.querySelector("shreddit-vote, .post-score, .ld-score");
  if (existing && hasVisibleText(existing)) return;

  // Inject into the credit-bar area, after the author name.
  var creditBar = post.querySelector("#pdp-credit-bar") ||
    post.querySelector('[slot="credit-bar"]');
  if (!creditBar) return;

  var span = document.createElement("span");
  span.className = "ld-score";
  span.textContent = score;
  span.setAttribute("style",
    "font-size:0.875rem;font-weight:700;color:var(--color-neutral-content-strong);" +
    "margin-left:4px");
  creditBar.appendChild(span);
}

// ---------------------------------------------------------------------------
// Comment injections
// ---------------------------------------------------------------------------

function injectCommentAuthor(comment) {
  var author = comment.getAttribute("author");
  if (!author) return;

  var meta = comment.querySelector(".author-name-meta");
  if (!meta) return;
  if (hasVisibleText(meta)) return;

  meta.appendChild(createAuthorLink(author));
}

function injectCommentScore(comment) {
  // Prefer the real displayed score from the JSON API (stored by before hook).
  // Fall back to the score attribute if the API fetch failed.
  var score = comment.getAttribute("data-ld-display-score");
  if (score === null || score === undefined || score === "") {
    score = comment.getAttribute("score");
  }
  if (score === null || score === undefined || score === "") return;

  // Remove any existing ld-comment-score first.
  comment.querySelectorAll(".ld-comment-score").forEach(function (el) { el.remove(); });

  // Inject into the comment action row area (below the comment body).
  // The action row is rendered by faceplate-loader[name^=CommentActionRow]
  // which is empty in saved HTML (loaded via JS at runtime).
  var actionRow = comment.querySelector(
    'faceplate-loader[name^="CommentActionRow"]'
  );
  if (!actionRow) return;

  // Build a vote button group that mimics Reddit's native UI:
  //   [upvote icon] [score] [downvote icon]
  // Uses the same SVG paths as Reddit's vote buttons.
  var group = document.createElement("div");
  group.className = "ld-comment-score";
  group.setAttribute("style",
    "display:inline-flex;align-items:center;flex-direction:row;" +
    "gap:0;cursor:auto;padding:2px 0;margin-top:2px");

  var UPVOTE_PATH = "M10 19a3.966 3.966 0 01-3.96-3.962V10.98H2.838a1.731 1.731 0 01-1.605-1.073 1.734 1.734 0 01.377-1.895L9.364.254a.925.925 0 011.272 0l7.754 7.759c.498.499.646 1.242.376 1.894-.27.652-.9 1.073-1.605 1.073h-3.202v4.058A3.965 3.965 0 019.999 19H10ZM2.989 9.179H7.84v5.731c0 1.13.81 2.163 1.934 2.278a2.163 2.163 0 002.386-2.15V9.179h4.851L10 2.163 2.989 9.179Z";
  var DOWNVOTE_PATH = "M10 1a3.966 3.966 0 013.96 3.962V9.02h3.202c.706 0 1.335.42 1.605 1.073.27.652.122 1.396-.377 1.895l-7.754 7.759a.925.925 0 01-1.272 0l-7.754-7.76a1.734 1.734 0 01-.376-1.894c.27-.652.9-1.073 1.605-1.073h3.202V4.962A3.965 3.965 0 0110 1Zm7.01 9.82h-4.85V5.09c0-1.13-.81-2.163-1.934-2.278a2.163 2.163 0 00-2.386 2.15v5.859H2.989l7.01 7.016 7.012-7.016Z";

  var upvoteIcon = makeVoteIcon(UPVOTE_PATH, "upvote");
  var scoreSpan = document.createElement("span");
  scoreSpan.setAttribute("style",
    "color:var(--color-neutral-content-weak);" +
    "font-weight:700;padding:0 2px");
  scoreSpan.textContent = score;
  var downvoteIcon = makeVoteIcon(DOWNVOTE_PATH, "downvote");

  group.appendChild(upvoteIcon);
  group.appendChild(scoreSpan);
  group.appendChild(downvoteIcon);
  actionRow.appendChild(group);
}

function makeVoteIcon(path, name) {
  var btn = document.createElement("button");
  btn.setAttribute("rpl", "");
  btn.setAttribute("aria-pressed", "false");
  btn.setAttribute("style",
    "background:transparent;border:0;padding:4px;cursor:auto;" +
    "display:inline-flex;align-items:center");
  btn.innerHTML =
    '<span class="flex mx-xs text-body-1 vote-icon-outline" ' +
    'style="color:var(--color-neutral-content-weak)">' +
    '<svg rpl="" fill="currentColor" height="16" icon-name="' + name + '" ' +
    'viewBox="0 0 20 20" width="16" ' +
    'xmlns="http://www.w3.org/2000/svg">' +
    '<path d="' + path + '" /></svg></span>';
  return btn;
}

function injectCommentAvatar(comment) {
  // The before hook fetches the avatar URL and stores the base64 data
  // as a data-ld-avatar-b64 attribute on the shreddit-comment element.
  var b64 = comment.getAttribute("data-ld-avatar-b64");
  if (!b64) return;

  var slot = comment.querySelector('[slot="commentAvatar"]');
  if (!slot) return;

  // Check if there's already an avatar image. Reddit renders avatars either as
  // a plain <img> or as an SVG <image> element inside a snoovatar wrapper, so
  // check both to avoid injecting a duplicate.
  var existingImg = slot.querySelector("img, image");
  if (existingImg) return;

  var div = document.createElement("div");
  div.className = "ld-comment-avatar";
  div.setAttribute("style",
    "display:inline-block;width:32px;height:32px;border-radius:50%;" +
    "overflow:hidden;vertical-align:middle;flex-shrink:0");

  var img = document.createElement("img");
  img.setAttribute("src", b64);
  img.setAttribute("alt", "User Avatar");
  img.setAttribute("style", "width:100%;height:100%;object-fit:cover");
  div.appendChild(img);
  slot.appendChild(div);
}
