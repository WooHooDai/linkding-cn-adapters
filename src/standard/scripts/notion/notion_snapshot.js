/**
 * Notion snapshot browser-script (runs inside SingleFile's browser process).
 *
 * builtin_engine = "singlefile" — this script runs in the real page via
 * SingleFile's --browser-script mechanism, NOT from a local file.
 *
 * Most snapshot concerns are now handled declaratively via config:
 *   - wait_elements: waits for .notion-page-content before capture
 *   - set_styles: expands scroll containers with !important and CSS vars
 *   - process_lazy_images: built-in lazy attr list handles Notion images
 *   - remove_elements: strips sidebar, topbar, help button
 *
 * This script handles what can't be done declaratively:
 *   1. Scroll through the page to trigger Notion's virtual scroll rendering.
 *      Notion only renders blocks that enter the viewport; CSS height/overflow
 *      changes alone don't trigger its IntersectionObserver-based lazy render.
 *   2. Expand collapsed "more properties" section (language-dependent text,
 *      needs DOM interaction to click the button).
 *   3. Fix referrer policy on images (runtime attribute manipulation).
 */
const builtin_engine = "singlefile";

async function before(url, config) {
  // Wait for Notion SPA to render content.
  // The framework's wait_elements also waits for .notion-page-content,
  // but we need content ready before we can expand properties.
  const waitForSelector = (sel, timeout = 30000) => new Promise((resolve) => {
    const el = document.querySelector(sel);
    if (el) return resolve(true);
    const observer = new MutationObserver(() => {
      if (document.querySelector(sel)) {
        observer.disconnect();
        resolve(true);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => { observer.disconnect(); resolve(false); }, timeout);
  });

  await waitForSelector('.notion-page-content', 30000);

  // Scroll through the entire page to trigger Notion's virtual scroll.
  // Notion renders content blocks lazily via IntersectionObserver — only
  // blocks near the viewport are in the DOM. CSS set_styles changes height
  // but doesn't fire the observer, so we must programmatically scroll.
  // Strategy: find the scroll container, step through it, wait for renders.
  const scroller = document.querySelector('.notion-scroller.vertical')
    || document.querySelector('.notion-scroller');
  if (scroller) {
    const step = Math.max(scroller.clientHeight - 100, 200);
    const total = scroller.scrollHeight;
    for (let y = 0; y < total; y += step) {
      scroller.scrollTo(0, y);
      await new Promise(r => setTimeout(r, 300));
    }
    // Final pass: scroll to bottom and back to top to catch any stragglers
    scroller.scrollTo(0, scroller.scrollHeight);
    await new Promise(r => setTimeout(r, 500));
    scroller.scrollTo(0, 0);
    await new Promise(r => setTimeout(r, 500));
  } else {
    // Fallback: use window scroll
    for (let i = 0; i < 50; i++) {
      window.scrollBy(0, window.innerHeight);
      await new Promise(r => setTimeout(r, 300));
      if ((window.innerHeight + window.scrollY) >= document.body.scrollHeight) break;
    }
    window.scrollTo(0, 0);
    await new Promise(r => setTimeout(r, 500));
  }

  // Expand collapsed "more properties" section.
  const morePropsBtn = document.querySelector(
    '.layout-content-with-divider div[role="button"][tabindex="0"]'
  );
  if (morePropsBtn) {
    const svg = morePropsBtn.querySelector('svg');
    const isCollapsed = svg && /arrowChevronSingleDown/i.test(svg.getAttribute('class') || '');
    if (isCollapsed) {
      morePropsBtn.click();
      // Wait for the property rows to render
      await new Promise(r => setTimeout(r, 1000));
    }
  }

  // Fix referrer policy for all images. Notion's same-origin referrer
  // policy can block image downloads after redirect to notionusercontent.com.
  document.querySelectorAll('img').forEach(img => {
    img.removeAttribute('referrerpolicy');
    img.setAttribute('referrerpolicy', 'no-referrer');
  });
}
