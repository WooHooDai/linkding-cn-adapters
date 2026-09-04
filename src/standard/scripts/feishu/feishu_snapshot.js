/**
 * Feishu snapshot browser-script for SingleFile.
 *
 * Strategy:
 *   1. Detect Feishu renderer.
 *   2. Scroll through virtualized content.
 *   3. After every scroll position, wait until visible images are either:
 *        - loaded, or
 *        - converted directly through Feishu's image API, or
 *        - timeout after 3s.
 *      Poll every 50ms.
 *   4. Clone only the currently mounted top-level blocks.
 *   5. For Callout blocks, independently accumulate their virtualized children.
 *   6. Rebuild the main render-unit-wrapper.
 *   7. Rebuild virtualized catalogue.
 *   8. Fix table/image layout.
 *   9. Expand containers for SingleFile capture.
 */

const builtin_engine = "singlefile";

/* -------------------------------------------------------------------------- */
/* Constants                                                                  */
/* -------------------------------------------------------------------------- */

const IMAGE_POLL_INTERVAL = 50;
const IMAGE_STABLE_COUNT = 2;
const IMAGE_VIEWPORT_TIMEOUT = 3000;

const INITIAL_RENDER_DELAY = 500;
const SCROLL_RENDER_DELAY = 50;

/* -------------------------------------------------------------------------- */
/* Shared utilities                                                           */
/* -------------------------------------------------------------------------- */

const sleep = (ms) =>
  new Promise((resolve) => setTimeout(resolve, ms));

function waitForSelector(sel, timeout = 30000) {
  return new Promise((resolve) => {
    const existing = document.querySelector(sel);
    if (existing) {
      resolve(true);
      return;
    }

    const observer = new MutationObserver(() => {
      if (document.querySelector(sel)) {
        observer.disconnect();
        resolve(true);
      }
    });

    if (!document.body) {
      resolve(false);
      return;
    }

    observer.observe(document.body, {
      childList: true,
      subtree: true,
    });

    setTimeout(() => {
      observer.disconnect();
      resolve(false);
    }, timeout);
  });
}

/* -------------------------------------------------------------------------- */
/* Image utilities                                                            */
/* -------------------------------------------------------------------------- */

const imageDataCache = new Map();
const imagePromiseCache = new Map();

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();

    reader.onloadend = () => resolve(reader.result);
    reader.onerror = reject;

    reader.readAsDataURL(blob);
  });
}

async function fetchToDataUrl(url) {
  if (!url) return null;

  if (url.startsWith("data:")) {
    return url;
  }

  if (imageDataCache.has(url)) {
    return imageDataCache.get(url);
  }

  if (imagePromiseCache.has(url)) {
    return imagePromiseCache.get(url);
  }

  const promise = (async () => {
    try {
      const response = await fetch(url, {
        credentials: "include",
      });

      if (!response.ok) {
        return null;
      }

      const blob = await response.blob();

      if (!blob.type || !blob.type.startsWith("image/")) {
        return null;
      }

      const dataUrl = await blobToDataUrl(blob);

      if (dataUrl) {
        imageDataCache.set(url, dataUrl);
      }

      return dataUrl;
    } catch {
      return null;
    } finally {
      imagePromiseCache.delete(url);
    }
  })();

  imagePromiseCache.set(url, promise);

  return promise;
}

function isEmptyImage(img) {
  const src = img.getAttribute("src") || "";
  return src === "data:," || src === "data:";
}

function isBlobImage(img) {
  const src = img.getAttribute("src") || "";
  return src.startsWith("blob:");
}

function getImageToken(img) {
  const owner = img.closest("[image-token]");
  if (!owner) return null;

  return owner.getAttribute("image-token");
}

async function convertUnloadedImage(img) {
  const token = getImageToken(img);

  if (!token) {
    return false;
  }

  try {
    const apiUrl =
      `/space/api/box/stream/download/asynccode/?code=` +
      `${encodeURIComponent(token)}&preview_type=1`;

    const response = await fetch(apiUrl, {
      credentials: "include",
    });

    if (!response.ok) {
      return false;
    }

    const blob = await response.blob();

    if (!blob.type || !blob.type.startsWith("image/")) {
      return false;
    }

    const dataUrl = await blobToDataUrl(blob);

    if (!dataUrl || dataUrl.length < 100) {
      return false;
    }

    img.setAttribute("src", dataUrl);
    img.removeAttribute("data-src");

    return true;
  } catch {
    return false;
  }
}

async function waitForImageStable(img, timeout = IMAGE_VIEWPORT_TIMEOUT) {
  const start = Date.now();

  let lastSrc = img.getAttribute("src") || "";
  let lastWidth = img.naturalWidth || 0;
  let stableCount = 0;

  while (Date.now() - start < timeout) {
    const src = img.getAttribute("src") || "";
    const width = img.naturalWidth || 0;

    const loaded =
      !isEmptyImage(img) &&
      (
        width > 0 ||
        src.startsWith("data:") ||
        src.startsWith("blob:")
      );

    if (loaded) {
      if (src === lastSrc && width === lastWidth) {
        stableCount++;

        if (stableCount >= IMAGE_STABLE_COUNT) {
          return true;
        }
      } else {
        stableCount = 0;
        lastSrc = src;
        lastWidth = width;
      }
    } else {
      stableCount = 0;
      lastSrc = src;
      lastWidth = width;
    }

    await sleep(IMAGE_POLL_INTERVAL);
  }

  return false;
}

async function canvasConvertImage(img) {
  try {
    if (!img.naturalWidth || !img.naturalHeight) {
      return false;
    }

    const canvas = document.createElement("canvas");

    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;

    const ctx = canvas.getContext("2d");

    if (!ctx) {
      return false;
    }

    ctx.drawImage(img, 0, 0);

    const dataUrl = canvas.toDataURL("image/png");

    if (!dataUrl || dataUrl.length < 100) {
      return false;
    }

    img.setAttribute("src", dataUrl);
    img.removeAttribute("data-src");

    return true;
  } catch {
    return false;
  }
}

async function convertBlobImage(img) {
  const src = img.getAttribute("src") || "";

  if (!src.startsWith("blob:")) {
    return false;
  }

  /*
   * Remember the current blob URL.
   *
   * Feishu can replace the blob URL during progressive loading.
   * We therefore check it again after waiting.
   */
  const originalSrc = src;

  await waitForImageStable(img);

  const currentSrc = img.getAttribute("src") || "";

  if (!currentSrc.startsWith("blob:")) {
    return currentSrc.startsWith("data:");
  }

  /*
   * Fetch the exact blob currently attached to this image.
   */
  try {
    const dataUrl = await fetchToDataUrl(currentSrc);

    /*
     * The image may have been replaced while fetch() was running.
     * In that case, don't overwrite the newer version.
     */
    const latestSrc = img.getAttribute("src") || "";

    if (
      dataUrl &&
      dataUrl.length > 100 &&
      (
        latestSrc === currentSrc ||
        latestSrc === originalSrc
      )
    ) {
      img.setAttribute("src", dataUrl);
      img.removeAttribute("data-src");
      return true;
    }
  } catch {
    // Continue to canvas fallback.
  }

  /*
   * Canvas fallback.
   *
   * This is useful when fetch(blob:) is unavailable but the image bitmap
   * itself is already available in the browser.
   */
  if (img.naturalWidth > 0) {
    if (await canvasConvertImage(img)) {
      return true;
    }
  }

  return false;
}

async function convertImage(img) {
  if (!img || !img.isConnected) {
    return false;
  }

  if (isEmptyImage(img)) {
    return convertUnloadedImage(img);
  }

  if (isBlobImage(img)) {
    return convertBlobImage(img);
  }

  const src = img.getAttribute("src") || "";
  const dataSrc = img.getAttribute("data-src");

  /*
   * Some Feishu image implementations expose the actual URL through
   * data-src rather than src.
   */
  if (
    dataSrc &&
    !dataSrc.startsWith("data:")
  ) {
    const dataUrl = await fetchToDataUrl(dataSrc);

    if (dataUrl) {
      img.setAttribute("src", dataUrl);
      img.removeAttribute("data-src");
      return true;
    }
  }

  /*
   * Normal HTTP image.
   *
   * We generally leave these alone because SingleFile can handle ordinary
   * network images itself. Only convert when the browser has already loaded
   * the image and the URL is explicitly Feishu-managed.
   */
  if (
    src.startsWith("http://") ||
    src.startsWith("https://")
  ) {
    return false;
  }

  return false;
}

function getImages(root) {
  if (!root) return [];

  const images = [];

  if (root.matches && root.matches("img")) {
    images.push(root);
  }

  images.push(...root.querySelectorAll("img"));

  return images;
}

function hasImagesNeedingAttention(root) {
  const images = getImages(root);

  for (const img of images) {
    if (isEmptyImage(img) || isBlobImage(img)) {
      return true;
    }

    const dataSrc = img.getAttribute("data-src");

    if (
      dataSrc &&
      !dataSrc.startsWith("data:")
    ) {
      return true;
    }
  }

  return false;
}

/**
 * Process all images currently mounted in a viewport.
 *
 * Important:
 * We don't simply "sleep 3 seconds".
 *
 * We poll every 50ms and return as soon as all images have reached a usable
 * state. The 3-second value is only a safety timeout.
 */
async function prepareViewport(root) {
  const images = getImages(root);

  if (images.length === 0) {
    return true;
  }

  const deadline = Date.now() + IMAGE_VIEWPORT_TIMEOUT;

  while (Date.now() < deadline) {
    let allReady = true;

    for (const img of images) {
      if (!img.isConnected) {
        continue;
      }

      if (isEmptyImage(img)) {
        allReady = false;
        continue;
      }

      if (isBlobImage(img)) {
        /*
         * A blob image may already be loaded, but we need to wait until
         * progressive loading has settled before converting it.
         */
        const srcBefore = img.getAttribute("src") || "";
        const widthBefore = img.naturalWidth || 0;

        if (!widthBefore) {
          allReady = false;
          continue;
        }

        const stable = await waitForImageStable(
          img,
          Math.max(IMAGE_POLL_INTERVAL * 2, 200)
        );

        if (!stable) {
          allReady = false;
          continue;
        }

        /*
         * Convert immediately after stabilization.
         */
        await convertBlobImage(img);

        if (isBlobImage(img)) {
          allReady = false;
        }

        /*
         * If Feishu changed the source while processing, check again.
         */
        const srcAfter = img.getAttribute("src") || "";

        if (
          srcAfter !== srcBefore ||
          (img.naturalWidth || 0) !== widthBefore
        ) {
          allReady = false;
        }

        continue;
      }

      const dataSrc = img.getAttribute("data-src");

      if (
        dataSrc &&
        !dataSrc.startsWith("data:")
      ) {
        await convertImage(img);

        if (img.getAttribute("data-src")) {
          allReady = false;
        }
      }
    }

    /*
     * Try direct API conversion for any images still unloaded.
     */
    for (const img of images) {
      if (!img.isConnected) continue;

      if (isEmptyImage(img)) {
        const converted = await convertUnloadedImage(img);

        if (!converted) {
          allReady = false;
        }
      }
    }

    if (!hasImagesNeedingAttention(root)) {
      /*
       * Give the browser two 50ms ticks to settle any src replacement.
       */
      await sleep(IMAGE_POLL_INTERVAL);
      await sleep(IMAGE_POLL_INTERVAL);

      if (!hasImagesNeedingAttention(root)) {
        return true;
      }
    } else {
      allReady = false;
    }

    if (allReady) {
      return true;
    }

    await sleep(IMAGE_POLL_INTERVAL);
  }

  /*
   * Timeout:
   * do one last direct conversion attempt for empty images.
   */
  for (const img of getImages(root)) {
    if (!img.isConnected) continue;

    if (isEmptyImage(img)) {
      await convertUnloadedImage(img);
    }

    if (isBlobImage(img)) {
      await convertBlobImage(img);
    }
  }

  return !hasImagesNeedingAttention(root);
}

async function convertImagesInElement(root) {
  if (!root) return;

  for (const img of getImages(root)) {
    if (isEmptyImage(img)) {
      await convertUnloadedImage(img);
    } else if (isBlobImage(img)) {
      await convertBlobImage(img);
    }
  }
}

/* -------------------------------------------------------------------------- */
/* New Feishu renderer                                                        */
/* -------------------------------------------------------------------------- */

async function collectNewFeishu(scroller, wrapper) {
  if (!scroller || !wrapper) {
    return;
  }

  const isLine = (el) =>
    el &&
    el.nodeType === Node.ELEMENT_NODE &&
    (
      el.classList.contains("ace-line") ||
      el.matches("[data-record-id], [data-block-id]") ||
      (el.id && el.id.startsWith("magicdomid"))
    );

  /*
   * The new renderer's #innerdocbody is itself the logical root.
   * Only direct children are collected as top-level lines.
   */
  const visibleLines = () =>
    Array.from(wrapper.children).filter(isLine);

  const collectedLines = new Map();

  const getLineKey = (el) => {
    return (
      el.getAttribute("data-record-id") ||
      el.getAttribute("data-block-id") ||
      el.id ||
      null
    );
  };

  const snapshotVisibleLines = async () => {
    const lines = visibleLines();

    for (const line of lines) {
      const key = getLineKey(line);

      if (!key) {
        continue;
      }

      /*
       * Even if the line was already collected, inspect it again when it
       * contains images. Feishu may progressively replace the image.
       */
      const needsImages = hasImagesNeedingAttention(line);

      if (
        collectedLines.has(key) &&
        !needsImages
      ) {
        continue;
      }

      if (needsImages) {
        await prepareViewport(line);
      }

      await convertImagesInElement(line);

      collectedLines.set(
        key,
        line.cloneNode(true)
      );
    }
  };

  scroller.scrollTop = 0;
  await sleep(INITIAL_RENDER_DELAY);

  let totalScroll = scroller.scrollHeight;

  const step = Math.max(
    Math.floor(scroller.clientHeight * 2 / 3),
    200
  );

  let previousCount = -1;

  for (
    let pass = 0;
    pass < 3 && collectedLines.size > previousCount;
    pass++
  ) {
    previousCount = collectedLines.size;

    for (
      let y = 0;
      y <= totalScroll + 3000;
      y += step
    ) {
      scroller.scrollTop = y;

      /*
       * Let React mount the new virtualized viewport.
       */
      await sleep(SCROLL_RENDER_DELAY);

      if (scroller.scrollHeight > totalScroll) {
        totalScroll = scroller.scrollHeight;
      }

      await snapshotVisibleLines();
    }

    scroller.scrollTop = totalScroll;
    await sleep(SCROLL_RENDER_DELAY);

    await snapshotVisibleLines();
  }

  /*
   * Comments are handled independently below.
   */
  const numericOrder = (el) => {
    const match = (el.id || "").match(/magicdomid-(\d+)/);

    if (match) {
      return parseInt(match[1], 10);
    }

    return Number.MAX_SAFE_INTEGER;
  };

  const sortedLines = Array.from(
    collectedLines.values()
  ).sort(
    (a, b) =>
      numericOrder(a) - numericOrder(b)
  );

  if (sortedLines.length > 0) {
    wrapper.replaceChildren(...sortedLines);
  }

  wrapper
    .querySelectorAll(
      ".adit-virtual-scroll-placeholder, " +
      ".fixed-size-list-placeholder"
    )
    .forEach((el) => el.remove());
}

/* -------------------------------------------------------------------------- */
/* Legacy renderer helpers                                                    */
/* -------------------------------------------------------------------------- */

/**
 * Find the actual main render-unit-wrapper.
 *
 * We intentionally prefer a wrapper directly under the main scroller.
 * This avoids accidentally selecting a nested render-unit-wrapper inside
 * a table / callout / other compound block.
 */
function findMainLegacyWrapper(scroller) {
  if (!scroller) {
    return null;
  }

  /*
   * First choice:
   * wrapper directly contained by the main scrolling container.
   */
  const direct = Array.from(
    scroller.querySelectorAll(".render-unit-wrapper")
  ).find((el) => {
    let parent = el.parentElement;

    while (parent && parent !== scroller) {
      if (
        parent.matches(
          ".docx-table-block, " +
          ".docx-callout-block, " +
          "[data-block-type='table_cell']"
        )
      ) {
        return false;
      }

      parent = parent.parentElement;
    }

    return parent === scroller;
  });

  if (direct) {
    return direct;
  }

  /*
   * Fallback: the first wrapper whose direct children contain block IDs.
   */
  const candidates = Array.from(
    scroller.querySelectorAll(".render-unit-wrapper")
  );

  for (const candidate of candidates) {
    const hasTopLevelBlock = Array.from(
      candidate.children
    ).some((child) =>
      child.hasAttribute("data-block-id")
    );

    if (hasTopLevelBlock) {
      return candidate;
    }
  }

  return null;
}

function getTopLevelLegacyBlocks(wrapper) {
  if (!wrapper) {
    return [];
  }

  /*
   * IMPORTANT:
   *
   * Do NOT use:
   *
   *   wrapper.querySelectorAll(...)
   *
   * because nested render-unit-wrapper instances inside tables, callouts,
   * cells, etc. would then leak their internal blocks into the top-level
   * collection.
   *
   * Only direct children are logical top-level blocks.
   */
  return Array.from(wrapper.children).filter(
    (el) =>
      el.nodeType === Node.ELEMENT_NODE &&
      el.hasAttribute("data-block-id")
  );
}

function isCalloutBlock(block) {
  if (!block) {
    return false;
  }

  return (
    block.getAttribute("data-block-type") === "callout" ||
    block.classList.contains("docx-callout-block") ||
    !!block.querySelector(".callout-render-unit")
  );
}

/* -------------------------------------------------------------------------- */
/* Legacy renderer                                                            */
/* -------------------------------------------------------------------------- */

async function collectLegacyFeishu(scroller, wrapper) {
  if (!scroller || !wrapper) {
    return;
  }

  scroller.scrollTo(0, 0);
  await sleep(INITIAL_RENDER_DELAY);

  /** @type {Map<string, HTMLElement>} */
  const collectedBlocks = new Map();

  /*
   * calloutBlockId -> Map(childBlockId -> cloned child)
   */
  const calloutChildren = new Map();

  /*
   * Catalogue items are independently virtualized.
   */
  const collectedCatalogueItems = new Map();

  /*
   * Blocks whose images could not be completed on first encounter.
   */
  const blocksNeedingRecheck = new Set();

  let totalScroll = scroller.scrollHeight;

  const step = Math.max(
    scroller.clientHeight - 50,
    300
  );

  const effectiveStep = Math.max(
    Math.floor(scroller.clientHeight * 2 / 3),
    200
  );

  /**
   * Collect children currently mounted inside a Callout.
   */
  const collectCalloutChildren = async (
    calloutBlock,
    calloutId
  ) => {
    const calloutWrapper =
      calloutBlock.querySelector(
        ".callout-render-unit"
      );

    if (!calloutWrapper) {
      return;
    }

    /*
     * Unlike top-level blocks, callout children are intentionally searched
     * recursively because the Callout owns its internal render tree.
     */
    const childBlocks = Array.from(
      calloutWrapper.querySelectorAll(
        "[data-block-id]"
      )
    );

    if (childBlocks.length === 0) {
      return;
    }

    if (!calloutChildren.has(calloutId)) {
      calloutChildren.set(
        calloutId,
        new Map()
      );
    }

    const childMap =
      calloutChildren.get(calloutId);

    for (const child of childBlocks) {
      const childId =
        child.getAttribute("data-block-id");

      if (
        !childId ||
        childId === calloutId
      ) {
        continue;
      }

      /*
       * A nested child may itself contain renderer elements with
       * data-block-id. We only care about the direct logical block
       * candidates where possible.
       */
      let logicalChild = child;

      const childParent = child.parentElement;

      if (
        childParent &&
        childParent !== calloutWrapper
      ) {
        /*
         * Walk upward until the direct child of calloutWrapper.
         */
        let current = child;

        while (
          current.parentElement &&
          current.parentElement !== calloutWrapper
        ) {
          current = current.parentElement;
        }

        if (
          current.parentElement === calloutWrapper &&
          current.hasAttribute("data-block-id")
        ) {
          logicalChild = current;
        }
      }

      const logicalChildId =
        logicalChild.getAttribute(
          "data-block-id"
        );

      if (!logicalChildId) {
        continue;
      }

      if (
        childMap.has(logicalChildId)
      ) {
        continue;
      }

      if (
        hasImagesNeedingAttention(
          logicalChild
        )
      ) {
        const loaded =
          await prepareViewport(
            logicalChild
          );

        if (!loaded) {
          await convertImagesInElement(
            logicalChild
          );
        }
      }

      await convertImagesInElement(
        logicalChild
      );

      childMap.set(
        logicalChildId,
        logicalChild.cloneNode(true)
      );
    }
  };

  /**
   * Collect currently mounted top-level blocks.
   */
  const collectVisibleBlocks = async () => {
    /*
     * CRITICAL:
     *
     * This is now based on wrapper.children rather than:
     *
     *   scroller.querySelectorAll(
     *       '.render-unit-wrapper > [data-block-id]'
     *   )
     *
     * That old approach could collect block IDs from nested render trees
     * and caused non-text/non-image blocks to be incorrectly treated as
     * nested and subsequently removed.
     */
    const blocks =
      getTopLevelLegacyBlocks(wrapper);

    for (const block of blocks) {
      const blockId =
        block.getAttribute(
          "data-block-id"
        );

      if (!blockId) {
        continue;
      }

      const isCallout =
        isCalloutBlock(block);

      /*
       * Callout children have their own virtualized renderer.
       * Accumulate them every time the Callout is mounted.
       */
      if (isCallout) {
        await collectCalloutChildren(
          block,
          blockId
        );
      }

      const existing =
        collectedBlocks.get(blockId);

      /*
       * First encounter.
       */
      if (!existing) {
        const needsImages =
          hasImagesNeedingAttention(block);

        if (needsImages) {
          const loaded =
            await prepareViewport(block);

          if (!loaded) {
            blocksNeedingRecheck.add(
              blockId
            );

            await convertImagesInElement(
              block
            );
          }
        }

        await convertImagesInElement(
          block
        );

        collectedBlocks.set(
          blockId,
          block.cloneNode(true)
        );

        /*
         * If images are still unresolved, revisit this block in a later pass.
         */
        if (
          hasImagesNeedingAttention(block)
        ) {
          blocksNeedingRecheck.add(
            blockId
          );
        } else {
          blocksNeedingRecheck.delete(
            blockId
          );
        }

        continue;
      }

      /*
       * Existing block:
       *
       * Normally we don't need to clone it again.
       * If its images were unresolved previously, however, the current
       * mounted instance may now contain the completed image.
       */
      if (
        blocksNeedingRecheck.has(blockId)
      ) {
        const loaded =
          await prepareViewport(block);

        if (loaded) {
          await convertImagesInElement(
            block
          );

          if (
            !hasImagesNeedingAttention(
              block
            )
          ) {
            collectedBlocks.set(
              blockId,
              block.cloneNode(true)
            );

            blocksNeedingRecheck.delete(
              blockId
            );
          }
        } else {
          await convertImagesInElement(
            block
          );
        }
      }
    }
  };

  const collectCatalogue = () => {
    document
      .querySelectorAll(
        ".catalogue__list-item[data-id]"
      )
      .forEach((item) => {
        const itemId =
          item.getAttribute("data-id");

        if (
          itemId &&
          !collectedCatalogueItems.has(
            itemId
          )
        ) {
          collectedCatalogueItems.set(
            itemId,
            item.cloneNode(true)
          );
        }
      });
  };

  /*
   * Multi-pass collection.
   */
  const maxPasses = 4;

  let lastCollectedCount = 0;
  let lastCatalogueCount = 0;
  let lastPendingImageCount = Number.MAX_SAFE_INTEGER;

  for (
    let pass = 0;
    pass < maxPasses;
    pass++
  ) {
    const passStep =
      pass === 0
        ? step
        : effectiveStep;

    for (
      let y = 0;
      y <= totalScroll + 2000;
      y += passStep
    ) {
      scroller.scrollTo(0, y);

      /*
       * Only a short initial render delay.
       *
       * prepareViewport() below performs the real 50ms polling when
       * mounted images actually need attention.
       */
      await sleep(
        SCROLL_RENDER_DELAY
      );

      if (
        scroller.scrollHeight >
        totalScroll
      ) {
        totalScroll =
          scroller.scrollHeight;
      }

      collectCatalogue();

      await collectVisibleBlocks();
    }

    /*
     * Always inspect the very bottom.
     */
    scroller.scrollTop = totalScroll;

    await sleep(
      SCROLL_RENDER_DELAY
    );

    collectCatalogue();

    await collectVisibleBlocks();

    const pendingImageCount =
      blocksNeedingRecheck.size;

    /*
     * Stop when:
     *
     * 1. no new blocks,
     * 2. no new catalogue items,
     * 3. unresolved image count did not improve.
     */
    if (
      pass >= 1 &&
      collectedBlocks.size ===
        lastCollectedCount &&
      collectedCatalogueItems.size ===
        lastCatalogueCount &&
      pendingImageCount ===
        lastPendingImageCount
    ) {
      break;
    }

    lastCollectedCount =
      collectedBlocks.size;

    lastCatalogueCount =
      collectedCatalogueItems.size;

    lastPendingImageCount =
      pendingImageCount;
  }

  /*
   * One final targeted pass for blocks whose images remained unresolved.
   *
   * We cannot jump directly to a block's position reliably, because Feishu's
   * virtual list does not expose a stable pixel offset for every block.
   * Therefore a final normal pass is safer.
   */
  if (blocksNeedingRecheck.size > 0) {
    for (
      let y = 0;
      y <= totalScroll + 1000;
      y += effectiveStep
    ) {
      scroller.scrollTo(0, y);

      await sleep(
        SCROLL_RENDER_DELAY
      );

      const blocks =
        getTopLevelLegacyBlocks(
          wrapper
        );

      for (const block of blocks) {
        const blockId =
          block.getAttribute(
            "data-block-id"
          );

        if (
          !blockId ||
          !blocksNeedingRecheck.has(
            blockId
          )
        ) {
          continue;
        }

        await prepareViewport(block);
        await convertImagesInElement(
          block
        );

        if (
          !hasImagesNeedingAttention(
            block
          )
        ) {
          collectedBlocks.set(
            blockId,
            block.cloneNode(true)
          );

          blocksNeedingRecheck.delete(
            blockId
          );
        }
      }

      if (
        blocksNeedingRecheck.size === 0
      ) {
        break;
      }
    }
  }

  /*
   * Merge accumulated Callout children back into their Callout.
   */
  for (
    const [
      calloutId,
      childMap
    ] of calloutChildren
  ) {
    const calloutEl =
      collectedBlocks.get(
        calloutId
      );

    if (!calloutEl) {
      continue;
    }

    const calloutWrapper =
      calloutEl.querySelector(
        ".callout-render-unit"
      );

    if (!calloutWrapper) {
      continue;
    }

    /*
     * Existing child IDs in the cloned Callout.
     */
    const existingIds =
      new Set();

    calloutWrapper
      .querySelectorAll(
        "[data-block-id]"
      )
      .forEach((child) => {
        const id =
          child.getAttribute(
            "data-block-id"
          );

        if (id) {
          existingIds.add(id);
        }
      });

    const sortedChildren =
      Array.from(
        childMap.entries()
      ).sort(
        (a, b) =>
          parseInt(a[0], 10) -
          parseInt(b[0], 10)
      );

    for (
      const [
        childId,
        childEl
      ] of sortedChildren
    ) {
      if (
        existingIds.has(childId)
      ) {
        continue;
      }

      calloutWrapper.appendChild(
        childEl
      );
    }
  }

  /*
   * ------------------------------------------------------------------------
   * Rebuild the main wrapper.
   *
   * IMPORTANT:
   *
   * We no longer infer nested blocks by:
   *
   *   parent.querySelectorAll('[data-block-id]')
   *
   * because that is not equivalent to logical Feishu block ownership.
   *
   * The main wrapper collection already contains only its direct children.
   * The only blocks that need to be excluded from the top-level list are
   * explicitly accumulated Callout children.
   * ------------------------------------------------------------------------
   */

  const nestedBlockIds =
    new Set();

  for (
    const childMap of
      calloutChildren.values()
  ) {
    for (
      const childId of childMap.keys()
    ) {
      nestedBlockIds.add(
        childId
      );
    }
  }

  if (
    wrapper &&
    collectedBlocks.size > 0
  ) {
    const sortedEntries =
      Array.from(
        collectedBlocks.entries()
      ).sort(
        (a, b) =>
          parseInt(a[0], 10) -
          parseInt(b[0], 10)
      );

    wrapper.innerHTML = "";

    const fragment =
      document.createDocumentFragment();

    for (
      const [
        blockId,
        element
      ] of sortedEntries
    ) {
      if (
        nestedBlockIds.has(blockId)
      ) {
        continue;
      }

      fragment.appendChild(
        element
      );
    }

    wrapper.appendChild(
      fragment
    );
  }

  /*
   * Return the collected blocks so later phases can use them if necessary.
   */
  return {
    collectedBlocks,
    calloutChildren,
    collectedCatalogueItems,
  };
}

/* -------------------------------------------------------------------------- */
/* Comment processing                                                         */
/* -------------------------------------------------------------------------- */

function findScrollableAncestor(element) {
  if (!element) {
    return null;
  }

  let current =
    element.parentElement;

  while (
    current &&
    current !== document.body &&
    current !== document.documentElement
  ) {
    const style =
      getComputedStyle(current);

    const overflowY =
      style.overflowY;

    const canScroll =
      (
        overflowY === "auto" ||
        overflowY === "scroll" ||
        overflowY === "overlay"
      ) &&
      current.scrollHeight >
        current.clientHeight + 1;

    if (canScroll) {
      return current;
    }

    current =
      current.parentElement;
  }

  return null;
}

function findCommentScroller(card) {
  if (!card) {
    return null;
  }

  /*
   * Prefer the closest actual scrollable ancestor.
   */
  const ancestor =
    findScrollableAncestor(card);

  if (ancestor) {
    return ancestor;
  }

  /*
   * Fallback: inspect known comment panel containers.
   */
  const panel =
    card.closest(
      ".js-panel-card-list, " +
      ".doc-comment-v2, " +
      ".comment-panel, " +
      ".comments-panel"
    );

  if (panel) {
    const candidates =
      Array.from(
        panel.querySelectorAll("*")
      );

    for (
      const el of candidates
    ) {
      if (
        el.scrollHeight >
          el.clientHeight + 1
      ) {
        const style =
          getComputedStyle(el);

        if (
          style.overflowY ===
            "auto" ||
          style.overflowY ===
            "scroll" ||
          style.overflowY ===
            "overlay"
        ) {
          return el;
        }
      }
    }
  }

  return null;
}

async function processCommentViewport(
  root
) {
  if (!root) {
    return;
  }

  const images =
    getImages(root);

  if (images.length === 0) {
    return;
  }

  await prepareViewport(root);

  await convertImagesInElement(
    root
  );
}

async function processFeishuComments() {
  const cards =
    () =>
      Array.from(
        document.querySelectorAll(
          ".js-panel-card"
        )
      );

  /*
   * No comments -> nothing to do.
   */
  if (cards().length === 0) {
    return;
  }

  /*
   * Find the independent comment scroll container.
   *
   * The first card is usually enough to locate it.
   */
  let firstCard =
    cards()[0];

  let commentScroller =
    findCommentScroller(
      firstCard
    );

  /*
   * If cards are initially mounted but their scroller is not yet established,
   * give Feishu a moment to finish mounting the panel.
   */
  if (!commentScroller) {
    await sleep(300);

    firstCard =
      cards()[0];

    if (firstCard) {
      commentScroller =
        findCommentScroller(
          firstCard
        );
    }
  }

  /*
   * Without a detectable independent scroller, process all currently mounted
   * cards. This is still useful for comments whose list isn't virtualized.
   */
  if (!commentScroller) {
    for (
      const card of cards()
    ) {
      await processCommentViewport(
        card
      );
    }

    return;
  }

  commentScroller.scrollTop = 0;

  await sleep(
    INITIAL_RENDER_DELAY
  );

  const processedCards =
    new Set();

  let totalScroll =
    commentScroller.scrollHeight;

  const step =
    Math.max(
      Math.floor(
        commentScroller.clientHeight *
          0.7
      ),
      150
    );

  /*
   * Multiple passes are intentional:
   *
   * comment virtualization can change scrollHeight as cards mount.
   */
  for (
    let pass = 0;
    pass < 4;
    pass++
  ) {
    for (
      let y = 0;
      y <= totalScroll + 1000;
      y += step
    ) {
      commentScroller.scrollTop =
        y;

      await sleep(
        SCROLL_RENDER_DELAY
      );

      if (
        commentScroller.scrollHeight >
        totalScroll
      ) {
        totalScroll =
          commentScroller.scrollHeight;
      }

      const mountedCards =
        cards();

      for (
        const card of mountedCards
      ) {
        /*
         * Process every currently mounted card.
         *
         * Even previously processed cards are checked again if they still
         * contain an image that needs attention.
         */
        const cardKey =
          card.getAttribute(
            "data-comment-id"
          ) ||
          card.getAttribute(
            "data-id"
          ) ||
          card;

        const needsProcessing =
          !processedCards.has(
            cardKey
          ) ||
          hasImagesNeedingAttention(
            card
          );

        if (!needsProcessing) {
          continue;
        }

        await processCommentViewport(
          card
        );

        if (
          !hasImagesNeedingAttention(
            card
          )
        ) {
          processedCards.add(
            cardKey
          );
        }
      }
    }

    commentScroller.scrollTop =
      totalScroll;

    await sleep(
      SCROLL_RENDER_DELAY
    );

    const mountedCards =
      cards();

    for (
      const card of mountedCards
    ) {
      const cardKey =
        card.getAttribute(
          "data-comment-id"
        ) ||
        card.getAttribute(
          "data-id"
        ) ||
        card;

      await processCommentViewport(
        card
      );

      if (
        !hasImagesNeedingAttention(
          card
        )
      ) {
        processedCards.add(
          cardKey
        );
      }
    }

    /*
     * If another pass doesn't reveal anything new and no mounted card has
     * unresolved images, we can stop.
     */
    if (
      processedCards.size ===
      cards().length &&
      !cards().some(
        (card) =>
          hasImagesNeedingAttention(
            card
          )
      )
    ) {
      /*
       * One extra pass is deliberately not performed here.
       * The outer loop already provides sufficient coverage.
       */
      break;
    }
  }

  /*
   * Restore the comment panel's top position.
   */
  commentScroller.scrollTop = 0;
}

/* -------------------------------------------------------------------------- */
/* Legacy post-processing                                                     */
/* -------------------------------------------------------------------------- */

function fixLegacyTables() {
  /*
   * Merge orphaned table-cell text blocks.
   *
   * Feishu sometimes renders the content block of a table cell as a
   * top-level block while the table cell itself is mounted.
   */
  document
    .querySelectorAll(
      ".docx-table-block"
    )
    .forEach((tableBlock) => {
      const tds =
        tableBlock.querySelectorAll(
          'td[data-block-type="table_cell"]'
        );

      for (
        const td of tds
      ) {
        const tdId =
          td.getAttribute(
            "data-block-id"
          );

        if (!tdId) {
          continue;
        }

        const ruw =
          td.querySelector(
            ".render-unit-wrapper"
          );

        if (!ruw) {
          continue;
        }

        if (
          ruw.querySelector(
            "[data-block-id]"
          )
        ) {
          continue;
        }

        const textBlockId =
          String(
            parseInt(tdId, 10) + 1
          );

        /*
         * Search the current document for the orphan.
         *
         * At this stage the top-level wrapper has already been rebuilt.
         */
        const textBlock =
          document.querySelector(
            `[data-block-id="${textBlockId}"].docx-text-block`
          );

        if (!textBlock) {
          continue;
        }

        ruw.innerHTML = "";

        ruw.appendChild(
          textBlock
        );
      }
    });
}

function fixLegacyTableStyles() {
  /*
   * table width placeholder.
   */
  document
    .querySelectorAll(
      ".docx-table-block table.table"
    )
    .forEach((tbl) => {
      const style =
        tbl.getAttribute("style") ||
        "";

      const widthMatch =
        style.match(
          /width:\s*(\d+(?:\.\d+)?)px/
        );

      if (
        widthMatch &&
        parseFloat(
          widthMatch[1]
        ) < 50
      ) {
        tbl.style.removeProperty(
          "width"
        );
      }
    });

  /*
   * scrollable wrapper.
   */
  document
    .querySelectorAll(
      ".docx-table-block .scrollable-wrapper"
    )
    .forEach((el) => {
      const width =
        el.style.width;

      if (
        !width ||
        width === "0px"
      ) {
        el.style.setProperty(
          "width",
          "fit-content",
          "important"
        );
      }

      if (
        el.style.left &&
        el.style.left !== "0px"
      ) {
        el.style.removeProperty(
          "left"
        );
      }
    });

  /*
   * scrollable container.
   */
  document
    .querySelectorAll(
      ".docx-table-block .scrollable-container"
    )
    .forEach((el) => {
      const width =
        el.style.width;

      if (
        !width ||
        width === "0px"
      ) {
        el.style.setProperty(
          "width",
          "fit-content",
          "important"
        );
      }
    });

  /*
   * content scroller.
   */
  document
    .querySelectorAll(
      ".docx-table-block .content-scroller"
    )
    .forEach((el) => {
      el.style.setProperty(
        "overflow",
        "visible",
        "important"
      );

      el.style.setProperty(
        "max-width",
        "none",
        "important"
      );
    });

  /*
   * table horizontal position.
   */
  document
    .querySelectorAll(
      ".scrollable-item"
    )
    .forEach((el) => {
      if (
        el.style.left &&
        el.style.left !== "0px"
      ) {
        el.style.setProperty(
          "left",
          "0px",
          "important"
        );
      }
    });
}

function fixLegacyImageStyles() {
  document
    .querySelectorAll(
      ".docx-image-block " +
      ".image-block-width-wrapper"
    )
    .forEach((el) => {
      const width =
        el.style.width;

      if (!width) {
        return;
      }

      const match =
        width.match(
          /^(\d+(?:\.\d+)?)px$/
        );

      if (
        match &&
        parseFloat(
          match[1]
        ) > 1000
      ) {
        el.style.removeProperty(
          "width"
        );
      }
    });
}

function cleanupVirtualArtifacts() {
  /*
   * Empty paragraphs.
   */
  document
    .querySelectorAll(
      "[data-block-id].isEmpty"
    )
    .forEach((el) => {
      el.remove();
    });

  /*
   * Virtual placeholders.
   */
  document
    .querySelectorAll(
      ".bear-virtual-renderUnit-placeholder"
    )
    .forEach((el) => {
      el.remove();
    });

  document
    .querySelectorAll(
      ".bear-virtual-pre-renderer"
    )
    .forEach((el) => {
      el.remove();
    });

  document
    .querySelectorAll(
      ".adit-virtual-scroll-placeholder, " +
      ".fixed-size-list-placeholder"
    )
    .forEach((el) => {
      el.remove();
    });
}

function rebuildCatalogue(
  collectedCatalogueItems
) {
  if (
    !collectedCatalogueItems ||
    collectedCatalogueItems.size === 0
  ) {
    return;
  }

  const catList =
    document.querySelector(
      ".catalogue__list"
    );

  if (!catList) {
    return;
  }

  /*
   * Build heading order from the already rebuilt document.
   */
  const headingOrder =
    new Map();

  document
    .querySelectorAll(
      '[data-block-type^="heading"]'
    )
    .forEach((heading) => {
      const rid =
        heading.getAttribute(
          "data-record-id"
        );

      if (rid) {
        headingOrder.set(
          rid,
          headingOrder.size
        );
      }
    });

  const sortedItems =
    Array.from(
      collectedCatalogueItems.entries()
    ).sort((a, b) => {
      const posA =
        headingOrder.has(a[0])
          ? headingOrder.get(a[0])
          : 999999;

      const posB =
        headingOrder.has(b[0])
          ? headingOrder.get(b[0])
          : 999999;

      return posA - posB;
    });

  catList
    .querySelectorAll(
      ".catalogue__list-item, " +
      ".fixed-size-list-placeholder"
    )
    .forEach((el) => {
      el.remove();
    });

  for (
    const [, itemEl]
      of sortedItems
  ) {
    catList.appendChild(
      itemEl
    );
  }
}

/* -------------------------------------------------------------------------- */
/* Final image pass                                                           */
/* -------------------------------------------------------------------------- */

async function finalImagePass() {
  /*
   * After rebuilding the DOM, some image nodes may have been cloned into
   * their final position. Run one final conversion pass before SingleFile
   * takes the snapshot.
   */
  const roots = [
    document.querySelector(
      ".bear-web-x-container"
    ),
    document.querySelector(
      "#innerdocbody"
    ),
  ].filter(Boolean);

  for (
    const root of roots
  ) {
    const images =
      getImages(root);

    if (images.length === 0) {
      continue;
    }

    /*
     * Don't wait 3s here unless there is actually an unresolved image.
     */
    if (
      hasImagesNeedingAttention(root)
    ) {
      await prepareViewport(root);
    }

    await convertImagesInElement(
      root
    );
  }

  /*
   * Comments may now contain their final cloned image nodes.
   */
  const commentCards =
    document.querySelectorAll(
      ".js-panel-card"
    );

  for (
    const card of commentCards
  ) {
    if (
      hasImagesNeedingAttention(card)
    ) {
      await prepareViewport(card);
    }

    await convertImagesInElement(
      card
    );
  }
}

/* -------------------------------------------------------------------------- */
/* Layout expansion                                                           */
/* -------------------------------------------------------------------------- */

function expandForCapture() {
  const expandSelectors = [
    "html, body",
    "#mainBox",
    "#mainContainer",
    ".app-main-container",
    ".app-main",
    ".suite-body",
    ".garr-container",
    ".bear-web-x-container",
  ];

  for (
    const sel of expandSelectors
  ) {
    document
      .querySelectorAll(sel)
      .forEach((el) => {
        el.style.setProperty(
          "height",
          "auto",
          "important"
        );

        el.style.setProperty(
          "overflow",
          "visible",
          "important"
        );

        el.style.setProperty(
          "max-height",
          "none",
          "important"
        );
      });
  }

  /*
   * mainContainer is absolute in the live application.
   * Keep it in normal flow for the snapshot.
   */
  const mainContainer =
    document.querySelector(
      "#mainContainer"
    );

  if (mainContainer) {
    mainContainer.style.setProperty(
      "position",
      "static",
      "important"
    );
  }

  const appMain =
    document.querySelector(
      ".app-main"
    );

  if (appMain) {
    appMain.style.setProperty(
      "min-width",
      "0",
      "important"
    );

    appMain.style.setProperty(
      "width",
      "auto",
      "important"
    );
  }

  /*
   * Catalogue.
   */
  const catContainer =
    document.querySelector(
      ".catalogue-container"
    );

  if (catContainer) {
    catContainer.style.setProperty(
      "height",
      "auto",
      "important"
    );

    catContainer.style.setProperty(
      "overflow",
      "visible",
      "important"
    );

    catContainer.style.setProperty(
      "position",
      "absolute",
      "important"
    );

    catContainer.style.setProperty(
      "top",
      "0",
      "important"
    );
  }

  const cat =
    document.querySelector(
      ".catalogue"
    );

  if (cat) {
    cat.style.setProperty(
      "position",
      "static",
      "important"
    );

    cat.style.setProperty(
      "height",
      "auto",
      "important"
    );
  }

  const catScroller =
    document.querySelector(
      ".catalogue__scroller"
    );

  if (catScroller) {
    catScroller.style.setProperty(
      "max-height",
      "none",
      "important"
    );

    catScroller.style.setProperty(
      "overflow",
      "visible",
      "important"
    );
  }
}

/* -------------------------------------------------------------------------- */
/* New Feishu section navigation                                              */
/* -------------------------------------------------------------------------- */

async function processSectionNav() {
  const nav = document.querySelector(
    ".section-nav-container .section-nav"
  );

  const scroller = nav?.querySelector(
    ".entries-container"
  );

  const list = scroller?.querySelector(
    "ul.full-entries"
  );

  if (!nav || !scroller || !list) {
    return;
  }

  /*
   * Feishu virtualizes the left document outline independently from the
   * document body.  Collect every mounted entry first, while keeping the
   * original scrolling element untouched during the scan.
   */
  const collected = new Map();

  const collectVisibleEntries = () => {
    list
      .querySelectorAll("li.full-entry[data-guid]")
      .forEach((item) => {
        const guid = item.getAttribute("data-guid");

        if (guid && !collected.has(guid)) {
          collected.set(guid, item.cloneNode(true));
        }
      });
  };

  scroller.scrollTop = 0;
  await sleep(INITIAL_RENDER_DELAY);
  collectVisibleEntries();

  let totalScroll = scroller.scrollHeight;
  const step = Math.max(
    Math.floor(scroller.clientHeight * 0.7),
    100
  );

  let lastCount = -1;

  /*
   * A few passes are intentional.  Some Feishu versions update the virtual
   * list's scrollHeight only after the newly visible entries have mounted.
   */
  for (
    let pass = 0;
    pass < 5 && collected.size !== lastCount;
    pass++
  ) {
    lastCount = collected.size;

    for (
      let y = 0;
      y <= totalScroll + 1000;
      y += step
    ) {
      scroller.scrollTop = y;
      await sleep(SCROLL_RENDER_DELAY);

      if (scroller.scrollHeight > totalScroll) {
        totalScroll = scroller.scrollHeight;
      }

      collectVisibleEntries();
    }

    scroller.scrollTop = totalScroll;
    await sleep(SCROLL_RENDER_DELAY);
    collectVisibleEntries();
  }

  if (collected.size === 0) {
    scroller.scrollTop = 0;
    return;
  }

  /*
   * Replace the virtualized list with the complete set of entries.
   */
  list.replaceChildren(
    ...Array.from(collected.values())
  );

  /*
   * The original .entries-container is controlled by Feishu's virtual
   * scrolling implementation (PerfectScrollbar in the current renderer).
   * Merely inserting all <li> elements is not enough: its cached scroll
   * metrics still describe the virtual list, so the resulting SingleFile
   * snapshot can contain all entries in HTML while the visible catalogue
   * itself cannot be scrolled.
   *
   * Clone the completed container and replace the original node.  This
   * deliberately detaches Feishu's virtual-scroll listeners/instance and
   * turns the catalogue into an ordinary native scroll container.
   */
  const viewportHeight =
    scroller.clientHeight ||
    nav.clientHeight ||
    parseFloat(getComputedStyle(nav).maxHeight) ||
    598;

  const replacement = scroller.cloneNode(true);

  replacement
    .querySelectorAll(
      ".ps__rail-x, .ps__rail-y"
    )
    .forEach((el) => el.remove());

  replacement.classList.remove(
    "ps",
    "ps--active-y",
    "ps--active-x"
  );

  replacement.style.setProperty(
    "height",
    `${viewportHeight}px`,
    "important"
  );

  replacement.style.setProperty(
    "max-height",
    `${viewportHeight}px`,
    "important"
  );

  replacement.style.setProperty(
    "overflow-y",
    "auto",
    "important"
  );

  replacement.style.setProperty(
    "overflow-x",
    "hidden",
    "important"
  );

  replacement.style.setProperty(
    "overscroll-behavior",
    "contain",
    "important"
  );

  scroller.replaceWith(replacement);

  replacement.scrollTop = 0;
}

/* -------------------------------------------------------------------------- */
/* Main                                                                       */
/* -------------------------------------------------------------------------- */

async function before(url, config) {
  /*
   * Wait for either Feishu renderer to appear.
   */
  await waitForSelector(
    ".render-unit-wrapper, " +
    ".page-block.root-block, " +
    "#innerdocbody, " +
    ".etherpad-container-wrapper",
    30000
  );

  /*
   * Newer renderer.
   */
  const newScroller =
    document.querySelector(
      ".etherpad-container-wrapper"
    );

  const newWrapper =
    document.querySelector(
      "#innerdocbody"
    );

  let legacyResult = null;

  /*
   * The section navigation is a separate virtual list.  Materialize it
   * before touching the document body, then detach its virtual-scroll
   * implementation so it cannot interfere with later body collection.
   */
  await processSectionNav();

  if (
    newScroller &&
    newWrapper
  ) {
    await collectNewFeishu(
      newScroller,
      newWrapper
    );
  } else {
    /*
     * Legacy renderer.
     */
    const scroller =
      document.querySelector(
        ".bear-web-x-container"
      );

    if (!scroller) {
      /*
       * Fallback for pages that use normal window scrolling.
       */
      for (
        let i = 0;
        i < 200;
        i++
      ) {
        window.scrollBy(
          0,
          window.innerHeight
        );

        await sleep(100);

        if (
          window.innerHeight +
            window.scrollY >=
          document.body.scrollHeight
        ) {
          break;
        }
      }

      window.scrollTo(
        0,
        0
      );

      await sleep(
        INITIAL_RENDER_DELAY
      );
    } else {
      const wrapper =
        findMainLegacyWrapper(
          scroller
        );

      if (wrapper) {
        legacyResult =
          await collectLegacyFeishu(
            scroller,
            wrapper
          );
      }
    }
  }

  /*
   * Comments are independent from the body virtual list.
   */
  await processFeishuComments();

  /*
   * Legacy-specific repairs.
   */
  if (legacyResult) {
    fixLegacyTables();
    fixLegacyTableStyles();
    fixLegacyImageStyles();

    rebuildCatalogue(
      legacyResult.collectedCatalogueItems
    );
  }

  /*
   * Remove virtual DOM artifacts.
   */
  cleanupVirtualArtifacts();

  /*
   * Final image conversion.
   */
  await finalImagePass();

  /*
   * Expand layout only AFTER all virtual scrolling and image work is done.
   */
  expandForCapture();

  /*
   * Give the browser one layout frame before SingleFile captures.
   */
  await sleep(500);
}