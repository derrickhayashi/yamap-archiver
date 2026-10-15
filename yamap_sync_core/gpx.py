from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from email.message import Message
from pathlib import Path
from urllib.parse import unquote

from .errors import YamapError


def filename_from_content_disposition(headers: Message, activity_id_value: int) -> str:
    value = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.IGNORECASE)
    if not match:
        match = re.search(r'filename="?([^";]+)', value, flags=re.IGNORECASE)
    name = match.group(1) if match else f"yamap_activity_{activity_id_value}.gpx"
    name = unquote(name).replace("/", "_").replace("\\", "_")
    return re.sub(r'[<>:"|?*\r\n]+', "_", name)


def validate_gpx(body: bytes) -> int:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise YamapError("Downloaded GPX is not valid XML") from exc
    count = sum(1 for item in root.iter() if item.tag.endswith("trkpt"))
    if not count:
        raise YamapError("Downloaded GPX contains no trackpoints")
    return count


def parse_gpx_segments(path: Path) -> tuple[list[list[list[float]]], int]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise YamapError(f"Could not parse GPX {path}: {exc}") from exc
    segments: list[list[list[float]]] = []
    trackpoints = 0
    for segment_node in root.findall(".//{*}trkseg"):
        segment: list[list[float]] = []
        for point in segment_node.findall("{*}trkpt"):
            try:
                latitude, longitude = float(point.attrib["lat"]), float(point.attrib["lon"])
            except (KeyError, TypeError, ValueError) as exc:
                raise YamapError(f"GPX {path} contains an invalid trackpoint") from exc
            if not (math.isfinite(latitude) and math.isfinite(longitude)):
                raise YamapError(f"GPX {path} contains a non-finite coordinate")
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise YamapError(f"GPX {path} contains an out-of-range coordinate")
            segment.append([round(longitude, 6), round(latitude, 6)])
            trackpoints += 1
        if segment:
            segments.append(segment)
    if not segments:
        raise YamapError(f"GPX {path} contains no track segments")
    return segments, trackpoints
