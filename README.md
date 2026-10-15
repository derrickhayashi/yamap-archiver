# YAMAP Archiver

A Python command line tool for maintaining a local YAMAP activity and GPX
archive. Requires Python 3.11 or newer.

## Get started

Run all steps from the same `yamap-archiver` directory: the default token and
archive paths are relative to your current directory.

### 1. Clone and install

```sh
git clone https://github.com/derrickhayashi/yamap-archiver.git
cd yamap-archiver

python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[login]'
python -m playwright install chromium
```

### 2. Log in to YAMAP

```sh
yamap-archiver login
```

Sign in normally in the browser window. The tool saves the session token to
`./yamap-token` with owner-only permissions. It does not read your password.
Subsequent syncs reuse this token without opening a browser. If the token is
rejected, run `yamap-archiver login` again and retry.

### 3. Preview the latest 10 activities

Replace `PROFILE_ID` with the number in your YAMAP profile URL:
`https://yamap.com/users/PROFILE_ID`.

```sh
yamap-archiver sync --profile-id PROFILE_ID --limit 10 --dry-run
```

Check `selected_ids` and `selected_activities` in the output. This selects up
to 10 activities, without downloading GPX files or creating an archive.

### 4. Download and create the archive

```sh
yamap-archiver sync --profile-id PROFILE_ID --limit 10
```

Expected: `"mode": "applied"`. The activity data and GPX files are saved under
`./yamap-archive/` inside your fresh clone.

### 5. Verify repeat syncing

Run the same command again:

```sh
yamap-archiver sync --profile-id PROFILE_ID --limit 10
```

If no new activities have appeared, expect `"new_ids": []` and
`"unchanged_ids": 10` (or the smaller number selected for your account).
Existing GPX files are reused; metadata, manifests, and map files are refreshed.
New activities are downloaded automatically. Use `--dry-run` whenever you only
want a preview.

## Output and token locations

- Archive: `./yamap-archive/` by default; override with `--archive-dir PATH`.
- Token: `./yamap-token` by default; override with `--token-file PATH`.
- `YAMAP_TOKEN`, if set, takes precedence over the token file.

Keep using the same working directory, or supply explicit paths. Keep your
token private and exclude it from Git if working in another repository.

## Discover public activities without login

```sh
yamap-archiver discover --profile-id PROFILE_ID --output /tmp/yamap-activities.json
```

This reads public activity information without downloading GPX files or
modifying an archive. Use `--compare PATH` to compare with an existing activity
file. Existing output files require `--overwrite` to replace them.

## Date ranges and download limits

Use `--from 2026-01-01 --to 2026-03-31` for an inclusive date range, or combine
these options with `--limit 10` for the newest 10 within that range. Either
date bound may be supplied alone. Dates refer to the activity's local start
date; newest means start time, with activity ID breaking equal-time ties.

The count includes already archived activities: repeating `--limit 10` skips
existing GPX rather than downloading the next 10 older activities. Previously
archived entries outside the selection are preserved unchanged. Only selected
entries appear in a newly created archive; no matches means no archive writes.
Proposals include `selected_ids`, `selected_activities`, and the chosen filters.

The tool still reads all metadata pages to validate discovery and detect
previously archived activities that have truly disappeared. These options
limit archived entries and GPX downloads, not metadata requests.

Archives contain `yamap_activities.json`, `gpx/`, `yamap_gpx_manifest.json`,
`yamap_gpx_manifest.csv`, and `yamap_routes.geojson`. GPX filenames are preserved
for existing records; new ones use `YYYY-MM-DD__ID.gpx`. Activity IDs, manifest
entries, filenames, and trackpoint counts must reconcile. An empty account
creates valid empty outputs. Never mix different users in one archive.

Previously archived activities that disappear from discovery are not deleted:
the sync stops so you can investigate authentication, visibility, or account
changes. Invalid downloads, partial pagination, and inconsistent archives stop
before canonical files are replaced. Existing GPX files are not downloaded
again. Changed remote GPX tracks for existing IDs are not automatically refreshed.

Updates are staged on the same filesystem. A cooperating-writer lock prevents
concurrent updates. Replacement failures trigger rollback; this is not a
crash-safe database transaction. Keep backups and avoid reading the archive
during replacement. After a crash, preserve any `.yamap-update-*` recovery
folder and inspect the archive before removing a stale sibling `.sync.lock`.
If rollback itself fails, the error identifies the retained recovery directory.

## Individual commands

```sh
yamap-archiver download-gpx ACTIVITY_ID \
  --output-dir /tmp/yamap-gpx --confirm-download
yamap-archiver build-map --activities /path/to/archive/yamap_activities.json \
  --manifest /path/to/archive/yamap_gpx_manifest.json \
  --gpx-dir /path/to/archive/gpx --output /tmp/yamap-routes.geojson
```

## Your data

Everything stays on your computer; nothing is uploaded. Your archive can contain
private activities and exact locations, so share it carefully and keep your
token private.

The tool uses an undocumented YAMAP endpoint, so changes to YAMAP may break it.

## Development

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
```

Tests use synthetic records and mocked HTTP responses; they never log in or
contact YAMAP. `examples/` contains synthetic input only. MIT licensed; see
[LICENSE](LICENSE).
