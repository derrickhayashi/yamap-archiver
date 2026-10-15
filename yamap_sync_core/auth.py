from __future__ import annotations

import os
import time
import tempfile
from pathlib import Path

from .errors import YamapError

TOKEN_COOKIE = "yamap_token"


def token_from_sources(token_file: Path | None) -> str | None:
    token = os.environ.get("YAMAP_TOKEN", "").strip()
    if token:
        return token
    if token_file:
        try:
            token = token_file.expanduser().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return token or None
    return None


def save_token(path: Path, token: str) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".yamap-token-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token.strip() + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)



def browser_login(token_file: Path, timeout_seconds: int) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise YamapError("The login command needs Playwright: python -m pip install 'yamap-archiver[login]'") from exc

    print("A browser window will open. Sign in to YAMAP there; credentials are never read by this script.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://yamap.com/login")
        deadline = time.monotonic() + timeout_seconds
        token: str | None = None
        while time.monotonic() < deadline:
            for cookie in context.cookies("https://yamap.com"):
                if cookie["name"] == TOKEN_COOKIE and cookie.get("value"):
                    token = str(cookie["value"])
                    break
            if token:
                break
            page.wait_for_timeout(1000)
        browser.close()
    if not token:
        raise YamapError(f"Login was not detected within {timeout_seconds} seconds")
    save_token(token_file, token)
    print(f"Saved a private token file with owner-only permissions: {token_file.expanduser()}")
