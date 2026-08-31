#!/usr/bin/env python3
"""Create a daily batch of current low-follower viral posts as KC娱乐 videos.

Discovery happens once. Every TikHub HTTP attempt made by discovery and all
per-note comment fetches consumes the same persistent budget file, whose limit
is always below 100.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "xhs2vid"
BEIJING = ZoneInfo("Asia/Shanghai")

DAILY_KEYWORDS = (
    "日常 离谱",
    "打工人 吐槽",
    "情感 扎心",
    "相亲 奇葩",
    "家庭 趣事",
    "邻居 奇葩",
    "恋爱 吐槽",
    "婚姻 现实",
)

# Cover voice followed by six deliberately distinct character voices. The
# faster Monkey tempo incorporates the user's previous feedback.
COVER_VOICE = ("BV005_streaming", 1.18, "娱乐扒妹")
CHARACTER_VOICES = (
    ("BV411_streaming", 1.24, "解说小帅"),
    ("zh_male_xionger_stream_gpu", 1.28, "熊二"),
    ("zh_male_sunwukong_clone2", 1.48, "猴哥"),
    ("BV050_streaming", 1.28, "动漫小新"),
    ("BV417_streaming", 1.24, "派星星"),
    ("zh_female_peiqi", 1.25, "佩奇猪"),
)


def run(command: list[str], *, cwd: Path = ROOT) -> None:
    printable = " ".join(command[:3]) + (" …" if len(command) > 3 else "")
    print(f"[run] {printable}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def safe_filename(value: str, *, limit: int = 42) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f\s]+", "_", value).strip("._")
    return (cleaned or "网友热议")[:limit]


def processed_note_ids(path: Path) -> list[str]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        entries = payload
    else:
        entries = payload.get("items") or payload.get("processed") or []
    result: list[str] = []
    for item in entries:
        note_id = item if isinstance(item, str) else item.get("note_id", "")
        if note_id:
            result.append(str(note_id))
    return result


def selected_reply_count(comments: list[dict]) -> int:
    return sum(bool(comment.get("sub_comments")) for comment in comments)


def voice_arguments(comments: list[dict], video_index: int) -> tuple[list[str], list[dict]]:
    segment_count = 1 + len(comments) + selected_reply_count(comments)
    rotated = list(CHARACTER_VOICES)
    offset = (video_index - 1) % len(rotated)
    rotated = rotated[offset:] + rotated[:offset]
    required_characters = segment_count - 1
    if required_characters > len(rotated):
        raise RuntimeError(
            f"video needs {required_characters} character voices; roster has {len(rotated)}"
        )
    roster = [COVER_VOICE, *rotated[:required_characters]]
    args: list[str] = []
    manifest: list[dict] = []
    for speaker, tempo, name in roster:
        args.extend(["--segment-speaker", speaker, "--segment-tempo", f"{tempo:.2f}"])
        manifest.append({"speaker_id": speaker, "tempo": tempo, "name": name})
    return args, manifest


def validate_video(path: Path) -> dict:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(probe.stdout)
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = float((payload.get("format") or {}).get("duration") or 0)
    if not video or not audio:
        raise RuntimeError("rendered file must contain video and audio streams")
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (1080, 1920):
        raise RuntimeError(f"unexpected frame size: {video.get('width')}x{video.get('height')}")
    if video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
        raise RuntimeError(
            f"unexpected codecs: {video.get('codec_name')}+{audio.get('codec_name')}"
        )
    if not 3 <= duration <= 300:
        raise RuntimeError(f"unexpected duration: {duration:.3f}s")
    run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"])
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "duration_seconds": round(duration, 3),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "frame_rate": video.get("avg_frame_rate"),
    }


def parse_args() -> argparse.Namespace:
    today = datetime.now(BEIJING).date().isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--date", default=today)
    parser.add_argument("--work-root", type=Path, default=ROOT / "work" / "xhs_daily")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "xhs_daily" / today)
    parser.add_argument(
        "--processed-manifest",
        type=Path,
        default=SCRIPT_DIR / "state" / "processed_note_ids.json",
    )
    parser.add_argument("--request-limit", type=int, default=90)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--top-author-check", type=int, default=20)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--reserve", type=int, default=3)
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="First output number; used when resuming a partially completed batch.",
    )
    parser.add_argument("--keyword", action="append", dest="keywords")
    parser.add_argument(
        "--avatar-provider",
        choices=("apimart", "local"),
        default="apimart",
    )
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=ROOT / "api_key" / "api_mart.txt",
        help="Local-only fallback; Actions should set APIMART_API_KEY.",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 5:
        parser.error("--limit must be between 1 and 5")
    if not 1 <= args.reserve <= 5:
        parser.error("--reserve must be between 1 and 5")
    if not 1 <= args.request_limit < 100:
        parser.error("--request-limit must be between 1 and 99")
    if not 1 <= args.pages <= 3:
        parser.error("--pages must be between 1 and 3")
    if not 1 <= args.top_author_check <= 20:
        parser.error("--top-author-check must be between 1 and 20")
    if not 1 <= args.max_attempts <= 2:
        parser.error("--max-attempts must be 1 or 2 for the daily batch")
    if not 1 <= args.start_index <= 5:
        parser.error("--start-index must be between 1 and 5")
    if args.start_index + args.limit - 1 > 5:
        parser.error("--start-index plus --limit must fit within five outputs")
    if args.keywords:
        args.keywords = [value.strip() for value in args.keywords if value.strip()]
        if not 1 <= len(args.keywords) <= 8:
            parser.error("provide between 1 and 8 --keyword values")
    return args


def main() -> None:
    args = parse_args()
    run_stamp = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S")
    batch_dir = (args.work_root / args.date / run_stamp).expanduser().resolve()
    discovery_dir = batch_dir / "discovery"
    notes_root = batch_dir / "notes"
    output_dir = args.output_dir.expanduser().resolve()
    discovery_dir.mkdir(parents=True, exist_ok=True)
    notes_root.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    budget_file = batch_dir / "tikhub_request_budget.json"

    excluded = processed_note_ids(args.processed_manifest.expanduser().resolve())
    desired_candidates = min(args.limit + args.reserve, args.top_author_check, 20)
    discovery_command = [
        sys.executable,
        str(SCRIPT_DIR / "discover_note.py"),
        str(discovery_dir),
        "--pages", str(args.pages),
        "--top-author-check", str(args.top_author_check),
        "--max-attempts", str(args.max_attempts),
        "--request-limit", str(args.request_limit),
        "--budget-file", str(budget_file),
        "--limit", str(desired_candidates),
        "--strict-low-fan",
        "--prefer-same-day",
    ]
    for keyword in args.keywords or DAILY_KEYWORDS:
        discovery_command.extend(["--keyword", keyword])
    for note_id in excluded:
        discovery_command.extend(["--exclude-note-id", note_id])
    run(discovery_command)

    candidates = json.loads(
        (discovery_dir / "selected_notes.json").read_text(encoding="utf-8")
    )
    summary: dict = {
        "date": args.date,
        "started_at": datetime.now(BEIJING).isoformat(),
        "requested": args.limit,
        "start_index": args.start_index,
        "batch_dir": str(batch_dir),
        "output_dir": str(output_dir),
        "budget_file": str(budget_file),
        "candidate_count": len(candidates),
        "items": [],
    }
    apimart_slots_used = 0

    for candidate_index, note in enumerate(candidates, 1):
        if sum(item.get("status") == "success" for item in summary["items"]) >= args.limit:
            break
        note_id = str(note["note_id"])
        item_index = args.start_index + sum(
            item.get("status") == "success" for item in summary["items"]
        )
        note_dir = notes_root / f"{candidate_index:02d}_{note_id}"
        note_dir.mkdir(parents=True, exist_ok=True)
        (note_dir / "chosen_note.json").write_text(
            json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        item: dict = {
            "candidate_index": candidate_index,
            "output_index": item_index,
            "note_id": note_id,
            "title": note.get("title") or "",
            "author_fans": note.get("author_fans"),
            "liked_count": note.get("liked_count"),
            "comments_count": note.get("comments_count"),
            "work_dir": str(note_dir),
            "status": "processing",
        }
        summary["items"].append(item)
        try:
            run([
                sys.executable,
                str(SCRIPT_DIR / "fetch_assets.py"),
                str(note_dir),
                "--max-attempts", str(args.max_attempts),
                "--request-limit", str(args.request_limit),
                "--budget-file", str(budget_file),
                "--max-subcomment-calls", "0",
            ])
            item_avatar_provider = args.avatar_provider
            if item_avatar_provider == "apimart" and apimart_slots_used >= args.limit:
                item_avatar_provider = "local"
            identity_command = [
                sys.executable,
                str(SCRIPT_DIR / "generate_identities.py"),
                "--work-dir", str(note_dir),
                "--provider", item_avatar_provider,
            ]
            if args.api_key_file:
                identity_command.extend(["--api-key-file", str(args.api_key_file)])
            if item_avatar_provider == "apimart":
                # Count the slot even when APIMart falls back locally: an
                # ambiguous POST timeout may already be billable upstream.
                apimart_slots_used += 1
            run(identity_command)
            item["avatar_provider"] = item_avatar_provider

            comments = json.loads((note_dir / "top_comments.json").read_text(encoding="utf-8"))
            if not comments:
                raise RuntimeError("no usable comment thread after normalization")
            voice_args, voice_manifest = voice_arguments(comments[:3], item_index)
            item["voices"] = voice_manifest
            title = safe_filename(str(note.get("title") or note.get("desc") or "网友热议"))
            output = output_dir / f"{item_index:02d}_{title}_{note_id[-8:]}.mp4"
            render_command = [
                sys.executable,
                str(SCRIPT_DIR / "render_video.py"),
                "--work-dir", str(note_dir),
                "--voice", "jianying-machine",
                "--jianying-processing", "character",
                "--include-subcomments",
                "--max-comments", "3",
                "--render-dir", str(note_dir / "render"),
                "--output", str(output),
                *voice_args,
            ]
            try:
                run(render_command)
            except subprocess.CalledProcessError:
                print(f"[warn] first render failed for {note_id}; retry cached pages once")
                run([*render_command, "--reuse-tts-cache"])
            item["video"] = validate_video(output)
            item["output"] = str(output)
            item["status"] = "success"
            success_number = sum(
                entry.get("status") == "success" for entry in summary["items"]
            )
            print(
                f"[success] {success_number}/{args.limit} "
                f"output#{item_index} {output.name}"
            )
        except Exception as exc:  # noqa: BLE001
            item["status"] = "failed"
            item["error"] = f"{type(exc).__name__}: {exc}"[:1000]
            (note_dir / "error.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
            print(f"[warn] candidate {note_id} failed: {item['error']}")

        (batch_dir / "daily_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    successes = [item for item in summary["items"] if item.get("status") == "success"]
    summary["completed_at"] = datetime.now(BEIJING).isoformat()
    summary["succeeded"] = len(successes)
    summary["target_met"] = len(successes) == args.limit
    summary["apimart_create_slot_limit"] = args.limit
    summary["apimart_create_slots_used"] = apimart_slots_used
    if budget_file.is_file():
        budget = json.loads(budget_file.read_text(encoding="utf-8"))
        summary["tikhub_requests_used"] = int(budget.get("used", 0))
        summary["tikhub_request_limit"] = int(budget.get("limit", args.request_limit))

    summary_path = batch_dir / "daily_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(summary_path, output_dir / "daily_summary.json")
    if budget_file.is_file():
        shutil.copy2(budget_file, output_dir / "tikhub_request_budget.json")
    for discovery_name in ("candidates.json", "selected_notes.json"):
        discovery_file = discovery_dir / discovery_name
        if discovery_file.is_file():
            shutil.copy2(discovery_file, output_dir / discovery_name)
    new_processed = {
        "date": args.date,
        "items": [
            {
                "note_id": item["note_id"],
                "title": item["title"],
                "output": Path(item["output"]).name,
                "rendered_at": summary["completed_at"],
            }
            for item in successes
        ],
    }
    (output_dir / "new_processed.json").write_text(
        json.dumps(new_processed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    latest_file = args.work_root.expanduser().resolve() / "latest_run.txt"
    latest_file.parent.mkdir(parents=True, exist_ok=True)
    latest_file.write_text(str(batch_dir), encoding="utf-8")
    print(
        f"[done] {len(successes)}/{args.limit} videos; "
        f"TikHub {summary.get('tikhub_requests_used', 0)}/{args.request_limit}; "
        f"summary={summary_path}"
    )
    if len(successes) != args.limit:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
