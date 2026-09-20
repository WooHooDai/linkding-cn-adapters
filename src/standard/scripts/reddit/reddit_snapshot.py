"""Reddit snapshot replace hook.

Delegates to the built-in SingleFile engine. Re-attaches JS browser before
hooks, which the framework skips when a replace hook is present.

Auth/cookie handling is managed by the framework (credential store + auth config).
"""

import logging

logger = logging.getLogger(__name__)


def replace(url: str, config: dict, output_path: str) -> None:
    # Re-attach JS browser before hooks (the framework skips them when a
    # replace hook is present, so we must set it ourselves for delegation).
    scripts = config.get("scripts") or []
    browser_before = []
    for entry in scripts:
        path = entry.get("path", "")
        if entry.get("hook") == "before" and path.endswith(".js"):
            browser_before.append(path)
    if browser_before:
        config["_browser_before_scripts"] = browser_before

    from site_adapters.services.engine import create_snapshot
    create_snapshot(url, output_path, config)
