from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Protocol

from .errors import YamapError
from .normalize import normalize_activity

PROFILE_URL = "https://yamap.com/users/{profile_id}"
DEFAULT_PER_PAGE = 24


class ActivityPageClient(Protocol):
    token: str | None
    def list_activity_page(self, profile_id: int, page: int, per_page: int) -> dict[str, Any]: ...


def activity_id(record: dict[str, Any]) -> int:
    match = re.search(r"/activities/(\d+)(?:/)?$", str(record.get("url", "")))
    if not match:
        raise YamapError(f"Activity has no recognizable YAMAP URL: {record.get('url')!r}")
    return int(match.group(1))


def discover_activities(client: ActivityPageClient, *, profile_id: int,
                        per_page: int = DEFAULT_PER_PAGE,
                        max_pages: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if profile_id <= 0 or per_page <= 0 or (max_pages is not None and max_pages <= 0):
        raise YamapError("Profile ID, page size and optional page limit must be positive")
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    expected_total: int | None = None
    total_pages: int | None = None
    page = 1
    while total_pages is None or page <= total_pages:
        if max_pages is not None and page > max_pages:
            break
        payload = client.list_activity_page(profile_id, page, per_page)
        raw_activities, meta = payload.get("activities"), payload.get("meta")
        if not isinstance(raw_activities, list) or not isinstance(meta, dict):
            raise YamapError(f"Page {page} has an unexpected response shape")
        try:
            page_total, count_total = int(meta["total_pages"]), int(meta["total_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise YamapError("Activity pagination metadata is invalid") from exc
        if page_total < 0 or count_total < 0 or (page_total == 0 and count_total != 0):
            raise YamapError("Activity pagination metadata is inconsistent")
        if total_pages is None:
            total_pages, expected_total = page_total, count_total
        elif page_total != total_pages or count_total != expected_total:
            raise YamapError("Activity count changed during pagination; retry the discovery run")
        for raw in raw_activities:
            normalized = normalize_activity(raw)
            item_id = activity_id(normalized)
            if item_id in seen:
                raise YamapError(f"Duplicate activity ID {item_id} returned on page {page}")
            seen.add(item_id)
            records.append(normalized)
        page += 1
    complete = max_pages is None or (total_pages is not None and max_pages >= total_pages)
    if complete and expected_total is not None and len(records) != expected_total:
        raise YamapError(f"Expected {expected_total} activities but received {len(records)} unique records")
    return records, {"profile_id": profile_id, "profile_url": PROFILE_URL.format(profile_id=profile_id),
                     "authenticated": bool(client.token), "expected_total": expected_total,
                     "fetched": len(records), "pages_fetched": page - 1,
                     "total_pages": total_pages, "complete": complete}


def read_activity_file(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise YamapError(f"Could not read activity file {path}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise YamapError(f"Activity file {path} must contain a JSON list of objects")
    return data


def compare_records(current: Iterable[dict[str, Any]], discovered: Iterable[dict[str, Any]]) -> dict[str, Any]:
    current_ids = {activity_id(item) for item in current}
    discovered_ids = {activity_id(item) for item in discovered}
    return {"new_ids": sorted(discovered_ids - current_ids, reverse=True),
            "not_visible_ids": sorted(current_ids - discovered_ids, reverse=True),
            "unchanged_ids": len(current_ids & discovered_ids)}
