from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .archive import activity_id
from .artifacts import read_json_list, unique_by
from .errors import YamapError
from .gpx import parse_gpx_segments

JAPAN_BOUNDS = (122.0, 24.0, 154.0, 46.0)


def route_region(segments: Iterable[Iterable[list[float]]]) -> str:
    coordinates = [coordinate for segment in segments for coordinate in segment]
    longitude = sum(point[0] for point in coordinates) / len(coordinates)
    latitude = sum(point[1] for point in coordinates) / len(coordinates)
    min_lon, min_lat, max_lon, max_lat = JAPAN_BOUNDS
    return "japan" if min_lon <= longitude <= max_lon and min_lat <= latitude <= max_lat else "overseas"


def build_route_geojson(activities_path: Path, manifest_path: Path, gpx_dir: Path) -> dict[str, Any]:
    activities = read_json_list(activities_path, "activity file")
    manifest = read_json_list(manifest_path, "GPX manifest")
    activities_by_id = unique_by(
        ({**item, "activity_id": str(activity_id(item))} for item in activities), "activity_id", "activity file"
    )
    manifest_by_id = unique_by(manifest, "activity_id", "GPX manifest")
    if set(activities_by_id) != set(manifest_by_id):
        missing = sorted(set(activities_by_id) - set(manifest_by_id))
        extra = sorted(set(manifest_by_id) - set(activities_by_id))
        raise YamapError(f"Activity/manifest ID mismatch; missing={missing}, extra={extra}")

    gpx_dir = gpx_dir.expanduser()
    if not gpx_dir.is_dir():
        raise YamapError(f"GPX directory does not exist: {gpx_dir}")
    expected_files = {str(item.get("gpx_file") or "") for item in manifest}
    if "" in expected_files:
        raise YamapError("GPX manifest entry is missing gpx_file")
    actual_files = {path.name for path in gpx_dir.glob("*.gpx")}
    if actual_files != expected_files:
        raise YamapError(
            f"GPX file mismatch; missing={sorted(expected_files - actual_files)}, "
            f"extra={sorted(actual_files - expected_files)}"
        )

    features: list[dict[str, Any]] = []
    all_longitudes: list[float] = []
    all_latitudes: list[float] = []
    total_trackpoints = 0
    region_counts = {"japan": 0, "overseas": 0}
    for activity in activities:
        item_id = str(activity_id(activity))
        manifest_item = manifest_by_id[item_id]
        segments, trackpoints = parse_gpx_segments(gpx_dir / str(manifest_item["gpx_file"]))
        try:
            expected_trackpoints = int(manifest_item["trackpoints"])
        except (KeyError, TypeError, ValueError) as exc:
            raise YamapError(f"Manifest entry {item_id} has an invalid trackpoint count") from exc
        if trackpoints != expected_trackpoints:
            raise YamapError(
                f"Trackpoint mismatch for activity {item_id}: manifest={expected_trackpoints}, GPX={trackpoints}"
            )
        region = route_region(segments)
        region_counts[region] += 1
        total_trackpoints += trackpoints
        for segment in segments:
            all_longitudes.extend(point[0] for point in segment)
            all_latitudes.extend(point[1] for point in segment)
        features.append({"type": "Feature", "id": item_id,
                         "properties": {"activity_id": item_id, "title": activity.get("title", ""),
                                        "date": activity.get("date", ""), "area": activity.get("area", ""),
                                        "activity_type": activity.get("activity_type", ""),
                                        "private": bool(activity.get("private")), "url": activity.get("url", ""),
                                        "gpx_file": manifest_item["gpx_file"], "trackpoints": trackpoints,
                                        "region": region},
                         "geometry": {"type": "MultiLineString", "coordinates": segments}})
    return {"type": "FeatureCollection",
            **({"bbox": [min(all_longitudes), min(all_latitudes), max(all_longitudes), max(all_latitudes)]}
               if all_longitudes else {}),
            "metadata": {"activities": len(features), "trackpoints": total_trackpoints,
                         "regions": region_counts, "coordinate_precision": 6},
            "features": features}
