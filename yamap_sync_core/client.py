from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import YamapError
from .models import HttpResponse

API_BASE_URL = "https://api.yamap.com/v6"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


class YamapClient:
    """Small HTTP client for the endpoints used by the YAMAP web app."""

    def __init__(self, *, token: str | None = None, base_url: str = API_BASE_URL,
                 delay_seconds: float = 0.5, timeout_seconds: float = 30.0,
                 retries: int = 3) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self._last_request_at: float | None = None

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.delay_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _request(self, path: str, *, params: dict[str, Any] | None = None) -> HttpResponse:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urlencode(params)}"
        headers = {
            "Accept": "application/json, application/gpx+xml, application/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "ja",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer token={self.token}"

        for attempt in range(self.retries + 1):
            self._pace()
            request = Request(url, headers=headers)
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return HttpResponse(response.read(), response.headers, response.status)
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == self.retries:
                    auth_hint = " Authentication may be required." if exc.code in {401, 403} else ""
                    raise YamapError(f"YAMAP returned HTTP {exc.code}.{auth_hint}".strip()) from exc
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                time.sleep(min(wait, 30.0))
            except URLError as exc:
                if attempt == self.retries:
                    raise YamapError(f"Could not reach YAMAP: {exc.reason}") from exc
                time.sleep(2**attempt)
            finally:
                self._last_request_at = time.monotonic()
        raise AssertionError("retry loop ended unexpectedly")

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(path, params=params)
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise YamapError(f"YAMAP returned invalid JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise YamapError(f"YAMAP returned an unexpected JSON shape for {path}")
        return payload

    def list_activity_page(self, profile_id: int, page: int, per_page: int) -> dict[str, Any]:
        return self.get_json(f"users/{profile_id}/activities", params={"page": page, "per": per_page})

    def download_gpx(self, activity_id: int) -> HttpResponse:
        if not self.token:
            raise YamapError("GPX export requires a YAMAP token. Run the login command or set YAMAP_TOKEN.")
        return self._request(f"activities/{activity_id}/points.xml")
