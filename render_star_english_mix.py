#!/usr/bin/env python3
"""Render a KC entertainment-style multi-star English-speaking montage."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


OUT_W = 1080
OUT_H = 1920
MAIN_Y = 332
MAIN_H = 1220
CAPTION_Y = 1300
BRAND_Y = 1806
ZH_FONT_PATH = "/System/Library/Fonts/Hiragino Sans GB.ttc"
EN_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
EN_REG_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial.ttf"

WORK_DIR = Path("work/mix")
RENDER_DIR = WORK_DIR / "render"
CLIP_DIR = WORK_DIR / "clips"
OUTPUT = Path("KC娱乐_男明星说英语你Pick谁.mp4")
NAV_ITEMS = ["肖战", "王一博", "龚俊", "丁禹兮"]
TITLE_MAIN = "男明星说英语"
TITLE_SUB = "你Pick谁？"
LOWER_RIBBON = "英语名场面 · 你来Pick"
TITLE_HIGHLIGHTS = ["No.1", "No.2", "No.3", "No.4", "肖战", "王一博", "龚俊", "丁禹兮", "英语", "Pick"]
TITLE_MAIN_HIGHLIGHTS = ["英语"]
TITLE_SUB_HIGHLIGHTS = ["Pick", "谁"]
TOP_TAG = "英语名场面"
STICKER_TOPIC = "英语"
NAV_X0 = 344
NAV_ITEM_W = 94
NAV_GAP = 8
NAV_FONT_SIZE = 20
MAIN_TOP_GRADIENT_H = 238
MAIN_TOP_GRADIENT_START_ALPHA = 244
MAIN_TOP_GRADIENT_END_ALPHA = 182
MAIN_TOP_MASK_H = 214
MAIN_TOP_MASK_ALPHA = 218


SEGMENTS: list[dict[str, Any]] = [
    {
        "kind": "card",
        "duration": 1.15,
        "title": "男明星说英语",
        "subtitle": "你Pick谁？",
        "badge": "KC娱乐",
        "name": "intro",
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.1 肖战",
        "subtitle": "开口就是氛围感",
        "badge": "第一位",
        "name": "card_xiaozhan",
    },
    {
        "kind": "clip",
        "name": "xiaozhan_quote",
        "person": "No.1 肖战",
        "source": Path("待混剪/肖战.mp4"),
        "start": 3.6,
        "duration": 4.45,
        "crop": {"top": 90, "bottom": 50, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 2.15,
                "zh": "万物皆有裂痕",
                "en": "There is a crack in everything.",
                "zh_highlights": ["裂痕"],
                "en_highlights": ["crack"],
            },
            {
                "start": 2.15,
                "end": 4.45,
                "zh": "那是光照进来的地方",
                "en": "That's how the light gets in.",
                "zh_highlights": ["光"],
                "en_highlights": ["light"],
            },
        ],
    },
    {
        "kind": "clip",
        "name": "xiaozhan_goodluck",
        "person": "No.1 肖战",
        "source": Path("待混剪/肖战.mp4"),
        "start": 12.35,
        "duration": 5.95,
        "crop": {"top": 90, "bottom": 50, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.1,
                "zh": "夏天和好运都在这句里",
                "en": "This is me, my summer, my Gucci.",
                "zh_highlights": ["夏天", "好运"],
                "en_highlights": ["summer", "Gucci"],
            },
            {
                "start": 3.1,
                "end": 5.95,
                "zh": "最后一句 Good luck 很轻松",
                "en": "Good luck.",
                "zh_highlights": ["Good luck", "轻松"],
                "en_highlights": ["Good luck"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.2 王一博",
        "subtitle": "街头文化聊得很自然",
        "badge": "第二位",
        "name": "card_wangyibo",
    },
    {
        "kind": "clip",
        "name": "wangyibo",
        "person": "No.2 王一博",
        "source": Path("待混剪/王一博.mp4"),
        "start": 0.0,
        "duration": 12.35,
        "crop": {"top": 170, "bottom": 150, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.4,
                "zh": "很喜欢这里的文化",
                "en": "I really like a lot of the culture here.",
                "zh_highlights": ["文化"],
                "en_highlights": ["culture"],
            },
            {
                "start": 3.4,
                "end": 6.75,
                "zh": "音乐和街头氛围都很吸引人",
                "en": "I like the music and the atmosphere of the streets.",
                "zh_highlights": ["音乐", "街头"],
                "en_highlights": ["music", "streets"],
            },
            {
                "start": 6.75,
                "end": 12.35,
                "zh": "还去海边看了滑板公园",
                "en": "I also went to the beach to see some skateboard parks.",
                "zh_highlights": ["海边", "滑板"],
                "en_highlights": ["beach", "skateboard"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.3 龚俊",
        "subtitle": "全英文介绍中国山水画",
        "badge": "第三位",
        "name": "card_gongjun",
    },
    {
        "kind": "clip",
        "name": "gongjun",
        "person": "No.3 龚俊",
        "source": Path("待混剪/龚俊.mp4"),
        "start": 0.0,
        "duration": 12.45,
        "crop": {"top": 260, "bottom": 260, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.15,
                "zh": "这是传统中国山水画",
                "en": "This is a traditional Chinese landscape painting.",
                "zh_highlights": ["山水画"],
                "en_highlights": ["landscape painting"],
            },
            {
                "start": 3.15,
                "end": 7.85,
                "zh": "我和吴大师一起创作",
                "en": "It's created by me and Master Wu.",
                "zh_highlights": ["吴大师"],
                "en_highlights": ["Master Wu"],
            },
            {
                "start": 7.85,
                "end": 12.45,
                "zh": "画里有自然与人的和谐",
                "en": "This painting reflects harmony between nature and humanity.",
                "zh_highlights": ["和谐"],
                "en_highlights": ["harmony"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.4 丁禹兮",
        "subtitle": "英文演讲谈气候议题",
        "badge": "第四位",
        "name": "card_dingyuxi",
    },
    {
        "kind": "clip",
        "name": "dingyuxi",
        "person": "No.4 丁禹兮",
        "source": Path("待混剪/丁禹兮.mp4"),
        "start": 7.16,
        "duration": 12.16,
        "crop": {"top": 320, "bottom": 340, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.96,
                "zh": "气候变化关乎我们每个人",
                "en": "Climate change concerns every one of us.",
                "zh_highlights": ["气候变化"],
                "en_highlights": ["Climate change"],
            },
            {
                "start": 3.96,
                "end": 7.08,
                "zh": "希望更多年轻人站出来",
                "en": "I hope more young people will step forward.",
                "zh_highlights": ["年轻人"],
                "en_highlights": ["young people"],
            },
            {
                "start": 7.08,
                "end": 12.16,
                "zh": "一起守护可持续的未来",
                "en": "Work together to safeguard a sustainable future.",
                "zh_highlights": ["可持续"],
                "en_highlights": ["sustainable future"],
            },
        ],
    },
]


def main() -> None:
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    CLIP_DIR.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    for index, segment in enumerate(SEGMENTS):
        output = CLIP_DIR / f"{index:02d}_{segment['name']}.mp4"
        if segment["kind"] == "card":
            render_card_segment(segment, output)
        else:
            render_video_segment(segment, output)
        outputs.append(output)

    concat_file = WORK_DIR / "concat_list.txt"
    concat_file.write_text(
        "".join(f"file '{path.resolve()}'\n" for path in outputs),
        encoding="utf-8",
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(OUTPUT),
        ]
    )
    print(f"Wrote {OUTPUT}")


def render_card_segment(segment: dict[str, Any], output: Path) -> None:
    png_path = RENDER_DIR / f"{segment['name']}.png"
    img = Image.new("RGBA", (OUT_W, OUT_H), (8, 10, 20, 255))
    draw = ImageDraw.Draw(img, "RGBA")

    draw_gradient(draw, 0, 0, OUT_W, OUT_H, (8, 10, 20, 255), (18, 18, 34, 255))
    draw.rectangle((0, 0, OUT_W, 120), fill=(255, 42, 120, 238))
    draw.rectangle((0, OUT_H - 180, OUT_W, OUT_H), fill=(4, 5, 12, 240))
    draw.rectangle((0, 330, 24, 1400), fill=(255, 42, 120, 235))
    draw.rectangle((OUT_W - 24, 420, OUT_W, 1490), fill=(0, 220, 255, 225))
    draw.rectangle((92, 540, 988, 552), fill=(255, 214, 47, 230))
    draw.rectangle((92, 1210, 988, 1222), fill=(0, 220, 255, 220))

    draw_tag(draw, 56, 70, str(segment.get("badge", "KC娱乐")), fill=(255, 214, 47, 246), text_fill=(8, 10, 18, 255))
    draw_sticker(draw, 816, 78, "HOT", STICKER_TOPIC)

    draw_center_highlight_line(
        draw,
        str(segment["title"]),
        y=690,
        max_width=940,
        font_size=86,
        min_size=54,
        highlights=TITLE_HIGHLIGHTS,
        fill=(255, 255, 255, 255),
        highlight_fill=(255, 221, 42, 255),
        stroke=6,
    )
    draw_center_highlight_line(
        draw,
        str(segment["subtitle"]),
        y=820,
        max_width=900,
        font_size=56,
        min_size=38,
        highlights=["Pick", "英文", "文化", "气候", "氛围", "语言", "台词", "方言"],
        fill=(224, 244, 255, 255),
        highlight_fill=(255, 221, 42, 255),
        stroke=5,
    )
    draw_center_highlight_line(
        draw,
        f"{TITLE_MAIN}，{TITLE_SUB}",
        y=1288,
        max_width=900,
        font_size=50,
        min_size=36,
        highlights=["Pick", "谁"],
        fill=(255, 255, 255, 255),
        stroke=5,
    )
    draw_brand(draw)
    img.save(png_path)

    duration = float(segment["duration"])
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            "30",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(png_path),
            "-f",
            "lavfi",
            "-t",
            f"{duration:.3f}",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-vf",
            "fps=30,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )


def render_video_segment(segment: dict[str, Any], output: Path) -> None:
    source = Path(segment["source"])
    duration = float(segment["duration"])
    media = probe_media(source)
    static_png = RENDER_DIR / f"{segment['name']}_static.png"
    render_static_overlay(segment, static_png)

    subtitle_inputs: list[tuple[Path, float, float]] = []
    for index, subtitle in enumerate(segment["subtitles"], start=1):
        path = RENDER_DIR / f"{segment['name']}_sub_{index:02d}.png"
        render_subtitle_overlay(subtitle, path)
        subtitle_inputs.append((path, float(subtitle["start"]), float(subtitle["end"])))

    image_inputs: list[str] = []
    for png in [static_png] + [item[0] for item in subtitle_inputs]:
        image_inputs.extend(["-loop", "1", "-framerate", "30", "-t", f"{duration:.3f}", "-i", str(png)])

    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            f"{float(segment['start']):.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            *image_inputs,
            "-filter_complex",
            build_filter_complex(
                media_width=int(media["width"]),
                media_height=int(media["height"]),
                crop=segment["crop"],
                subtitle_inputs=subtitle_inputs,
            ),
            "-t",
            f"{duration:.3f}",
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
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
    )


def render_static_overlay(segment: dict[str, Any], output: Path) -> None:
    img = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    draw_gradient(draw, 0, 0, OUT_W, 354, (6, 10, 24, 242), (18, 11, 36, 150))
    draw_gradient(
        draw,
        0,
        MAIN_Y,
        OUT_W,
        MAIN_Y + MAIN_TOP_GRADIENT_H,
        (5, 5, 12, MAIN_TOP_GRADIENT_START_ALPHA),
        (5, 5, 12, MAIN_TOP_GRADIENT_END_ALPHA),
    )
    draw.rectangle((22, MAIN_Y, OUT_W - 22, MAIN_Y + MAIN_TOP_MASK_H), fill=(5, 5, 12, MAIN_TOP_MASK_ALPHA))
    draw_gradient(draw, 0, 1148, OUT_W, 1280, (5, 5, 12, 46), (5, 5, 12, 255))
    draw.rectangle((0, 1206, OUT_W, 1574), fill=(5, 5, 12, 255))
    draw.rectangle((0, 1548, OUT_W, OUT_H), fill=(5, 5, 12, 255))

    draw.rectangle((0, MAIN_Y - 8, 22, MAIN_Y + MAIN_H + 8), fill=(255, 42, 120, 235))
    draw.rectangle((OUT_W - 22, MAIN_Y + 48, OUT_W, MAIN_Y + MAIN_H + 34), fill=(0, 220, 255, 220))
    draw.rectangle((22, MAIN_Y - 8, 240, MAIN_Y + 4), fill=(255, 214, 47, 240))
    draw.rectangle((812, MAIN_Y + MAIN_H + 24, OUT_W - 22, MAIN_Y + MAIN_H + 36), fill=(255, 214, 47, 230))

    draw_navigation(draw, str(segment["person"]))
    draw_tag(draw, 46, 84, TOP_TAG, fill=(255, 42, 120, 242))
    draw_tag(draw, 744, 316, str(segment["person"]), fill=(0, 205, 255, 224), text_fill=(8, 10, 18, 255), small=True)
    draw_tag(draw, 60, 1216, "重点来了", fill=(255, 214, 47, 236), text_fill=(13, 12, 18, 255), small=True)
    draw_sticker(draw, 812, 96, "PICK", STICKER_TOPIC)

    draw_center_highlight_line(
        draw,
        TITLE_MAIN,
        y=180,
        max_width=980,
        font_size=74,
        min_size=52,
        highlights=TITLE_MAIN_HIGHLIGHTS,
        fill=(255, 255, 255, 255),
        stroke=5,
    )
    draw_center_highlight_line(
        draw,
        TITLE_SUB,
        y=268,
        max_width=980,
        font_size=58,
        min_size=42,
        highlights=TITLE_SUB_HIGHLIGHTS,
        fill=(255, 255, 255, 255),
        highlight_fill=(255, 220, 46, 255),
        stroke=5,
    )

    draw.rounded_rectangle((286, 1570, 794, 1630), radius=24, fill=(255, 42, 120, 226))
    draw_center_highlight_line(
        draw,
        LOWER_RIBBON,
        y=1579,
        max_width=470,
        font_size=34,
        min_size=28,
        highlights=["Pick"],
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
    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(output)


def render_subtitle_overlay(item: dict[str, Any], output: Path) -> None:
    img = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    draw_center_highlight_text(
        draw,
        str(item["zh"]),
        y=CAPTION_Y,
        max_width=960,
        max_height=108,
        font_size=66,
        min_size=42,
        highlights=[str(value) for value in item.get("zh_highlights", [])],
        fill=(255, 255, 255, 255),
        highlight_fill=(255, 221, 42, 255),
        stroke=6,
    )
    draw_center_highlight_text(
        draw,
        str(item["en"]),
        y=CAPTION_Y + 88,
        max_width=940,
        max_height=100,
        font_size=34,
        min_size=24,
        highlights=[str(value) for value in item.get("en_highlights", [])],
        fill=(224, 244, 255, 242),
        highlight_fill=(255, 221, 42, 255),
        stroke=3,
        english=True,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(output)


def build_filter_complex(
    *,
    media_width: int,
    media_height: int,
    crop: dict[str, int],
    subtitle_inputs: list[tuple[Path, float, float]],
) -> str:
    left = make_even(int(crop.get("left", 0)))
    top = make_even(int(crop.get("top", 0)))
    right = make_even(int(crop.get("right", 0)))
    bottom = make_even(int(crop.get("bottom", 0)))
    fit_mode = str(crop.get("fit", "cover"))
    crop_w = make_even(max(2, media_width - left - right))
    crop_h = make_even(max(2, media_height - top - bottom))
    if fit_mode == "contain":
        main_filter = (
            f"[mainsrc]crop={crop_w}:{crop_h}:{left}:{top},"
            "scale=1080:1220:force_original_aspect_ratio=decrease,"
            "fps=30,"
            "eq=brightness=0.012:contrast=1.035:saturation=1.025:gamma=1.006,"
            "unsharp=5:5:0.18,"
            "format=rgba,"
            "pad=1080:1220:(ow-iw)/2:(oh-ih)/2:color=black@0[main]"
        )
    else:
        main_filter = (
            f"[mainsrc]crop={crop_w}:{crop_h}:{left}:{top},"
            "scale=1080:1220:force_original_aspect_ratio=increase,"
            "crop=1080:1220,fps=30,"
            "eq=brightness=0.012:contrast=1.035:saturation=1.025:gamma=1.006,"
            "unsharp=5:5:0.18[main]"
        )

    parts = [
        "[0:v]setpts=PTS-STARTPTS,split=2[bgsrc][mainsrc]",
        (
            "[bgsrc]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=34:steps=2,"
            "eq=brightness=-0.16:contrast=1.055:saturation=1.35:gamma=1.004,"
            "noise=alls=1.2:allf=t+u[bg]"
        ),
        main_filter,
        f"[bg][main]overlay=0:{MAIN_Y}:format=auto[base0]",
        "[base0][1:v]overlay=0:0:format=auto[base1]",
    ]
    previous = "base1"
    for idx, (_, start, end) in enumerate(subtitle_inputs, start=2):
        label = f"v{idx}"
        expr = f"gte(t\\,{start:.3f})*lt(t\\,{end:.3f})"
        parts.append(f"[{previous}][{idx}:v]overlay=0:0:format=auto:enable={expr}[{label}]")
        previous = label
    parts.append(f"[{previous}]fps=30,format=yuv420p[vout]")
    parts.append(
        "[0:a]asetpts=PTS-STARTPTS,"
        "highpass=f=72,lowpass=f=18500,"
        "acompressor=threshold=-20dB:ratio=1.12:attack=12:release=160,"
        "equalizer=f=3200:t=q:w=1.2:g=0.35,"
        "aresample=48000,volume=1.04[aout]"
    )
    return ";".join(parts)


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
    font = load_font(32 if small else 36, bold=True)
    pad_x = 22 if small else 26
    pad_y = 12 if small else 14
    box = draw.textbbox((0, 0), text, font=font, stroke_width=0)
    w = box[2] - box[0] + pad_x * 2
    h = box[3] - box[1] + pad_y * 2
    draw.rounded_rectangle((x, y, x + w, y + h), radius=18, fill=fill)
    draw.text((x + pad_x, y + pad_y - 3), text, font=font, fill=text_fill)


def draw_sticker(draw: ImageDraw.ImageDraw, x: int, y: int, top: str, bottom: str) -> None:
    draw.rounded_rectangle((x + 10, y + 10, x + 186, y + 132), radius=26, fill=(0, 0, 0, 115))
    draw.rounded_rectangle((x, y, x + 176, y + 122), radius=26, fill=(255, 214, 47, 246))
    draw.rectangle((x, y + 70, x + 176, y + 122), fill=(255, 42, 120, 246))
    top_font = load_en_font(44, bold=True)
    top_box = draw.textbbox((0, 0), top, font=top_font, stroke_width=2)
    draw.text(
        (x + (176 - (top_box[2] - top_box[0])) / 2, y + 16),
        top,
        font=top_font,
        fill=(14, 10, 20, 255),
        stroke_width=2,
        stroke_fill=(255, 255, 255, 220),
    )
    bottom_font = load_font(34, bold=True)
    bottom_box = draw.textbbox((0, 0), bottom, font=bottom_font, stroke_width=2)
    draw.text(
        (x + (176 - (bottom_box[2] - bottom_box[0])) / 2, y + 76),
        bottom,
        font=bottom_font,
        fill=(255, 255, 255, 255),
        stroke_width=2,
        stroke_fill=(80, 0, 35, 220),
    )


def draw_navigation(draw: ImageDraw.ImageDraw, person: str) -> None:
    active = next((idx for idx, name in enumerate(NAV_ITEMS) if name in person), 0)
    x0 = NAV_X0
    y0 = 88
    gap = NAV_GAP
    item_w = NAV_ITEM_W
    item_h = 34
    font = load_font(NAV_FONT_SIZE, bold=True)
    for idx, name in enumerate(NAV_ITEMS):
        x = x0 + idx * (item_w + gap)
        is_active = idx == active
        fill = (255, 214, 47, 248) if is_active else (42, 52, 70, 224)
        text_fill = (8, 10, 18, 255) if is_active else (210, 226, 238, 230)
        draw.rounded_rectangle((x, y0, x + item_w, y0 + item_h), radius=12, fill=fill)
        if idx < active:
            draw.rectangle((x + 10, y0 + item_h - 6, x + item_w - 10, y0 + item_h - 3), fill=(0, 220, 255, 230))
        box = draw.textbbox((0, 0), name, font=font, stroke_width=0)
        draw.text(
            (x + (item_w - (box[2] - box[0])) / 2, y0 + 4),
            name,
            font=font,
            fill=text_fill,
        )


def draw_brand(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle((0, 1764, OUT_W, OUT_H), fill=(4, 5, 12, 236))
    draw.rectangle((236, 1778, 318, 1788), fill=(255, 42, 120, 250))
    draw.rectangle((762, 1778, 844, 1788), fill=(0, 220, 255, 238))
    draw_center_highlight_line(
        draw,
        "KC娱乐",
        y=BRAND_Y,
        max_width=620,
        font_size=74,
        min_size=58,
        highlights=["KC"],
        fill=(255, 255, 255, 255),
        highlight_fill=(255, 218, 45, 255),
        stroke=5,
    )
    draw_center_highlight_line(
        draw,
        "ENTERTAINMENT",
        y=1880,
        max_width=520,
        font_size=24,
        min_size=20,
        highlights=[],
        fill=(0, 220, 255, 210),
        stroke=1,
        english=True,
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
    text = " ".join(str(text).split()).strip()
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
    highlight_fill: tuple[int, int, int, int],
    stroke: int,
    english: bool = False,
) -> None:
    text = " ".join(str(text).split()).strip()
    if not text:
        return
    size = font_size
    while size >= min_size:
        font = load_en_font(size, bold=True) if english else load_font(size, bold=True)
        lines = wrap_text(draw, text, font, max_width, stroke)
        line_height = max(measure_text(draw, line, font, stroke_width=stroke)[1] for line in lines) + int(size * 0.18)
        if line_height * len(lines) <= max_height:
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
        return lines[:2]
    lines: list[str] = []
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
    return lines[:2]


def split_highlight_runs(text: str, highlights: list[str]) -> list[tuple[str, bool]]:
    ranges: list[tuple[int, int]] = []
    lower = text.lower()
    for raw in highlights:
        phrase = " ".join(str(raw).split()).strip()
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
    del bold
    return ImageFont.truetype(ZH_FONT_PATH, size)


def load_en_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(EN_FONT_PATH if bold else EN_REG_FONT_PATH, size)


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


def make_even(value: int) -> int:
    return max(0, value - value % 2)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
