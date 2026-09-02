def before(url, config):
    """Render the note in a real browser before SingleFile captures it."""
    from urllib.parse import urlparse

    from site_adapters.services.engine.browser_provider import launch_browser
    from site_adapters.services.auth.cookies import cookie_string_to_playwright_list

    target_url = config.get("request_url") or url
    timeout_ms = int((config.get("timeout") or 30) * 1000)

    headers = config.get("headers") or {}
    user_agent = headers.get("User-Agent") or headers.get("user-agent") or (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/101.0.0.0 Safari/537.36"
    )

    browser = launch_browser(headless=True)
    context = None
    playwright = getattr(browser, "__playwright__", None)

    try:
        # Match a common desktop viewport instead of Playwright's 1280x720
        # default, which makes xiaohongshu's media container narrower.
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1440, "height": 900},
        )

        cookie_str = config.get("user_cookie") or ""
        if cookie_str:
            domain = urlparse(target_url).hostname or ""
            cookies = cookie_string_to_playwright_list(cookie_str, domain)
            if cookies:
                context.add_cookies(cookies)

        page = context.new_page()
        page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )

        # Let the page's own scripts produce the signed comment API request.
        # SingleFile still captures the returned DOM with scripts blocked.
        try:
            page.wait_for_function(
                """() => {
                    const root = document.querySelector(".comments-el");
                    return root && !root.querySelector(".loading");
                }""",
                timeout=15000,
            )
        except Exception:
            pass

        page.evaluate(
            """async () => {
                window.scrollTo(0, document.body.scrollHeight);
                await new Promise((resolve) => setTimeout(resolve, 300));
                window.scrollTo(0, 0);
            }"""
        )

        return page.content()
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass
        try:
            browser.close()
        except Exception:
            pass
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass
