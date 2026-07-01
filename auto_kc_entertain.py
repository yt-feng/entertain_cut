#!/usr/bin/env python3
"""One-click KC entertainment renderer for videos in new_video_pending."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}
WHISPER_MODEL = Path(os.environ.get("WHISPER_MODEL", "/Users/ytfeng/Models/whisper/ggml-small.bin")).expanduser()
MAIN_RATIO = 1080 / 796
ROOT_DIR = Path(__file__).resolve().parent
TITLE_ANCHORS = [
    "王一博",
    "肖战",
    "朱珠",
    "王濛",
    "涂雅",
    "乌兰图雅",
    "刘亦菲",
    "迪丽热巴",
    "汤唯",
    "关晓彤",
    "龚俊",
    "丁禹兮",
    "王菲",
    "窦唯",
    "窦靖童",
    "万金慧",
    "者兰女",
    "张艺兴",
]
GENERIC_TITLE_LINES = {
    "这段太有梗",
    "反应全是真",
    "品牌排面给足",
    "代言质感拉满",
    "质感拉满",
    "气场名场面",
    "爆笑名场面",
    "重点来了",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Specific video to process.")
    parser.add_argument("--input-dir", type=Path, default=Path("new_video_pending"))
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--work-dir", type=Path, default=Path("work/auto_kc"))
    parser.add_argument("--api-key-dir", type=Path, default=Path("api_key"))
    parser.add_argument("--language", default="auto", help="Whisper language, e.g. zh, en, auto.")
    parser.add_argument("--encoder", choices=["auto", "videotoolbox", "libx264"], default="libx264")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--all", action="store_true", help="Process all videos in input-dir. This is the default.")
    parser.add_argument("--latest", action="store_true", help="Only process the newest video in input-dir.")
    parser.add_argument("--force", action="store_true", help="Reprocess videos even if their content hash was already rendered.")
    parser.add_argument("--force-fallback", action="store_true", help="Skip DeepSeek and use local fallback captions.")
    parser.add_argument("--landscape-crop", default="", help="Override crop as width:height:x:y.")
    args = parser.parse_args()

    args.source = project_path(args.source) if args.source else None
    args.input_dir = project_path(args.input_dir)
    args.output_dir = project_path(args.output_dir)
    args.work_dir = project_path(args.work_dir)
    args.api_key_dir = project_path(args.api_key_dir)

    check_runtime()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    sources = find_sources(args.source, args.input_dir, latest_only=args.latest)
    if not sources:
        write_run_summary(args.work_dir, [], [])
        print(f"No video found in {args.input_dir}. Put videos into new_video_pending and run again.")
        return

    api_key = read_api_key(args.api_key_dir)
    manifest = load_manifest(args.work_dir)
    outputs: list[Path] = []
    for source in sources:
        output = process_one(source, args, api_key, manifest)
        if output is not None:
            outputs.append(output)
    save_manifest(args.work_dir, manifest)
    write_run_summary(args.work_dir, sources, outputs)

    if outputs:
        print("Done:")
        for output in outputs:
            print(f"  {output}")
    else:
        print("No new videos to process. Use --force to regenerate existing outputs.")


def find_sources(source: Path | None, input_dir: Path, *, latest_only: bool) -> list[Path]:
    if source is not None:
        return [source]
    if not input_dir.exists():
        return []
    videos = sorted(
        [path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return videos[:1] if latest_only else videos


def process_one(
    source: Path,
    args: argparse.Namespace,
    api_key: str,
    manifest: dict[str, Any],
) -> Path | None:
    if not source.exists():
        raise SystemExit(f"Source not found: {source}")
    source_hash = file_sha256(source)
    manifest_item = manifest.get(source_hash)
    if manifest_item and not args.force and args.source is None:
        output = Path(str(manifest_item.get("output", "")))
        if output.exists():
            print(f"Skipping already processed video: {source} -> {output}")
            return None

    media = probe_media(source)
    task_dir = args.work_dir / f"{safe_slug(source.stem)}_{source_hash[:8]}"
    asr_dir = task_dir / "asr"
    render_dir = task_dir / "render"
    frames_dir = task_dir / "frames"
    task_dir.mkdir(parents=True, exist_ok=True)
    asr_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    announce(f"Processing {source.name}")
    announce("1/6 抽取源视频关键帧，留给内容理解和质检")
    extract_keyframes(source, frames_dir / "source", float(media["duration"]))
    announce("2/6 尝试识别画面文字/OCR，用来辅助判断标题、字幕和水印")
    visual_text = collect_visual_text(frames_dir, task_dir / "visual_text.txt")

    audio = asr_dir / "audio_16k_mono.wav"
    transcript_base = asr_dir / "transcript"
    transcript_json = transcript_base.with_suffix(".json")
    announce("3/6 提取音频并用 Whisper 转写")
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio),
        ]
    )
    run(
        [
            whisper_cli(),
            "-m",
            str(WHISPER_MODEL),
            "-f",
            str(audio),
            "-l",
            args.language,
            "-oj",
            "-osrt",
            "-otxt",
            "-of",
            str(transcript_base),
            "-np",
        ]
    )

    transcript = load_transcript(transcript_json)
    duration = float(media["duration"])
    analysis = build_analysis(source, media, transcript, visual_text)
    plan = fallback_plan(source.stem, transcript, duration)
    announce("4/6 根据当前视频重新生成 KC 娱乐包装方案")
    if api_key and not args.force_fallback:
        try:
            plan = ask_deepseek(api_key, analysis)
        except Exception as exc:  # noqa: BLE001 - fallback should keep one-click rendering unblocked.
            print(f"DeepSeek plan failed, using fallback captions: {exc}")
    elif not api_key:
        print("No DeepSeek key found; using fallback captions.")
    plan = normalize_plan(plan, source.stem, transcript, duration)

    plan_path = task_dir / "caption_plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    output_name = safe_filename(str(plan.get("output_name") or ""))
    if not output_name:
        output_name = f"KC娱乐_{safe_filename(source.stem)}"
    if not output_name.startswith("KC娱乐_"):
        output_name = f"KC娱乐_{output_name}"
    output = unique_output_path(args.output_dir / f"{output_name}.mp4", force=args.force)

    announce("5/6 套 KC 娱乐版式、裁掉原始顶部/底部文字并做画面去重包装")
    render_cmd = [
        "python3",
        str(render_script_path()),
        "--source",
        str(source.resolve()),
        "--plan",
        str(plan_path.resolve()),
        "--out",
        str(output.resolve()),
        "--work-dir",
        str(render_dir.resolve()),
        "--encoder",
        args.encoder,
        "--threads",
        str(max(1, args.threads)),
    ]
    if int(media["height"]) > int(media["width"]):
        top_crop, bottom_crop = vertical_edge_crops(int(media["height"]))
        render_cmd.extend(
            [
                "--vertical-top-crop",
                str(top_crop),
                "--vertical-bottom-crop",
                str(bottom_crop),
            ]
        )
        usable_ratio = (int(media["height"]) - top_crop - bottom_crop) / max(1, int(media["width"]))
        if usable_ratio >= 1.18:
            render_cmd.append("--preserve-vertical-source")
    else:
        crop = args.landscape_crop or landscape_edge_crop(int(media["width"]), int(media["height"]))
        render_cmd.extend(["--landscape-crop", crop])

    run(render_cmd)
    announce("6/6 抽取成片关键帧，保存本次质检留痕")
    extract_keyframes(output, frames_dir / "final", float(media["duration"]))
    manifest[source_hash] = {
        "source": str(source),
        "output": str(output),
        "caption_plan": str(plan_path),
        "visual_text": str(task_dir / "visual_text.txt"),
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "duration": duration,
        "width": int(media["width"]),
        "height": int(media["height"]),
    }
    return output


def ask_deepseek(api_key: str, analysis: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the senior packaging editor for KC娱乐. "
                    "For every source video, analyze it from scratch: identify the topic, people, hook, joke, "
                    "information value, and the best short-video packaging angle. "
                    "You are also a ruthless short-video headline editor: titles must be short, sharp, specific, "
                    "and built around a person/brand/topic plus an unexpected tension. "
                    "Never reuse titles, callouts, badges, or examples from another video. "
                    "Use OCR text when available to understand original on-screen text, but always plan to crop "
                    "or replace the original platform captions/watermarks. "
                    "Fix obvious ASR mistakes from context. Return strict JSON only."
                ),
            },
            {
                "role": "user",
                "content": f"""
Build a fresh KC娱乐 packaging plan for this video.

Source analysis data:
{json.dumps(analysis, ensure_ascii=False, indent=2)}

Methodology:
- First infer the content type: celebrity/variety joke, brand/ad material, interview, English-learning clip, fan support, music/performance, lifestyle, or other.
- Choose a packaging angle that matches this video only. Do not use generic labels if a more specific one is available.
- The rendered KC wrapper is fixed, but every text element below must be content-specific.
- The original platform logo, top text, bottom subtitle strip, and edge watermarks will be removed/cropped before packaging.
- Headline rule: title_lines must be exactly 2 strings. Line 1 should name the strongest anchor when supported: celebrity name, brand, role, or concrete topic (王一博, 肖战, 房价, 浪姐, etc.). Line 2 should create a curiosity gap, contradiction, reversal, stakes, or emotional reason to keep watching.
- Prefer click-worthy tensions that are true to the source: counterintuitive claims, status reversal, before/after contrast, hidden cause, forced choice, emotional collapse, unexpected calm, "everyone thought X but Y", or a concrete number/result.
- For public/financial/economic topics, do not overclaim. If the evidence is uncertain, phrase it as tension or question, e.g. "房价还低迷 / 却说触底了", not a fake certainty.
- Avoid soft generic title lines such as "这段太有梗", "反应全是真", "质感拉满", "重点来了", unless no concrete person/topic/twist exists.
- Keep titles short enough for mobile: each title line preferably 3-9 Chinese characters, maximum 12 Chinese characters. Shorter and sharper is better.
- Make top_badge, side_badge, caption_badge, sticker_bottom, and lower_ribbon different roles, not repeated copies.
- sticker_top must be 1-6 uppercase English letters or digits only, such as HOT, NEW, TOP, 90, AI. No emoji, punctuation, or Chinese.
- sticker_bottom must be short Chinese, preferably 2-5 characters.
- Chinese subtitles should summarize or sharpen the moment, not blindly copy ASR.
- English subtitles should be short natural translations; if the video is English-learning content, preserve the useful English phrase.
- Fix ASR homophones from context, but do not invent events or names not supported by filename/transcript.
- If visual_text is available, use it only as source-content evidence; do not reuse platform watermarks as KC packaging copy.
- Use 4 to 8 subtitle blocks covering the main beats. Do not leave long meaningful speech uncovered.
- Highlight phrases must appear verbatim in the corresponding title/subtitle text.
- output_name must be a short Chinese file stem. It may start with KC娱乐_, but does not need to.

Return JSON only with exactly these top-level keys:
output_name, title_lines, title_highlights, top_badge, side_badge, caption_badge,
sticker_top, sticker_bottom, lower_ribbon, packaging_brief, dedupe_notes, subtitles.

Each subtitle object must contain:
start, end, zh, en, zh_highlights, en_highlights.
""",
            },
        ],
        "temperature": 0.25,
        "response_format": {"type": "json_object"},
    }
    req = Request(
        DEEPSEEK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return parse_json_object(content)


def fallback_plan(source_stem: str, transcript: list[dict[str, Any]], duration: float) -> dict[str, Any]:
    chunks = chunk_transcript(transcript, duration, target_blocks=5)
    source_text = f"{source_stem} {' '.join(item['text'] for item in transcript)}"
    headline = fallback_headline(source_text, source_stem)
    title_lines = headline["title_lines"]
    title_highlights = headline["title_highlights"]
    top_badge = headline["top_badge"]
    side_badge = headline["side_badge"]
    sticker_bottom = headline["sticker_bottom"]
    lower_ribbon = headline["lower_ribbon"]
    output_name = f"KC娱乐_{safe_filename(''.join(title_lines))}"

    subtitles = []
    for idx, chunk in enumerate(chunks, start=1):
        text = clean_asr_text(" ".join(item["text"] for item in chunk["items"]))
        subtitles.append(
            {
                "index": idx,
                "start": chunk["start"],
                "end": chunk["end"],
                "zh": text or "这个反应太真实了",
                "en": "",
                "zh_highlights": pick_highlights(text),
                "en_highlights": [],
            }
        )
    return {
        "output_name": output_name,
        "title_lines": title_lines,
        "title_highlights": title_highlights,
        "top_badge": top_badge,
        "side_badge": side_badge,
        "caption_badge": "重点来了",
        "sticker_top": "HOT",
        "sticker_bottom": sticker_bottom,
        "lower_ribbon": lower_ribbon,
        "packaging_brief": "本地 fallback：按文件名和转写粗分类型后生成 KC 包装。",
        "dedupe_notes": "裁掉原片顶部/底部平台文字，叠加 KC 标题、字幕、品牌区和轻微画面增强。",
        "subtitles": subtitles,
    }


def normalize_plan(
    plan: dict[str, Any],
    source_stem: str,
    transcript: list[dict[str, Any]],
    duration: float,
) -> dict[str, Any]:
    fallback = fallback_plan(source_stem, transcript, duration)
    title_lines = plan.get("title_lines")
    if not isinstance(title_lines, list) or len(title_lines) < 2:
        title_lines = fallback["title_lines"]
    title_lines = [normalize_space(str(line)) for line in title_lines[:2]]
    if not all(title_lines):
        title_lines = fallback["title_lines"]
    title_highlight_candidates = plan.get("title_highlights", fallback["title_highlights"])
    if title_is_generic(title_lines) and not title_is_generic(fallback["title_lines"]):
        title_lines = fallback["title_lines"]
        title_highlight_candidates = fallback["title_highlights"]

    subtitles: list[dict[str, Any]] = []
    for idx, item in enumerate(plan.get("subtitles", []), start=1):
        if not isinstance(item, dict):
            continue
        try:
            start = max(0.0, float(item["start"]))
            end = min(duration, float(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        zh = clean_asr_text(normalize_space(str(item.get("zh", ""))))
        en = normalize_space(str(item.get("en", "")))
        if not zh and not en:
            continue
        subtitles.append(
            {
                "index": idx,
                "start": round(start, 3),
                "end": round(end, 3),
                "zh": zh,
                "en": en,
                "zh_highlights": valid_phrases(zh, item.get("zh_highlights", [])),
                "en_highlights": valid_phrases(en, item.get("en_highlights", []), case_insensitive=True),
            }
        )
    if not subtitles:
        subtitles = fallback["subtitles"]

    output_name = safe_filename(str(plan.get("output_name") or fallback["output_name"]))
    return {
        "output_name": output_name,
        "title_lines": title_lines,
        "title_highlights": valid_phrases(" ".join(title_lines), title_highlight_candidates),
        "top_badge": normalize_space(str(plan.get("top_badge", fallback["top_badge"]))) or fallback["top_badge"],
        "side_badge": normalize_space(str(plan.get("side_badge", fallback["side_badge"]))) or fallback["side_badge"],
        "caption_badge": normalize_space(str(plan.get("caption_badge", fallback["caption_badge"]))) or fallback["caption_badge"],
        "sticker_top": normalize_sticker_top(plan.get("sticker_top", fallback["sticker_top"])),
        "sticker_bottom": normalize_sticker_bottom(plan.get("sticker_bottom", fallback["sticker_bottom"])),
        "lower_ribbon": normalize_space(str(plan.get("lower_ribbon", fallback["lower_ribbon"]))) or fallback["lower_ribbon"],
        "packaging_brief": normalize_space(str(plan.get("packaging_brief", fallback["packaging_brief"]))),
        "dedupe_notes": normalize_space(str(plan.get("dedupe_notes", fallback["dedupe_notes"]))),
        "subtitles": subtitles,
    }


def build_analysis(
    source: Path,
    media: dict[str, float],
    transcript: list[dict[str, Any]],
    visual_text: dict[str, Any],
) -> dict[str, Any]:
    duration = float(media["duration"])
    orientation = "vertical" if int(media["height"]) > int(media["width"]) else "landscape"
    transcript_text = " ".join(item["text"] for item in transcript)
    return {
        "filename": source.name,
        "stem": source.stem,
        "duration": round(duration, 3),
        "width": int(media["width"]),
        "height": int(media["height"]),
        "orientation": orientation,
        "detected_title_anchor": detect_title_anchor(f"{source.name} {transcript_text}"),
        "headline_strategy": [
            "Prefer a real person/brand/topic name over generic words.",
            "Pair the anchor with one unexpected tension: reversal, counterintuitive fact, hidden cause, stakes, or emotional contrast.",
            "Keep title_lines short and sharp; avoid vague lines like 这段太有梗 or 质感拉满.",
        ],
        "top_bottom_cleanup": (
            "landscape sources crop central subject area to remove top logos/titles and bottom original subtitles"
            if orientation == "landscape"
            else "vertical sources crop top/bottom edges and preserve full subject area when possible"
        ),
        "source_frame_samples": visual_text.get("frames", []),
        "visual_text": visual_text,
        "transcript_text": transcript_text,
        "transcript": transcript,
    }


def fallback_headline(source_text: str, source_stem: str) -> dict[str, Any]:
    anchor = detect_title_anchor(f"{source_stem} {source_text}")
    if any(word in source_text for word in ["房价", "房地产", "楼市", "买房", "卖房", "触底", "回升"]):
        return {
            "title_lines": ["房价还低迷", "却说触底了"],
            "title_highlights": ["房价", "触底"],
            "top_badge": "反常识",
            "side_badge": "楼市转折",
            "sticker_bottom": "反转",
            "lower_ribbon": "房价低迷下的反常识判断",
        }
    if any(word in source_text for word in ["淘汰", "哭", "崩", "破防", "眼泪", "情绪"]):
        first = f"{anchor}哭崩" if anchor else "她突然哭崩"
        return {
            "title_lines": [first, "原因太扎心"],
            "title_highlights": [anchor or "哭崩", "扎心"],
            "top_badge": "情绪爆点",
            "side_badge": "原因反转",
            "sticker_bottom": "破防",
            "lower_ribbon": "哭到停不下来的真正原因",
        }
    if any(word in source_text for word in ["英语", "英文", "English", "跟练", "表达", "逻辑"]):
        first = f"{anchor}开口" if anchor else "这句英文"
        return {
            "title_lines": [first, "比想象稳"],
            "title_highlights": [anchor or "英文", "稳"],
            "top_badge": "英语名场面",
            "side_badge": "反差感",
            "sticker_bottom": "开口跪",
            "lower_ribbon": "一开口就有反差",
        }
    if any(word in source_text for word in ["得宝", "Tempo", "白象", "代言", "品牌", "同款", "小卡", "明信片"]):
        first = f"{anchor}同款" if anchor else "明星同款"
        second = "还送小卡" if any(word in source_text for word in ["小卡", "明信片"]) else "排面拉满"
        return {
            "title_lines": [first, second],
            "title_highlights": [anchor or "同款", "小卡" if "小卡" in second else "排面"],
            "top_badge": "明星同款",
            "side_badge": "福利别漏",
            "sticker_bottom": "同款",
            "lower_ribbon": "明星同款福利点",
        }
    if anchor:
        return {
            "title_lines": [f"{anchor}这次", "反差太大"],
            "title_highlights": [anchor, "反差"],
            "top_badge": "意外一幕",
            "side_badge": "反差感",
            "sticker_bottom": "反转",
            "lower_ribbon": "越看越不对劲的一幕",
        }
    return {
        "title_lines": ["前面还正常", "下一秒反转"],
        "title_highlights": ["正常", "反转"],
        "top_badge": "意外一幕",
        "side_badge": "下一秒",
        "sticker_bottom": "反转",
        "lower_ribbon": "看到后面才懂",
    }


def detect_title_anchor(text: str) -> str:
    for anchor in TITLE_ANCHORS:
        if anchor in text:
            return anchor
    return ""


def title_is_generic(title_lines: list[str]) -> bool:
    compact = {normalize_space(line) for line in title_lines}
    if compact & GENERIC_TITLE_LINES:
        return True
    joined = "".join(compact)
    return bool(joined) and not detect_title_anchor(joined) and any(word in joined for word in ["有梗", "真实", "质感", "名场面"])


def chunk_transcript(transcript: list[dict[str, Any]], duration: float, *, target_blocks: int) -> list[dict[str, Any]]:
    if not transcript:
        block = duration / target_blocks
        return [
            {"start": round(i * block, 3), "end": round(min(duration, (i + 1) * block), 3), "items": []}
            for i in range(target_blocks)
        ]
    items_per = max(1, math.ceil(len(transcript) / target_blocks))
    chunks = []
    for idx in range(0, len(transcript), items_per):
        items = transcript[idx : idx + items_per]
        chunks.append({"start": items[0]["start"], "end": items[-1]["end"], "items": items})
    return chunks


def load_transcript(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for item in data.get("transcription", []):
        offsets = item.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", 0)) / 1000.0
        text = clean_asr_text(str(item.get("text", "")))
        if text and end > start:
            result.append({"start": start, "end": end, "text": text})
    return result


def clean_asr_text(text: str) -> str:
    text = normalize_space(text)
    replacements = {
        "银姐": "颖姐",
        "肉毒蒜": "肉毒算",
        "我不以美的": "我不医美的",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def normalize_sticker_top(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]", "", normalize_space(str(value))).upper()
    return text[:6] if text else "HOT"


def normalize_sticker_bottom(value: Any) -> str:
    text = normalize_space(str(value))
    text = re.sub(r"\s+", "", text)
    return text[:5] if text else "爆点"


def landscape_edge_crop(width: int, height: int) -> str:
    top_margin = even_int(height * 0.16)
    bottom_margin = even_int(height * 0.18)
    crop_h = max(240, height - top_margin - bottom_margin)
    crop_h = min(height, even_int(crop_h))
    crop_w = min(width, even_int(crop_h * MAIN_RATIO))
    crop_x = even_int((width - crop_w) / 2)
    crop_y = min(height - crop_h, top_margin)
    crop_y = even_int(crop_y)
    return f"{crop_w}:{crop_h}:{crop_x}:{crop_y}"


def vertical_edge_crops(height: int) -> tuple[int, int]:
    return even_int(height * 0.15), even_int(height * 0.20)


def even_int(value: float) -> int:
    result = max(0, int(round(value)))
    return result - (result % 2)


def load_manifest(work_dir: Path) -> dict[str, Any]:
    path = manifest_path(work_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(work_dir: Path, manifest: dict[str, Any]) -> None:
    path = manifest_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def manifest_path(work_dir: Path) -> Path:
    return work_dir / "processed_manifest.json"


def write_run_summary(work_dir: Path, sources: list[Path], outputs: list[Path]) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    outputs_path = work_dir / "last_run_outputs.txt"
    summary_path = work_dir / "last_run_summary.md"
    outputs_path.write_text(
        "\n".join(str(path.resolve()) for path in outputs) + ("\n" if outputs else ""),
        encoding="utf-8",
    )
    lines = [
        "# KC娱乐自动处理报告",
        "",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 扫描视频数：{len(sources)}",
        f"- 新生成成片数：{len(outputs)}",
        "",
        "## 成片",
    ]
    if outputs:
        lines.extend(f"- {path.resolve()}" for path in outputs)
    else:
        lines.append("- 没有新成片；可能这些素材已经处理过。需要重跑时在终端执行：`python3 auto_kc_entertain.py --force`")
    lines.extend(
        [
            "",
            "## 说明",
            "- 新视频放进 `new_video_pending/` 后，双击 `run_kc_entertain.command` 即可批量处理。",
            "- 每条素材的转写、可选 OCR、包装计划、源抽帧和成片抽帧保存在 `work/auto_kc/<素材名>_<hash>/`。",
            "- 脚本会按内容 hash 跳过已处理素材，避免重复生成。",
            "- 若本机没有 OCR 引擎，流程会明确记录并继续使用文件名和 Whisper 转写逐条包装。",
        ]
    )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_output_path(path: Path, *, force: bool) -> Path:
    if force or not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for idx in range(2, 1000):
        candidate = parent / f"{stem}_{idx}{suffix}"
        if not candidate.exists():
            return candidate
    raise SystemExit(f"Too many output name collisions for {path}")


def extract_keyframes(source: Path, out_prefix: Path, duration: float) -> None:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    timestamps = sorted({max(0.0, min(duration - 0.05, value)) for value in [1.0, duration * 0.35, duration * 0.65, duration - 1.0]})
    for idx, timestamp in enumerate(timestamps, start=1):
        output = out_prefix.parent / f"{out_prefix.name}_{idx:02d}.png"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                str(output),
            ]
        )


def collect_visual_text(frames_dir: Path, out_path: Path) -> dict[str, Any]:
    frames = sorted(frames_dir.glob("source_*.png"))
    result: dict[str, Any] = {
        "available": False,
        "engine": "",
        "reason": "",
        "frames": [str(path.resolve()) for path in frames],
        "text": "",
        "items": [],
    }
    engine = shutil.which("tesseract")
    if not frames:
        result["reason"] = "no source frames extracted"
        write_visual_text_report(out_path, result)
        return result
    if not engine:
        result["reason"] = "tesseract not installed; visual OCR skipped"
        write_visual_text_report(out_path, result)
        print("OCR unavailable: tesseract not installed; using filename + Whisper transcript.", flush=True)
        return result

    result["available"] = True
    result["engine"] = engine
    lang = tesseract_language(engine)
    items = []
    for frame in frames:
        text = run_tesseract(engine, frame, lang)
        if text:
            items.append({"frame": str(frame.resolve()), "text": text})
    result["items"] = items
    result["text"] = "\n".join(item["text"] for item in items)
    if not items:
        result["reason"] = "OCR engine ran but did not detect readable text"
    write_visual_text_report(out_path, result)
    return result


def write_visual_text_report(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"available: {result.get('available')}",
        f"engine: {result.get('engine')}",
        f"reason: {result.get('reason')}",
        "",
        "frames:",
        *[f"- {frame}" for frame in result.get("frames", [])],
        "",
        "text:",
        str(result.get("text") or ""),
        "",
        "json:",
        json.dumps(result, ensure_ascii=False, indent=2),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tesseract_language(engine: str) -> str:
    try:
        result = subprocess.run(
            [engine, "--list-langs"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError:
        return ""
    langs = {line.strip() for line in result.stdout.splitlines() if line.strip() and "List of" not in line}
    if {"chi_sim", "eng"}.issubset(langs):
        return "chi_sim+eng"
    if "chi_sim" in langs:
        return "chi_sim"
    if "eng" in langs:
        return "eng"
    return ""


def run_tesseract(engine: str, frame: Path, lang: str) -> str:
    base_cmd = [engine, str(frame), "stdout", "--psm", "6"]
    commands = [base_cmd + ["-l", lang]] if lang else []
    commands.append(base_cmd)
    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError:
            continue
        text = clean_ocr_text(result.stdout)
        if text:
            return text
    return ""


def clean_ocr_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = normalize_space(line)
        if len(line) >= 2:
            lines.append(line)
    return "\n".join(lines)


def read_api_key(api_key_dir: Path) -> str:
    candidates = [
        api_key_dir / "Deepseek_api.txt",
        api_key_dir / "deepseek_api.txt",
        ROOT_DIR / "Deepseek_api.txt",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def probe_media(path: Path) -> dict[str, float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    data = json.loads(result.stdout)
    width = height = 0
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = int(stream.get("width", 0))
            height = int(stream.get("height", 0))
            break
    return {"duration": float(data["format"]["duration"]), "width": float(width), "height": float(height)}


def whisper_cli() -> str:
    found = shutil.which("whisper-cli")
    if found:
        return found
    fallback = Path("/opt/homebrew/bin/whisper-cli")
    if fallback.exists():
        return str(fallback)
    raise SystemExit("whisper-cli not found")


def render_script_path() -> Path:
    candidates = [
        ROOT_DIR / "Archive" / "render_entertain_vertical.py",
        ROOT_DIR / "render_entertain_vertical.py",
    ]
    for path in candidates:
        if path.exists():
            return path
    raise SystemExit("render_entertain_vertical.py not found in project root or Archive/")


def check_runtime() -> None:
    missing = [binary for binary in ["ffmpeg", "ffprobe"] if not shutil.which(binary)]
    if not WHISPER_MODEL.exists():
        missing.append(str(WHISPER_MODEL))
    if missing:
        raise SystemExit(f"Missing runtime dependencies: {', '.join(missing)}")
    whisper_cli()
    render_script_path()


def project_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT_DIR / path


def announce(message: str) -> None:
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def valid_phrases(text: str, phrases: Any, *, case_insensitive: bool = False) -> list[str]:
    if not isinstance(phrases, list):
        return []
    haystack = text.lower() if case_insensitive else text
    result = []
    for raw in phrases:
        phrase = normalize_space(str(raw))
        if not phrase or phrase in result:
            continue
        needle = phrase.lower() if case_insensitive else phrase
        if needle in haystack:
            result.append(phrase)
    return result[:4]


def pick_highlights(text: str) -> list[str]:
    phrases = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,6}", text)
    return phrases[:2]


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def safe_slug(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text, flags=re.UNICODE).strip("._")
    return text or "video"


def safe_filename(text: str) -> str:
    text = normalize_space(text)
    text = re.sub(r"[/:*?\"<>|\\]+", "", text)
    text = re.sub(r"\s+", "", text)
    return text[:64].strip("._")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


if __name__ == "__main__":
    main()
