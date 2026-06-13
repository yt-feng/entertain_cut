#!/usr/bin/env python3
"""Create a short-video subtitle/highlight plan for the entertainment clip."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


DEFAULT_TITLE_LINES = ["跟着浪姐学英语", "乘风2026终于等到曾沛慈"]


FALLBACK_PLAN: dict[str, Any] = {
    "title_lines": DEFAULT_TITLE_LINES,
    "title_highlights": ["英语", "曾沛慈"],
    "top_badge": "开口跪现场",
    "lower_ribbon": "英语跟唱 · 曾沛慈现场",
    "subtitles": [
        {
            "start": 0.0,
            "end": 15.5,
            "en": "Guitar intro",
            "zh": "前奏一响，氛围来了",
            "en_highlights": ["Guitar"],
            "zh_highlights": ["前奏"],
        },
        {
            "start": 15.5,
            "end": 19.5,
            "en": "When I find myself in times of trouble",
            "zh": "当我陷入困境的时候",
            "en_highlights": ["trouble"],
            "zh_highlights": ["困境"],
        },
        {
            "start": 19.5,
            "end": 23.0,
            "en": "Mother Mary comes to me",
            "zh": "玛丽来到我身边",
            "en_highlights": ["Mother Mary"],
            "zh_highlights": ["玛丽"],
        },
        {
            "start": 23.0,
            "end": 30.0,
            "en": "Speaking words of wisdom, let it be",
            "zh": "说出智慧之言：顺其自然",
            "en_highlights": ["wisdom", "let it be"],
            "zh_highlights": ["智慧", "顺其自然"],
        },
        {
            "start": 30.0,
            "end": 37.0,
            "en": "In my hour of darkness, she stands before me",
            "zh": "黑暗时刻，她就站在面前",
            "en_highlights": ["darkness"],
            "zh_highlights": ["黑暗时刻"],
        },
        {
            "start": 37.0,
            "end": 44.0,
            "en": "Speaking words of wisdom, let it be",
            "zh": "一句 Let it be，稳稳落下",
            "en_highlights": ["wisdom", "let it be"],
            "zh_highlights": ["Let it be"],
        },
        {
            "start": 44.0,
            "end": 57.0,
            "en": "Let it be",
            "zh": "就让它去吧",
            "en_highlights": ["Let it be"],
            "zh_highlights": ["让它去"],
        },
        {
            "start": 57.0,
            "end": 61.3,
            "en": "Outro",
            "zh": "这段英文歌太稳了",
            "en_highlights": [],
            "zh_highlights": ["太稳"],
        },
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("原视频.mp4"))
    parser.add_argument("--transcript", type=Path, default=Path("work/asr/transcript.json"))
    parser.add_argument("--out", type=Path, default=Path("work/caption_plan.json"))
    parser.add_argument("--title-line1", default=DEFAULT_TITLE_LINES[0])
    parser.add_argument("--title-line2", default=DEFAULT_TITLE_LINES[1])
    parser.add_argument("--top-badge", default="开口跪现场")
    parser.add_argument("--lower-ribbon", default="")
    parser.add_argument(
        "--context-note",
        default=(
            "It is Peggy Tseng / 曾沛慈 singing an English song with guitar accompaniment. "
            "The video should feel like a Chinese entertainment account teaching English."
        ),
    )
    parser.add_argument("--force-fallback", action="store_true")
    args = parser.parse_args()

    transcript = load_transcript(args.transcript)
    duration = probe_duration(args.source)
    plan = {
        **FALLBACK_PLAN,
        "title_lines": [args.title_line1, args.title_line2],
        "top_badge": args.top_badge,
        "lower_ribbon": args.lower_ribbon or FALLBACK_PLAN["lower_ribbon"],
    }

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if api_key and not args.force_fallback:
        try:
            plan = ask_deepseek(
                api_key,
                transcript,
                title_lines=[args.title_line1, args.title_line2],
                top_badge=args.top_badge,
                lower_ribbon=args.lower_ribbon or FALLBACK_PLAN["lower_ribbon"],
                context_note=args.context_note,
                duration=duration,
            )
        except Exception as exc:  # noqa: BLE001 - local fallback keeps rendering unblocked.
            print(f"DeepSeek plan failed, using fallback: {exc}", flush=True)

    plan = normalize_plan(plan, duration=duration)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}", flush=True)


def load_transcript(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments: list[dict[str, Any]] = []
    for item in data.get("transcription", []):
        offsets = item.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", 0)) / 1000.0
        text = normalize_space(str(item.get("text", "")))
        if text and end > start:
            segments.append({"start": start, "end": end, "text": text})
    return segments


def ask_deepseek(
    api_key: str,
    transcript: list[dict[str, Any]],
    *,
    title_lines: list[str],
    top_badge: str,
    lower_ribbon: str,
    context_note: str,
    duration: float,
) -> dict[str, Any]:
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a bilingual short-video subtitle editor for Chinese entertainment accounts. "
                    "Fix obvious ASR mistakes, make concise Chinese subtitles, and select short highlight phrases. "
                    "Return strict JSON only."
                ),
            },
            {
                "role": "user",
                "content": f"""
Create a 9:16 entertainment short-video caption plan for this {duration:.2f}-second English singing clip.

Visual/context notes:
- {context_note}
- Fixed title line 1: {title_lines[0]}
- Fixed title line 2: {title_lines[1]}
- Fixed top_badge: {top_badge}
- Preferred lower_ribbon: {lower_ribbon}
- Keep the English subtitle concise and natural.
- Chinese subtitle style should be punchy, useful for "learning English from Sisters Who Make Waves", but not vulgar.
- Do not invent events beyond the audio/context.
- Keep subtitles as 6 to 10 timed blocks.
- Return title_lines as exactly 2 strings.
- Return top_badge as a short Chinese label.
- Return lower_ribbon as a short Chinese label for the bottom packaging strip.
- For each subtitle, return start, end, en, zh, en_highlights, zh_highlights.
- Highlight phrases must appear verbatim in the corresponding text.
- Use 0-2 highlights per language.

Return JSON only in this shape:
{{
  "title_lines": ["跟着浪姐学英语", "乘风2026终于等到曾沛慈"],
  "title_highlights": ["英语", "曾沛慈"],
  "top_badge": "开口跪现场",
  "lower_ribbon": "英语跟唱 · 曾沛慈现场",
  "subtitles": [
    {{
      "start": 0.0,
      "end": 15.5,
      "en": "Guitar intro",
      "zh": "前奏一响，氛围来了",
      "en_highlights": ["Guitar"],
      "zh_highlights": ["前奏"]
    }}
  ]
}}

ASR transcript:
{json.dumps(transcript, ensure_ascii=False, indent=2)}
""",
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = Request(
        DEEPSEEK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urlopen(req, timeout=120) as resp:
        response = json.loads(resp.read().decode("utf-8"))
    content = response["choices"][0]["message"]["content"]
    return parse_json_object(content)


def normalize_plan(plan: dict[str, Any], duration: float) -> dict[str, Any]:
    title_lines = plan.get("title_lines")
    if not isinstance(title_lines, list) or len(title_lines) != 2:
        title_lines = list(FALLBACK_PLAN["title_lines"])
    title_lines = [normalize_space(str(line)) for line in title_lines[:2]]
    if not all(title_lines):
        title_lines = list(FALLBACK_PLAN["title_lines"])

    top_badge = normalize_space(str(plan.get("top_badge", FALLBACK_PLAN["top_badge"])))
    if not top_badge:
        top_badge = FALLBACK_PLAN["top_badge"]
    lower_ribbon = normalize_space(str(plan.get("lower_ribbon", FALLBACK_PLAN["lower_ribbon"])))
    if not lower_ribbon:
        lower_ribbon = FALLBACK_PLAN["lower_ribbon"]
    title_highlights = valid_phrases(
        " ".join(title_lines),
        plan.get("title_highlights", FALLBACK_PLAN["title_highlights"]),
        case_insensitive=True,
    )

    subtitles: list[dict[str, Any]] = []
    for idx, item in enumerate(plan.get("subtitles", []), start=1):
        try:
            start = max(0.0, float(item["start"]))
            end = min(duration, float(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        en = normalize_space(str(item.get("en", "")))
        zh = normalize_space(str(item.get("zh", "")))
        if not en and not zh:
            continue
        subtitles.append(
            {
                "index": idx,
                "start": round(start, 3),
                "end": round(end, 3),
                "en": en,
                "zh": zh,
                "en_highlights": valid_phrases(en, item.get("en_highlights", []), case_insensitive=True),
                "zh_highlights": valid_phrases(zh, item.get("zh_highlights", [])),
            }
        )

    if not subtitles:
        subtitles = [
            {**sub, "index": idx}
            for idx, sub in enumerate(FALLBACK_PLAN["subtitles"], start=1)
        ]

    return {
        "title_lines": title_lines,
        "title_highlights": title_highlights,
        "top_badge": top_badge,
        "lower_ribbon": lower_ribbon,
        "subtitles": subtitles,
    }


def probe_duration(path: Path) -> float:
    import subprocess

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return float(result.stdout.strip())


def valid_phrases(text: str, phrases: Any, *, case_insensitive: bool = False) -> list[str]:
    if not isinstance(phrases, list):
        return []
    haystack = text.lower() if case_insensitive else text
    result: list[str] = []
    for raw in phrases:
        phrase = normalize_space(str(raw))
        if not phrase or phrase in result:
            continue
        needle = phrase.lower() if case_insensitive else phrase
        if needle in haystack:
            result.append(phrase)
    return result[:2]


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


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


if __name__ == "__main__":
    main()
