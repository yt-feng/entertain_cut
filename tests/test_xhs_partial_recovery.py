"""Existing-artifact-only recovery cannot mark 4/5 as completed delivery."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'xhs2vid'))
import recover_partial_delivery as recovery


class PartialRecoveryTests(unittest.TestCase):
    date = '2026-09-28'
    run_id = '36491177384'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = self.root / 'runs'
        self.source = self.runs / self.run_id / 'outputs/xhs_lowfan' / self.date
        self.source.mkdir(parents=True)
        self.output = self.root / 'output'
        self.processed = self.root / 'processed.json'
        self.state = self.root / 'state.json'
        self.items = [{'note_id': f'n{i}', 'output': f'0{i+1}_video{i}.mp4', 'author_fans': 100,
                       'liked_count': 201, 'comments_count': 3} for i in range(4)]
        for item in self.items:
            (self.source / item['output']).write_bytes(b'media-test-double-' + item['note_id'].encode())
        self.dump(self.source / 'new_processed.json', {'date': self.date, 'items': []})
        self.dump(self.source / 'resume_processed.json', {'date': self.date, 'items': self.items})
        self.dump(self.source / 'resume_summary.json', {'date': self.date, 'items': [
            {**item, 'status': 'success'} for item in self.items]})
        self.dump(self.source / 'cumulative_tikhub_request_budget.json',
                  {'used': 57, 'sources': [{'run_id': '36474803824', 'used': 57}]})
        self.dump(self.source / 'tikhub_request_budget.json', {'limit': 42, 'used': 42})
        self.dump(self.processed, {'version': 1, 'items': []})
        self.dump(self.state, {'version': 1, 'items': []})

    def dump(self, path, payload):
        recovery.write_json(path, payload)

    def prepare(self):
        with patch('prepare_resume.validate_media') as validator:
            result = recovery.prepare(self.runs, self.date, self.output, self.processed, self.state)
        self.assertEqual(validator.call_count, 4)
        return result

    def upload_receipt(self):
        directory = '/我的坚果云/KC Desk Notes/Ops/' + self.date + '/Portal 娱乐/'
        receipt = json.loads((self.output / 'partial_recovery_receipt.json').read_text())
        payload = {'status': 'completed', 'dry_run': False, 'date': self.date,
                   'category': 'Portal 娱乐', 'source_dir': str(self.output),
                   'remote_directory': directory, 'file_count': 4, 'verified_count': 4,
                   'failed_count': 0, 'files': [
                       {'source': item['name'], 'size': item['bytes'], 'verified_size': item['bytes'],
                        'status': 'verified', 'remote_path': directory + item['name']}
                       for item in receipt['files']]}
        path = self.output / 'jianguoyun_upload_manifest.json'
        self.dump(path, payload)
        return path, payload

    def test_prepare_keeps_target_five_budget_99_and_exact_existing_four(self):
        result = self.prepare()
        self.assertEqual((result['target'], result['expected_upload_count'], result['target_met']), (5, 4, False))
        self.assertEqual(result['prior_tikhub_requests'], 99)
        self.assertEqual(result['new_tikhub_requests'], 0)
        self.assertEqual(len(list(self.output.glob('*.mp4'))), 4)
        self.assertEqual(json.loads(self.state.read_text())['items'], [])
        self.assertEqual(json.loads(self.processed.read_text())['items'], [])

    def test_remaining_budget_requires_normal_resume_instead_of_final_partial_recovery(self):
        self.dump(self.source / 'tikhub_request_budget.json', {'limit': 42, 'used': 41})
        with patch('prepare_resume.validate_media'):
            with self.assertRaisesRegex(ValueError, 'exhausted cumulative 99-request budget'):
                recovery.prepare(self.runs, self.date, self.output, self.processed, self.state)
        self.assertEqual(json.loads(self.processed.read_text())['items'], [])
        self.assertEqual(json.loads(self.state.read_text())['items'], [])

    def test_verified_upload_records_exact_four_dedupe_but_remains_deferred(self):
        self.prepare()
        upload, _ = self.upload_receipt()
        state = recovery.finalize(self.output, upload, self.processed, self.state, '900')
        self.assertEqual((state['status'], state['target'], state['succeeded']), ('deferred', 5, 4))
        self.assertEqual(state['reason'], 'partial_verified_upload_budget_exhausted')
        recorded = json.loads(self.processed.read_text())['items']
        self.assertEqual({item['note_id'] for item in recorded}, {'n0', 'n1', 'n2', 'n3'})
        self.assertTrue(all(item['delivery_date'] == self.date for item in recorded))
        receipt = json.loads((self.output / 'partial_recovery_receipt.json').read_text())
        self.assertEqual(receipt['verified_count'], 4)
        self.assertFalse(receipt['target_met'])
        self.assertEqual(json.loads((self.output / 'cumulative_tikhub_request_budget.json').read_text())['used'], 99)

    def test_partial_wrong_identity_size_date_or_dry_run_upload_never_records_state(self):
        self.prepare()
        upload, valid = self.upload_receipt()
        mutations = [lambda x: x.update(verified_count=3), lambda x: x.update(dry_run=True),
                     lambda x: x.update(date='2026-09-29'), lambda x: x.update(file_count=5),
                     lambda x: x['files'][0].update(source='unexpected.mp4'),
                     lambda x: x['files'][0].update(verified_size=999),
                     lambda x: x['files'][0].update(remote_path='/wrong/file.mp4')]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = copy.deepcopy(valid)
                mutate(payload)
                self.dump(upload, payload)
                with self.assertRaises(ValueError):
                    recovery.finalize(self.output, upload, self.processed, self.state, '900')
                self.assertEqual(json.loads(self.processed.read_text())['items'], [])
                self.assertEqual(json.loads(self.state.read_text())['items'], [])

    def test_video_or_budget_mutation_after_prepare_is_rejected(self):
        self.prepare()
        upload, _ = self.upload_receipt()
        path = self.output / 'cumulative_tikhub_request_budget.json'
        original = path.read_bytes()
        self.dump(path, {'used': 98})
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            recovery.finalize(self.output, upload, self.processed, self.state, '900')
        path.write_bytes(original)
        (self.output / self.items[0]['output']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'video changed'):
            recovery.finalize(self.output, upload, self.processed, self.state, '900')

    def test_media_and_strict_eligibility_failures_are_fatal(self):
        with patch('prepare_resume.validate_media', side_effect=ValueError('decode failed')):
            with self.assertRaisesRegex(ValueError, 'decode failed'):
                recovery.prepare(self.runs, self.date, self.output, self.processed, self.state)
        # Separate output avoids relying on a half-prepared directory.
        self.items[0]['liked_count'] = 199
        self.dump(self.source / 'resume_summary.json', {'items': [
            {**item, 'status': 'success'} for item in self.items]})
        with self.assertRaisesRegex(ValueError, 'low-fan viral'):
            recovery.prepare(self.runs, self.date, self.root / 'bad-quality', self.processed, self.state)

    def test_full_delivery_state_cannot_be_downgraded_and_other_date_is_excluded(self):
        self.dump(self.state, {'items': [{'date': self.date, 'status': 'delivered', 'succeeded': 5}]})
        with self.assertRaisesRegex(ValueError, 'already delivered'):
            recovery.prepare(self.runs, self.date, self.output, self.processed, self.state)
        self.dump(self.state, {'items': []})
        self.dump(self.processed, {'items': [{**self.items[0], 'delivery_date': '2026-09-27'}]})
        with patch('prepare_resume.validate_media'):
            with self.assertRaisesRegex(ValueError, 'exactly four'):
                recovery.prepare(self.runs, self.date, self.output, self.processed, self.state)

    def test_same_date_reverification_is_idempotent_but_another_partial_set_is_rejected(self):
        self.dump(self.processed, {'items': [{**item, 'delivery_date': self.date} for item in self.items]})
        self.prepare()
        upload, _ = self.upload_receipt()
        recovery.finalize(self.output, upload, self.processed, self.state, '900')
        self.assertEqual(len(json.loads(self.processed.read_text())['items']), 4)
        self.dump(self.processed, {'items': [{'note_id': 'another-note', 'delivery_date': self.date}]})
        with patch('prepare_resume.validate_media'):
            with self.assertRaisesRegex(ValueError, 'Another partial set'):
                recovery.prepare(self.runs, self.date, self.root / 'another-output', self.processed, self.state)

    def test_source_run_requires_exact_repo_main_workflow_and_terminal_identity(self):
        valid = {'id': int(self.run_id), 'repository': {'full_name': 'yt-feng/entertain_cut'},
                 'head_repository': {'full_name': 'yt-feng/entertain_cut'}, 'head_branch': 'main',
                 'path': '.github/workflows/xhs-lowfan-kc-daily.yml', 'event': 'schedule',
                 'status': 'completed', 'conclusion': 'failure'}
        recovery.validate_source_run(valid, self.run_id, 'yt-feng/entertain_cut')
        for field, value in [('id', 900), ('head_branch', 'other'), ('path', 'other.yml'),
                             ('status', 'in_progress'), ('event', 'pull_request'),
                             ('repository', {'full_name': 'another/repo'}),
                             ('head_repository', {'full_name': 'another/repo'})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                recovery.validate_source_run({**valid, field: value}, self.run_id, 'yt-feng/entertain_cut')

    def test_workflow_is_manual_existing_only_no_generation_credentials_or_gate_change(self):
        workflow = (ROOT / '.github/workflows/xhs-recover-existing-partial.yml').read_text()
        self.assertNotIn('schedule:', workflow)
        for forbidden in ('TIKHUB_API_KEY', 'APIMART_API_KEY', 'TAVILY_API_KEY', 'run_daily_batch.py',
                          'discover_note.py', 'generate_identities.py', 'render_video.py'):
            self.assertNotIn(forbidden, workflow)
        self.assertIn('group: xhs-lowfan-kc-daily', workflow)
        daily = (ROOT / '.github/workflows/xhs-lowfan-kc-daily.yml').read_text()
        self.assertIn('target_count="5"', daily)
        self.assertIn('if (( ${#videos[@]} != KC_TARGET_COUNT )); then', daily)


if __name__ == '__main__':
    unittest.main()
