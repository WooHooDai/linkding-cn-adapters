const builtin_engine = "singlefile";

// Click all "load all replies" buttons, retrying until none remain or max rounds hit.
// Each click may reveal more nested buttons, so we loop until stable.

async function before(url, config) {
  const MAX_ROUNDS = 30;
  const CLICK_DELAY = 400;

  for (let round = 0; round < MAX_ROUNDS; round++) {
    const buttons = document.querySelectorAll("button.comment-children__load-all");
    if (!buttons.length) break;

    for (const btn of buttons) {
      try { btn.click(); } catch (e) {}
    }
    await sleep(CLICK_DELAY);
  }
}

function sleep(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}
