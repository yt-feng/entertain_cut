#!/usr/bin/env python3
"""Render the source clip as a vertical entertainment short."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


OUT_W = 1080
OUT_H = 1920
MAIN_Y = 360
MAIN_H = 796
CAPTION_Y = 1376
TITLE_LINE1_Y = 188
TITLE_LINE2_Y = 276
ZH_FONT_CANDIDATES = [
    os.environ.get("KC_ZH_FONT", ""),
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]
EN_BOLD_FONT_CANDIDATES = [
    os.environ.get("KC_EN_BOLD_FONT", ""),
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]
EN_REG_FONT_CANDIDATES = [
    os.environ.get("KC_EN_FONT", ""),
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("原视频.mp4"))
    parser.add_argument("--plan", type=Path, default=Path("work/caption_plan.json"))
    parser.add_argument("--out", type=Path, default=Path("KC娱乐_竖屏娱乐营销号.mp4"))
    parser.add_argument("--work-dir", type=Path, default=Path("work/render"))
    parser.add_argument("--encoder", choices=["auto", "videotoolbox", "libx264"], default="auto")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--duration", type=float, default=None, help="Optional output duration in seconds.")
    parser.add_argument(
        "--vertical-bottom-crop",
        type=int,
        default=0,
        help="Pixels to crop from the bottom of vertical sources before fitting the main layer.",
    )
    parser.add_argument(
        "--vertical-top-crop",
        type=int,
        default=0,
        help="Pixels to crop from the top of vertical sources before fitting the main layer.",
    )
    parser.add_argument(
        "--preserve-vertical-source",
        action="store_true",
        help="Fit full vertical sources into the main layer without cropping top/bottom.",
    )
    parser.add_argument(
        "--landscape-crop",
        default="",
        help="Optional landscape crop as width:height:x:y, used before fitting the main layer.",
    )
    parser.add_argument(
        "--cleanup-band",
        choices=["none", "soft", "solid"],
        default="none",
        help="Optional mid-frame source subtitle cleanup band. Default is none.",
    )
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"Source not found: {args.source}")
    if not args.plan.exists():
        raise SystemExit(f"Caption plan not found: {args.plan}")
    landscape_crop = parse_crop_arg(args.landscape_crop) if args.landscape_crop else None

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    media = probe_media(args.source)
    duration = media["duration"]
    if args.duration is not None:
        if args.duration <= 0:
            raise SystemExit("--duration must be greater than zero")
        duration = min(duration, args.duration)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    static_png = args.work_dir / "static_overlay.png"
    subtitle_pngs = render_overlays(plan, args.work_dir, static_png, cleanup_band=args.cleanup_band)

    command = build_ffmpeg_command(
        source=args.source,
        static_png=static_png,
        subtitle_pngs=subtitle_pngs,
        output=args.out,
        duration=duration,
        source_width=int(media["width"]),
        source_height=int(media["height"]),
        encoder=args.encoder,
        threads=max(1, args.threads),
        vertical_top_crop=max(0, args.vertical_top_crop),
        vertical_bottom_crop=max(0, args.vertical_bottom_crop),
        preserve_vertical_source=args.preserve_vertical_source,
        landscape_crop=landscape_crop,
    )

    if args.encoder == "auto":
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError:
            print("VideoToolbox failed; retrying with libx264.", flush=True)
            command = build_ffmpeg_command(
                source=args.source,
                static_png=static_png,
                subtitle_pngs=subtitle_pngs,
                output=args.out,
                duration=duration,
                source_width=int(media["width"]),
                source_height=int(media["height"]),
                encoder="libx264",
                threads=max(1, args.threads),
                vertical_top_crop=max(0, args.vertical_top_crop),
                vertical_bottom_crop=max(0, args.vertical_bottom_crop),
                preserve_vertical_source=args.preserve_vertical_source,
                landscape_crop=landscape_crop,
            )
            subprocess.run(command, check=True)
    else:
        subprocess.run(command, check=True)

    print(f"Wrote {args.out}", flush=True)


def render_overlays(
    plan: dict[str, Any],
    work_dir: Path,
    static_png: Path,
    *,
    cleanup_band: str,
) -> list[tuple[Path, float, float]]:
    render_static_overlay(plan, static_png, cleanup_band=cleanup_band)
    result: list[tuple[Path, float, float]] = []
    for item in plan.get("subtitles", []):
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        output = work_dir / f"subtitle_{int(item.get('index', len(result) + 1)):03d}.png"
        render_subtitle_overlay(item, output)
        result.append((output, start, end))
    return result


def render_static_overlay(plan: dict[str, Any], output: Path, *, cleanup_band: str = "none") -> None:
    img = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    draw_gradient(draw, 0, 0, OUT_W, 360, (6, 10, 24, 232), (18, 11, 36, 130))
    draw_gradient(draw, 0, 1268, OUT_W, OUT_H, (6, 7, 14, 0), (5, 5, 12, 238))
    draw.rectangle((0, 1548, OUT_W, OUT_H), fill=(5, 5, 12, 218))

    draw.rectangle((0, MAIN_Y - 10, 22, MAIN_Y + MAIN_H + 16), fill=(255, 41, 121, 235))
    draw.rectangle((OUT_W - 22, MAIN_Y + 46, OUT_W, MAIN_Y + MAIN_H + 40), fill=(0, 220, 255, 220))
    draw.rectangle((22, MAIN_Y - 10, 240, MAIN_Y + 2), fill=(255, 214, 47, 240))
    draw.rectangle((812, MAIN_Y + MAIN_H + 28, OUT_W - 22, MAIN_Y + MAIN_H + 40), fill=(255, 214, 47, 230))
    draw_cleanup_band(draw, cleanup_band)

    draw_tag(draw, 46, 84, str(plan.get("top_badge", "气场名场面")), fill=(255, 42, 120, 242))
    draw_tag(draw, 744, 330, str(plan.get("side_badge", "表达太稳")), fill=(0, 205, 255, 224), text_fill=(8, 10, 18, 255), small=True)
    draw_tag(draw, 60, 1222, str(plan.get("caption_badge", "重点来了")), fill=(255, 214, 47, 236), text_fill=(13, 12, 18, 255), small=True)

    draw_sticker(draw, 812, 54, str(plan.get("sticker_top", "HOT")), str(plan.get("sticker_bottom", "气场")))
    draw_burst(draw, 930, 1210)

    title_lines = plan.get("title_lines", ["朱珠不卑不亢", "强大气场"])
    title_lines = [str(line).strip() for line in title_lines[:2]]
    title_highlights = [str(value) for value in plan.get("title_highlights", [])]
    draw_center_highlight_line(
        draw,
        title_lines[0] if title_lines else "",
        y=TITLE_LINE1_Y,
        max_width=760,
        font_size=76,
        min_size=56,
        highlights=title_highlights,
        fill=(255, 255, 255, 255),
        stroke=5,
    )
    if len(title_lines) > 1:
        draw_center_highlight_line(
            draw,
            title_lines[1],
            y=TITLE_LINE2_Y,
            max_width=760,
            font_size=58,
            min_size=42,
            highlights=title_highlights,
            fill=(255, 255, 255, 255),
            highlight_fill=(255, 220, 46, 255),
            stroke=5,
        )

    draw.rounded_rectangle((286, 1570, 794, 1630), radius=24, fill=(255, 42, 120, 226))
    draw_center_highlight_line(
        draw,
        str(plan.get("lower_ribbon", "英语跟唱 · 曾沛慈现场")),
        y=1579,
        max_width=470,
        font_size=34,
        min_size=28,
        highlights=["名场面"],
        fill=(255, 255, 255, 255),
        highlight_fill=(255, 226, 60, 255),
        stroke=2,
    )
    draw_center_highlight_line(
        draw,
        "喜欢记得点关注",
        y=1674,
        max_width=860,
        font_size=58,
        min_size=46,
        highlights=["关注"],
        fill=(255, 255, 255, 255),
        highlight_fill=(255, 221, 42, 255),
        stroke=5,
    )

    draw_brand(draw)
    source_credit = str(plan.get("source_credit", "")).strip()
    if source_credit:
        draw_source_credit(draw, source_credit)
    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(output)


def render_subtitle_overlay(item: dict[str, Any], output: Path) -> None:
    img = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    zh = str(item.get("zh", "")).strip()
    en = str(item.get("en", "")).strip()
    zh_highlights = [str(value) for value in item.get("zh_highlights", [])]
    en_highlights = [str(value) for value in item.get("en_highlights", [])]

    draw_center_highlight_text(
        draw,
        zh,
        y=CAPTION_Y,
        max_width=960,
        max_height=112,
        font_size=68,
        min_size=42,
        highlights=zh_highlights,
        fill=(255, 255, 255, 255),
        highlight_fill=(255, 221, 42, 255),
        stroke=6,
    )
    draw_center_highlight_text(
        draw,
        en,
        y=CAPTION_Y + 92,
        max_width=940,
        max_height=88,
        font_size=36,
        min_size=24,
        highlights=en_highlights,
        fill=(224, 244, 255, 242),
        highlight_fill=(255, 221, 42, 255),
        stroke=3,
        english=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(output)


def draw_cleanup_band(draw: ImageDraw.ImageDraw, mode: str) -> None:
    if mode == "none":
        return
    alpha = 236 if mode == "soft" else 255
    x0, y0, x1, y1 = 82, 1030, 998, 1144
    shadow = (0, 0, 0, 96 if mode == "soft" else 130)
    fill = (5, 7, 16, alpha)
    draw.rounded_rectangle((x0 + 10, y0 + 12, x1 + 10, y1 + 12), radius=22, fill=shadow)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=22, fill=fill)
    draw.rectangle((x0 + 28, y0, x0 + 222, y0 + 10), fill=(255, 214, 47, 232))
    draw.rectangle((x1 - 222, y1 - 10, x1 - 28, y1), fill=(0, 220, 255, 220))


def draw_gradient(
    draw: ImageDraw.ImageDraw,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    start: tuple[int, int, int, int],
    end: tuple[int, int, int, int],
) -> None:
    height = max(1, y1 - y0)
    for y in range(y0, y1):
        t = (y - y0) / height
        rgba = tuple(round(start[i] * (1 - t) + end[i] * t) for i in range(4))
        draw.line((x0, y, x1, y), fill=rgba)


def draw_tag(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    *,
    fill: tuple[int, int, int, int],
    text_fill: tuple[int, int, int, int] = (255, 255, 255, 255),
    small: bool = False,
) -> None:
    pad_x = 22 if small else 26
    pad_y = 12 if small else 14
    box_max_width = max(80, OUT_W - x - 46)
    content_max_width = max(28, box_max_width - pad_x * 2)
    text = fit_single_line(
        draw,
        normalize_space(text),
        max_width=content_max_width,
        start_size=32 if small else 36,
        min_size=22,
    )
    font = load_font(32 if small else 36, bold=True)
    while measure_text(draw, text, font, stroke_width=0)[0] > content_max_width and font.size > 22:
        font = load_font(font.size - 2, bold=True)
    if measure_text(draw, text, font, stroke_width=0)[0] > content_max_width:
        text = fit_single_line(draw, text, max_width=content_max_width, start_size=font.size, min_size=font.size)
    box = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    w = box[2] - box[0] + pad_x * 2
    h = box[3] - box[1] + pad_y * 2
    draw.rounded_rectangle((x, y, x + w, y + h), radius=18, fill=fill)
    draw.text((x + pad_x, y + pad_y - 3), text, font=font, fill=text_fill)


def fit_single_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    start_size: int,
    min_size: int,
) -> str:
    text = normalize_space(text)
    font = load_font(start_size, bold=True)
    if measure_text(draw, text, font, stroke_width=0)[0] <= max_width:
        return text
    font = load_font(min_size, bold=True)
    if measure_text(draw, text, font, stroke_width=0)[0] <= max_width:
        return text
    suffix = "..."
    while text and measure_text(draw, text + suffix, font, stroke_width=0)[0] > max_width:
        text = text[:-1]
    return (text + suffix) if text else suffix


def draw_sticker(draw: ImageDraw.ImageDraw, x: int, y: int, top: str, bottom: str) -> None:
    top = re.sub(r"[^A-Za-z0-9]", "", top).upper()[:6] or "HOT"
    bottom = normalize_space(bottom)[:5] or "爆点"
    draw.rounded_rectangle((x + 10, y + 10, x + 186, y + 132), radius=26, fill=(0, 0, 0, 115))
    draw.rounded_rectangle((x, y, x + 176, y + 122), radius=26, fill=(255, 214, 47, 246))
    draw.rectangle((x, y + 70, x + 176, y + 122), fill=(255, 42, 120, 246))
    top_font = load_en_font(50, bold=True)
    while measure_text(draw, top, top_font, stroke_width=2)[0] > 138 and top_font.size > 34:
        top_font = load_en_font(top_font.size - 2, bold=True)
    top_width = measure_text(draw, top, top_font, stroke_width=2)[0]
    draw.text((x + (176 - top_width) / 2, y + 16), top, font=top_font, fill=(14, 10, 20, 255), stroke_width=2, stroke_fill=(255, 255, 255, 220))

    bottom_font = load_font(34, bold=True)
    while measure_text(draw, bottom, bottom_font, stroke_width=2)[0] > 142 and bottom_font.size > 24:
        bottom_font = load_font(bottom_font.size - 2, bold=True)
    bottom_width = measure_text(draw, bottom, bottom_font, stroke_width=2)[0]
    draw.text((x + (176 - bottom_width) / 2, y + 76), bottom, font=bottom_font, fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(80, 0, 35, 220))


def draw_burst(draw: ImageDraw.ImageDraw, cx: int, cy: int) -> None:
    points: list[tuple[float, float]] = []
    for idx in range(18):
        radius = 74 if idx % 2 == 0 else 42
        angle = -math.pi / 2 + idx * math.pi / 9
        points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
    draw.polygon(points, fill=(255, 214, 47, 222), outline=(255, 42, 120, 255))
    draw.text((cx - 46, cy - 24), "WOW", font=load_en_font(28, bold=True), fill=(16, 12, 24, 255))


def draw_brand(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle((0, 1764, OUT_W, OUT_H), fill=(4, 5, 12, 236))
    draw.rectangle((236, 1778, 318, 1788), fill=(255, 42, 120, 250))
    draw.rectangle((762, 1778, 844, 1788), fill=(0, 220, 255, 238))
    draw_center_highlight_line(
        draw,
        "KC娱乐",
        y=1788,
        max_width=560,
        font_size=64,
        min_size=54,
        highlights=["KC"],
        fill=(255, 255, 255, 255),
        highlight_fill=(255, 218, 45, 255),
        stroke=4,
    )
    draw_center_highlight_line(
        draw,
        "ENTERTAINMENT",
        y=1874,
        max_width=520,
        font_size=22,
        min_size=18,
        highlights=[],
        fill=(0, 220, 255, 210),
        stroke=1,
        english=True,
    )


def draw_source_credit(draw: ImageDraw.ImageDraw, text: str) -> None:
    font = load_font(18, bold=False)
    box = draw.textbbox((0, 0), text, font=font, stroke_width=1)
    x = 18
    y = OUT_H - (box[3] - box[1]) - 10
    draw.text(
        (x, y),
        text,
        font=font,
        fill=(255, 255, 255, 92),
        stroke_width=1,
        stroke_fill=(0, 0, 0, 110),
    )


def draw_center_highlight_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    y: int,
    max_width: int,
    font_size: int,
    min_size: int,
    highlights: list[str],
    fill: tuple[int, int, int, int],
    highlight_fill: tuple[int, int, int, int] = (255, 221, 42, 255),
    stroke: int = 4,
    english: bool = False,
) -> None:
    text = normalize_space(text)
    if not text:
        return

    size = font_size
    while size >= min_size:
        font = load_en_font(size, bold=True) if english else load_font(size, bold=True)
        runs = split_highlight_runs(text, highlights)
        width = sum(measure_text(draw, run, font, stroke_width=stroke)[0] for run, _ in runs)
        if width <= max_width:
            break
        size -= 2

    font = load_en_font(max(size, min_size), bold=True) if english else load_font(max(size, min_size), bold=True)
    runs = split_highlight_runs(text, highlights)
    widths = [measure_text(draw, run, font, stroke_width=stroke)[0] for run, _ in runs]
    x = (OUT_W - sum(widths)) / 2
    for (run, is_highlight), width in zip(runs, widths):
        if not run:
            continue
        draw.text(
            (x, y),
            run,
            font=font,
            fill=highlight_fill if is_highlight else fill,
            stroke_width=stroke,
            stroke_fill=(0, 0, 0, 230),
        )
        x += width


def draw_center_highlight_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    y: int,
    max_width: int,
    max_height: int,
    font_size: int,
    min_size: int,
    highlights: list[str],
    fill: tuple[int, int, int, int],
    highlight_fill: tuple[int, int, int, int] = (255, 221, 42, 255),
    stroke: int = 4,
    english: bool = False,
) -> None:
    text = normalize_space(text)
    if not text:
        return

    size = font_size
    while size >= min_size:
        font = load_en_font(size, bold=True) if english else load_font(size, bold=True)
        lines = wrap_text(draw, text, font, max_width, stroke)
        line_height = max(measure_text(draw, line, font, stroke_width=stroke)[1] for line in lines) + int(size * 0.18)
        total_height = line_height * len(lines)
        if total_height <= max_height:
            break
        size -= 2

    font = load_en_font(max(size, min_size), bold=True) if english else load_font(max(size, min_size), bold=True)
    lines = wrap_text(draw, text, font, max_width, stroke)
    line_height = max(measure_text(draw, line, font, stroke_width=stroke)[1] for line in lines) + int(max(size, min_size) * 0.18)
    total_height = line_height * len(lines)
    line_y = y + max(0, (max_height - total_height) // 2)
    for line in lines:
        draw_center_highlight_line(
            draw,
            line,
            y=line_y,
            max_width=max_width,
            font_size=max(size, min_size),
            min_size=min_size,
            highlights=highlights,
            fill=fill,
            highlight_fill=highlight_fill,
            stroke=stroke,
            english=english,
        )
        line_y += line_height


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    stroke: int,
) -> list[str]:
    if measure_text(draw, text, font, stroke_width=stroke)[0] <= max_width:
        return [text]

    if " " in text:
        words = text.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if measure_text(draw, candidate, font, stroke_width=stroke)[0] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines[:2] if len(lines) > 2 else lines

    lines = []
    current = ""
    for char in text:
        candidate = current + char
        if measure_text(draw, candidate, font, stroke_width=stroke)[0] <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines[:2] if len(lines) > 2 else lines


def split_highlight_runs(text: str, highlights: list[str]) -> list[tuple[str, bool]]:
    ranges: list[tuple[int, int]] = []
    lower = text.lower()
    for raw in highlights:
        phrase = normalize_space(raw)
        if not phrase:
            continue
        start = 0
        needle = phrase.lower()
        while True:
            found = lower.find(needle, start)
            if found < 0:
                break
            ranges.append((found, found + len(phrase)))
            start = found + len(phrase)
    if not ranges:
        return [(text, False)]

    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    runs: list[tuple[str, bool]] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            runs.append((text[cursor:start], False))
        runs.append((text[start:end], True))
        cursor = end
    if cursor < len(text):
        runs.append((text[cursor:], False))
    return runs


def measure_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    *,
    stroke_width: int,
) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    width = math.ceil(draw.textlength(text, font=font))
    return width, box[3] - box[1]


def load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(resolve_font("zh", bold=bold), size)


def load_en_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(resolve_font("en", bold=bold), size)


@lru_cache(maxsize=None)
def resolve_font(kind: str, *, bold: bool) -> str:
    candidates = ZH_FONT_CANDIDATES if kind == "zh" else (EN_BOLD_FONT_CANDIDATES if bold else EN_REG_FONT_CANDIDATES)
    for raw_path in candidates:
        path = str(raw_path or "").strip()
        if path and Path(path).exists():
            return path
    fallback_names = ["NotoSansCJK-Bold.ttc", "DejaVuSans-Bold.ttf"] if bold else ["NotoSansCJK-Regular.ttc", "DejaVuSans.ttf"]
    for name in fallback_names:
        try:
            ImageFont.truetype(name, 24)
        except OSError:
            continue
        return name
    raise SystemExit(f"No usable {'Chinese' if kind == 'zh' else 'English'} font found")


def build_ffmpeg_command(
    *,
    source: Path,
    static_png: Path,
    subtitle_pngs: list[tuple[Path, float, float]],
    output: Path,
    duration: float,
    source_width: int,
    source_height: int,
    encoder: str,
    threads: int,
    vertical_top_crop: int,
    vertical_bottom_crop: int,
    preserve_vertical_source: bool,
    landscape_crop: tuple[int, int, int, int] | None,
) -> list[str]:
    image_inputs: list[str] = []
    for png in [static_png] + [item[0] for item in subtitle_pngs]:
        image_inputs.extend(["-loop", "1", "-framerate", "30", "-t", f"{duration:.3f}", "-i", str(png)])

    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-nostdin",
        "-loglevel",
        "error",
        "-nostats",
        "-filter_threads",
        str(threads),
        "-filter_complex_threads",
        str(threads),
        "-i",
        str(source),
        *image_inputs,
        "-filter_complex",
        build_filter_complex(
            subtitle_pngs,
            source_width=source_width,
            source_height=source_height,
            vertical_top_crop=vertical_top_crop,
            vertical_bottom_crop=vertical_bottom_crop,
            preserve_vertical_source=preserve_vertical_source,
            landscape_crop=landscape_crop,
        ),
        "-t",
        f"{duration:.3f}",
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        *encoder_args(encoder, threads),
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output),
    ]


def build_filter_complex(
    subtitle_pngs: list[tuple[Path, float, float]],
    *,
    source_width: int,
    source_height: int,
    vertical_top_crop: int,
    vertical_bottom_crop: int,
    preserve_vertical_source: bool,
    landscape_crop: tuple[int, int, int, int] | None,
) -> str:
    if source_height > source_width:
        top_crop = min(vertical_top_crop, source_height - 2)
        bottom_crop = min(vertical_bottom_crop, source_height - top_crop - 2)
        usable_height = max(2, source_height - top_crop - bottom_crop)
        usable_height -= usable_height % 2
        top_crop -= top_crop % 2
        if preserve_vertical_source:
            main_filter = (
                f"[mainsrc]crop={source_width}:{usable_height}:0:{top_crop},"
                "scale=1080:1440:force_original_aspect_ratio=decrease,"
                "pad=1080:1440:(ow-iw)/2:(oh-ih)/2:color=black@0.28,fps=30,"
                "eq=brightness=0.012:contrast=1.035:saturation=1.025:gamma=1.006,"
                "unsharp=5:5:0.18[main]"
            )
        else:
            main_filter = (
                f"[mainsrc]crop={source_width}:{usable_height}:0:{top_crop},"
                "scale=1080:1440:force_original_aspect_ratio=increase,"
                "crop=1080:1440,fps=30,"
                "zoompan=z='1.004+0.003*sin(on/90)':x='iw/2-(iw/zoom/2)':"
                "y='ih/2-(ih/zoom/2)':d=1:s=1080x1440:fps=30,"
                "eq=brightness=0.012:contrast=1.035:saturation=1.025:gamma=1.006,"
                "unsharp=5:5:0.18[main]"
            )
        main_y = 300
    else:
        if landscape_crop is not None:
            crop_w, crop_h, crop_x, crop_y = clamp_crop(landscape_crop, source_width, source_height)
        elif source_width <= 1120:
            crop_h = source_height - source_height % 2
            crop_w = min(
                source_width - source_width % 2,
                int(round((crop_h * OUT_W / MAIN_H) / 2) * 2),
            )
            crop_x = max(0, (((source_width - crop_w) // 2) // 2) * 2)
            crop_y = 0
        else:
            crop_w = min(760, source_width - source_width % 2)
            crop_h = min(560, source_height - source_height % 2)
            crop_x = min(320, max(0, source_width - crop_w))
            crop_y = min(20, max(0, source_height - crop_h))
            crop_x -= crop_x % 2
            crop_y -= crop_y % 2
        main_filter = (
            f"[mainsrc]crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale=1080:-2,fps=30,"
            "zoompan=z='1.004+0.003*sin(on/90)':x='iw/2-(iw/zoom/2)':"
            "y='ih/2-(ih/zoom/2)':d=1:s=1080x796:fps=30,"
            "eq=brightness=0.012:contrast=1.035:saturation=1.025:gamma=1.006,"
            "unsharp=5:5:0.18[main]"
        )
        main_y = MAIN_Y

    parts = [
        "[0:v]setpts=PTS-STARTPTS,split=2[bgsrc][mainsrc]",
        (
            "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=34:steps=2,"
            "eq=brightness=-0.165:contrast=1.055:saturation=1.38:gamma=1.004,"
            "noise=alls=1.4:allf=t+u[bg]"
        ),
        main_filter,
        f"[bg][main]overlay=0:{main_y}:format=auto[base0]",
        "[base0][1:v]overlay=0:0:format=auto[base1]",
    ]

    previous = "base1"
    for idx, (_, start, end) in enumerate(subtitle_pngs, start=2):
        label = f"v{idx}"
        expr = f"gte(t\\,{start:.3f})*lt(t\\,{end:.3f})"
        parts.append(f"[{previous}][{idx}:v]overlay=0:0:format=auto:enable={expr}[{label}]")
        previous = label
    parts.append(f"[{previous}]fps=30,setsar=1,format=yuv420p[vout]")
    parts.append(
        "[0:a]asetpts=PTS-STARTPTS,"
        "highpass=f=72,lowpass=f=18500,"
        "acompressor=threshold=-20dB:ratio=1.12:attack=12:release=160,"
        "equalizer=f=3200:t=q:w=1.2:g=0.35,"
        "aresample=48000,volume=1.018[aout]"
    )
    return ";".join(parts)


def parse_crop_arg(raw: str) -> tuple[int, int, int, int]:
    try:
        width, height, x, y = [int(part) for part in raw.split(":")]
    except ValueError as exc:
        raise SystemExit("--landscape-crop must be formatted as width:height:x:y") from exc
    if width <= 0 or height <= 0 or x < 0 or y < 0:
        raise SystemExit("--landscape-crop values must be positive width/height and non-negative x/y")
    return width, height, x, y


def clamp_crop(
    crop: tuple[int, int, int, int],
    source_width: int,
    source_height: int,
) -> tuple[int, int, int, int]:
    crop_w, crop_h, crop_x, crop_y = crop
    crop_w = min(crop_w, source_width)
    crop_h = min(crop_h, source_height)
    crop_x = min(crop_x, max(0, source_width - crop_w))
    crop_y = min(crop_y, max(0, source_height - crop_h))
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    crop_x -= crop_x % 2
    crop_y -= crop_y % 2
    if crop_w < 2 or crop_h < 2:
        raise SystemExit("--landscape-crop is too small after clamping")
    return crop_w, crop_h, crop_x, crop_y


def encoder_args(encoder: str, threads: int) -> list[str]:
    if encoder in {"auto", "videotoolbox"}:
        return [
            "-c:v",
            "h264_videotoolbox",
            "-profile:v",
            "high",
            "-b:v",
            "9M",
            "-maxrate",
            "12M",
            "-bufsize",
            "18M",
            "-allow_sw",
            "1",
            "-tag:v",
            "avc1",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-threads",
        str(threads),
        "-pix_fmt",
        "yuv420p",
    ]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


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
    import json

    data = json.loads(result.stdout)
    width = 0
    height = 0
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = int(stream.get("width", 0))
            height = int(stream.get("height", 0))
            break
    return {
        "duration": float(data["format"]["duration"]),
        "width": float(width),
        "height": float(height),
    }


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"Command failed with exit code {exc.returncode}", file=sys.stderr)
        raise
