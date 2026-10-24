// xueqiu snapshot before hook: inline the dynamically-injected iconfont so icons
// survive the snapshot. The page registers @font-face for iconfont from a
// cross-origin stylesheet that SingleFile cannot resolve, so without this hook
// the font is missing from the saved page and glyphs render empty. Requires
// "--block-fonts": "false" in singlefile_args, otherwise this fetch is blocked.
const builtin_engine = "singlefile";

async function before(url, config) {
  function arrayBufferToBase64(buffer) {
    let binary = "";
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  // The @font-face rules for iconfont are injected by JS into a cross-origin
  // stylesheet that document.styleSheets cannot read. Use the known font URL.
  // NOTE: if xueqiu moves the font, update FONT_URLS.
  const FONT_URLS = [
    "https://assets.imedao.com/ugc/font/iconfont.woff2"
  ];

  for (const url of FONT_URLS) {
    try {
      const resp = await fetch(url);
      if (!resp.ok) continue;
      const buf = await resp.arrayBuffer();
      const b64 = arrayBufferToBase64(buf);
      const mime = /\.woff2\b/i.test(url) ? "font/woff2" : "font/woff";
      const style = document.createElement("style");
      style.textContent =
        '@font-face{font-family:"iconfont";src:url("data:' + mime + ";base64," + b64 +
        '") format("' + (mime === "font/woff2" ? "woff2" : "woff") + '");}';
      document.head.appendChild(style);
    } catch (e) { /* keep going */ }
  }
}
