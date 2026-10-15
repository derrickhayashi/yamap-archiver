import io
import json
import os
import tempfile
import unittest
from contextlib import chdir, redirect_stdout, redirect_stderr
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from yamap_sync_core.archive import discover_activities
from yamap_sync_core.auth import save_token
from yamap_sync_core.cli import main
from yamap_sync_core.client import YamapClient
from yamap_sync_core.errors import YamapError
from yamap_sync_core.models import HttpResponse
from yamap_sync_core.sync import (
    RecoveryRequired, archive_lock, core_replacements, prepare_archive, read_archive,
    replace_archive, sync_archive,
)

GPX = b'<gpx><trk><trkseg><trkpt lat="0" lon="0"/><trkpt lat="0.001" lon="0.001"/></trkseg></trk></gpx>'


def raw(number):
    return {'id': number, 'title': f'Synthetic activity {number}', 'start_at': 1704067200,
            'time_zone': 0, 'public_type': 'public', 'duration': 60, 'distance': 100}


class FakeClient:
    token = 'synthetic-test-token'

    def __init__(self, ids=(1,)):
        self.ids = ids
        self.downloads = []
        self.body = GPX

    def list_activity_page(self, profile_id, page, per_page):
        return {'activities': [raw(number) for number in self.ids],
                'meta': {'total_count': len(self.ids), 'total_pages': 1}}

    def download_gpx(self, number):
        self.downloads.append(number)
        return HttpResponse(self.body, Message(), 200)


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'archive'
        self.client = FakeClient()

    def sync(self, **kwargs):
        return sync_archive(self.client, profile_id=123, archive_dir=self.data, **kwargs)

    def test_default_sync_reports_proposal_then_downloads_and_applies(self):
        reports = []
        original = self.client.download_gpx
        def download(number):
            self.assertEqual(reports[0]['mode'], 'proposal')
            self.assertEqual(reports[0]['new_ids'], [1])
            return original(number)
        self.client.download_gpx = download
        result = self.sync(report=reports.append)
        self.assertEqual(result['mode'], 'applied')
        self.assertEqual(self.client.downloads, [1])
        self.assertEqual(len(read_archive(self.data)[0]), 1)

    def test_dry_run_never_downloads_or_writes(self):
        self.sync(dry_run=True)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.client.downloads, [])

    def test_first_sync_and_repeat_preserve_bytes_and_skip_downloads(self):
        report = self.sync()
        self.assertEqual(report['activities'], 1)
        before = snapshot(self.data)
        self.sync()
        self.assertEqual(snapshot(self.data), before)
        self.assertEqual(self.client.downloads, [1])
        read_archive(self.data)

    def test_incremental_preserves_legacy_filename_and_refreshes_metadata(self):
        self.sync()
        manifest = json.loads((self.data / 'yamap_gpx_manifest.json').read_text())
        old = manifest[0]['gpx_file']
        (self.data / 'gpx' / old).rename(self.data / 'gpx' / 'legacy.gpx')
        manifest[0]['gpx_file'] = 'legacy.gpx'
        (self.data / 'yamap_gpx_manifest.json').write_text(json.dumps(manifest))
        self.client.ids = (2, 1)
        self.sync()
        manifest = json.loads((self.data / 'yamap_gpx_manifest.json').read_text())
        self.assertEqual(manifest[1]['gpx_file'], 'legacy.gpx')
        self.assertEqual((self.data / 'gpx/legacy.gpx').read_bytes(), GPX)
        self.assertEqual(self.client.downloads, [1, 2])
        self.assertEqual(len(read_archive(self.data)[0]), 2)

    def test_missing_activities_refused_without_writes_or_downloads(self):
        self.sync()
        before = snapshot(self.data)
        self.client.ids = (2,)
        with self.assertRaisesRegex(YamapError, 'not visible'):
            self.sync()
        self.assertEqual(snapshot(self.data), before)
        self.assertEqual(self.client.downloads, [1])

    def test_invalid_download_leaves_existing_archive_unchanged(self):
        self.sync()
        before = snapshot(self.data)
        self.client.ids = (1, 2)
        for body in (b'<html/>', b'invalid', b'<gpx><trk><trkseg><trkpt lat="999" lon="0"/></trkseg></trk></gpx>'):
            with self.subTest(body=body):
                self.client.body = body
                with self.assertRaises(YamapError):
                    self.sync()
                self.assertEqual(snapshot(self.data), before)
                self.assertEqual(set(self.root.iterdir()), {self.data})

    def test_partial_archive_is_rejected(self):
        self.data.mkdir()
        (self.data / 'yamap_activities.json').write_text('[]')
        with self.assertRaisesRegex(YamapError, 'Incomplete'):
            self.sync()
        self.assertEqual(self.client.downloads, [])

    def test_unauthenticated_first_sync_refused(self):
        self.client.token = None
        with self.assertRaisesRegex(YamapError, 'authentication'):
            self.sync()
        self.assertFalse(self.data.exists())

    def test_empty_account_creates_valid_empty_archive(self):
        self.client.ids = ()
        self.sync()
        self.assertEqual(read_archive(self.data), ([], []))
        routes = json.loads((self.data / 'yamap_routes.geojson').read_text())
        self.assertEqual(routes['features'], [])
        self.assertNotIn('bbox', routes)
        self.sync()

    def test_corrupt_manifest_trackpoints_refused_before_network_downloads(self):
        self.sync()
        path = self.data / 'yamap_gpx_manifest.json'
        manifest = json.loads(path.read_text())
        manifest[0]['trackpoints'] = 999
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(YamapError, 'Trackpoint mismatch'):
            self.sync()
        self.assertEqual(self.client.downloads, [1])

    def test_manifest_cannot_traverse_directories(self):
        self.sync()
        path = self.data / 'yamap_gpx_manifest.json'
        manifest = json.loads(path.read_text())
        manifest[0]['gpx_file'] = '../outside.gpx'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(YamapError, 'Invalid GPX filename'):
            self.sync()

    def test_lock_prevents_second_writer(self):
        with archive_lock(self.data):
            with self.assertRaisesRegex(YamapError, 'locked'):
                self.sync()
        self.assertFalse(self.data.exists())
        self.assertEqual(self.client.downloads, [])

    def test_private_peaks_require_private_adapter(self):
        self.sync()
        (self.data / 'yamap_peaks.json').write_text('{}')
        before = snapshot(self.data)
        with self.assertRaisesRegex(YamapError, 'private staged finalizer'):
            self.sync()
        self.assertEqual(snapshot(self.data), before)

    def test_report_is_flushed_before_download(self):
        stream = io.StringIO()
        original = self.client.download_gpx
        def download(number):
            self.assertIn('proposal', stream.getvalue())
            return original(number)
        self.client.download_gpx = download
        with patch('yamap_sync_core.cli.make_client', return_value=self.client), redirect_stdout(stream):
            self.assertEqual(main(['sync', '--profile-id', '123', '--archive-dir', str(self.data)]), 0)

    def test_sync_refuses_token_inside_archive(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['sync', '--profile-id', '123', '--archive-dir', str(self.data),
                  '--token-file', str(self.data / 'token')])
        self.assertFalse(self.data.exists())

    def test_expired_authentication_leaves_archive_unchanged(self):
        self.sync()
        before = snapshot(self.data)
        client = YamapClient(token='synthetic-token', delay_seconds=0)
        error = HTTPError('https://example.invalid', 403, 'Forbidden', Message(), io.BytesIO(b''))
        with patch('yamap_sync_core.client.urlopen', side_effect=error):
            with self.assertRaisesRegex(YamapError, 'HTTP 403'):
                sync_archive(client, profile_id=123, archive_dir=self.data)
        self.assertEqual(snapshot(self.data), before)

    def test_second_download_failure_does_not_apply_first_download(self):
        self.client.ids = (1, 2)
        original = self.client.download_gpx
        def download(number):
            if number == 1:
                raise YamapError('simulated remote failure')
            return original(number)
        self.client.download_gpx = download
        with self.assertRaisesRegex(YamapError, 'remote failure'):
            self.sync()
        self.assertEqual(self.client.downloads, [2])
        self.assertFalse(self.data.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_replacement_failure_rolls_back_every_position(self):
        self.sync()
        before = snapshot(self.data)
        activities, _ = read_archive(self.data)
        real_replace = os.replace
        for failed_name in ('gpx', 'yamap_gpx_manifest.json', 'yamap_gpx_manifest.csv',
                            'yamap_routes.geojson', 'yamap_activities.json'):
            with self.subTest(failed_name=failed_name), tempfile.TemporaryDirectory(dir=self.root) as temp:
                prepared = Path(temp)
                prepare_archive(self.data, activities, None, prepared)
                def replace(source, target):
                    if source == prepared / failed_name:
                        raise OSError('simulated replacement failure')
                    return real_replace(source, target)
                with patch('yamap_sync_core.sync.os.replace', side_effect=replace):
                    with self.assertRaisesRegex(OSError, 'simulated'):
                        replace_archive(prepared, core_replacements(prepared, self.data))
                self.assertEqual(snapshot(self.data), before)

    def test_failed_rollback_keeps_recovery_files(self):
        self.sync()
        real_replace = os.replace
        self.client.ids = (1, 2)
        def replace(source, target):
            if source.name == 'yamap_activities.json' or source.parent.name == 'backups':
                raise OSError('simulated disk error')
            return real_replace(source, target)
        with patch('yamap_sync_core.sync.os.replace', side_effect=replace):
            with self.assertRaisesRegex(RecoveryRequired, 'Preserve recovery files'):
                self.sync()
        staging = list(self.root.glob('.yamap-update-*'))
        self.assertEqual(len(staging), 1)
        self.assertTrue((staging[0] / 'backups').is_dir())


class SelectionTests(unittest.TestCase):
    setUp = SyncTests.setUp
    sync = SyncTests.sync

    def activities(self, entries):
        # Entries intentionally retain supplied order rather than assuming the
        # endpoint sorts correctly. Dates use each activity's own timezone.
        from datetime import datetime
        rows = []
        for number, started in entries:
            timestamp = datetime.fromisoformat(started)
            row = raw(number)
            row.update(start_at=int(timestamp.timestamp()),
                       time_zone=timestamp.utcoffset().total_seconds() / 3600)
            rows.append(row)
        self.client.list_activity_page = lambda *args: {
            'activities': rows, 'meta': {'total_count': len(rows), 'total_pages': 1}}

    def test_latest_ten_sorted_by_start_not_id_or_response_order(self):
        self.activities([(100 - day, f'2026-01-{day:02d}T10:00:00+09:00') for day in range(1, 16)])
        result = self.sync(limit=10)
        self.assertEqual(result['selected_ids'], list(range(85, 95)))
        self.assertEqual(self.client.downloads, list(range(85, 95)))
        self.assertEqual(len(read_archive(self.data)[0]), 10)

    def test_repeated_limit_counts_existing_entries_without_filling_older(self):
        self.activities([(day, f'2026-01-{day:02d}T10:00:00+09:00') for day in range(1, 13)])
        self.sync(limit=10)
        before = snapshot(self.data)
        self.sync(limit=10)
        self.assertEqual(self.client.downloads, list(range(12, 2, -1)))
        self.assertEqual(snapshot(self.data), before)

    def test_narrower_selection_preserves_existing_outside_selection(self):
        self.activities([(day, f'2026-01-{day:02d}T10:00:00+09:00') for day in range(1, 5)])
        self.sync()
        before = snapshot(self.data)
        self.sync(limit=1)
        self.assertEqual(len(read_archive(self.data)[0]), 4)
        for name, body in before.items():
            if name.startswith('gpx/'):
                self.assertEqual(snapshot(self.data)[name], body)
        self.assertEqual(len(self.client.downloads), 4)

    def test_new_latest_entry_keeps_older_archive_entries(self):
        self.activities([(1, '2026-01-01T10:00:00+09:00')])
        self.sync(limit=1)
        old_gpx = next((self.data / 'gpx').iterdir())
        old_bytes = old_gpx.read_bytes()
        self.activities([(1, '2026-01-01T10:00:00+09:00'), (2, '2026-01-02T10:00:00+09:00')])
        result = self.sync(limit=1)
        self.assertEqual(result['selected_ids'], [2])
        self.assertEqual(result['activities'], 2)
        self.assertEqual(self.client.downloads, [1, 2])
        self.assertEqual(old_gpx.read_bytes(), old_bytes)
        self.assertEqual(len(read_archive(self.data)[0]), 2)

    def test_limit_preview_has_selected_ids_and_does_not_write(self):
        reports = []
        self.client.ids = tuple(range(1, 20))
        self.sync(limit=10, dry_run=True, report=reports.append)
        self.assertEqual(reports[0]['selected_ids'], list(range(19, 9, -1)))
        self.assertEqual(reports[0]['selected_activities'], 10)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.client.downloads, [])

    def test_inclusive_local_dates_then_limit(self):
        self.activities([(1, '2026-01-01T23:59:00-10:00'),
                         (2, '2026-01-02T00:01:00+14:00'),
                         (3, '2026-01-03T12:00:00+09:00'),
                         (4, '2026-01-04T00:00:00+09:00')])
        result = self.sync(from_date='2026-01-02', to_date='2026-01-03', limit=10)
        self.assertEqual(result['selected_ids'], [3, 2])
        self.assertEqual(self.client.downloads, [3, 2])

    def test_limit_selects_newest_within_date_range(self):
        self.activities([(day, f'2026-01-{day:02d}T10:00:00+09:00') for day in range(1, 6)])
        result = self.sync(from_date='2026-01-02', to_date='2026-01-04', limit=2)
        self.assertEqual(result['selected_ids'], [4, 3])

    def test_empty_selection_leaves_archive_unchanged(self):
        self.sync()
        before = snapshot(self.data)
        result = self.sync(from_date='2099-01-01')
        self.assertEqual(result['mode'], 'no_changes')
        self.assertEqual(snapshot(self.data), before)
        self.assertEqual(self.client.downloads, [1])

    def test_empty_selection_does_not_create_new_archive(self):
        self.sync(to_date='2000-01-01')
        self.assertFalse(self.data.exists())
        self.assertEqual(self.client.downloads, [])

    def test_invalid_options_fail_before_network_or_writes(self):
        with patch.object(self.client, 'list_activity_page') as request:
            for options in ({'limit': 0}, {'limit': -1}, {'from_date': '2026-02-30'},
                            {'to_date': '2026-1-2'}, {'from_date': '2026-03-01', 'to_date': '2026-02-01'}):
                with self.subTest(options=options), self.assertRaises(YamapError):
                    self.sync(**options)
            request.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_true_missing_activity_still_stops_limited_sync(self):
        self.sync()
        before = snapshot(self.data)
        self.client.ids = (2, 3)
        with self.assertRaisesRegex(YamapError, 'not visible'):
            self.sync(limit=1)
        self.assertEqual(snapshot(self.data), before)
        self.assertEqual(self.client.downloads, [1])

    def test_cli_forwards_selection_and_previews(self):
        stream = io.StringIO()
        self.client.ids = (1, 2, 3)
        with patch('yamap_sync_core.cli.make_client', return_value=self.client), redirect_stdout(stream):
            main(['sync', '--profile-id', '123', '--archive-dir', str(self.data),
                  '--from', '2024-01-01', '--to', '2024-01-01', '--limit', '2', '--dry-run'])
        self.assertEqual(json.loads(stream.getvalue())['selected_ids'], [3, 2])
        self.assertFalse(self.data.exists())


class DefaultPathTests(unittest.TestCase):
    def test_login_and_sync_share_cwd_token_and_default_archive(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory), patch.dict(os.environ, {}, clear=True):
            def login(path, timeout):
                save_token(path, 'synthetic-session')
            client = FakeClient()
            with patch('yamap_sync_core.cli.browser_login', side_effect=login) as browser, redirect_stdout(io.StringIO()):
                self.assertEqual(main(['login']), 0)
            self.assertEqual(browser.call_args.args[0], Path('yamap-token'))
            token = Path('yamap-token')
            self.assertEqual(token.read_text().strip(), 'synthetic-session')
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)
            with patch('yamap_sync_core.cli.YamapClient', return_value=client) as factory, redirect_stdout(io.StringIO()):
                main(['sync', '--profile-id', '123'])
            self.assertEqual(factory.call_args.kwargs['token'], 'synthetic-session')
            self.assertEqual(len(read_archive(Path('yamap-archive'))[0]), 1)
            self.assertTrue(token.is_file())
            self.assertFalse((Path('yamap-archive') / 'yamap-token').exists())

    def test_explicit_paths_override_both_defaults(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory), patch.dict(os.environ, {}, clear=True):
            token = Path('credentials/custom-token')
            save_token(token, 'synthetic-override')
            with patch('yamap_sync_core.cli.YamapClient', return_value=FakeClient()) as factory, redirect_stdout(io.StringIO()):
                main(['sync', '--profile-id', '123', '--archive-dir', 'custom-archive',
                      '--token-file', str(token)])
            self.assertEqual(factory.call_args.kwargs['token'], 'synthetic-override')
            self.assertEqual(len(read_archive(Path('custom-archive'))[0]), 1)
            self.assertFalse(Path('yamap-archive').exists())
            self.assertFalse(Path('yamap-token').exists())

    def test_missing_default_token_allows_public_discovery(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory), patch.dict(os.environ, {}, clear=True):
            with patch('yamap_sync_core.cli.YamapClient', return_value=FakeClient()) as factory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                main(['discover', '--profile-id', '123'])
            self.assertIsNone(factory.call_args.kwargs['token'])
            self.assertEqual(list(Path('.').iterdir()), [])

    def test_environment_token_takes_precedence_over_default_file(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory), patch.dict(os.environ, {'YAMAP_TOKEN': 'synthetic-env'}, clear=True):
            save_token(Path('yamap-token'), 'synthetic-file')
            with patch('yamap_sync_core.cli.YamapClient', return_value=FakeClient()) as factory, redirect_stdout(io.StringIO()):
                main(['sync', '--profile-id', '123', '--dry-run'])
            self.assertEqual(factory.call_args.kwargs['token'], 'synthetic-env')
            self.assertFalse(Path('yamap-archive').exists())


class ProtocolTests(unittest.TestCase):
    def test_expired_token_stops_without_retry_or_body_disclosure(self):
        client = YamapClient(token='synthetic-token', delay_seconds=0)
        error = HTTPError('https://example.invalid', 401, 'Unauthorized', Message(), io.BytesIO(b'private-response'))
        with patch('yamap_sync_core.client.urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(YamapError, 'HTTP 401') as caught:
                client.list_activity_page(123, 1, 24)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn('private-response', str(caught.exception))

    def test_incomplete_pagination_does_not_produce_archive(self):
        client = FakeClient()
        client.list_activity_page = lambda *args: {'activities': [raw(1)], 'meta': {'total_count': 2, 'total_pages': 1}}
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'archive'
            with self.assertRaisesRegex(YamapError, 'Expected 2'):
                sync_archive(client, profile_id=123, archive_dir=data)
            self.assertFalse(data.exists())
            self.assertEqual(client.downloads, [])

    def test_duplicate_pagination_is_rejected(self):
        client = FakeClient((1, 1))
        with self.assertRaisesRegex(YamapError, 'Duplicate'):
            discover_activities(client, profile_id=123)

    def test_token_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'token'
            path.write_text('old')
            path.chmod(0o644)
            save_token(path, 'synthetic-token')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_profile_is_required(self):
        for command in (['discover'], ['sync', '--archive-dir', '/tmp/synthetic-archive']):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(command)


if __name__ == '__main__':
    unittest.main()
