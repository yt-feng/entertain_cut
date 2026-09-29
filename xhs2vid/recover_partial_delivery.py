#!/usr/bin/env python3
"""Deliver exactly four existing verified outputs while retaining a five-item goal.

This recovery entry point has no discovery, generation, or provider API client.
It is separate from the daily workflow's exact-five publication gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from prepare_resume import merge_artifacts, read_json
from record_delivery_state import update_state
from workflow_support import validated_iso_date

TARGET = 5
EXPECTED_UPLOADS = 4
CATEGORY = 'Portal 娱乐'


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_source_run(payload: dict, run_id: str, repository: str) -> None:
    if (not run_id.isdigit() or str(payload.get('id')) != run_id
            or (payload.get('repository') or {}).get('full_name') != repository
            or (payload.get('head_repository') or {}).get('full_name') != repository
            or payload.get('head_branch') != 'main'
            or payload.get('path') != '.github/workflows/xhs-lowfan-kc-daily.yml'
            or payload.get('event') not in {'schedule', 'workflow_dispatch'}
            or payload.get('status') != 'completed'
            or payload.get('conclusion') not in {'success', 'failure', 'cancelled', 'timed_out'}):
        raise ValueError('Recovery source must be a completed main-branch XHS daily run in this repository')


def assert_not_delivered(state: Path, date: str) -> None:
    if state.is_file():
        for item in read_json(state).get('items', []):
            if item.get('date') == date and item.get('status') == 'delivered':
                raise ValueError('Do not replace an already delivered business date with a partial outcome')


def prepare(resume_root: Path, date: str, output_dir: Path,
            processed_manifest: Path, delivery_state: Path) -> dict:
    date = validated_iso_date(date)
    output_dir = output_dir.resolve()
    assert_not_delivered(delivery_state, date)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError('Partial recovery output directory must be empty')
    ledger = read_json(processed_manifest) if processed_manifest.is_file() else {'items': []}
    output_dir.mkdir(parents=True, exist_ok=True)
    # Re-verifying the same date is idempotent; another date's delivered notes
    # remain excluded. Re-upload uses the same file paths and verifies sizes.
    excluded = output_dir / 'other_date_processed.json'
    write_json(excluded, {'items': [item for item in ledger.get('items', [])
                                  if item.get('delivery_date') != date]})
    summary = merge_artifacts(resume_root.resolve(), date, output_dir, TARGET,
                              processed_manifest=excluded, validate_media_files=True)
    if summary['prior_tikhub_requests'] != 99:
        raise ValueError('Existing partial recovery requires an exhausted cumulative 99-request budget')
    if summary['resumed_count'] != EXPECTED_UPLOADS or summary['target_met']:
        raise ValueError('Partial recovery requires exactly four verified existing videos for the five-item goal')
    manifest = read_json(output_dir / 'resume_processed.json')
    items = manifest['items']
    expected_names = {item['output'] for item in items}
    actual_names = {path.name for path in output_dir.rglob('*.mp4')}
    if (len({item['note_id'] for item in items}) != EXPECTED_UPLOADS
            or len(expected_names) != EXPECTED_UPLOADS or actual_names != expected_names):
        raise ValueError('Partial recovery output/identity inventory mismatch')
    existing_same_day = {item.get('note_id') for item in ledger.get('items', [])
                         if item.get('delivery_date') == date}
    if not existing_same_day.issubset({item['note_id'] for item in items}):
        raise ValueError('Another partial set already exists for this date; reconcile it before recovery')
    for item in items:
        item['delivery_date'] = date
    write_json(output_dir / 'partial_processed.json', {'date': date, 'items': items})
    receipt = {
        'schema': 'xhs-existing-partial-recovery/v1', 'status': 'prepared', 'date': date,
        'target': TARGET, 'expected_upload_count': EXPECTED_UPLOADS, 'target_met': False,
        'new_tikhub_requests': 0, 'new_generated_videos': 0,
        'prior_tikhub_requests': summary['prior_tikhub_requests'],
        'source_run_ids': summary['contributing_run_ids'], 'output_dir': str(output_dir),
        'manifest_sha256': sha256(output_dir / 'partial_processed.json'),
        'budget_sha256': sha256(output_dir / 'cumulative_tikhub_request_budget.json'),
        'files': [{'note_id': item['note_id'], 'name': item['output'],
                   'bytes': (output_dir / item['output']).stat().st_size,
                   'sha256': sha256(output_dir / item['output'])} for item in items],
    }
    write_json(output_dir / 'partial_recovery_receipt.json', receipt)
    return receipt


def finalize(output_dir: Path, upload_manifest: Path, processed_manifest: Path,
             delivery_state: Path, run_id: str) -> dict:
    output_dir = output_dir.resolve()
    receipt = read_json(output_dir / 'partial_recovery_receipt.json')
    date = validated_iso_date(receipt['date'])
    assert_not_delivered(delivery_state, date)
    if (receipt.get('schema') != 'xhs-existing-partial-recovery/v1'
            or receipt.get('status') != 'prepared' or receipt.get('target') != TARGET
            or receipt.get('expected_upload_count') != EXPECTED_UPLOADS
            or receipt.get('target_met') is not False
            or receipt.get('prior_tikhub_requests') != 99
            or receipt.get('new_tikhub_requests') != 0 or receipt.get('new_generated_videos') != 0
            or receipt.get('output_dir') != str(output_dir)):
        raise ValueError('Invalid partial recovery receipt')
    for filename, field in [('partial_processed.json', 'manifest_sha256'),
                            ('cumulative_tikhub_request_budget.json', 'budget_sha256')]:
        if sha256(output_dir / filename) != receipt[field]:
            raise ValueError('Prepared recovery metadata changed before verification')
    files = receipt['files']
    expected = {item['name']: item for item in files}
    if len(files) != EXPECTED_UPLOADS or len(expected) != EXPECTED_UPLOADS:
        raise ValueError('Invalid prepared file inventory')
    if {path.name for path in output_dir.rglob('*.mp4')} != set(expected):
        raise ValueError('MP4 inventory changed after preparation')
    for name, item in expected.items():
        if Path(name).name != name or sha256(output_dir / name) != item['sha256']:
            raise ValueError('Prepared video changed before verification')
    upload = read_json(upload_manifest)
    if (upload.get('status') != 'completed' or upload.get('dry_run') is not False
            or upload.get('date') != date or upload.get('category') != CATEGORY
            or Path(upload.get('source_dir', '')).resolve() != output_dir
            or upload.get('file_count') != EXPECTED_UPLOADS
            or upload.get('verified_count') != EXPECTED_UPLOADS or upload.get('failed_count') != 0):
        raise ValueError('Jianguoyun has not verified the four prepared outputs')
    remote_directory = str(upload.get('remote_directory') or '')
    if not remote_directory.endswith('/' + date + '/' + CATEGORY + '/'):
        raise ValueError('Jianguoyun destination does not match the recovery date/category')
    uploaded = upload.get('files')
    if (not isinstance(uploaded, list) or len(uploaded) != EXPECTED_UPLOADS
            or {item.get('source') for item in uploaded} != set(expected)):
        raise ValueError('Jianguoyun file identity inventory mismatch')
    for item in uploaded:
        source = item['source']
        if (item.get('status') != 'verified' or item.get('size') != expected[source]['bytes']
                or item.get('verified_size') != expected[source]['bytes']
                or item.get('remote_path') != remote_directory + source):
            raise ValueError('Jianguoyun per-file verification mismatch')
    subprocess.run([sys.executable, str(Path(__file__).with_name('record_processed.py')),
                    '--new', str(output_dir / 'partial_processed.json'),
                    '--manifest', str(processed_manifest)], check=True)
    state = update_state(delivery_state, date, 'deferred', target=TARGET, succeeded=EXPECTED_UPLOADS,
                         reason='partial_verified_upload_budget_exhausted', run_id=run_id,
                         message='Four existing videos uploaded and verified; one of five remains missing. '
                                 f'No new generation; TikHub count remains {receipt["prior_tikhub_requests"]}/99.')
    receipt.update(status='partial_verified_upload', succeeded=EXPECTED_UPLOADS, verified_count=EXPECTED_UPLOADS,
                   target_met=False, upload_manifest_sha256=sha256(upload_manifest), recovery_run_id=run_id)
    write_json(output_dir / 'partial_recovery_receipt.json', receipt)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    source = sub.add_parser('validate-run')
    source.add_argument('--metadata', type=Path, required=True)
    source.add_argument('--run-id', required=True)
    source.add_argument('--repository', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--resume-root', type=Path, required=True)
    prep.add_argument('--date', required=True)
    finish = sub.add_parser('finalize')
    finish.add_argument('--upload-manifest', type=Path, required=True)
    finish.add_argument('--run-id', required=True)
    for command in (prep, finish):
        command.add_argument('--output-dir', type=Path, required=True)
        command.add_argument('--processed-manifest', type=Path, required=True)
        command.add_argument('--delivery-state', type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop('command')
    if command == 'validate-run':
        validate_source_run(read_json(args.pop('metadata')), **args)
        return
    result = prepare(**args) if command == 'prepare' else finalize(**args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
