/**
 * Feishu snapshot browser-script (runs inside SingleFile's browser process).
 *
 * builtin_engine = "singlefile" — this script runs in the real page via
 * SingleFile's --browser-script mechanism, NOT from a local file.
 *
 * Feishu docx pages use React virtual scrolling: only blocks near the
 * current scroll viewport exist in the DOM. Blocks that scroll out of view
 * are unmounted and replaced by placeholder divs
 * (.bear-virtual-renderUnit-placeholder). A single page can have 6000+ blocks
 * and 350K+ pixels of content, but at any given time only ~20-60 blocks
 * are actually rendered.
 *
 * STRATEGY: Scroll-and-collect.
 *   1. Wait for the page content container to appear.
 *   2. Scroll through the entire .bear-web-x-container (the native scroll
 *      container with overflow:hidden scroll). At each position, clone
 *      visible blocks (identified by data-block-id) into a Map.
 *   3. For blocks with unloaded images (src="data:,"), poll up to 3s for
 *      the image to load (src changes to "blob:..."), then clone.
 *      A second scroll pass re-collects blocks that were still loading.
 *   4. After scrolling, replace the render-unit-wrapper's children with
 *      collected blocks sorted by numeric block-id.
 *   5. Convert blob: URLs to data: URLs (Feishu uses blob URLs for images;
 *      SingleFile cannot serialize them, so they become empty data:,).
 *   6. Fix table inline styles (React sets width:2px as placeholder).
 *   7. Expand all containers (height:auto, overflow:visible) for capture.
 *   8. Fix #mainContainer position:absolute → static so body gets full height.
 *   9. Show the TOC (catalogue-container height:auto).
 *
 * IMPORTANT: Do NOT change display:flex → display:block on parent containers.
 * The native flex layout distributes height correctly to .bear-web-x-container
 * (656px in 720px viewport), which is needed for scrolling. Changing display
 * causes the container to lose its height, making scroll impossible.
 *
 * set_styles (from cleanup config) runs AFTER this before hook. It will
 * apply the final height:auto + overflow:visible styles, which is fine
 * because by then all content is already in the DOM.
 */
const builtin_engine = "singlefile";

async function before(url, config) {
  // Wait for the main content container to appear.
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

  await waitForSelector('.render-unit-wrapper, .page-block.root-block', 30000);

  const scroller = document.querySelector('.bear-web-x-container');
  if (!scroller) {
    // Fallback: simple window scroll
    for (let i = 0; i < 200; i++) {
      window.scrollBy(0, window.innerHeight);
      await new Promise(r => setTimeout(r, 400));
      if ((window.innerHeight + window.scrollY) >= document.body.scrollHeight) break;
    }
    window.scrollTo(0, 0);
    await new Promise(r => setTimeout(r, 500));
    return;
  }

  // Phase 1: Scroll through the entire page and collect blocks.
  // Feishu's virtual scroll unmounts blocks that leave the viewport,
  // so we must clone each block before it gets recycled.
  scroller.scrollTo(0, 0);
  await new Promise(r => setTimeout(r, 500));

  /** @type {Map<string, HTMLElement>} block-id → cloned element */
  const collectedBlocks = new Map();
  let totalScroll = scroller.scrollHeight;
  // Step size: clientHeight ensures full viewport coverage.
  // The virtual scroll renders blocks within ~2x viewport height.
  const step = Math.max(scroller.clientHeight - 50, 300);
  // Use a smaller step (2/3 viewport) to ensure overlap between scroll
  // positions — virtual scroll may only render blocks within ~1x viewport
  // height, so stepping by full clientHeight can skip boundary blocks.
  const effectiveStep = Math.max(Math.floor(scroller.clientHeight * 2 / 3), 200);
  const scrollDelay = 60; // ms — enough for React to render (2-3 frames)

  // Helper: check if a block has images that haven't loaded yet.
  // Unloaded state: src is "data:," (empty placeholder, ~6 bytes).
  // Loaded state: src is "blob:..." (Feishu uses blob URLs for loaded images).
  const hasUnloadedImages = (el) => {
    const imgs = el.querySelectorAll('img');
    for (const img of imgs) {
      if (img.src.startsWith('data:,')) return true;
    }
    return false;
  };

  // Helper: convert blob: URLs to data: URLs in-place on the live DOM.
  // Must be called BEFORE cloneNode — cloned img elements lose their
  // blob: URL references (cloneNode serializes src to the string "data:,").
  //
  // Feishu images use progressive loading: a low-res thumbnail loads first,
  // then the full-res image replaces it. Both use blob: URLs. If we fetch
  // the blob too early, we get the low-res version.
  //
  // Strategy:
  //   1. Wait for img.naturalWidth to stabilize (no change for 400ms).
  //   2. Fetch the blob and convert to data: URL via FileReader.
  //   3. If the resulting naturalWidth is suspiciously small (< 400px),
  //      try the canvas approach — drawImage captures whatever bitmap
  //      the <img> element currently has loaded, which may be higher-res.
  //   4. As a last resort, try to find the original image URL from
  //      PerformanceResourceTiming and fetch that directly.
  const convertBlobImages = async (el) => {
    const blobImgs = Array.from(el.querySelectorAll('img[src^="blob:"]'));
    for (const img of blobImgs) {
      // Wait for naturalWidth to stabilize (progressive loading may
      // replace the blob with a higher-res version).
      let lastW = 0;
      let stable = 0;
      for (let i = 0; i < 25; i++) { // 25 × 200ms = 5s max
        const w = img.naturalWidth;
        if (w > 0 && w === lastW) {
          stable++;
          if (stable >= 2) break; // stable for 400ms
        } else {
          stable = 0;
          lastW = w;
        }
        await new Promise(r => setTimeout(r, 200));
      }

      let converted = false;

      // Method 1: fetch blob + FileReader
      try {
        const response = await fetch(img.src);
        const blob = await response.blob();
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onloadend = () => resolve(reader.result);
          reader.onerror = reject;
          reader.readAsDataURL(blob);
        });

        // Check if the converted image is suspiciously small
        // by loading it into a temporary Image
        const tempImg = new Image();
        await new Promise((resolve, reject) => {
          tempImg.onload = resolve;
          tempImg.onerror = reject;
          tempImg.src = dataUrl;
        });

        if (tempImg.naturalWidth >= 400 || tempImg.naturalWidth >= img.naturalWidth) {
          img.src = dataUrl;
          converted = true;
        }
      } catch (e) {
        // Fall through to Method 2
      }

      // Method 2: canvas.drawImage — captures the full bitmap
      // currently loaded in the <img> element.
      if (!converted) {
        try {
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth || img.width;
          canvas.height = img.naturalHeight || img.height;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0);
          const canvasUrl = canvas.toDataURL('image/png');
          img.src = canvasUrl;
          converted = true;
        } catch (e2) {
          // CORS-tainted canvas — can't use toDataURL
        }
      }

      // Method 3: Find original image URL from PerformanceResourceTiming
      // and fetch it directly. This works when the blob is a low-res
      // thumbnail but the original fetch URL returns full-res.
      if (!converted) {
        try {
          const entries = performance.getEntriesByType('resource');
          // Look for image download URLs from Feishu's internal API
          const imgEntry = entries.find(e =>
            e.initiatorType === 'img' &&
            e.name.includes('download') &&
            e.name.includes('feishu')
          );
          if (imgEntry) {
            const response = await fetch(imgEntry.name, { credentials: 'include' });
            const blob = await response.blob();
            const dataUrl = await new Promise((resolve, reject) => {
              const reader = new FileReader();
              reader.onloadend = () => resolve(reader.result);
              reader.onerror = reject;
              reader.readAsDataURL(blob);
            });
            img.src = dataUrl;
            converted = true;
          }
        } catch (e3) {
          // All methods failed — leave image as-is
        }
     }
   }
 };

  // Helper: convert images that are still at src="data:," (never loaded).
  // These images have an image-token attribute on a parent element.
  // We construct the Feishu internal API download URL from the token
  // and fetch the image directly.
  const convertUnloadedImages = async (el) => {
    const emptyImgs = Array.from(el.querySelectorAll('img[src^="data:,"]'));
    for (const img of emptyImgs) {
      // Find the image-token from the parent .image-block element
      const imageBlock = img.closest('[image-token]');
      if (!imageBlock) continue;
      const token = imageBlock.getAttribute('image-token');
      if (!token) continue;

      try {
        // Feishu internal API for image download
        const apiUrl = `/space/api/box/stream/download/asynccode/?code=${token}&preview_type=1`;
        const response = await fetch(apiUrl, { credentials: 'include' });
        if (!response.ok) continue;
        const blob = await response.blob();
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onloadend = () => resolve(reader.result);
          reader.onerror = reject;
          reader.readAsDataURL(blob);
        });
        if (dataUrl && dataUrl.length > 100) {
          img.src = dataUrl;
        }
      } catch (e) {
        // Image fetch failed — leave as data:,
      }
    }
  };

  // Wait for an image's src to change from "data:," to a real URL.
  // This only waits for the initial load (blob: URL to appear).
  // The actual resolution stabilization is handled in convertBlobImages.
  const waitForImageLoad = (block, maxWait = 5000) => new Promise((resolve) => {
    const imgs = Array.from(block.querySelectorAll('img'));
    if (imgs.length === 0) return resolve(false);

    let resolved = false;
    const check = () => {
      if (resolved) return;
      const allLoaded = imgs.every(img => !img.src.startsWith('data:,'));
      if (allLoaded) {
        resolved = true;
        resolve(true);
        return true;
      }
      return false;
    };

    if (check()) return;

    const interval = setInterval(() => {
      if (check()) clearInterval(interval);
    }, 200);

    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        clearInterval(interval);
        resolve(false);
      }
    }, maxWait);
  });

  // Track blocks whose images were unloaded on first encounter.
  // We'll do a second scroll pass to re-collect them.
  const blocksNeedingRecheck = new Set();

  // Callout blocks have their own internal virtual scrolling.
  // When the main scroller is at different positions, the callout renders
  // different subsets of its child blocks. We accumulate ALL child blocks
  // we've ever seen inside each callout, then merge them into a single
  // clone at the end.
  // Map: calloutBlockId → Map(childBlockId → cloned child element)
  const calloutChildren = new Map();
  // Set of block IDs that are callout blocks (type=callout)
  const calloutBlockIds = new Set();

  // Catalogue (TOC) items are also virtual-scrolled by Feishu.
  // The catalogue__scroller only renders items near the current scroll
  // position. We collect all catalogue__list-item elements we see during
  // scrolling, then merge them at the end.
  // Map: data-id → cloned <li> element
  const collectedCatalogueItems = new Map();

  // Multi-pass scroll collection.
  // Pass 0: Collect all blocks. For image blocks with src="data:,",
  //         wait for the image to load (poll up to 3s), then clone.
  //         Mark for recheck if still unloaded after timeout.
  // Pass 1: Re-visit blocks that had unloaded images and re-clone if loaded.
  //         Also accumulate new child blocks seen inside callouts.
  // Pass 2+: Extra passes for very long pages where virtual scrolling may
  //          miss blocks due to fast scroll timing. Stop when no new blocks.
  const maxPasses = 3;
  let lastCollectedCount = 0;
  let lastCatalogueCount = 0;
  for (let pass = 0; pass < maxPasses; pass++) {
    // Pass 0 uses the larger step for fast coverage; later passes use the
    // smaller step to catch blocks missed at the boundary.
    const passStep = pass === 0 ? step : effectiveStep;
    for (let y = 0; y <= totalScroll + 2000; y += passStep) {
      scroller.scrollTo(0, y);
      await new Promise(r => setTimeout(r, scrollDelay));

      // Update scroll target if content grew (lazy-loaded sections)
      if (scroller.scrollHeight > totalScroll) {
        totalScroll = scroller.scrollHeight;
      }

      // Collect catalogue (TOC) items — they are virtual-scrolled too.
      document.querySelectorAll('.catalogue__list-item[data-id]').forEach(item => {
        const itemId = item.getAttribute('data-id');
        if (itemId && !collectedCatalogueItems.has(itemId)) {
          collectedCatalogueItems.set(itemId, item.cloneNode(true));
        }
      });

      // Collect rendered blocks that have data-block-id
      const blocks = scroller.querySelectorAll('.render-unit-wrapper > [data-block-id]');
      for (const block of blocks) {
        const blockId = block.getAttribute('data-block-id');
        if (!blockId) continue;

        // Check if this is a callout block (contains .callout-render-unit)
        const calloutWrapper = block.querySelector('.callout-render-unit');
        const isCallout = block.getAttribute('data-block-type') === 'callout' ||
                          (calloutWrapper && block.classList.contains('docx-callout-block'));
        if (isCallout) {
          calloutBlockIds.add(blockId);
          // Collect all currently-rendered child blocks inside the callout
          const childBlocks = calloutWrapper.querySelectorAll('[data-block-id]');
          if (childBlocks.length > 0) {
            if (!calloutChildren.has(blockId)) {
              calloutChildren.set(blockId, new Map());
            }
            const childMap = calloutChildren.get(blockId);
            for (const child of childBlocks) {
              const childId = child.getAttribute('data-block-id');
              if (!childId || childId === blockId) continue;
              if (!childMap.has(childId)) {
                // New child block seen for the first time
                if (hasUnloadedImages(child)) {
                  await waitForImageLoad(child, 3000);
                }
                await convertBlobImages(child);
                childMap.set(childId, child.cloneNode(true));
              }
            }
          }
        }

        if (!collectedBlocks.has(blockId)) {
          // First time seeing this block
          if (hasUnloadedImages(block)) {
            // Image not loaded yet — wait for progressive loading to finish
            const loaded = await waitForImageLoad(block, 5000);
            if (!loaded && hasUnloadedImages(block)) {
              // Image never loaded via blob URL — try fetching via API.
              await convertUnloadedImages(block);
            }
            if (hasUnloadedImages(block)) {
              blocksNeedingRecheck.add(blockId);
            }
          }
          // Convert blob: URLs to data: URLs BEFORE cloning.
          // cloneNode loses blob: URL references (they become "data:,").
          await convertBlobImages(block);
          // For callout blocks, don't clone yet — we'll build the final
          // version in Phase 2 after accumulating all children.
          if (!isCallout) {
            collectedBlocks.set(blockId, block.cloneNode(true));
          } else {
            // Store a base clone (shell without children) — we'll merge children later
            collectedBlocks.set(blockId, block.cloneNode(true));
          }
        } else if (blocksNeedingRecheck.has(blockId)) {
          // Second pass: re-collect if images have now loaded.
          // Wait again for progressive loading to stabilize.
          await waitForImageLoad(block, 5000);
          // If still unloaded, try fetching via image-token API.
          if (hasUnloadedImages(block)) {
            await convertUnloadedImages(block);
          }
          if (!hasUnloadedImages(block)) {
            await convertBlobImages(block);
            collectedBlocks.set(blockId, block.cloneNode(true));
            blocksNeedingRecheck.delete(blockId);
          }
        }
      }
    }

    // Stop early if no new blocks were collected in this pass.
    if (pass >= 1 && collectedBlocks.size === lastCollectedCount &&
        collectedCatalogueItems.size === lastCatalogueCount) {
      break;
    }
    lastCollectedCount = collectedBlocks.size;
    lastCatalogueCount = collectedCatalogueItems.size;
  }

  // After both passes: for each callout block, merge ALL accumulated child
  // blocks into the callout's .callout-render-unit container. This ensures
  // we have the complete set of children even if they were rendered at
  // different scroll positions.
  for (const [calloutId, childMap] of calloutChildren) {
    const calloutEl = collectedBlocks.get(calloutId);
    if (!calloutEl) continue;
    const calloutWrapper = calloutEl.querySelector('.callout-render-unit');
    if (!calloutWrapper) continue;

    // Get existing child IDs in the clone
    const existingIds = new Set();
    for (const existing of calloutWrapper.querySelectorAll('[data-block-id]')) {
      existingIds.add(existing.getAttribute('data-block-id'));
    }

    // Append any accumulated children that aren't already in the clone
    const sortedChildren = Array.from(childMap.entries())
      .sort((a, b) => parseInt(a[0], 10) - parseInt(b[0], 10));
    for (const [childId, childEl] of sortedChildren) {
      if (existingIds.has(childId)) continue;
      calloutWrapper.appendChild(childEl);
    }
  }

  // Phase 2: Replace virtual scroll content with collected blocks.
  // Some blocks (e.g., callout 127) contain nested sub-blocks (221-229)
  // that were also independently collected during scrolling. We keep the
  // nested version (inside its parent which provides styling context)
  // and skip the standalone duplicate.
  //
  // Additionally, callout blocks have their own internal virtual scrolling.
  // When a callout was first collected, some of its child blocks may not
  // have been rendered yet. Those children were collected as standalone
  // top-level blocks during scrolling. We need to merge them back into the
  // parent callout's render-unit-wrapper to restore the correct structure.
  const wrapper = document.querySelector('.render-unit-wrapper');
  if (wrapper && collectedBlocks.size > 0) {
    // Sort by numeric block-id to restore document order
    const sortedEntries = Array.from(collectedBlocks.entries())
      .sort((a, b) => parseInt(a[0], 10) - parseInt(b[0], 10));

    // Identify blocks that are nested inside other collected blocks.
    const nestedBlockIds = new Set();
    // Map: parent block-id → Set of nested block-ids found in parent
    const parentNested = new Map(); // parentId → Set(childIds)
    for (const [parentId, element] of sortedEntries) {
      const nested = element.querySelectorAll('[data-block-id]');
      const childIds = new Set();
      for (const n of nested) {
        const nid = n.getAttribute('data-block-id');
        if (nid && nid !== parentId) {
          nestedBlockIds.add(nid);
          childIds.add(nid);
        }
      }
      if (childIds.size > 0) {
        parentNested.set(parentId, childIds);
      }
    }

    // For each parent that has nested children, check if all expected
    // children are present. If a child was collected standalone but is
    // missing from the parent (because the parent was cloned before the
    // child rendered), merge the standalone child into the parent's
    // callout-render-unit container.
    for (const [parentId, childIds] of parentNested) {
      const parentEl = collectedBlocks.get(parentId);
      if (!parentEl) continue;

      // Find the callout's inner render-unit-wrapper
      const calloutWrapper = parentEl.querySelector('.callout-render-unit');
      if (!calloutWrapper) continue;

      // Check which collected blocks are children of this parent
      // but NOT currently present in the parent's clone.
      for (const [blockId, element] of sortedEntries) {
        if (!childIds.has(blockId)) continue;
        // Check if this child is already in the parent clone
        if (parentEl.querySelector(`[data-block-id="${blockId}"]`)) {
          continue; // Already present in parent
        }
        // This child was collected as a standalone block but should be
        // inside the parent. Merge it into the callout wrapper.
        // Insert in correct order (by block-id).
        const childIdNum = parseInt(blockId, 10);
        let inserted = false;
        const children = Array.from(calloutWrapper.children);
        for (const sibling of children) {
          const siblingId = sibling.getAttribute('data-block-id');
          if (siblingId && parseInt(siblingId, 10) > childIdNum) {
            calloutWrapper.insertBefore(element, sibling);
            inserted = true;
            break;
          }
        }
        if (!inserted) {
          calloutWrapper.appendChild(element);
        }
        // Mark as nested so it's skipped in the top-level loop
        nestedBlockIds.add(blockId);
      }
    }

    // Also mark any blocks that were accumulated as callout children.
    // These may not appear in the parent's clone (they were added after
    // the parent was cloned), but they should still be skipped at the
    // top level since they belong inside the callout.
    for (const [, childMap] of calloutChildren) {
      for (const childId of childMap.keys()) {
        nestedBlockIds.add(childId);
      }
    }

    // Clear existing content (rendered blocks + placeholders)
    wrapper.innerHTML = '';

    // Insert collected blocks in order, skipping any that are nested inside
    // another block (to avoid duplicate content).
    const fragment = document.createDocumentFragment();
    for (const [blockId, element] of sortedEntries) {
      if (nestedBlockIds.has(blockId)) {
        // This block is already included inside its parent block — skip it.
        continue;
      }
      fragment.appendChild(element);
    }
    wrapper.appendChild(fragment);
  }

  // Phase 2a: Merge orphaned table cell text blocks back into their <td> elements.
  //
  // Feishu tables have <td> elements (data-block-type=table_cell) that each
  // contain a render-unit-wrapper. When the table is scrolled into view,
  // the render-unit-wrapper renders the cell's text content as a
  // docx-text-block child. However, Feishu's virtual scrolling sometimes
  // renders the cell content as a standalone top-level block in the main
  // render-unit-wrapper instead of inside the <td>. This leaves the <td>'s
  // render-unit-wrapper with only a bear-virtual-renderUnit-placeholder.
  //
  // Feishu assigns sequential block IDs: the <td> gets ID N, and its text
  // content block gets ID N+1. So we can match orphaned text blocks to
  // their parent <td> by checking td_id + 1 = text_block_id.
  //
  // This phase finds <td> elements whose render-unit-wrapper contains only
  // a placeholder (no real content), looks up the corresponding text block
  // (td_id + 1) from collectedBlocks, and moves it into the <td>. Since
  // appendChild moves DOM nodes, this automatically removes the text block
  // from the top-level wrapper (where it was placed in Phase 2).
  document.querySelectorAll('.docx-table-block').forEach(tableBlock => {
    const tds = tableBlock.querySelectorAll('td[data-block-type="table_cell"]');
    for (const td of tds) {
      const tdId = td.getAttribute('data-block-id');
      if (!tdId) continue;

      // Check if this <td>'s render-unit-wrapper lacks real content
      const ruw = td.querySelector('.render-unit-wrapper');
      if (!ruw) continue;
      // Skip if the render-unit-wrapper already has a real block inside
      if (ruw.querySelector('[data-block-id]')) continue;

      // Find the orphaned text block: td_id + 1
      const textBlockId = String(parseInt(tdId, 10) + 1);
      const textBlock = collectedBlocks.get(textBlockId);
      if (!textBlock) continue;
      // Verify it's a text block (not another table_cell or table)
      if (!textBlock.classList.contains('docx-text-block')) continue;

      // Move the text block into the <td>'s render-unit-wrapper,
      // replacing any placeholder. appendChild moves the node from
      // wherever it currently is (top-level wrapper) into the <td>.
      ruw.innerHTML = '';
      ruw.appendChild(textBlock);
    }
  });

  // Phase 2b: Fix table inline styles.
  // React's virtual scrolling sets width:2px (or similar small px) on
  // <table class="table"> elements as a placeholder before the component
  // measures and sets the real width. After cloning, this inline style
  // persists and overrides CSS width:fit-content from .table-scrollable-content.
  // Remove the bogus width so CSS takes over.
  document.querySelectorAll('.docx-table-block table.table').forEach(tbl => {
    const style = tbl.getAttribute('style') || '';
    // Only remove if width is a small pixel value (placeholder, not real width)
    const widthMatch = style.match(/width:\s*(\d+(?:\.\d+)?)px/);
    if (widthMatch && parseFloat(widthMatch[1]) < 50) {
      tbl.style.removeProperty('width');
    }
  });

  // Phase 2c: Fix table scrollable-wrapper and scrollable-container.
  // React sets width:0px on .scrollable-wrapper and .scrollable-container
  // as a placeholder before measuring. Also .scrollable-wrapper may have
  // a negative left offset for centering. These inline styles break table
  // rendering in the snapshot.
  document.querySelectorAll('.docx-table-block .scrollable-wrapper').forEach(el => {
    const w = el.style.width;
    if (!w || w === '0px') {
      el.style.setProperty('width', 'fit-content', 'important');
    }
    // Remove the negative left offset used for centering
    if (el.style.left && el.style.left !== '0px') {
      el.style.removeProperty('left');
    }
  });
  document.querySelectorAll('.docx-table-block .scrollable-container').forEach(el => {
    const w = el.style.width;
    if (!w || w === '0px') {
      el.style.setProperty('width', 'fit-content', 'important');
    }
  });

  // Phase 2d: Fix table content-scroller overflow.
  // .content-scroller has overflow:hidden which clips tables in the snapshot.
  document.querySelectorAll('.docx-table-block .content-scroller').forEach(el => {
    el.style.setProperty('overflow', 'visible', 'important');
    el.style.setProperty('max-width', 'none', 'important');
  });

  // Phase 2d-bis: Fix table scrollable-item left offset.
  // React sets left:NNNpx on .scrollable-item to center the table within
  // the scrollable-container. In the snapshot this pushes tables to the
  // right. Remove the left offset so tables align left.
  document.querySelectorAll('.scrollable-item').forEach(el => {
    if (el.style.left && el.style.left !== '0px') {
      el.style.setProperty('left', '0px', 'important');
    }
  });

  // Phase 2e: Fix image block width-wrapper.
  // React may set an excessively large width (e.g., 3017px) on
  // .image-block-width-wrapper as a placeholder. This stretches the image
  // and causes blurriness. Remove the inline width so CSS takes over.
  document.querySelectorAll('.docx-image-block .image-block-width-wrapper').forEach(el => {
    const w = el.style.width;
    if (w) {
      const pxMatch = w.match(/^(\d+(?:\.\d+)?)px$/);
      if (pxMatch && parseFloat(pxMatch[1]) > 1000) {
        // Bogus large width — remove it
        el.style.removeProperty('width');
      }
    }
  });

  // Phase 2f: Remove empty blocks and virtual scroll artifacts.
  // After collecting blocks, remove:
  // - isEmpty text blocks (empty paragraphs that show as blank lines).
  //   These have the .isEmpty class directly ON the [data-block-id] element
  //   (e.g. <div class="block docx-text-block isEmpty" data-block-id=5>),
  //   NOT on a descendant. Use the compound selector [data-block-id].isEmpty.
  // - bear-virtual-renderUnit-placeholder divs (virtual scroll remnants)
  // - bear-virtual-pre-renderer divs (virtual scroll pre-renderer remnants)
  document.querySelectorAll('[data-block-id].isEmpty').forEach(el => {
    el.remove();
  });
  document.querySelectorAll('.bear-virtual-renderUnit-placeholder').forEach(el => {
    el.remove();
  });
  document.querySelectorAll('.bear-virtual-pre-renderer').forEach(el => {
    el.remove();
  });

  // Phase 2g: Rebuild the catalogue (TOC) from collected items.
  // Feishu's catalogue is also virtual-scrolled, so only items near the
  // current scroll position exist in the DOM at any time. We collected all
  // catalogue__list-item elements we saw during scrolling; now merge them
  // back into the <ul class="catalogue__list"> container, sorted by their
  // position in the document (determined by the corresponding heading order).
  if (collectedCatalogueItems.size > 0) {
    const catList = document.querySelector('.catalogue__list');
    if (catList) {
      // Build a map of heading record-id → position index for ordering.
      // Each catalogue item has data-id matching a heading's data-record-id.
      const headingOrder = new Map();
      document.querySelectorAll('[data-block-type^="heading"]').forEach(h => {
        const rid = h.getAttribute('data-record-id');
        if (rid) headingOrder.set(rid, headingOrder.size);
      });

      // Sort collected catalogue items by their heading's position in the doc.
      const sortedItems = Array.from(collectedCatalogueItems.entries())
        .sort((a, b) => {
          const posA = headingOrder.has(a[0]) ? headingOrder.get(a[0]) : 999999;
          const posB = headingOrder.has(b[0]) ? headingOrder.get(b[0]) : 999999;
          return posA - posB;
        });

      // Remove existing items and placeholders, then re-insert sorted.
      catList.querySelectorAll('.catalogue__list-item, .fixed-size-list-placeholder')
        .forEach(el => el.remove());
      for (const [, itemEl] of sortedItems) {
        catList.appendChild(itemEl);
      }
    }
  }

  // Phase 3: Expand all containers for capture.
  // Set height:auto + overflow:visible so all content is visible.
  // Keep display:flex — do NOT change to display:block.
  const expandSelectors = [
    'html, body',
    '#mainBox',
    '#mainContainer',
    '.app-main-container',
    '.app-main',
    '.suite-body',
    '.garr-container',
    '.bear-web-x-container',
  ];
  for (const sel of expandSelectors) {
    document.querySelectorAll(sel).forEach(el => {
      el.style.setProperty('height', 'auto', 'important');
      el.style.setProperty('overflow', 'visible', 'important');
      el.style.setProperty('max-height', 'none', 'important');
    });
  }

  // #mainContainer has position:absolute which takes it out of flow,
  // causing body to collapse to ~2px. Set to static so body gets full height.
  const mainContainer = document.querySelector('#mainContainer');
  if (mainContainer) {
    mainContainer.style.setProperty('position', 'static', 'important');
  }

  // .app-main has min-width:200px which constrains the content width.
  const appMain = document.querySelector('.app-main');
  if (appMain) {
    appMain.style.setProperty('min-width', '0', 'important');
    appMain.style.setProperty('width', 'auto', 'important');
  }

  // Show the TOC (catalogue). In the original page, .catalogue-container is
  // position:absolute (floating above content). We keep it absolute so it
  // doesn't take up vertical space in the document flow — otherwise the
  // content gets pushed down by the catalogue's height, creating a large
  // blank gap above the text.
  const catContainer = document.querySelector('.catalogue-container');
  if (catContainer) {
    catContainer.style.setProperty('height', 'auto', 'important');
    catContainer.style.setProperty('overflow', 'visible', 'important');
    catContainer.style.setProperty('position', 'absolute', 'important');
    // Remove any top offset (e.g. top:-46px) that was set by React.
    catContainer.style.setProperty('top', '0', 'important');
  }
 const cat = document.querySelector('.catalogue');
 if (cat) {
   cat.style.setProperty('position', 'static', 'important');
   cat.style.setProperty('height', 'auto', 'important');
 }

  // Remove max-height on catalogue__scroller — React sets it to the
  // viewport height at capture time, truncating the TOC for long documents.
  const catScroller = document.querySelector('.catalogue__scroller');
  if (catScroller) {
    catScroller.style.setProperty('max-height', 'none', 'important');
    catScroller.style.setProperty('overflow', 'visible', 'important');
  }

 // Wait for layout reflow before capture.
 await new Promise(r => setTimeout(r, 500));
}
