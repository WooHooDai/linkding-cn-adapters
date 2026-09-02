const builtin_engine = "singlefile";

function removeElement(element) {
  if (!element) return;
  if (typeof element.remove === "function") {
    element.remove();
  } else if (element.parentNode) {
    element.parentNode.removeChild(element);
  }
}

function preserveEngageBarLeft() {
  const root = document.querySelector("#noteContainer");
  if (!root) return;

  root
    .querySelectorAll(".interactions.engage-bar .content-edit, .interactions.engage-bar .bottom")
    .forEach(removeElement);

  root
    .querySelectorAll(".interactions.engage-bar .buttons.engage-bar-style")
    .forEach((buttons) => {
      const left = buttons.querySelector(".left");
      if (!left) return;
      buttons.replaceChildren(left);
    });
}

function preserveIconSymbols() {
  const root = document.querySelector("#noteContainer");
  if (!root) return;

  const referencedIds = new Set();
  root.querySelectorAll("use").forEach((use) => {
    const ref =
      use.getAttribute("href") ||
      use.getAttribute("xlink:href") ||
      "";
    if (ref.startsWith("#")) referencedIds.add(ref.slice(1));
  });
  if (!referencedIds.size) return;

  const sprite = document.createElement("svg");
  sprite.setAttribute("aria-hidden", "true");
  sprite.setAttribute("focusable", "false");
  sprite.style.cssText =
    "position:absolute;width:0;height:0;overflow:hidden;";

  referencedIds.forEach((id) => {
    const symbol = document.getElementById(id);
    if (!symbol || symbol.tagName.toLowerCase() !== "symbol") return;
    sprite.appendChild(symbol.cloneNode(true));
  });

  if (sprite.childElementCount) root.appendChild(sprite);
}

async function before(url, config) {
  preserveIconSymbols();
  preserveEngageBarLeft();

  const images = Array.from(document.querySelectorAll('meta[property="og:image"]'))
    .map((meta) => meta.content)
    .filter((url) => url && url.includes("sns-webpic-qc.xhscdn.com/"))
    .map((url) => url.replace(/^http:/, "https:"));
  if (!images.length) return;

  const container = document.querySelector(".xhs-slider-container:not([elementtiming])");
  if (!container) return;

  container.innerHTML = "";

  const pending = images.map((imageUrl) => {
    const img = document.createElement("img");
    img.src = imageUrl;
    container.appendChild(img);
    return img.decode().catch(() => {});
  });

  await Promise.all(pending);
}

async function after() {
  preserveEngageBarLeft();

  const videoMeta = document.querySelector('meta[property="og:video"]');
  const videoUrl =
    videoMeta && videoMeta.content.startsWith("https://")
      ? videoMeta.content
      : "";
  if (!videoUrl) return;

  const container =
    document.querySelector(".video-player-media.media-container") ||
    document.querySelector(".media-container");
  if (!container) return;

  const video = document.createElement("video");
  video.setAttribute("src", videoUrl);
  video.setAttribute("controls", "");
  video.setAttribute("playsinline", "");
  video.setAttribute("preload", "metadata");
  video.style.cssText =
    "width:100%;height:100%;object-fit:contain;background:#000;";
  container.innerHTML = "";
  container.appendChild(video);

  const meta = Array.from(document.querySelectorAll("meta")).find(
    (element) =>
      String(element.getAttribute("http-equiv") || "").toLowerCase() ===
      "content-security-policy"
  );
  if (!meta) return;

  const content = meta.getAttribute("content") || "";
  const updated = content.replace(/media-src\s+([^;]+)/i, (match, value) => {
    if (/\bhttps:\b/.test(value)) return match;
    return `media-src ${value.trim()} https:`;
  });
  if (updated !== content) {
    meta.setAttribute("content", updated);
  }
}
