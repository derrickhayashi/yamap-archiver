"""Staged archive preparation and rollback-capable replacement.

Replacement handles ordinary exceptions, not power loss or process termination.
Callers must hold archive_lock across reading, preparing and replacing an archive.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .archive import activity_id, compare_records, discover_activities, read_activity_file
from .artifacts import read_json_list, unique_by
from .errors import YamapError
from .gpx import parse_gpx_segments
from .map_builder import build_route_geojson

FIELDS = ('date', 'title', 'activity_id', 'gpx_file', 'trackpoints', 'yamap_url')
OUTPUTS = ('gpx', 'yamap_gpx_manifest.json', 'yamap_gpx_manifest.csv',
           'yamap_routes.geojson', 'yamap_activities.json')


class RecoveryRequired(YamapError):
    """Rollback failed; the staging directory must be preserved."""


@contextmanager
def archive_lock(archive_dir: Path):
    """Cooperating writers share a lock beside the archive, outside its outputs."""
    path = archive_dir.parent / f'.{archive_dir.name}.sync.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise YamapError(f'Archive is locked: {path}. Remove only after confirming no sync is running.') from exc
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()


def checked_name(value: object) -> str:
    name = str(value or '')
    if not name or Path(name).name != name or '\\' in name or not name.endswith('.gpx'):
        raise YamapError(f'Invalid GPX filename in manifest: {name!r}')
    return name


def read_archive(data: Path) -> tuple[list[dict], list[dict]]:
    """Accept a new archive or a complete reconciled archive, never a partial one."""
    for name in OUTPUTS:
        if (data / name).is_symlink():
            raise YamapError(f'Archive output cannot be a symlink: {name}')
    present = [(data / name).exists() for name in ('yamap_activities.json', 'yamap_gpx_manifest.json', 'gpx')]
    if not any(present):
        if any((data / name).exists() for name in OUTPUTS):
            raise YamapError('Archive contains derived outputs without its canonical inputs')
        return [], []
    if not all(present):
        raise YamapError('Incomplete archive: activities, manifest and gpx directory are required together')
    activities = read_activity_file(data / 'yamap_activities.json')
    manifest = read_json_list(data / 'yamap_gpx_manifest.json', 'GPX manifest')
    for row in manifest:
        name = checked_name(row.get('gpx_file'))
        if (data / 'gpx' / name).is_symlink():
            raise YamapError('GPX symlinks are not supported')
    if len({row['gpx_file'] for row in manifest}) != len(manifest):
        raise YamapError('Duplicate GPX filenames in manifest')
    if any(not path.is_file() or path.suffix != '.gpx' for path in (data / 'gpx').iterdir()):
        raise YamapError('GPX directory contains unsupported files; move them outside the archive GPX directory')
    build_route_geojson(data / 'yamap_activities.json', data / 'yamap_gpx_manifest.json', data / 'gpx')
    return activities, manifest


def index_activities(records: list[dict]) -> dict[str, dict]:
    return unique_by(({**row, 'activity_id': str(activity_id(row))} for row in records),
                     'activity_id', 'activities')


def prepare_archive(data: Path, candidate: list[dict], staging: Path | None,
                    prepared: Path) -> dict:
    """Prepare core artifacts without changing canonical paths; preserve existing filenames."""
    current, existing_manifest = read_archive(data)
    current_ids = set(index_activities(current))
    candidates = index_activities(candidate)
    missing = current_ids - candidates.keys()
    if missing:
        raise YamapError(f'Previously archived activities are not visible: {sorted(missing)}')
    existing = unique_by(existing_manifest, 'activity_id', 'GPX manifest')
    new_ids = candidates.keys() - current_ids
    staged = {path.stem: path for path in staging.glob('*.gpx')} if staging else {}
    if set(staged) != new_ids:
        raise YamapError(f'Staged GPX mismatch; missing={sorted(new_ids - staged.keys())}, '
                         f'extra={sorted(staged.keys() - new_ids)}')
    prepared_gpx = prepared / 'gpx'
    prepared_gpx.mkdir()
    manifest = []
    names: set[str] = set()
    for row in candidate:
        item_id = str(activity_id(row))
        if item_id in existing:
            name = checked_name(existing[item_id]['gpx_file'])
            source = data / 'gpx' / name
        else:
            date = str(row.get('date') or '')
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                raise YamapError(f'Activity {item_id} has an invalid date')
            name = f'{date}__{item_id}.gpx'
            source = staged[item_id]
        if name in names:
            raise YamapError(f'Conflicting GPX filename: {name}')
        names.add(name)
        shutil.copy2(source, prepared_gpx / name)
        _, count = parse_gpx_segments(prepared_gpx / name)
        manifest.append(dict(zip(FIELDS, (row.get('date', ''), row.get('title', ''), item_id,
                                         name, count, row.get('url', '')))))
    for name, value in [('yamap_activities.json', candidate), ('yamap_gpx_manifest.json', manifest)]:
        (prepared / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with (prepared / 'yamap_gpx_manifest.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator='\n')
        writer.writeheader()
        writer.writerows(manifest)
    routes = build_route_geojson(prepared / 'yamap_activities.json', prepared / 'yamap_gpx_manifest.json', prepared_gpx)
    (prepared / 'yamap_routes.geojson').write_text(json.dumps(routes, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    return {'activities': len(candidate), 'new_activity_ids': sorted(new_ids, key=int),
            'renamed_activity_ids': [], 'trackpoints': routes['metadata']['trackpoints'],
            'regions': routes['metadata']['regions']}


def replace_archive(prepared: Path, replacements: list[tuple[Path, Path]]) -> None:
    """Extracted transaction: back up each target, replace activities last, roll back on failure."""
    backups_dir = prepared / 'backups'
    backups_dir.mkdir()
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for index, (source, target) in enumerate(replacements):
            if target.exists():
                backup = backups_dir / f'{index}-{target.name}'
                os.replace(target, backup)
                backups.append((backup, target))
            os.replace(source, target)
            installed.append(target)
    except BaseException:
        try:
            for target in reversed(installed):
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            for backup, target in reversed(backups):
                os.replace(backup, target)
        except BaseException as recovery:
            raise RecoveryRequired(f'Rollback failed. Preserve recovery files at {prepared}') from recovery
        raise


def core_replacements(prepared: Path, data: Path) -> list[tuple[Path, Path]]:
    return [(prepared / name, data / name) for name in OUTPUTS]


def parse_date(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise YamapError(f'{option} must be a valid date in YYYY-MM-DD format') from exc


def select_activities(records: list[dict], *, limit: int | None,
                      from_date: str | None, to_date: str | None) -> list[dict]:
    """Select newest activity starts, with inclusive activity-local date bounds."""
    lower = parse_date(from_date, '--from')
    upper = parse_date(to_date, '--to')
    if lower and upper and lower > upper:
        raise YamapError('--from must be on or before --to')
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise YamapError('--limit must be a positive integer')
    if limit is None and lower is None and upper is None:
        return records
    selected = []
    for row in records:
        local_date = parse_date(row.get('date'), 'Activity date')
        if local_date is None:
            raise YamapError('Activity is missing its date')
        if (lower and local_date < lower) or (upper and local_date > upper):
            continue
        try:
            started = datetime.fromisoformat(row['start_at'])
            if started.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise YamapError('Activity is missing a timezone-aware start_at timestamp') from exc
        selected.append((started.astimezone(timezone.utc), activity_id(row), row))
    selected.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in selected[:limit]]


def sync_archive(client, *, profile_id: int, archive_dir: Path,
                 dry_run: bool = False,
                 limit: int | None = None, from_date: str | None = None, to_date: str | None = None,
                 report: Callable[[dict], None] = lambda value: None) -> dict:
    if profile_id <= 0:
        raise YamapError('Profile ID must be positive')
    # Validate options before reading files or making network requests.
    select_activities([], limit=limit, from_date=from_date, to_date=to_date)
    filtered = limit is not None or from_date is not None or to_date is not None
    data = archive_dir.expanduser().resolve()
    current, manifest = read_archive(data)
    candidate, discovery = discover_activities(client, profile_id=profile_id)
    if not discovery.get('complete'):
        raise YamapError('Discovery is incomplete; archive not changed')
    index_activities(candidate)
    full_comparison = compare_records(current, candidate)
    selected = select_activities(candidate, limit=limit, from_date=from_date, to_date=to_date)
    selected_ids = [activity_id(row) for row in selected]
    if filtered:
        selected_set = set(selected_ids)
        # Keep existing entries outside this request unchanged; never interpret a
        # selection limit as an activity disappearing from the source account.
        candidate = selected + [row for row in current if activity_id(row) not in selected_set]
    comparison = compare_records(current, candidate)
    comparison['not_visible_ids'] = full_comparison['not_visible_ids']
    proposal = {'mode': 'proposal', **comparison, 'activities': len(candidate)}
    if filtered:
        proposal.update(selected_ids=selected_ids, selected_activities=len(selected),
                        filters={'limit': limit, 'from': from_date, 'to': to_date})
    report(proposal)
    if comparison['not_visible_ids']:
        raise YamapError(f"Previously archived activities are not visible: {comparison['not_visible_ids']}")
    if dry_run:
        return proposal
    if filtered and not selected:
        result = {**proposal, 'mode': 'no_changes'}
        report(result)
        return result
    if comparison['new_ids'] and not client.token:
        raise YamapError('GPX sync requires authentication. Run login or set YAMAP_TOKEN.')
    # Public core cannot update private peak outputs in the same transaction.
    if (data / 'yamap_peaks.json').exists():
        raise YamapError('Archive contains private peak outputs; use the private staged finalizer to update it.')
    data.parent.mkdir(parents=True, exist_ok=True)
    with archive_lock(data):
        if read_archive(data) != (current, manifest):
            raise YamapError('Archive changed after discovery; rerun sync')
        prepared = Path(tempfile.mkdtemp(prefix='.yamap-update-', dir=data.parent))
        preserve = False
        created_data = not data.exists()
        try:
            staged = prepared / 'downloads'
            staged.mkdir()
            downloads = ([item_id for item_id in selected_ids if item_id in comparison['new_ids']]
                         if filtered else comparison['new_ids'])
            for item_id in downloads:
                response = client.download_gpx(item_id)
                path = staged / f'{item_id}.gpx'
                path.write_bytes(response.body)
                parse_gpx_segments(path)
            summary = prepare_archive(data, candidate, staged, prepared)
            data.mkdir(mode=0o700, exist_ok=True)
            replace_archive(prepared, core_replacements(prepared, data))
        except RecoveryRequired:
            preserve = True
            raise
        except BaseException:
            if created_data and data.is_dir() and not any(data.iterdir()):
                data.rmdir()
            raise
        finally:
            if not preserve:
                shutil.rmtree(prepared)
    result = {'mode': 'applied', **summary}
    if filtered:
        result.update(selected_ids=selected_ids, selected_activities=len(selected), filters=proposal['filters'])
    report(result)
    return result
