const builtin_engine = "singlefile";

async function before(url, config) {
  // 先滚动触发懒加载，等 React 完成相关区块的插入/重建后再做移除——
  // 若先移除再滚动，React 可能在滚动期间重新渲染并恢复被删的属性/节点。
  window.scrollTo(0, document.body.scrollHeight);
  await new Promise((resolve) => setTimeout(resolve, 2500));
  window.scrollTo(0, 0);
  await new Promise((resolve) => setTimeout(resolve, 800));

  // 1) 移除登录/注册区块（语言无关：英文 "Log in or sign up" / 中文 "登录或注册"）。
  // 区块位于 Column body 内部且只有运行时哈希类名，声明式 remove_elements 无法稳定命中。
  // 定位方式：文本以登录标题开头、且内部包含唯一稳定锚点 a[href*="show_choice_screen"]
  // （"Log in with username instead"）的容器。注意不能用 querySelector 直接取该链接——
  // 左导航栏也有同 href 的 "Log in" 链接，需以容器文本+链接存在性双重约束精确定位。
  // 且需取文本最短的容器（登录区块本体）：中文布局下页脚可能嵌套在同一容器内，
  // 若取首个匹配可能误删包含登录区的外层包装。
  const columnBody = document.querySelector('[aria-label="Column body"]');
  const loginCandidates = [...document.querySelectorAll("div")].filter((el) => {
    const text = (el.textContent || "").trim();
    return (
      text.length > 25 &&
      text.length < 1500 &&
      /^(log in or sign up|登录|注册)/i.test(text) &&
      el.querySelector('a[href*="show_choice_screen"]') &&
      // 防御：绝不移除包含正文列容器的元素
      !(columnBody && el.contains(columnBody))
    );
  });
  loginCandidates.sort(
    (a, b) => (a.textContent || "").length - (b.textContent || "").length
  );
  if (loginCandidates.length) {
    loginCandidates[0].remove();
  }

  // 2) 移除 Related threads（相关主题）区块：快照只保留主帖内容，相关帖标题/文本/图片
  // 不应进入离线快照。该区块以 "Related threads" / "相关主题" 标题开头，取文本最短的容器精确定位。
  // 注：不要移除 data-pagelet——defuddle 的 ThreadsExtractor 会按 data-pagelet 定位主帖
  // （pagelet 分支），该分支只提取主帖及其全部图片、不混入相关帖；移除该属性会强制走
  // region 分支遍历整列，反而把相关帖文本混入阅读模式。
  const relatedCandidates = [...document.querySelectorAll("div")].filter((el) => {
    const text = (el.textContent || "").trim();
    return (
      text.length > 15 &&
      text.length < 5000 &&
      /^(related threads|相关主题|相關主題|相关帖子|相關帖子)/i.test(text)
    );
  });
  relatedCandidates.sort(
    (a, b) => (a.textContent || "").length - (b.textContent || "").length
  );
  // 3) defuddle ThreadsExtractor 兼容修复（阅读模式图片/正文缺失的根因修复）：
  //    a. SingleFile 序列化会把相对链接绝对化（a[href^="/@"] → https://www.threads.com/@），
  //       ThreadsExtractor 依赖 a[href^="/@"] 定位帖子卡片与提取作者，绝对化后匹配失败，
  //       阅读模式提取为空。改回相对路径。
  //    b. 中文"登录优先"布局下主帖卡片可能不含任何 @ 链接，getUsername 拿不到作者，
  //       同样导致提取为空。为缺少 @ 锚点的 pressable 容器注入隐藏锚点
  //       （handle 取自 og:title 的 (@handle) 部分）。
  document
    .querySelectorAll('a[href^="https://www.threads.com/@"]')
    .forEach((a) => {
      a.setAttribute("href", a.getAttribute("href").replace("https://www.threads.com", ""));
    });
  const ogTitle =
    document.querySelector('meta[property="og:title"]')?.getAttribute("content") || "";
  const handleMatch = ogTitle.match(/\(@([^)]+)\)/);
  const handle = handleMatch ? handleMatch[1] : "";
  if (handle) {
    document.querySelectorAll("[data-pressable-container]").forEach((pc) => {
      // 只处理顶层 pressable 容器
      if (pc.parentElement.closest("[data-pressable-container]")) return;
      if (pc.querySelector('a[href^="/@"]')) return;
      const anchor = document.createElement("a");
      anchor.setAttribute("href", "/@" + handle);
      anchor.setAttribute("role", "link");
      anchor.textContent = handle;
      anchor.setAttribute("style", "display:none");
      pc.appendChild(anchor);
      const timeEl = document.createElement("time");
      timeEl.setAttribute("datetime", new Date().toISOString());
      timeEl.setAttribute("style", "display:none");
      pc.appendChild(timeEl);
    });
  }
}

async function after(url, config) {}
