const builtin_engine = "singlefile";

async function before(url, config) {
  document.querySelector("button.QuestionRichText-more")?.click();  // 展开问题详情
}

async function after(url, config) {
}
