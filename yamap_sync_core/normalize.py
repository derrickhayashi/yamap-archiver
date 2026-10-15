from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import YamapError


def local_datetime(timestamp: int | float, offset_hours: int | float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=float(offset_hours))))


def timestamp_iso(timestamp: int | float | None, offset_hours: int | float) -> str | None:
    return None if timestamp is None else local_datetime(timestamp, offset_hours).isoformat(timespec="seconds")


def duration_hhmm(seconds: int | float) -> str:
    hours, minutes = divmod(int(float(seconds)) // 60, 60)
    return f"{hours:02d}:{minutes:02d}"


def format_area(raw: dict[str, Any]) -> str:
    activity_map = raw.get("map") or {}
    name = str(activity_map.get("name") or "")
    prefectures = [str(item["name"]) for item in activity_map.get("prefectures") or [] if item.get("name")]
    return f"{name}({','.join(prefectures)})" if name and prefectures else name


def format_distance(meters: int | float) -> str:
    value = float(meters or 0)
    return f"{math.floor(value)} m" if value < 1000 else f"{math.floor(value / 100) / 10:.1f} km"


def format_location(coord: Any) -> dict[str, float] | None:
    if not isinstance(coord, (list, tuple)) or len(coord) != 2:
        return None
    try:
        longitude, latitude = (float(coord[0]), float(coord[1]))
    except (TypeError, ValueError):
        return None
    return {"latitude": latitude, "longitude": longitude}


def altitude_m(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def normalize_activity(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        activity_id = int(raw["id"])
        started_at = int(raw["start_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise YamapError("Activity is missing a valid id or start_at value") from exc
    whole = raw.get("activity_whole_section") or {}
    offset = raw.get("time_zone", 9)
    total_time = whole.get("total_time", raw.get("duration", 0))
    distance = whole.get("distance", raw.get("distance", 0))
    cumulative_up = whole.get("cumulative_up", raw.get("cumulative_up", 0))
    cumulative_down = whole.get("cumulative_down", raw.get("cumulative_down", 0))
    activity_type = raw.get("activity_type") or {}
    return {
        "date": local_datetime(started_at, offset).date().isoformat(),
        "title": str(raw.get("title") or ""), "area": format_area(raw),
        "time": duration_hhmm(total_time or 0),
        "active_time": duration_hhmm(whole.get("active_time", total_time) or 0),
        "rest_time": duration_hhmm(whole.get("rest_time", 0) or 0),
        "distance": format_distance(distance or 0),
        "elevation_gain": f"{round(float(cumulative_up or 0))} m",
        "elevation_loss": f"{round(float(cumulative_down or 0))} m",
        "activity_type": str(activity_type.get("name") or ""),
        "start_at": timestamp_iso(raw.get("start_at"), offset),
        "finish_at": timestamp_iso(raw.get("finish_at"), offset),
        "start_altitude_m": altitude_m(whole.get("start_altitude")),
        "stop_altitude_m": altitude_m(whole.get("stop_altitude")),
        "start_location": format_location(whole.get("start_coord")),
        "stop_location": format_location(whole.get("stop_coord")),
        "private": str(raw.get("public_type") or "") != "public",
        "url": f"https://yamap.com/activities/{activity_id}",
    }
