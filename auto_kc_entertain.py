#!/usr/bin/env python3
"""One-click KC entertainment renderer for videos in new_video_pending."""

from __future__ import annotations

import argparse
import html
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
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageStat


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
    "于正",
    "即梦",
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
    parser.add_argument("--metadata-file", type=Path, default=None, help="Optional selected-video metadata JSON, e.g. Douyin selected.json.")
    args = parser.parse_args()

    args.source = project_path(args.source) if args.source else None
    args.input_dir = project_path(args.input_dir)
    args.output_dir = project_path(args.output_dir)
    args.work_dir = project_path(args.work_dir)
    args.api_key_dir = project_path(args.api_key_dir)
    args.metadata_file = project_path(args.metadata_file) if args.metadata_file else None

    check_runtime()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    sources = find_sources(args.source, args.input_dir, latest_only=args.latest)
    if not sources:
        write_run_summary(args.work_dir, [], [])
        print(f"No video found in {args.input_dir}. Put videos into new_video_pending and run again.")
        return

    api_key = read_api_key(args.api_key_dir)
    metadata_index = load_source_metadata(args.metadata_file, args.input_dir)
    manifest = load_manifest(args.work_dir)
    outputs: list[Path] = []
    for source in sources:
        output = process_one(source, args, api_key, manifest, metadata_for_source(source, metadata_index))
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
    source_metadata: dict[str, Any],
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
    announce("1/7 抽取源视频关键帧，留给内容理解和质检")
    extract_keyframes(source, frames_dir / "source", float(media["duration"]))
    visual_layout = analyze_visual_layout(frames_dir, media)
    (task_dir / "visual_layout.json").write_text(json.dumps(visual_layout, ensure_ascii=False, indent=2), encoding="utf-8")
    announce("2/7 尝试识别画面文字/OCR，用来辅助判断标题、字幕和水印")
    visual_text = collect_visual_text(frames_dir, task_dir / "visual_text.txt")

    audio = asr_dir / "audio_16k_mono.wav"
    transcript_base = asr_dir / "transcript"
    transcript_json = transcript_base.with_suffix(".json")
    announce("3/7 提取音频并用 Whisper 转写")
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

    raw_transcript = load_transcript(transcript_json)
    duration = float(media["duration"])
    announce("4/7 DeepSeek 校正 Whisper 错字、人名、剧名和品牌名")
    fact_evidence = collect_fact_check_evidence(source, raw_transcript, visual_text, task_dir)
    transcript = raw_transcript
    polish_report: dict[str, Any] = {"available": False, "reason": "not requested", "corrections": []}
    if api_key and not args.force_fallback and raw_transcript:
        try:
            transcript, polish_report = polish_transcript_with_deepseek(
                api_key,
                source,
                media,
                raw_transcript,
                visual_text,
                fact_evidence,
            )
        except Exception as exc:  # noqa: BLE001 - raw ASR should still keep one-click rendering unblocked.
            polish_report = {"available": False, "reason": f"DeepSeek polish failed: {exc}", "corrections": []}
            print(f"DeepSeek transcript polish failed, using raw Whisper transcript: {exc}")
    elif not api_key:
        polish_report = {"available": False, "reason": "No DeepSeek key found", "corrections": []}
        print("No DeepSeek key found; using raw Whisper transcript.")
    elif args.force_fallback:
        polish_report = {"available": False, "reason": "--force-fallback enabled", "corrections": []}
        print("Skipping DeepSeek transcript polish because --force-fallback is enabled.")
    write_polished_transcript(asr_dir, transcript, polish_report)

    analysis = build_analysis(source, media, transcript, visual_text, polish_report, visual_layout, fact_evidence, source_metadata)
    plan = fallback_plan(source.stem, transcript, duration, source_metadata)
    announce("5/7 根据当前视频重新生成 KC 娱乐包装方案")
    if api_key and not args.force_fallback:
        try:
            plan = ask_deepseek(api_key, analysis)
        except Exception as exc:  # noqa: BLE001 - fallback should keep one-click rendering unblocked.
            print(f"DeepSeek plan failed, using fallback captions: {exc}")
    elif not api_key:
        print("No DeepSeek key found; using fallback captions.")
    plan = normalize_plan(plan, source.stem, transcript, duration, source_metadata)

    plan_path = task_dir / "caption_plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    output_name = safe_filename(str(plan.get("output_name") or ""))
    if not output_name:
        output_name = f"KC娱乐_{safe_filename(source.stem)}"
    if not output_name.startswith("KC娱乐_"):
        output_name = f"KC娱乐_{output_name}"
    output = unique_output_path(args.output_dir / f"{output_name}.mp4", force=args.force)

    announce("6/7 套 KC 娱乐版式、裁掉原始顶部/底部文字并做画面去重包装")
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
        "--cleanup-band",
        str(visual_layout.get("cleanup_band", "none")),
    ]
    if int(media["height"]) > int(media["width"]):
        top_crop, bottom_crop = vertical_edge_crops(int(media["height"]), visual_layout)
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
    announce("7/7 抽取成片关键帧，保存本次质检留痕")
    extract_keyframes(output, frames_dir / "final", float(media["duration"]))
    manifest[source_hash] = {
        "source": str(source),
        "output": str(output),
        "caption_plan": str(plan_path),
        "visual_text": str(task_dir / "visual_text.txt"),
        "transcript_polished": str(asr_dir / "transcript_polished.json"),
        "visual_layout": visual_layout,
        "source_metadata": source_metadata,
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
- If ASR is empty, music-only, or mostly non-speech, use source_metadata.title/desc and visual_text as the main content basis; still cover the full clip with meaningful subtitles instead of leaving later subtitle slots blank.
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


def polish_transcript_with_deepseek(
    api_key: str,
    source: Path,
    media: dict[str, float],
    transcript: list[dict[str, Any]],
    visual_text: dict[str, Any],
    fact_evidence: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a meticulous Chinese entertainment ASR proofreader. "
                    "Correct Whisper homophones and typos before the video is packaged. "
                    "Pay special attention to celebrity names, film/TV/drama titles, variety show names, "
                    "AI product names, brands, character names, and common Chinese entertainment terms. "
                    "Use filename, OCR/visual text, and external search evidence as evidence. "
                    "When search evidence conflicts with a homophone guess, prefer the externally supported entity. "
                    "Never invent unsupported facts. "
                    "Preserve the original meaning and timing. Return strict JSON only."
                ),
            },
            {
                "role": "user",
                "content": f"""
Polish this Whisper transcript for a KC娱乐 short video.

Source metadata:
{json.dumps({
    "filename": source.name,
    "stem": source.stem,
    "duration": round(float(media["duration"]), 3),
    "width": int(media["width"]),
    "height": int(media["height"]),
    "visual_text": visual_text,
    "fact_check_evidence": fact_evidence,
}, ensure_ascii=False, indent=2)}

Raw Whisper transcript:
{json.dumps(transcript, ensure_ascii=False, indent=2)}

Rules:
- Keep the same number of transcript segments whenever possible.
- Preserve start/end times. If you return times, they must stay close to the original.
- Correct obvious homophones and wrong words using context.
- Check names of people, dramas, films, shows, brands and AI tools. Examples: 即梦, 即梦片场, 王一博, 肖战, 得宝/Tempo.
- Use fact_check_evidence to resolve entity names. For example, if search snippets support 于正 + 即梦 + AI剧, correct homophones such as 愚症/余胜/于胜 to 于正.
- Use externally supported drama/show titles when evidence is strong, e.g. 《紫禁攻略之颠倒梦想》.
- If uncertain, keep the original wording instead of guessing.
- Do not summarize; produce corrected spoken/on-screen subtitle text suitable for later packaging.

Return JSON only with exactly these top-level keys:
corrected_transcript, corrections.

corrected_transcript: list of objects with start, end, text.
corrections: list of objects with from, to, reason.
""",
            },
        ],
        "temperature": 0.1,
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
    report = parse_json_object(content)
    corrected = normalize_polished_transcript(report, transcript)
    corrections = report.get("corrections", [])
    if not isinstance(corrections, list):
        corrections = []
    polish_report = {
        "available": True,
        "engine": "deepseek-chat",
        "corrections": corrections[:50],
        "fact_check_evidence": fact_evidence,
    }
    return corrected, polish_report


def collect_fact_check_evidence(
    source: Path,
    transcript: list[dict[str, Any]],
    visual_text: dict[str, Any],
    task_dir: Path,
) -> dict[str, Any]:
    queries = fact_check_queries(source, transcript, visual_text)
    evidence: dict[str, Any] = {"available": False, "queries": queries, "items": [], "errors": []}
    for query in queries:
        try:
            items = search_sogou_via_jina(query)
        except Exception as exc:  # noqa: BLE001 - fact-check search should never block rendering.
            evidence["errors"].append({"query": query, "error": str(exc)})
            continue
        evidence["items"].extend(items)
        if len(evidence["items"]) >= 8:
            break
    evidence["items"] = dedupe_evidence_items(evidence["items"])[:8]
    evidence["available"] = bool(evidence["items"])
    write_fact_check_evidence(task_dir, evidence)
    if evidence["available"]:
        print(f"Fact-check evidence collected: {len(evidence['items'])} item(s).", flush=True)
    else:
        print("Fact-check evidence unavailable; continuing with filename + ASR context.", flush=True)
    return evidence


def fact_check_queries(source: Path, transcript: list[dict[str, Any]], visual_text: dict[str, Any]) -> list[str]:
    text = normalize_space(
        " ".join(
            [
                source.stem,
                " ".join(item.get("text", "") for item in transcript[:14]),
                str(visual_text.get("text", "")),
            ]
        )
    )
    queries: list[str] = []
    source_query = re.sub(r"[_@#]+", " ", source.stem)
    source_query = re.sub(r"\d+[a-f0-9]{2,}$", "", source_query, flags=re.I)
    if source_query:
        queries.append(source_query)
    if any(word in text for word in ["AI剧", "AI聚集", "即梦", "题梦", "激闷", "紫金", "紫禁", "颠倒", "梦想"]):
        queries.extend(
            [
                "AI剧 即梦片场 紫禁攻略 颠倒梦想",
                "即梦片场 AI剧 于正",
                "于正 即梦 AI剧",
            ]
        )
    if any(word in text for word in ["得宝", "Tempo", "王一博", "白象"]):
        queries.append("王一博 Tempo 得宝 代言 同款")
    if any(word in text for word in ["房价", "房地产", "楼市", "触底"]):
        queries.append("中国 房地产 触底 回升 房价")
    result = []
    for query in queries:
        query = normalize_space(query)
        if query and query not in result:
            result.append(query)
    return result[:4]


def search_sogou_via_jina(query: str) -> list[dict[str, str]]:
    url = "https://r.jina.ai/http://r.jina.ai/http://https://www.sogou.com/web?query=" + quote(query)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=25) as resp:
        text = resp.read().decode("utf-8", "ignore")
    return parse_search_markdown(text, query, url)


def parse_search_markdown(text: str, query: str, url: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    blocks = re.split(r"\n### ", text)
    for block in blocks[1:]:
        block = "### " + block
        title_match = re.search(r"### \[(.*?)\]\((.*?)\)", block, flags=re.S)
        if not title_match:
            continue
        title = clean_markdown_text(title_match.group(1))
        link = title_match.group(2).strip()
        snippet_raw = re.split(r"\n### ", block, maxsplit=1)[0]
        snippet_raw = re.sub(r"!\[.*?\]\(.*?\)", " ", snippet_raw, flags=re.S)
        snippet_raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", snippet_raw)
        snippet = clean_markdown_text(snippet_raw)
        if not relevant_evidence_text(f"{title} {snippet}", query):
            continue
        items.append(
            {
                "engine": "sogou_via_jina",
                "query": query,
                "title": title[:160],
                "snippet": snippet[:420],
                "url": link[:260],
            }
        )
        if len(items) >= 4:
            break
    if not items:
        for line in text.splitlines():
            clean = clean_markdown_text(line)
            if len(clean) >= 24 and relevant_evidence_text(clean, query):
                items.append({"engine": "sogou_via_jina", "query": query, "title": "", "snippet": clean[:420], "url": url})
                if len(items) >= 3:
                    break
    return items


def relevant_evidence_text(text: str, query: str) -> bool:
    text = normalize_space(text)
    if not text:
        return False
    query_terms = [term for term in re.split(r"\s+", query) if len(term) >= 2]
    hits = sum(1 for term in query_terms if term in text)
    if hits >= max(1, min(2, len(query_terms))):
        return True
    return any(term in text for term in ["于正", "即梦", "王一博", "肖战", "Tempo", "得宝", "房地产", "房价"])


def clean_markdown_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[_*`]+", "", text)
    return normalize_space(text)


def dedupe_evidence_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    seen = set()
    for item in items:
        key = (item.get("title", ""), item.get("snippet", "")[:80])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def write_fact_check_evidence(task_dir: Path, evidence: dict[str, Any]) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    json_path = task_dir / "fact_check_evidence.json"
    txt_path = task_dir / "fact_check_evidence.txt"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["Fact-check evidence", "", "Queries:"]
    lines.extend(f"- {query}" for query in evidence.get("queries", []))
    lines.extend(["", "Items:"])
    for item in evidence.get("items", []):
        lines.append(f"- [{item.get('engine')}] {item.get('title')}")
        lines.append(f"  {item.get('snippet')}")
        lines.append(f"  {item.get('url')}")
    if evidence.get("errors"):
        lines.extend(["", "Errors:"])
        lines.extend(f"- {err.get('query')}: {err.get('error')}" for err in evidence["errors"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def normalize_polished_transcript(report: dict[str, Any], original: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_items = report.get("corrected_transcript") or report.get("transcript") or []
    if not isinstance(raw_items, list) or not raw_items:
        return original
    result: list[dict[str, Any]] = []
    for idx, original_item in enumerate(original):
        raw = raw_items[idx] if idx < len(raw_items) and isinstance(raw_items[idx], dict) else {}
        text = clean_asr_text(normalize_space(str(raw.get("text", original_item.get("text", "")))))
        if not text:
            text = str(original_item.get("text", ""))
        start = float(original_item["start"])
        end = float(original_item["end"])
        try:
            returned_start = float(raw.get("start", start))
            returned_end = float(raw.get("end", end))
        except (TypeError, ValueError):
            returned_start = start
            returned_end = end
        if abs(returned_start - start) <= 0.35:
            start = returned_start
        if abs(returned_end - end) <= 0.35 and returned_end > start:
            end = returned_end
        result.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return result


def write_polished_transcript(asr_dir: Path, transcript: list[dict[str, Any]], report: dict[str, Any]) -> None:
    json_path = asr_dir / "transcript_polished.json"
    txt_path = asr_dir / "transcript_polished.txt"
    payload = {"report": report, "transcription": transcript}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = []
    for item in transcript:
        lines.append(f"[{format_timestamp(float(item['start']))} --> {format_timestamp(float(item['end']))}] {item['text']}")
    if report.get("corrections"):
        lines.extend(["", "Corrections:"])
        for item in report["corrections"]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('from', '')} -> {item.get('to', '')}: {item.get('reason', '')}")
    elif report.get("reason"):
        lines.extend(["", f"Reason: {report['reason']}"])
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fallback_plan(
    source_stem: str,
    transcript: list[dict[str, Any]],
    duration: float,
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chunks = chunk_transcript(transcript, duration, target_blocks=5)
    metadata_text = metadata_search_text(source_metadata or {})
    source_text = f"{source_stem} {metadata_text} {' '.join(item['text'] for item in transcript)}"
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
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fallback = fallback_plan(source_stem, transcript, duration, source_metadata)
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
    subtitles = ensure_subtitle_coverage(subtitles, duration, plan, fallback, source_metadata or {})

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


def ensure_subtitle_coverage(
    subtitles: list[dict[str, Any]],
    duration: float,
    plan: dict[str, Any],
    fallback: dict[str, Any],
    source_metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    if not subtitles or duration <= 0:
        return subtitles
    subtitles = sorted(subtitles, key=lambda item: float(item.get("start", 0)))
    last = subtitles[-1]
    last_end = float(last.get("end", 0))
    if duration - last_end <= 0.75:
        return subtitles

    filler = subtitle_filler_text(plan, fallback, source_metadata)
    if len(subtitles) == 1 and float(subtitles[0].get("start", 0)) <= 1.0:
        subtitles[0]["end"] = round(duration, 3)
        if filler and len(str(subtitles[0].get("zh", ""))) <= 4:
            subtitles[0]["zh"] = filler
            subtitles[0]["zh_highlights"] = pick_highlights(filler)
        return subtitles

    start = max(0.0, min(duration - 0.75, last_end))
    subtitles.append(
        {
            "index": len(subtitles) + 1,
            "start": round(start, 3),
            "end": round(duration, 3),
            "zh": filler or str(last.get("zh") or fallback["lower_ribbon"]),
            "en": "",
            "zh_highlights": pick_highlights(filler),
            "en_highlights": [],
        }
    )
    return subtitles


def subtitle_filler_text(plan: dict[str, Any], fallback: dict[str, Any], source_metadata: dict[str, Any]) -> str:
    for value in [
        source_metadata.get("title"),
        source_metadata.get("desc"),
        plan.get("packaging_brief"),
        fallback.get("lower_ribbon"),
        " ".join(str(line) for line in fallback.get("title_lines", [])),
    ]:
        text = clean_video_description(str(value or ""))
        if text:
            return text[:32]
    return ""


def build_analysis(
    source: Path,
    media: dict[str, float],
    transcript: list[dict[str, Any]],
    visual_text: dict[str, Any],
    polish_report: dict[str, Any],
    visual_layout: dict[str, Any],
    fact_evidence: dict[str, Any],
    source_metadata: dict[str, Any] | None = None,
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
        "visual_layout": visual_layout,
        "source_metadata": source_metadata or {},
        "fact_check_evidence": fact_evidence,
        "transcript_polish": polish_report,
        "transcript_text": transcript_text,
        "transcript": transcript,
    }


def analyze_visual_layout(frames_dir: Path, media: dict[str, float]) -> dict[str, Any]:
    frames = sorted(frames_dir.glob("source_*.png"))
    orientation = "vertical" if int(media["height"]) > int(media["width"]) else "landscape"
    result: dict[str, Any] = {
        "orientation": orientation,
        "cleanup_band": "none",
        "reason": "",
        "max_mid_seam_score": 0.0,
        "max_upper_lower_repeat_score": 0.0,
        "vertical_crop_profile": "edge_cleanup",
        "vertical_top_crop_ratio": 0.06,
        "vertical_bottom_crop_ratio": 0.39,
        "frame_metrics": [],
        "cleanup_policy": "crop_bottom_first",
    }
    if orientation != "vertical" or not frames:
        result["reason"] = "landscape source or no frames"
        return result

    metrics = []
    for frame in frames:
        try:
            seam_score = mid_horizontal_seam_score(frame)
            repeat_score = upper_lower_repeat_score(frame)
        except OSError:
            continue
        metrics.append(
            {
                "frame": frame.name,
                "mid_seam_score": round(seam_score, 3),
                "upper_lower_repeat_score": round(repeat_score, 3),
            }
        )
    max_score = max((item["mid_seam_score"] for item in metrics), default=0.0)
    max_repeat = max((item["upper_lower_repeat_score"] for item in metrics), default=0.0)
    result["max_mid_seam_score"] = round(max_score, 3)
    result["max_upper_lower_repeat_score"] = round(max_repeat, 3)
    result["frame_metrics"] = metrics

    repeated_split = any(
        item["mid_seam_score"] >= 34.0 and item["upper_lower_repeat_score"] >= 0.82 for item in metrics
    )
    if repeated_split:
        result["vertical_crop_profile"] = "upper_panel"
        result["vertical_bottom_crop_ratio"] = 0.55
        result["reason"] = "detected repeated upper/lower panels with a mid subtitle seam; crop to the clean upper panel"
        return result

    result["reason"] = "standard vertical layout; crop top/bottom edges instead of drawing a visible cleanup band"
    return result


def mid_horizontal_seam_score(path: Path) -> float:
    with Image.open(path) as img:
        gray = img.convert("L").resize((160, 284))
    width, height = gray.size
    pixels = gray.load()
    y0 = int(height * 0.42)
    y1 = int(height * 0.66)
    best = 0.0
    for y in range(max(1, y0), min(height - 1, y1)):
        total = 0
        for x in range(width):
            total += abs(int(pixels[x, y]) - int(pixels[x, y - 1]))
        score = total / width
        if score > best:
            best = score
    return best


def upper_lower_repeat_score(path: Path) -> float:
    with Image.open(path) as img:
        gray = img.convert("L")
    width, height = gray.size
    if width < 4 or height < 12:
        return 0.0

    seam_margin = max(2, int(height * 0.035))
    top_y0 = int(height * 0.06)
    top_y1 = max(top_y0 + 2, height // 2 - seam_margin)
    bottom_y0 = min(height - 2, height // 2 + seam_margin)
    bottom_y1 = max(bottom_y0 + 2, int(height * 0.94))
    top = gray.crop((0, top_y0, width, top_y1)).resize((96, 96))
    bottom = gray.crop((0, bottom_y0, width, bottom_y1)).resize((96, 96))
    diff = ImageChops.difference(top, bottom)
    mean_diff = ImageStat.Stat(diff).mean[0]
    return max(0.0, min(1.0, 1.0 - (mean_diff / 255.0)))


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
    if re.fullmatch(r"[\[\(（【]?\s*(music|音乐|bgm|applause|鼓掌|silence|静音)\s*[\]\)）】]?", text, flags=re.I):
        return ""
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


def vertical_edge_crops(height: int, visual_layout: dict[str, Any] | None = None) -> tuple[int, int]:
    if visual_layout and visual_layout.get("cleanup_band") not in {"", "none", None}:
        return even_int(height * 0.15), even_int(height * 0.20)
    top_ratio = 0.06
    bottom_ratio = 0.39
    if visual_layout:
        top_ratio = float(visual_layout.get("vertical_top_crop_ratio", top_ratio))
        bottom_ratio = float(visual_layout.get("vertical_bottom_crop_ratio", bottom_ratio))
    return even_int(height * top_ratio), even_int(height * bottom_ratio)


def even_int(value: float) -> int:
    result = max(0, int(round(value)))
    return result - (result % 2)


def load_source_metadata(metadata_file: Path | None, input_dir: Path) -> dict[str, dict[str, Any]]:
    candidates = []
    if metadata_file:
        candidates.append(metadata_file)
    candidates.extend(
        [
            input_dir / "source_metadata.json",
            input_dir / "selected.json",
            input_dir.parent / "selected.json",
            input_dir.parent / "reports" / "selected.json",
        ]
    )
    for path in candidates:
        if path and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            return index_source_metadata(data)
    return {}


def index_source_metadata(data: Any) -> dict[str, dict[str, Any]]:
    if isinstance(data, dict):
        if "items" in data:
            data = data.get("items")
        elif "selected" in data:
            data = data.get("selected")
        else:
            return {str(key): value for key, value in data.items() if isinstance(value, dict)}
    if not isinstance(data, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        keys = [
            str(item.get("aweme_id") or ""),
            str(item.get("id") or ""),
            str(item.get("file") or ""),
            str(item.get("filename") or ""),
            str(item.get("name") or ""),
        ]
        for key in keys:
            key = key.strip()
            if key:
                result[key] = item
    return result


def metadata_for_source(source: Path, metadata_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    for key in [source.name, source.stem]:
        if key in metadata_index:
            return metadata_index[key]
    for aweme_id in re.findall(r"\d{15,}", source.name):
        if aweme_id in metadata_index:
            return metadata_index[aweme_id]
    return {}


def metadata_search_text(source_metadata: dict[str, Any]) -> str:
    return normalize_space(
        " ".join(
            clean_video_description(str(source_metadata.get(key) or ""))
            for key in ("title", "desc", "caption", "author", "source_keyword")
        )
    )


def clean_video_description(text: str) -> str:
    text = html.unescape(normalize_space(text))
    text = re.sub(r"https?://\\S+", " ", text)
    text = re.sub(r"#([^#\\s]+)", r" \1 ", text)
    text = re.sub(r"\\s+", " ", text).strip(" ,，。")
    return text


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


def format_timestamp(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


if __name__ == "__main__":
    main()
