from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .archive import DEFAULT_PER_PAGE, compare_records, discover_activities, read_activity_file
from .artifacts import write_geojson_safely, write_json_safely
from .auth import browser_login, token_from_sources
from .client import API_BASE_URL, YamapClient
from .errors import YamapError
from .sync import sync_archive
from .gpx import filename_from_content_disposition, validate_gpx
from .map_builder import build_route_geojson


def make_client(args: argparse.Namespace) -> YamapClient:
    return YamapClient(token=token_from_sources(args.token_file), base_url=args.api_base_url,
                       delay_seconds=args.delay, timeout_seconds=args.timeout)


def command_discover(args: argparse.Namespace) -> int:
    client = make_client(args)
    records, summary = discover_activities(client, profile_id=args.profile_id,
                                           per_page=args.per_page, max_pages=args.max_pages)
    if args.compare:
        summary["comparison"] = compare_records(read_activity_file(args.compare.expanduser()), records)
    if args.output:
        write_json_safely(args.output, records, overwrite=args.overwrite)
        summary["output"] = str(args.output.expanduser())
    else:
        summary["activities"] = records
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not client.token:
        print("Note: unauthenticated discovery only sees public activities.", file=sys.stderr)
    return 0


def command_download_gpx(args: argparse.Namespace) -> int:
    if not args.confirm_download:
        raise YamapError(
            f"Refusing to download activity {args.activity_id}. "
            "Re-run with --confirm-download after reviewing the exact ID."
        )
    response = make_client(args).download_gpx(args.activity_id)
    trackpoints = validate_gpx(response.body)
    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = args.output_name or filename_from_content_disposition(response.headers, args.activity_id)
    if Path(output_name).name != output_name or output_name in {".", ".."}:
        raise YamapError("--output-name must be a filename without directory components")
    output = output_dir / output_name
    if output.exists() and not args.overwrite:
        raise YamapError(f"Refusing to overwrite {output}; pass --overwrite if that is intentional")
    with tempfile.NamedTemporaryFile("wb", dir=output_dir, delete=False) as handle:
        handle.write(response.body)
        temp_path = Path(handle.name)
    os.replace(temp_path, output)
    print(json.dumps({"activity_id": args.activity_id, "trackpoints": trackpoints, "output": str(output)},
                     ensure_ascii=False))
    return 0


def command_build_map(args: argparse.Namespace) -> int:
    data = build_route_geojson(args.activities, args.manifest, args.gpx_dir)
    write_geojson_safely(args.output, data, overwrite=args.overwrite)
    print(json.dumps({"output": str(args.output.expanduser()), **data["metadata"]},
                     ensure_ascii=False, indent=2))
    return 0


def command_sync(args: argparse.Namespace) -> int:
    archive = args.archive_dir.expanduser().resolve()
    if args.token_file and args.token_file.expanduser().resolve().is_relative_to(archive):
        raise YamapError("Store the token file outside --archive-dir")
    sync_archive(make_client(args), profile_id=args.profile_id, archive_dir=archive,
                 dry_run=args.dry_run,
                 limit=args.limit, from_date=args.from_date, to_date=args.to_date,
                 report=lambda value: print(json.dumps(value, ensure_ascii=False, indent=2), flush=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and prepare a local YAMAP activity archive")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-base-url", default=API_BASE_URL, help=argparse.SUPPRESS)
    common.add_argument("--token-file", type=Path, default=Path("./yamap-token"),
                        help="Token file (default: ./yamap-token; YAMAP_TOKEN takes precedence)")
    common.add_argument("--delay", type=float, default=0.5, help="Minimum delay between requests (default: 0.5s)")
    common.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")

    discover = subparsers.add_parser("discover", parents=[common], help="Read all activity metadata directly")
    discover.add_argument("--profile-id", type=int, required=True, help="YAMAP profile ID")
    discover.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE)
    discover.add_argument("--max-pages", type=int)
    discover.add_argument("--compare", type=Path)
    discover.add_argument("--output", type=Path)
    discover.add_argument("--overwrite", action="store_true")
    discover.set_defaults(func=command_discover)

    login = subparsers.add_parser("login", help="Use a browser only to obtain a session token")
    login.add_argument("--token-file", type=Path, default=Path("./yamap-token"),
                       help="Save session token here (default: ./yamap-token)")
    login.add_argument("--timeout", type=int, default=300)
    login.set_defaults(func=lambda args: (browser_login(args.token_file, args.timeout), 0)[1])

    gpx = subparsers.add_parser("download-gpx", parents=[common], help="Download one explicitly approved GPX")
    gpx.add_argument("activity_id", type=int)
    gpx.add_argument("--output-dir", type=Path, required=True)
    gpx.add_argument("--output-name", help="Optional exact filename for a staged download")
    gpx.add_argument("--confirm-download", action="store_true")
    gpx.add_argument("--overwrite", action="store_true")
    gpx.set_defaults(func=command_download_gpx)

    build_map = subparsers.add_parser("build-map", help="Build map-ready GeoJSON from local GPX files")
    build_map.add_argument("--activities", type=Path, required=True)
    build_map.add_argument("--manifest", type=Path, required=True)
    build_map.add_argument("--gpx-dir", type=Path, required=True)
    build_map.add_argument("--output", type=Path, required=True)
    build_map.add_argument("--overwrite", action="store_true")
    build_map.set_defaults(func=command_build_map)

    sync = subparsers.add_parser("sync", parents=[common], help="Discover and safely update a local archive")
    sync.add_argument("--profile-id", type=int, required=True)
    sync.add_argument("--archive-dir", type=Path, default=Path("./yamap-archive"),
                      help="Archive directory (default: ./yamap-archive)")
    sync.add_argument("--dry-run", action="store_true", help="Report changes without downloads or archive writes")
    sync.add_argument("--limit", type=int, help="Select the latest N activities (including already archived entries)")
    sync.add_argument("--from", dest="from_date", help="Include activities starting on or after YYYY-MM-DD (activity-local date)")
    sync.add_argument("--to", dest="to_date", help="Include activities starting on or before YYYY-MM-DD (activity-local date)")
    sync.set_defaults(func=command_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (YamapError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
