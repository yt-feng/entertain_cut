"""Offline regressions from the 2026-09-28 4/5 + 42-request failures.

HTTP responses and rendering are test doubles; the endpoint attempt sequence
is copied from run 36491177384. These tests do not claim a new rendered video.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import httpx
import test_xhs2vid_daily as shared


discover, fetch, batch = shared.discover, shared.fetch, shared.batch
from tikhub_budget import RequestBudgetExceeded, RequestBudgetReserved, TikHubRequestBudget

TRACE = json.loads((Path(__file__).parent / 'fixtures/xhs-recovery-budget-36491177384.json').read_text())


class RequestReservationTests(unittest.TestCase):
    def test_unknown_discovery_error_after_four_notes_is_not_a_short_pool(self):
        payload = {'data': {'items': [{'note_id': str(i), 'title': 'test'} for i in range(4)]}}
        with patch.multiple(discover, KEYWORDS=['example'], PAGES=1), \
                patch.object(discover, 'search_notes', side_effect=[
                    (payload, 'test-endpoint'), ValueError('unexpected provider JSON contract')]), \
                patch.object(discover.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'unexpected provider JSON contract'):
                discover.search_all()

    def test_bad_json_does_not_turn_into_budget_defer_on_retry_or_fallback(self):
        for module in (discover, fetch):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as temporary:
                budget = TikHubRequestBudget(Path(temporary) / 'budget.json', limit=1)
                response = httpx.Response(200, content=b'not json', request=httpx.Request('GET', 'https://invalid.test'))
                with patch.multiple(module, BUDGET=budget, MAX_ATTEMPTS=2, ACCESS_BLOCKED=None), \
                        patch.object(module.client, 'get', return_value=response) as request:
                    with self.assertRaises(ValueError):
                        if module is discover:
                            with patch.object(discover, 'RESERVED_REQUESTS', 0):
                                discover.search_notes('example', 1, 'general')
                        else:
                            fetch.fetch_with_endpoint_fallback(fetch.comment_requests('note'))
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(budget.snapshot()['used'], 1)

    def test_each_retry_and_fallback_stops_before_reserved_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            budget = TikHubRequestBudget(Path(temporary) / 'budget.json', limit=7)
            for _ in range(3):
                budget.consume('earlier')
            def unavailable(path, **kwargs):
                return httpx.Response(404, request=httpx.Request('GET', 'https://invalid.test' + path))
            with patch.multiple(discover, BUDGET=budget, RESERVED_REQUESTS=2,
                                MAX_ATTEMPTS=2, ACTIVE_SEARCH_ENDPOINT=None, ACCESS_BLOCKED=None), \
                    patch.object(discover.client, 'get', side_effect=unavailable) as request, \
                    patch.object(discover.time, 'sleep'):
                with self.assertRaises(RequestBudgetReserved):
                    discover.search_notes('example', 1, 'general')
            self.assertEqual(request.call_count, 2)
            self.assertEqual(budget.snapshot(), {'used': 5, 'remaining': 2, 'limit': 7})
            # Comments can use their reserved slots; the overall cap still holds.
            budget.consume('comment')
            budget.consume('comment fallback')
            with self.assertRaises(RequestBudgetExceeded):
                budget.consume('not sent')
            for invalid in (-1, True, 1.5):
                with self.assertRaises(ValueError):
                    budget.consume('invalid', reserve=invalid)

    def test_reservation_does_not_hide_observed_auth_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            for function in ('search', 'author'):
                budget = TikHubRequestBudget(Path(temporary) / (function + '.json'), limit=2)
                blocked = httpx.Response(403, request=httpx.Request('GET', 'https://invalid.test'))
                with patch.multiple(discover, BUDGET=budget, RESERVED_REQUESTS=1,
                                    MAX_ATTEMPTS=1, ACCESS_BLOCKED=None, ACTIVE_SEARCH_ENDPOINT=None), \
                        patch.object(discover.client, 'get', return_value=blocked) as request:
                    if function == 'search':
                        with self.assertRaises(discover.TikHubAccessBlocked):
                            discover.search_notes('example', 1, 'general')
                    else:
                        self.assertEqual(discover.author_fans('author'), -1)
                    self.assertEqual(discover.ACCESS_BLOCKED.status_code, 403)
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(budget.snapshot()['remaining'], 1)

    def test_production_42_attempt_trace_preserves_comments_and_discovered_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_budget = TikHubRequestBudget(root / 'old.json', limit=TRACE['current_cap'])
            for event in TRACE['attempts']:
                old_budget.consume(event['endpoint'])
            self.assertEqual(TRACE['prior_used'] + old_budget.snapshot()['used'], 99)
            with self.assertRaises(RequestBudgetExceeded):
                old_budget.consume('comments')

            budget = TikHubRequestBudget(root / 'fixed.json', limit=42)
            actual_paths = []
            def response(path, **kwargs):
                actual_paths.append(path)
                number = len(actual_paths)
                request = httpx.Request('GET', 'https://invalid.test' + path)
                code = 400 if number == 7 else 404 if 8 <= number <= 12 else 200
                # Actual response bodies were not in the production log: use
                # one explicit synthetic note to test retention at the stop.
                payload = {'data': {'items': [{'note_id': 'retained-before-stop', 'title': 'test'}]}}
                return httpx.Response(code, json=payload, request=request)
            with patch.multiple(discover, BUDGET=budget, RESERVED_REQUESTS=10,
                                MAX_ATTEMPTS=1, ACTIVE_SEARCH_ENDPOINT=None, ACCESS_BLOCKED=None,
                                BUDGET_STOP=None, KEYWORDS=[str(i) for i in range(8)], PAGES=2), \
                    patch.object(discover.client, 'get', side_effect=response), \
                    patch.object(discover.time, 'sleep'):
                notes = discover.search_all()
                self.assertIn('retained-before-stop', notes)
                self.assertEqual(discover.BUDGET_STOP['stage'], 'search')
                self.assertEqual(actual_paths, [event['endpoint'] for event in TRACE['attempts'][:32]])
                self.assertEqual(budget.snapshot()['remaining'], 10)
                # The five observed author attempts now leave five slots.
                discover.RESERVED_REQUESTS = 4
                for event in TRACE['attempts'][37:]:
                    discover.api_get(event['endpoint'], {})
            with patch.multiple(fetch, BUDGET=budget, MAX_ATTEMPTS=1, ACCESS_BLOCKED=None), \
                    patch.object(fetch.client, 'get', return_value=httpx.Response(
                        200, json={'data': {'comments': []}},
                        request=httpx.Request('GET', 'https://invalid.test/comments'))) as comments:
                fetch.fetch_with_endpoint_fallback(fetch.comment_requests('note'))
            self.assertEqual(comments.call_count, 1)
            self.assertEqual(budget.snapshot()['used'], 38)
            self.assertLessEqual(TRACE['prior_used'] + budget.snapshot()['used'], 99)

    def test_author_fallback_stop_keeps_already_verified_strict_candidate(self):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            names = ['OUT_DIR', 'PAGES', 'TOP_AUTHOR_CHECK', 'MAX_ATTEMPTS', 'HOT_TERMS',
                     'KEYWORDS', 'BUDGET', 'BUDGET_STOP', 'RESERVED_REQUESTS', 'ACCESS_BLOCKED',
                     'ACTIVE_SEARCH_ENDPOINT']
            for name in names:
                stack.enter_context(patch.object(discover, name, getattr(discover, name)))
            stack.enter_context(patch.object(discover, 'KEY', 'offline-test'))
            stack.enter_context(patch.object(discover.time, 'sleep'))
            now = discover.time.time()
            def search():
                for _ in range(2):
                    discover.BUDGET.consume('search', reserve=discover.RESERVED_REQUESTS)
                return {str(i): {'note_id': str(i), 'title': 'eligible', 'cover_url': 'test',
                                  'author_id': str(i), 'author_fans': None, 'liked_count': 300-i,
                                  'comments_count': 3, 'timestamp': now} for i in range(3)}
            stack.enter_context(patch.object(discover, 'search_all', side_effect=search))
            calls = []
            def response(path, **kwargs):
                calls.append(path)
                user = kwargs['params']['user_id']
                return httpx.Response(200 if user == '0' else 404, json={'fans': 100},
                                      request=httpx.Request('GET', 'https://invalid.test' + path))
            stack.enter_context(patch.object(discover.client, 'get', side_effect=response))
            stack.enter_context(patch.object(sys, 'argv', ['discover', str(root), '--request-limit', '6',
                                                          '--reserve-requests', '1', '--top-author-check', '3',
                                                          '--max-attempts', '1', '--strict-low-fan', '--limit', '3']))
            discover.main()
            self.assertEqual(discover.BUDGET.snapshot()['remaining'], 1)
            self.assertEqual(json.loads((root / 'discovery_status.json').read_text())['status'], 'ready')
            selected = json.loads((root / 'selected_notes.json').read_text())
            self.assertEqual([note['note_id'] for note in selected], ['0'])
            self.assertEqual(discover.BUDGET_STOP['stage'], 'author')


class BatchOutcomeTests(unittest.TestCase):
    def run_batch(self, *, count=4, limit=5, exhausted=False, failure=None, start=1, marker=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        output = root / 'output'
        output.mkdir()
        (root / 'processed.json').write_text('{"items": []}')
        # A recovered file must never be overwritten/deleted by a failed top-up.
        if start > 1:
            (output / '01_recovered.mp4').write_bytes(b'recovered-verified-elsewhere')
        commands = []
        def run(command, **kwargs):
            name = Path(command[1]).name
            commands.append(name)
            if name == 'discover_note.py':
                discovery = Path(command[2])
                discovery.mkdir(parents=True, exist_ok=True)
                (discovery / 'discovery_status.json').write_text('{"status": "ready"}')
                (discovery / 'selected_notes.json').write_text(json.dumps([
                    {'note_id': f'n{i}', 'title': f'test{i}', 'author_fans': 100,
                     'liked_count': 200, 'comments_count': 3} for i in range(count)]))
                budget = TikHubRequestBudget(output / 'tikhub_request_budget.json', limit=10)
                if exhausted:
                    for _ in range(10):
                        budget.consume('discovery')
                self.assertEqual(command[command.index('--reserve-requests') + 1], str(limit * 2 + 3))
            elif name == 'fetch_assets.py':
                note_dir = Path(command[2])
                note_id = json.loads((note_dir / 'chosen_note.json').read_text())['note_id']
                if marker and note_id == 'n1':
                    budget = TikHubRequestBudget(output / 'tikhub_request_budget.json', limit=10)
                    while budget.snapshot()['remaining']:
                        budget.consume('comment fallback')
                    (note_dir / 'tikhub_budget_exhausted.json').write_text(json.dumps({
                        'note_id': note_id, 'reason': 'tikhub_request_budget_exhausted'}))
                    raise subprocess.CalledProcessError(75, command)
                if failure == 'fetch' and note_id == 'n0':
                    # Even an old marker must not suppress an ordinary failure.
                    (note_dir / 'tikhub_access_blocked.json').write_text('{"reason":"tikhub_access_blocked"}')
                    raise subprocess.CalledProcessError(1, command)
                (note_dir / 'top_comments.json').write_text('[{"sub_comments": []}]')
            elif name == 'render_video.py':
                if failure == 'render' and command[command.index('--output') + 1].endswith('_n0.mp4'):
                    raise subprocess.CalledProcessError(1, command)
                Path(command[command.index('--output') + 1]).write_bytes(b'mocked-video')
        argv = ['batch', '--date', datetime.now(batch.BEIJING).date().isoformat(),
                '--limit', str(limit), '--start-index', str(start), '--request-limit', '10',
                '--output-dir', str(output), '--work-root', str(root / 'work'),
                '--processed-manifest', str(root / 'processed.json'), '--no-hot-context',
                '--avatar-provider', 'local']
        code = 0
        with patch.object(sys, 'argv', argv), patch.object(batch, 'run', side_effect=run), \
                patch.object(batch, 'validate_video', return_value={'test_double': True}):
            try:
                batch.main()
            except SystemExit as exc:
                code = exc.code
        return root, output, commands, code

    def test_four_successes_are_deferred_without_discard_or_false_delivery(self):
        _, output, commands, code = self.run_batch()
        self.assertEqual(code, 0)
        state = json.loads((output / 'delivery_status.json').read_text())
        self.assertEqual((state['status'], state['reason'], state['succeeded'], state['target']),
                         ('deferred', 'insufficient_strict_candidates', 4, 5))
        self.assertEqual(len(json.loads((output / 'new_processed.json').read_text())['items']), 4)
        self.assertEqual(len(list(output.glob('*.mp4'))), 4)
        self.assertEqual(commands.count('render_video.py'), 4)
        self.assertFalse(json.loads((output / 'daily_summary.json').read_text())['target_met'])

    def test_full_target_still_ready(self):
        _, output, _, code = self.run_batch(count=5)
        self.assertEqual(code, 0)
        state = json.loads((output / 'delivery_status.json').read_text())
        self.assertEqual((state['status'], state['succeeded']), ('ready', 5))

    def test_exhausted_topup_does_not_fetch_or_touch_recovered_file(self):
        _, output, commands, code = self.run_batch(count=3, limit=1, start=5, exhausted=True)
        self.assertEqual(code, 0)
        self.assertNotIn('fetch_assets.py', commands)
        self.assertEqual((output / '01_recovered.mp4').read_bytes(), b'recovered-verified-elsewhere')
        state = json.loads((output / 'delivery_status.json').read_text())
        self.assertEqual((state['reason'], state['succeeded']), ('tikhub_request_budget_exhausted', 0))

    def test_budget_marker_stops_remaining_candidates_and_keeps_success(self):
        _, output, commands, code = self.run_batch(count=4, marker=True)
        self.assertEqual(code, 0)
        self.assertEqual(commands.count('fetch_assets.py'), 2)
        self.assertEqual(json.loads((output / 'delivery_status.json').read_text())['succeeded'], 1)

    def test_real_failure_remains_failure_even_if_later_budget_deferred(self):
        _, output, _, code = self.run_batch(count=4, marker=True, failure='fetch')
        self.assertEqual(code, 2)
        summary = json.loads((output / 'daily_summary.json').read_text())
        self.assertEqual(summary['status'], 'incomplete')
        self.assertEqual(summary['later_defer_reason'], 'tikhub_request_budget_exhausted')
        self.assertEqual(summary['items'][0]['status'], 'failed')

    def test_renderer_failure_is_not_reclassified_as_candidate_shortage(self):
        _, output, commands, code = self.run_batch(count=4, failure='render')
        self.assertEqual(code, 2)
        self.assertEqual(commands.count('render_video.py'), 5)  # Failed render retried once.
        self.assertEqual(json.loads((output / 'delivery_status.json').read_text())['status'], 'incomplete')

    def test_ready_empty_candidates_are_contract_error(self):
        with self.assertRaisesRegex(RuntimeError, 'ready discovery'):
            self.run_batch(count=0)


class FetchBudgetTests(unittest.TestCase):
    def test_exhaustion_has_typed_evidence_before_cover_and_clears_stale_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'chosen_note.json').write_text('{"note_id":"n1","cover_url":"invalid"}')
            (root / 'tikhub_access_blocked.json').write_text('{}')
            budget = TikHubRequestBudget(root / 'budget.json', limit=1)
            budget.consume('previous')
            with patch.object(sys, 'argv', ['fetch', str(root), '--request-limit', '1',
                                            '--budget-file', str(root / 'budget.json')]), \
                    patch.object(fetch, 'KEY', 'offline-test'), \
                    patch.object(fetch.cover_client, 'get') as cover, \
                    patch.object(fetch.client, 'get') as request:
                with self.assertRaises(SystemExit) as raised:
                    fetch.main()
            self.assertEqual(raised.exception.code, 75)
            self.assertFalse((root / 'tikhub_access_blocked.json').exists())
            self.assertEqual(json.loads((root / 'tikhub_budget_exhausted.json').read_text())['note_id'], 'n1')
            cover.assert_not_called()
            request.assert_not_called()

    def test_comment_auth_block_is_not_relabelled_when_budget_runs_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            budget = TikHubRequestBudget(Path(temporary) / 'budget.json', limit=1)
            response = httpx.Response(403, request=httpx.Request('GET', 'https://invalid.test'))
            with patch.multiple(fetch, BUDGET=budget, MAX_ATTEMPTS=1, ACCESS_BLOCKED=None), \
                    patch.object(fetch.client, 'get', return_value=response) as request:
                with self.assertRaises(fetch.TikHubAccessBlocked):
                    fetch.fetch_with_endpoint_fallback(fetch.comment_requests('note'))
                self.assertEqual(fetch.ACCESS_BLOCKED.status_code, 403)
                self.assertEqual(request.call_count, 1)

    def test_non_exhausted_counter_cannot_be_deferred_as_exhaustion(self):
        with tempfile.TemporaryDirectory() as temporary:
            budget = TikHubRequestBudget(Path(temporary) / 'budget.json', limit=1)
            with patch.object(fetch, 'BUDGET', budget):
                with self.assertRaisesRegex(RuntimeError, 'exhausted shared counter'):
                    fetch.defer_exhausted_budget()



class WorkflowOutcomeTests(unittest.TestCase):
    def test_workflow_preserves_partial_counts_across_resume_and_state_record(self):
        workflow = (shared.ROOT / '.github/workflows/xhs-lowfan-kc-daily.yml').read_text()
        def shell_step(name):
            block = workflow.split('      - name: ' + name + '\n', 1)[1].split('\n      - name:', 1)[0]
            return textwrap.dedent(block.split('        run: |\n', 1)[1])
        for prior, new, limit in ((0, 4, 5), (4, 0, 1), (3, 1, 2)):
            with self.subTest(prior=prior, new=new), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / 'output'
                output.mkdir()
                (output / 'delivery_status.json').write_text(json.dumps({
                    'status': 'deferred', 'reason': 'insufficient_strict_candidates',
                    'message': 'Target not met', 'succeeded': new,
                }))
                env_file = root / 'env'
                environment = dict(os.environ, KC_OUTPUT_DIR=str(output), GITHUB_ENV=str(env_file),
                                   KC_COMPLETED_COUNT=str(prior), KC_BATCH_LIMIT=str(limit),
                                   KC_TARGET_COUNT='5')
                subprocess.run(['bash', '-eo', 'pipefail', '-c', shell_step('Apply structured batch outcome')],
                               env=environment, check=True, capture_output=True, text=True)
                values = dict(line.split('=', 1) for line in env_file.read_text().splitlines())
                self.assertEqual(values['KC_COMPLETED_COUNT'], '4')
                self.assertEqual(values['KC_DEFERRED'], 'true')
                state = shared.delivery_state.update_state(root / 'state.json', '2026-09-28',
                    'deferred', target=5, succeeded=int(values['KC_COMPLETED_COUNT']))
                self.assertEqual((state['target'], state['succeeded'], state['status']), (5, 4, 'deferred'))
        self.assertIn('--succeeded "$KC_COMPLETED_COUNT"', shell_step('Record deferred delivery state'))
        self.assertIn('insufficient_strict_candidates|tikhub_stage_budget_reserved)', workflow)
        self.assertIn('remaining_tikhub_budget="$((99 - prior_tikhub_used))"', workflow)
        ci = (shared.ROOT / '.github/workflows/kc-pipeline-tests.yml').read_text()
        self.assertIn("discover -s tests -p 'test_xhs*.py'", ci)

if __name__ == '__main__':
    unittest.main()
