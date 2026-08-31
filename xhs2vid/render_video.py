#!/usr/bin/env python3
"""把小红书热帖渲染成 KC娱乐 format 的热评视频。

结构: KC娱乐标题区 + 中间白色面板轮播 [封面截图, 热评1, 热评2, 热评3]
      + KC娱乐品牌底栏 + 高亮字幕 + 1.5x TTS 配音。
蒙版: 评论卡头像/昵称打马赛克; 阅读区按口播进度由模糊渐变为清晰; 段间 xfade。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    import numpy as np
except ImportError:  # 程序火焰不依赖 numpy，仍可作为兼容回退。
    np = None

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"
DEFAULT_RENDER = WORK / "render"
RENDER = DEFAULT_RENDER
RENDER.mkdir(parents=True, exist_ok=True)

SAMPLE = ROOT / "最终成品示例.mp4"
NOTE: dict = {}
ALL_COMMENTS: list[dict] = []
COMMENTS: list[dict] = []

W, H = 1080, 1920
PANEL_TOP, PANEL_BOTTOM = 430, 1360          # 中间白色面板
PANEL_H = PANEL_BOTTOM - PANEL_TOP
SUBTITLE_Y = 1292                            # 字幕中心线(绝对坐标)
FOOTER_TOP = PANEL_BOTTOM                    # KC 品牌底栏起点
CARD_SAFE_H = 770                            # 评论卡内容区高度(给字幕留空)
TTS_RATE = 278                               # 原 185，约 1.5 倍语速
TTS_BACKEND = "tingting"
TTS_SPEAKER = "BV001_fast_streaming"
TTS_PROCESSING = "sample-machine"
REUSE_TTS_CACHE = False
JIANYING_PROCESSING_FILTERS = {
    "sample-machine": (
        "asetrate=48000*1.4605,aresample=48000,atempo=1.01753,"
        "volume=3dB,alimiter=limit=0.97"
    ),
    "character": "loudnorm=I=-16:LRA=7:TP=-1.5",
}
DEFAULT_CHARACTER_TEMPO = 1.08
PAGE_PAD = 0.16
SEGMENT_PAD = 0.20
REVEAL_BLUR = 18
FIRE_T = 0.80
FIRE_GROW_FRAMES = 5
ZOOM_T = 0.40
FPS = 30
REFERENCE_FIRE_BASE_FRAME = 82
REFERENCE_FIRE_FIRST_FRAME = 83
REFERENCE_FIRE_LAST_FRAME = 106
REFERENCE_PANEL_BOX = (0, 567, 1080, 1360)
REFERENCE_TEXT_BANDS = (
    (125, 265, 955, 415),
    (125, 500, 955, 655),
    (250, 820, 835, PANEL_H - 1),
)
REFERENCE_REPAIR_ANGLES = (37, 71, 109, 143, 217, 251, 289, 323)

def first_existing_font(env_name: str, candidates: tuple[str, ...]) -> str:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        if Path(configured).is_file():
            return configured
        raise FileNotFoundError(f"{env_name} points to a missing font: {configured}")
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError(
        f"No usable font found for {env_name}; install fonts-noto-cjk or set {env_name}"
    )


FONT_REGULAR = first_existing_font(
    "KC_CJK_REGULAR_FONT",
    (
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ),
)
FONT_BOLD = first_existing_font(
    "KC_CJK_BOLD_FONT",
    (
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        FONT_REGULAR,
    ),
)
FONT_LATIN = first_existing_font(
    "KC_LATIN_BOLD_FONT",
    (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        FONT_BOLD,
    ),
)
GRAY = (153, 153, 153)
KC_BG_TOP = (14, 16, 31)
KC_BG_BOTTOM = (4, 5, 12)
KC_MAGENTA = (255, 42, 120)
KC_CYAN = (0, 220, 255)
KC_YELLOW = (255, 221, 42)

IP_MAP = {
    "Hubei": "湖北", "Henan": "河南", "Guangxi": "广西", "Guangdong": "广东",
    "Beijing": "北京", "Shanghai": "上海", "Zhejiang": "浙江", "Jiangsu": "江苏",
    "Sichuan": "四川", "Shandong": "山东", "Hunan": "湖南", "Fujian": "福建",
    "Anhui": "安徽", "Hebei": "河北", "Shanxi": "山西", "Liaoning": "辽宁",
    "Jilin": "吉林", "Heilongjiang": "黑龙江", "Chongqing": "重庆",
    "Yunnan": "云南", "Guizhou": "贵州", "Shaanxi": "陕西", "Gansu": "甘肃",
    "Jiangxi": "江西", "Tianjin": "天津", "Hainan": "海南",
}

EMOJI_TAG = re.compile(r"\[[^\[\]]{1,10}\]")
HIGHLIGHT_WORDS = [
    "聊天记录", "公司大群", "善良本性", "普通男同事", "表白小作文", "每一句",
    "不收敛分寸", "朋友圈", "电梯间", "感化你", "天神啊", "爱太多人",
    "狗都不要", "不会要", "冒犯", "表白", "接受你", "看着你", "网友回复", "关注",
    "结婚生子", "深度绑定", "父母", "边界", "控制你", "予取予求",
]


def font(path: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size, index=index)


def hei_w6(size: int) -> ImageFont.FreeTypeFont:
    # Hiragino Sans GB: W6 index 1. Noto CJK TTC: Simplified Chinese index 2.
    index = 1 if "Hiragino Sans GB" in FONT_BOLD else (2 if "NotoSansCJK" in FONT_BOLD else 0)
    return font(FONT_BOLD, size, index=index)


def hei_w3(size: int) -> ImageFont.FreeTypeFont:
    index = 0 if "Hiragino Sans GB" in FONT_REGULAR else (2 if "NotoSansCJK" in FONT_REGULAR else 0)
    return font(FONT_REGULAR, size, index=index)


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=WORK,
        help="chosen_note.json、评论缓存和封面所在目录。",
    )
    parser.add_argument(
        "--voice",
        choices=("tingting", "jianying-machine"),
        default="tingting",
        help="旁白：原版 macOS Tingting，或样本 1 风格的剪映机械声。",
    )
    parser.add_argument(
        "--speaker",
        default="BV001_fast_streaming",
        help="--voice jianying-machine 使用的剪映 speaker_id。",
    )
    parser.add_argument(
        "--segment-speaker",
        action="append",
        dest="segment_speakers",
        help=(
            "按封面、评论1、回复1、评论2、回复2……顺序指定 speaker_id；"
            "可重复传入，数量必须与实际段落数一致。"
        ),
    )
    parser.add_argument(
        "--jianying-processing",
        choices=tuple(JIANYING_PROCESSING_FILTERS),
        default="sample-machine",
        help="sample-machine 复刻原机械声；character 保留角色音原本音高。",
    )
    parser.add_argument(
        "--segment-tempo",
        action="append",
        dest="segment_tempos",
        type=float,
        help=(
            "按封面、评论1、回复1……逐段指定不变调语速；可重复传入，"
            "仅用于 character 模式。"
        ),
    )
    parser.add_argument(
        "--reuse-tts-cache",
        action="store_true",
        help="复用 render-dir 中已有的同名 WAV，避免重复调用配音接口。",
    )
    parser.add_argument(
        "--include-subcomments",
        action="store_true",
        help="从 comments_raw.json 读取每条热评缓存的最高赞子评论并单独成段。",
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        default=3,
        help="最多使用多少条一级热评（默认 3）。",
    )
    parser.add_argument(
        "--render-dir",
        type=Path,
        help="中间渲染文件目录；默认是 <work-dir>/render。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="成片路径；不指定时沿用 KC娱乐_<标题>.mp4。",
    )
    args = parser.parse_args()
    if args.max_comments < 1:
        parser.error("--max-comments 必须大于 0")
    speaker_ids = [args.speaker, *(args.segment_speakers or [])]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in speaker_ids):
        parser.error("speaker_id 只能包含英文字母、数字、下划线和连字符")
    if args.segment_tempos:
        if args.jianying_processing != "character":
            parser.error("--segment-tempo 只能与 --jianying-processing character 一起使用")
        if any(not 0.75 <= tempo <= 2.0 for tempo in args.segment_tempos):
            parser.error("--segment-tempo 必须在 0.75 到 2.0 之间")
    return args


def clean_text(text: str) -> str:
    text = EMOJI_TAG.sub("", text)
    for ch in "“”《》\"'":
        text = text.replace(ch, "，")
    text = re.sub(r"([？！。，])\1+", r"\1", text)
    text = re.sub(r"，+", "，", text).strip("， ")
    return text.strip("，。！？.!?… ")


def load_preview_subcomments(comments: list[dict]) -> list[dict | None]:
    """从抓取接口缓存中找回 fetch_assets.py 未写入 top_comments.json 的子评论。"""
    raw_path = WORK / "comments_raw.json"
    if not raw_path.is_file():
        raise FileNotFoundError(f"缺少子评论缓存: {raw_path}")
    payload = json.loads(raw_path.read_text())
    raw_comments = payload.get("data", {}).get("data", {}).get("comments", [])
    by_text = {item.get("content", ""): item for item in raw_comments}

    replies: list[dict | None] = []
    for parent in comments:
        raw_parent = by_text.get(parent.get("text", ""), {})
        direct_candidates = parent.get("sub_comments") or []
        candidates = [
            item for item in (direct_candidates or raw_parent.get("sub_comments") or [])
            if item.get("text") or item.get("content")
        ]
        if not candidates:
            replies.append(None)
            continue
        child = max(candidates, key=lambda item: int(item.get("like_count") or 0))
        user = child.get("user") or {}
        replies.append({
            "text": child.get("text") or child.get("content") or "",
            "like_count": int(child.get("like_count") or 0),
            "time": int(child.get("time") or time.time()),
            "ip_location": child.get("ip_location") or "",
            "sub_comment_count": 0,
            "nickname": child.get("nickname") or user.get("nickname") or "网友",
            "avatar_file": child.get("avatar_file") or "",
        })
    return replies


def split_long_clause(clause: str, limit: int) -> list[str]:
    """超长子句按 jieba 词边界均衡切分。"""
    import jieba

    words = list(jieba.cut(clause))
    target = math.ceil(len(clause) / math.ceil(len(clause) / limit))
    parts: list[str] = []
    cur = ""
    for word in words:
        if cur and len(cur) + len(word) > target:
            parts.append(cur)
            cur = word
        else:
            cur += word
    if cur:
        parts.append(cur)
    # 合并过短的尾巴
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) + len(part) <= limit:
            merged[-1] += part
        else:
            merged.append(part)
    return merged


def split_pages(text: str, limit: int = 16) -> list[str]:
    """按标点切句, 超长子句按词边界切, 短句合并成 <=limit 字的字幕页。"""
    clauses = []
    for raw in re.split(r"[，。？！；、\s]+", text):
        if not raw:
            continue
        if len(raw) > limit:
            clauses.extend(split_long_clause(raw, limit))
        else:
            clauses.append(raw)
    pages: list[str] = []
    cur = ""
    for clause in clauses:
        if not cur:
            cur = clause
        elif len(cur) + len(clause) + 1 <= limit:
            cur = f"{cur} {clause}"
        else:
            pages.append(cur)
            cur = clause
    if cur:
        pages.append(cur)
    return pages


def wrap_cjk(text: str, fnt: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for ch in text:
        if fnt.getlength(cur + ch) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def hours_ago(ts: int) -> str:
    delta = max(int((time.time() - ts) / 3600), 1)
    if delta < 24:
        return f"{delta}小时前"
    return f"{delta // 24}天前"


def draw_mosaic(draw_img: Image.Image, box: tuple[int, int, int, int], seed: str, block: int = 14) -> None:
    rng = random.Random(seed)
    palette = [
        (196, 178, 166), (176, 158, 150), (208, 196, 186), (162, 148, 142),
        (188, 168, 154), (214, 204, 196), (150, 140, 136), (200, 184, 172),
    ]
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(draw_img)
    for by in range(y0, y1, block):
        for bx in range(x0, x1, block):
            d.rectangle(
                [bx, by, min(bx + block, x1), min(by + block, y1)],
                fill=rng.choice(palette),
            )


def draw_identity_avatar(
    target: Image.Image,
    box: tuple[int, int, int, int],
    record: dict,
    *,
    seed: str,
) -> None:
    """Draw a generated avatar when present; retain the mosaic as a safe fallback."""
    avatar_file = str(record.get("avatar_file") or "").strip()
    avatar_path = (WORK / avatar_file).resolve() if avatar_file else None
    try:
        if not avatar_path or not avatar_path.is_file():
            raise FileNotFoundError(avatar_file)
        width = box[2] - box[0]
        height = box[3] - box[1]
        with Image.open(avatar_path) as source:
            avatar = ImageOps.fit(
                source.convert("RGB"),
                (width, height),
                method=Image.Resampling.LANCZOS,
            )
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, width - 1, height - 1), fill=255)
        target.paste(avatar, (box[0], box[1]), mask)
        ImageDraw.Draw(target).ellipse(box, outline=(235, 235, 235), width=3)
    except (FileNotFoundError, OSError):
        draw_mosaic(target, box, seed=seed)


def draw_identity_name(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    record: dict,
    *,
    max_width: int,
    size: int,
) -> None:
    nickname = clean_text(str(record.get("nickname") or "网友")) or "网友"
    fnt = hei_w6(size)
    nickname = truncate_to_width(nickname, fnt, max_width)
    draw.text(position, nickname, font=fnt, fill=(80, 80, 80))


def draw_heart(d: ImageDraw.ImageDraw, cx: int, cy: int, size: int, color) -> None:
    r = size // 4
    d.ellipse([cx - 2 * r, cy - r * 2, cx, cy], fill=color)
    d.ellipse([cx, cy - r * 2, cx + 2 * r, cy], fill=color)
    d.polygon([(cx - 2 * r, cy - r // 2), (cx + 2 * r, cy - r // 2), (cx, cy + r * 2)], fill=color)


def centered_text(
    d: ImageDraw.ImageDraw,
    y: int,
    parts: list[tuple[str, tuple[int, int, int]]],
    fnt: ImageFont.FreeTypeFont,
    *,
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int] = (0, 0, 0),
) -> None:
    """绘制可分色的水平居中文案。"""
    widths = [fnt.getlength(text) for text, _ in parts]
    x = (W - sum(widths)) / 2
    for (text, color), width in zip(parts, widths):
        d.text(
            (x, y),
            text,
            font=fnt,
            fill=color,
            anchor="lm",
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        x += width


def highlight_parts(
    text: str,
    highlights: list[str],
    normal: tuple[int, int, int],
    accent: tuple[int, int, int] = KC_YELLOW,
) -> list[tuple[str, tuple[int, int, int]]]:
    """把命中的关键词拆成可分色绘制的片段。"""
    terms = [term for term in highlights if term and term in text]
    if not terms:
        return [(text, normal)]
    pattern = re.compile("(" + "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True)) + ")")
    return [(part, accent if part in terms else normal) for part in pattern.split(text) if part]


def pick_highlights(text: str, *, fallback: bool = True) -> list[str]:
    hits = [word for word in HIGHLIGHT_WORDS if word in text]
    if hits or not fallback:
        return hits[:2]
    compact = re.sub(r"\s+", "", text).strip("，。！？.!?… ")
    # Mixed Chinese/Latin title tails such as “被hr说教” should remain one
    # semantic highlight instead of coloring only punctuation or “说教”.
    mixed_tail = re.search(r"([被让给把]?[A-Za-z0-9]+[\u4e00-\u9fff]{2,4})$", compact)
    if mixed_tail:
        return [mixed_tail.group(1)]
    return [compact[-3:]] if len(compact) >= 3 else [compact]


def split_header_title(
    title: str, preferred_tail: str = "", max_width: int = 780
) -> list[str]:
    """短标题保持单行；其余标题沿词边界拆分，并尽量单列高亮尾句。"""
    if len(title) <= 6 and hei_w6(66).getlength(title) <= max_width:
        return [title]

    import jieba

    boundaries: set[int] = set()
    cursor = 0
    for token in jieba.cut(title):
        cursor += len(token)
        if 2 <= cursor <= len(title) - 2:
            boundaries.add(cursor)
    boundaries.update(
        match.end()
        for match in re.finditer(r"[，。？！；：]", title)
        if 2 <= match.end() <= len(title) - 2
    )
    if not boundaries:
        boundaries.add(max(1, len(title) // 2))

    preferred_start = title.rfind(preferred_tail) if preferred_tail else -1
    if preferred_start in boundaries:
        left = title[:preferred_start].rstrip("，。？！；： ")
        right = title[preferred_start:].lstrip("，。？！；： ")
        if left and right:
            return [left, right]

    def candidate(boundary: int) -> tuple[float, str, str]:
        left = title[:boundary].rstrip("，。？！；： ")
        right = title[boundary:].lstrip("，。？！；： ")
        left_width = hei_w6(66).getlength(left)
        right_width = hei_w6(58).getlength(right)
        overflow = max(left_width - max_width, 0) + max(right_width - max_width, 0)
        orphan_penalty = 180 if left[-1:] in "的地得和与及或" or right[:1] in "的地得和与及或" else 0
        score = overflow * 8 + abs(left_width - right_width) + orphan_penalty
        return score, left, right

    _, left, right = min(candidate(boundary) for boundary in boundaries)
    return [left, right]


def truncate_to_width(
    text: str, fnt: ImageFont.FreeTypeFont, max_width: int
) -> str:
    """极长标题在最小字号仍放不下时安全截断。"""
    if fnt.getlength(text) <= max_width:
        return text
    shortened = text
    while shortened and fnt.getlength(shortened + "…") > max_width:
        shortened = shortened[:-1]
    return shortened + "…"


def draw_tag(
    d: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    fill: tuple[int, int, int],
    *,
    text_fill: tuple[int, int, int] = (255, 255, 255),
    size: int = 34,
) -> None:
    d.rounded_rectangle(box, radius=20, fill=fill)
    fnt = hei_w6(size)
    d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), text, font=fnt, fill=text_fill, anchor="mm")


def draw_kc_header(img: Image.Image) -> None:
    """绘制与仓库主 KC 模板一致的深色标题、标签和霓虹边框。"""
    d = ImageDraw.Draw(img)
    for y in range(0, PANEL_TOP):
        t = y / max(PANEL_TOP - 1, 1)
        color = tuple(round(KC_BG_BOTTOM[i] * (1 - t) + KC_BG_TOP[i] * t) for i in range(3))
        d.line((0, y, W, y), fill=color)

    draw_tag(d, (46, 54, 300, 118), "网友热议", KC_MAGENTA)
    draw_tag(d, (792, 48, 1012, 116), "HOT", KC_YELLOW, text_fill=(10, 10, 18), size=42)
    draw_tag(d, (792, 116, 1012, 174), "热议中", KC_MAGENTA, size=30)

    title = clean_text(NOTE["title"])
    title_hits = pick_highlights(title, fallback=True)
    lines = split_header_title(title, title_hits[0] if title_hits else "")
    layout = [(lines[0], 270, 66)] if len(lines) == 1 else [
        (lines[0], 218, 66),
        (lines[1], 310, 58),
    ]
    for line, y, start_size in layout:
        size = start_size
        while size > 42 and hei_w6(size).getlength(line) > 780:
            size -= 2
        fnt = hei_w6(size)
        line = truncate_to_width(line, fnt, 780)
        line_hits = [term for term in title_hits if term in line]
        highlights = [max(line_hits, key=len)] if line_hits else []
        centered_text(
            d,
            y,
            highlight_parts(line, highlights, (255, 255, 255)),
            fnt,
            stroke_width=4,
            stroke_fill=(0, 0, 0),
        )

    d.rectangle((0, PANEL_TOP - 10, 242, PANEL_TOP), fill=KC_MAGENTA)
    d.rectangle((242, PANEL_TOP - 4, 844, PANEL_TOP), fill=KC_YELLOW)
    d.rectangle((844, PANEL_TOP - 10, W, PANEL_TOP), fill=KC_CYAN)


def draw_kc_footer(img: Image.Image) -> None:
    """绘制仓库统一的 KC娱乐底部品牌区，替代示例中的人物贴纸。"""
    d = ImageDraw.Draw(img)
    footer_h = H - FOOTER_TOP
    for y in range(FOOTER_TOP, H):
        t = (y - FOOTER_TOP) / max(footer_h - 1, 1)
        color = tuple(round(KC_BG_TOP[i] * (1 - t) + KC_BG_BOTTOM[i] * t) for i in range(3))
        d.line((0, y, W, y), fill=color)

    # 与仓库主模板一致的霓虹分隔线、栏目条、CTA 和中英品牌名。
    d.rectangle((0, FOOTER_TOP, 242, FOOTER_TOP + 10), fill=KC_MAGENTA)
    d.rectangle((242, FOOTER_TOP, 844, FOOTER_TOP + 3), fill=KC_YELLOW)
    d.rectangle((844, FOOTER_TOP, W, FOOTER_TOP + 10), fill=KC_CYAN)

    ribbon = (244, 1418, 836, 1484)
    d.rounded_rectangle((ribbon[0] + 8, ribbon[1] + 8, ribbon[2] + 8, ribbon[3] + 8), radius=28, fill=(0, 0, 0))
    d.rounded_rectangle(ribbon, radius=28, fill=KC_MAGENTA)
    centered_text(
        d,
        1450,
        [("网友热议  ·  热门观点", (255, 255, 255))],
        hei_w6(34),
        stroke_width=2,
        stroke_fill=(110, 0, 45),
    )

    centered_text(
        d,
        1572,
        [("喜欢记得点", (255, 255, 255)), ("关注", KC_YELLOW)],
        hei_w6(58),
        stroke_width=4,
        stroke_fill=(0, 0, 0),
    )

    d.rectangle((236, 1700, 318, 1710), fill=KC_MAGENTA)
    d.rectangle((762, 1700, 844, 1710), fill=KC_CYAN)
    centered_text(
        d,
        1774,
        [("KC", KC_YELLOW), ("娱乐", (255, 255, 255))],
        hei_w6(70),
        stroke_width=4,
        stroke_fill=(0, 0, 0),
    )
    centered_text(
        d,
        1855,
        [("ENTERTAINMENT", KC_CYAN)],
        font(FONT_LATIN, 23),
        stroke_width=1,
        stroke_fill=(0, 0, 0),
    )


def build_template() -> Image.Image:
    img = Image.new("RGB", (W, H), KC_BG_BOTTOM)
    d = ImageDraw.Draw(img)
    d.rectangle([0, PANEL_TOP, W, PANEL_BOTTOM], fill=(255, 255, 255))
    draw_kc_header(img)
    draw_kc_footer(img)
    return img


def panel_cover() -> Image.Image:
    """封面截图段: 等宽缩放后截取带贴纸的核心区域。"""
    cover_path = WORK / "cover.png"
    if not cover_path.is_file():
        cover_path = WORK / "cover.webp"
    cover = Image.open(cover_path).convert("RGB")
    scale = W / cover.width
    scaled = cover.resize((W, int(cover.height * scale)), Image.LANCZOS)
    crop_top = int(460 * scale)
    crop_top = min(crop_top, max(scaled.height - PANEL_H, 0))
    return scaled.crop((0, crop_top, W, crop_top + PANEL_H))


def panel_comment(comment: dict, idx: int) -> Image.Image:
    img = Image.new("RGB", (W, PANEL_H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    text = clean_text(comment["text"])
    max_w = 1020 - 205
    tail_h = 68 + (108 if comment.get("sub_comment_count") else 0)
    size = 58
    while size > 38:
        fnt = hei_w3(size)
        lines = wrap_cjk(text, fnt, max_w)
        text_h = len(lines) * int(size * 1.42)
        if 150 + text_h + tail_h + 24 <= CARD_SAFE_H:
            break
        size -= 4
    fnt = hei_w3(size)
    lines = wrap_cjk(text, fnt, max_w)
    line_h = int(size * 1.42)
    content_h = 150 + len(lines) * line_h + tail_h
    top = max((CARD_SAFE_H - content_h) // 2, 24)

    # 使用流水线生成的匿名头像和昵称，不展示原平台身份。
    nickname = str(comment.get("nickname") or "网友")
    draw_identity_avatar(
        img,
        (62, top, 62 + 130, top + 130),
        comment,
        seed=nickname + str(idx),
    )
    draw_identity_name(d, (215, top + 42), comment, max_width=420, size=38)

    ty = top + 150
    for line in lines:
        d.text((205, ty), line, font=fnt, fill=(20, 20, 20))
        ty += line_h

    meta_f = hei_w3(40)
    ty += 18
    meta = f"{hours_ago(int(comment['time']))} · {IP_MAP.get(comment['ip_location'], comment['ip_location'])}"
    d.text((205, ty), meta, font=meta_f, fill=GRAY)
    reply_x = 205 + meta_f.getlength(meta) + 55
    d.text((reply_x, ty), "回复", font=meta_f, fill=(120, 120, 120))

    like_txt = str(comment["like_count"])
    like_f = hei_w3(44)
    lx = 1005 - like_f.getlength(like_txt)
    d.text((lx, ty - 4), like_txt, font=like_f, fill=GRAY)
    draw_heart(d, int(lx) - 42, ty + 20, 40, (150, 150, 150))

    if comment.get("sub_comment_count"):
        ty += 88
        exp_f = hei_w3(38)
        d.line([(205, ty + 22), (275, ty + 22)], fill=(210, 210, 210), width=2)
        exp_txt = f"展开{comment['sub_comment_count']}条回复"
        d.text((295, ty), exp_txt, font=exp_f, fill=(110, 110, 110))
        cx = 295 + exp_f.getlength(exp_txt) + 34
        d.line([(cx - 12, ty + 16), (cx, ty + 28), (cx + 12, ty + 16)], fill=(110, 110, 110), width=4, joint="curve")
    return img


def panel_subcomment(parent: dict, reply: dict, idx: int) -> Image.Image:
    """画一张带父评论上下文的缩进子评论卡。"""
    img = Image.new("RGB", (W, PANEL_H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([54, 30, 350, 102], radius=24, fill=(255, 235, 244))
    d.text((82, 45), "网友回复", font=hei_w6(36), fill=KC_MAGENTA)

    # 上方保留父评论摘要，让“回复谁”一眼可见。
    d.rounded_rectangle([54, 126, 1026, 350], radius=30, fill=(246, 247, 249))
    d.text((82, 146), "上一条观点", font=hei_w6(30), fill=(120, 120, 120))
    parent_text = clean_text(parent["text"])
    parent_f = hei_w3(34)
    parent_lines_all = wrap_cjk(parent_text, parent_f, 890)
    parent_lines = parent_lines_all[:3]
    if len(parent_lines_all) > 3 and parent_lines[-1]:
        parent_lines[-1] = parent_lines[-1][:-1] + "…"
    py = 198
    for line in parent_lines:
        d.text((82, py), line, font=parent_f, fill=(105, 105, 105))
        py += 46

    # 缩进线 + 子评论身份区。
    d.line([(104, 372), (104, 708)], fill=(225, 225, 225), width=5)
    d.ellipse([93, 374, 115, 396], fill=KC_CYAN)
    nickname = str(reply.get("nickname") or "网友")
    draw_identity_avatar(
        img,
        (132, 382, 132 + 108, 382 + 108),
        reply,
        seed=nickname + str(idx),
    )
    draw_identity_name(d, (264, 410), reply, max_width=400, size=36)

    reply_text = clean_text(reply["text"])
    size = 56
    max_w = 1026 - 264
    while size > 40:
        reply_f = hei_w3(size)
        reply_lines = wrap_cjk(reply_text, reply_f, max_w)
        if len(reply_lines) <= 3:
            break
        size -= 4
    reply_f = hei_w3(size)
    reply_lines = wrap_cjk(reply_text, reply_f, max_w)
    ty = 514
    for line in reply_lines:
        d.text((264, ty), line, font=reply_f, fill=(20, 20, 20))
        ty += int(size * 1.42)

    meta_f = hei_w3(38)
    ty += 20
    location = IP_MAP.get(reply["ip_location"], reply["ip_location"])
    meta = f"{hours_ago(int(reply['time']))} · {location}" if location else hours_ago(int(reply["time"]))
    d.text((264, ty), meta, font=meta_f, fill=GRAY)
    d.text((264 + meta_f.getlength(meta) + 48, ty), "回复", font=meta_f, fill=(120, 120, 120))

    like_txt = str(reply["like_count"])
    like_f = hei_w3(42)
    lx = 1005 - like_f.getlength(like_txt)
    d.text((lx, ty - 3), like_txt, font=like_f, fill=GRAY)
    draw_heart(d, int(lx) - 40, ty + 18, 37, (150, 150, 150))
    return img


def compose_page(template: Image.Image, panel: Image.Image, subtitle: str, *, blurred: bool = False) -> Image.Image:
    img = template.copy()
    if blurred:
        panel = panel.filter(ImageFilter.GaussianBlur(REVEAL_BLUR))
    img.paste(panel, (0, PANEL_TOP))
    d = ImageDraw.Draw(img)
    size = 56
    while size > 40 and hei_w6(size).getlength(subtitle) > 980:
        size -= 2
    centered_text(
        d,
        SUBTITLE_Y,
        highlight_parts(subtitle, pick_highlights(subtitle), (255, 255, 255)),
        hei_w6(size),
        stroke_width=7,
        stroke_fill=(0, 0, 0),
    )
    return img


def interpolate_keyframes(index: int, keyframes: list[tuple[int, float]]) -> float:
    """在少量视觉关键帧间做线性插值。"""
    if index <= keyframes[0][0]:
        return keyframes[0][1]
    for (left_i, left_v), (right_i, right_v) in zip(keyframes, keyframes[1:]):
        if index <= right_i:
            span = max(right_i - left_i, 1)
            return left_v + (right_v - left_v) * (index - left_i) / span
    return keyframes[-1][1]


def draw_fire_tail(
    index: int, strength: float, reveal_radius: float | None
) -> Image.Image:
    """生成固定粒子轨迹的整面余焰；只让火苗运动，避免逐帧随机闪烁。"""
    tail = Image.new("RGBA", (W, PANEL_H), (0, 0, 0, 0))
    td = ImageDraw.Draw(tail, "RGBA")
    heat_alpha = round(30 * strength)
    td.rectangle((0, 0, W, PANEL_H), fill=(255, 70, 0, heat_alpha))

    for flame_index in range(132):
        rng = random.Random(73129 + flame_index)
        base_x = rng.uniform(-80, W + 80)
        base_y = rng.uniform(-100, PANEL_H + 120)
        speed = rng.uniform(2.0, 7.2)
        sway = rng.uniform(5, 34)
        phase = rng.uniform(0, math.tau)
        radius_x = rng.uniform(24, 92)
        radius_y = radius_x * rng.uniform(0.65, 1.55)
        x = base_x + math.sin(phase + index * 0.23) * sway
        y = (base_y - index * speed + 120) % (PANEL_H + 240) - 120
        green = rng.randint(48, 178)
        particle_alpha = round(rng.randint(70, 190) * strength)
        td.ellipse(
            (x - radius_x, y - radius_y, x + radius_x, y + radius_y),
            fill=(255, green, 0, particle_alpha),
        )
        if flame_index % 4 == 0:
            core_x = radius_x * 0.42
            core_y = radius_y * 0.45
            td.ellipse(
                (x - core_x, y - core_y, x + core_x, y + core_y),
                fill=(255, 222, 45, round(particle_alpha * 0.78)),
            )

    # 爆开的前三帧只显示火圈以内，0.13 秒后覆盖整张阅读面板。
    if reveal_radius is not None:
        mask = Image.new("L", (W, PANEL_H), 0)
        md = ImageDraw.Draw(mask)
        cx, cy = W // 2, PANEL_H // 2
        md.ellipse(
            (cx - reveal_radius, cy - reveal_radius,
             cx + reveal_radius, cy + reveal_radius),
            fill=255,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(20))
        tail.putalpha(ImageChops.multiply(tail.getchannel("A"), mask))

    soft = tail.filter(ImageFilter.GaussianBlur(26))
    soft.alpha_composite(tail)
    return soft


def build_procedural_fire_transition(
    template: Image.Image,
    from_panel: Image.Image,
    subtitle: str,
) -> Path:
    """样例不可用时的程序火焰回退。"""
    from_img = compose_page(template, from_panel, subtitle)
    frame_count = round(FIRE_T * FPS)
    start_radius, end_radius = 100.0, 570.0
    local_cy = PANEL_H // 2
    alpha_keys = [
        (0, 0.35), (1, 0.35), (2, 0.80), (3, 1.00), (9, 1.00),
        (12, 0.90), (16, 0.68), (20, 0.38), (frame_count - 1, 0.25),
    ]

    for index in range(frame_count):
        if index <= 1:
            radius = start_radius
        else:
            grow = min((index - 1) / max(FIRE_GROW_FRAMES - 2, 1), 1.0)
            eased = 1 - (1 - grow) ** 2
            radius = start_radius + (end_radius - start_radius) * eased
        strength = interpolate_keyframes(index, alpha_keys)

        # 整个 0.8 秒都以旧画面为底，和样例一样在结束处直接切评论卡。
        frame = from_img.convert("RGBA")
        fire = Image.new("RGBA", (W, PANEL_H), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fire, "RGBA")
        box = (W / 2 - radius, local_cy - radius, W / 2 + radius, local_cy + radius)
        fd.ellipse(box, outline=(255, 58, 0, round(220 * strength)), width=54)
        inner = min(18, max(2, int(radius * 0.22)))
        fd.ellipse(
            (box[0] + inner, box[1] + inner, box[2] - inner, box[3] - inner),
            outline=(255, 226, 38, round(245 * strength)),
            width=24,
        )
        for flame_index in range(30):
            rng = random.Random(90210 + flame_index)
            angle = (
                flame_index * math.tau / 30
                + rng.uniform(-0.05, 0.05)
                + index * rng.uniform(-0.018, 0.018)
            )
            fx = W / 2 + math.cos(angle) * radius
            fy = local_cy + math.sin(angle) * radius
            flame = rng.randint(18, 58) * (0.90 + 0.12 * math.sin(index * 0.4 + flame_index))
            fd.ellipse(
                (fx - flame, fy - flame * 0.7, fx + flame, fy + flame * 0.7),
                fill=(
                    255,
                    rng.randint(65, 165),
                    0,
                    round(rng.randint(100, 205) * strength),
                ),
            )

        if index >= 2:
            reveal_radius = radius + 80 if index < FIRE_GROW_FRAMES - 1 else None
            tail = draw_fire_tail(index, strength, reveal_radius)
            frame.alpha_composite(tail, (0, PANEL_TOP))
        glow = fire.filter(ImageFilter.GaussianBlur(28))
        frame.alpha_composite(glow, (0, PANEL_TOP))
        frame.alpha_composite(fire, (0, PANEL_TOP))
        frame.convert("RGB").save(RENDER / f"fire_{index:03d}.png")

    output = RENDER / "fire_transition.mp4"
    run([
        "ffmpeg", "-y", "-v", "error", "-framerate", str(FPS),
        "-i", str(RENDER / "fire_%03d.png"),
        "-c:v", "libx264", "-preset", "medium", "-crf", "17",
        "-pix_fmt", "yuv420p", str(output),
    ])
    return output


def extract_reference_fire_frames() -> list[Path]:
    """按帧号抽取样例底板和 24 帧火焰，避免 seek 带来的半帧偏移。"""
    if not SAMPLE.is_file():
        raise FileNotFoundError(f"缺少火焰样例：{SAMPLE}")

    raw_pattern = RENDER / "reference_fire_raw_%03d.png"
    for old_frame in RENDER.glob("reference_fire_raw_*.png"):
        old_frame.unlink()
    try:
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(SAMPLE),
            "-vf",
            (
                "select='between(n,"
                f"{REFERENCE_FIRE_BASE_FRAME},{REFERENCE_FIRE_LAST_FRAME})'"
            ),
            "-vsync", "0", str(raw_pattern),
        ])
        frames = sorted(RENDER.glob("reference_fire_raw_*.png"))
        expected = REFERENCE_FIRE_LAST_FRAME - REFERENCE_FIRE_BASE_FRAME + 1
        if len(frames) != expected:
            raise RuntimeError(f"样例火焰应抽取 {expected} 帧，实际得到 {len(frames)} 帧")
        return frames
    except Exception:
        for partial_frame in RENDER.glob("reference_fire_raw_*.png"):
            partial_frame.unlink()
        raise


def build_reference_fire_transition(
    template: Image.Image,
    from_panel: Image.Image,
    subtitle: str,
) -> Path:
    """把样例的真实爆燃逐帧抠出，覆盖在本次封面上。"""
    if np is None:
        raise RuntimeError("当前 Python 缺少 numpy")

    raw_frames = extract_reference_fire_frames()
    try:
        frame_count = round(FIRE_T * FPS)
        if len(raw_frames) - 1 != frame_count:
            raise RuntimeError(
                f"火焰持续帧数不匹配：需要 {frame_count}，得到 {len(raw_frames) - 1}"
            )

        from_img = compose_page(template, from_panel, subtitle).convert("RGB")
        from_array = np.asarray(from_img, dtype=np.uint8)
        new_panel = np.asarray(
            from_img.crop((0, PANEL_TOP, W, PANEL_BOTTOM)),
            dtype=np.float32,
        )

        # 原样例的正文和字幕已经烙在火焰下面。修补整个文字带，而不是只修
        # 字形轮廓；否则 alpha 中会留下可辨认的汉字阴影。
        repair_mask_img = Image.new("L", (W, PANEL_H), 0)
        repair_draw = ImageDraw.Draw(repair_mask_img)
        for box in REFERENCE_TEXT_BANDS:
            repair_draw.rectangle(box, fill=255)
        repair_mask_img = repair_mask_img.filter(ImageFilter.GaussianBlur(14))
        repair_mask = np.asarray(repair_mask_img, dtype=np.float32) / 255.0

        # 从同一半径、不同角度取真实火焰纹理做中位数补图。无效旋转坐标
        # 使用 NaN 并在中位数时忽略，防止面板四角出现白色三角缺口。
        yy, xx = np.indices((PANEL_H, W), dtype=np.float32)
        cx, cy = (W - 1) / 2.0, (PANEL_H - 1) / 2.0
        rx, ry = xx - cx, yy - cy
        rotation_maps = []
        for angle_deg in REFERENCE_REPAIR_ANGLES:
            angle = math.radians(angle_deg)
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            source_x = np.rint(cx + cos_a * rx - sin_a * ry).astype(np.int32)
            source_y = np.rint(cy + sin_a * rx + cos_a * ry).astype(np.int32)
            valid = (
                (source_x >= 0) & (source_x < W)
                & (source_y >= 0) & (source_y < PANEL_H)
            )
            rotation_maps.append((source_x, source_y, valid))

        reference_crop = REFERENCE_PANEL_BOX
        for index, raw_path in enumerate(raw_frames[1:]):
            with Image.open(raw_path) as raw_image:
                if raw_image.width < reference_crop[2] or raw_image.height < reference_crop[3]:
                    raise RuntimeError(
                        f"样例画面尺寸过小：{raw_image.width}x{raw_image.height}"
                    )
                source_panel_img = (
                    raw_image.convert("RGB")
                    .crop(reference_crop)
                    .resize((W, PANEL_H), Image.Resampling.LANCZOS)
                )
            source_panel = np.asarray(source_panel_img, dtype=np.float32)

            donors = [source_panel]
            for source_x, source_y, valid in rotation_maps:
                donor = np.full(source_panel.shape, np.nan, dtype=np.float32)
                donor[valid] = source_panel[source_y[valid], source_x[valid]]
                donors.append(donor)
            repaired_texture = np.nanmedian(np.stack(donors, axis=0), axis=0)
            repaired_panel = (
                source_panel * (1.0 - repair_mask[..., None])
                + repaired_texture * repair_mask[..., None]
            )

            # 样例阅读区是白底。蓝通道吸收量给出火焰透明度上界，再用红黄
            # 色度滤掉 H.264 灰噪；最后反解前景色并合成到当前封面。
            blue_absorption = np.clip((255.0 - repaired_panel[..., 2]) / 255.0, 0.0, 1.0)
            warm_gate = np.clip(
                (repaired_panel[..., 0] - repaired_panel[..., 2] - 2.0) / 20.0,
                0.0,
                1.0,
            )
            yellow_gate = np.clip(
                (repaired_panel[..., 1] - repaired_panel[..., 2] - 1.0) / 16.0,
                0.0,
                1.0,
            )
            alpha = blue_absorption * np.maximum(warm_gate, yellow_gate)
            alpha = np.clip((alpha - 0.010) / 0.990, 0.0, 1.0)
            alpha_img = Image.fromarray(np.uint8(np.rint(alpha * 255.0)))
            alpha_img = alpha_img.filter(ImageFilter.GaussianBlur(0.6))
            alpha = np.asarray(alpha_img, dtype=np.float32) / 255.0

            denominator = np.maximum(alpha[..., None], 0.03)
            foreground = np.clip(
                (repaired_panel - (1.0 - alpha[..., None]) * 255.0) / denominator,
                0.0,
                255.0,
            )
            composited_panel = np.clip(
                alpha[..., None] * foreground
                + (1.0 - alpha[..., None]) * new_panel,
                0.0,
                255.0,
            ).astype(np.uint8)

            # 只替换阅读面板，标题和底栏始终沿用当前视频；样例背景不会泄漏。
            frame_array = from_array.copy()
            frame_array[PANEL_TOP:PANEL_BOTTOM] = composited_panel
            Image.fromarray(frame_array).save(RENDER / f"fire_{index:03d}.png")

        output = RENDER / "fire_transition.mp4"
        run([
            "ffmpeg", "-y", "-v", "error", "-framerate", str(FPS),
            "-i", str(RENDER / "fire_%03d.png"),
            "-c:v", "libx264", "-preset", "medium", "-crf", "17",
            "-pix_fmt", "yuv420p", str(output),
        ])
        print(
            "[fire] reference overlay "
            f"frames {REFERENCE_FIRE_FIRST_FRAME}-{REFERENCE_FIRE_LAST_FRAME}"
        )
        return output
    finally:
        for raw_frame in raw_frames:
            raw_frame.unlink(missing_ok=True)


def build_fire_transition(
    template: Image.Image,
    from_panel: Image.Image,
    subtitle: str,
) -> Path:
    """优先使用样例真实火焰；素材或依赖不可用时自动回退。"""
    try:
        return build_reference_fire_transition(template, from_panel, subtitle)
    except Exception as exc:
        print(f"[fire] reference overlay unavailable, use procedural fallback: {exc}")
        return build_procedural_fire_transition(template, from_panel, subtitle)


def extract_reference_sfx() -> tuple[Path, Path]:
    """从用户给的示例中截取火焰爆开声和相机快门声。"""
    fire_sfx = RENDER / "fire_boom.wav"
    camera_sfx = RENDER / "camera_shutter.wav"
    if SAMPLE.is_file():
        try:
            run([
                "ffmpeg", "-y", "-v", "error", "-ss", "2.75", "-t", f"{FIRE_T:.2f}",
                "-i", str(SAMPLE), "-vn", "-ar", "44100", "-ac", "2", str(fire_sfx),
            ])
            run([
                "ffmpeg", "-y", "-v", "error", "-ss", "6.02", "-t", "0.28",
                "-i", str(SAMPLE), "-vn", "-ar", "44100", "-ac", "2", str(camera_sfx),
            ])
            return fire_sfx, camera_sfx
        except Exception as exc:  # noqa: BLE001
            print(f"[sfx] reference extraction unavailable, synthesize locally: {exc}")

    def write_wav(path: Path, duration: float, kind: str) -> None:
        sample_rate = 44_100
        frame_count = round(duration * sample_rate)
        rng = random.Random(731 if kind == "fire" else 947)
        frames = bytearray()
        for index in range(frame_count):
            t = index / sample_rate
            if kind == "fire":
                envelope = math.exp(-4.2 * t)
                rumble = math.sin(math.tau * (64 + 26 * t) * t) * 0.46
                noise = (rng.random() * 2 - 1) * 0.34
                crackle = (rng.random() * 2 - 1) * 0.28 if rng.random() < 0.025 else 0.0
                value = envelope * (rumble + noise + crackle)
            else:
                click = math.exp(-95 * t) * (rng.random() * 2 - 1)
                second_t = max(t - 0.075, 0.0)
                second = (
                    math.exp(-135 * second_t) * (rng.random() * 2 - 1) * 0.65
                    if t >= 0.075
                    else 0.0
                )
                value = click + second
            sample = max(-32760, min(32760, round(value * 23_000)))
            frames.extend(struct.pack("<hh", sample, sample))
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(frames)

    write_wav(fire_sfx, FIRE_T, "fire")
    write_wav(camera_sfx, 0.28, "camera")
    print("[sfx] generated portable fire/camera effects")
    return fire_sfx, camera_sfx


def tts(text: str, out_wav: Path, speaker: str, tempo: float) -> float:
    def probe_duration(path: Path) -> float:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            check=True, capture_output=True, text=True,
        )
        duration = float(probe.stdout.strip())
        if duration <= 0.05:
            raise RuntimeError(f"audio is empty or too short: {path}")
        return duration

    if REUSE_TTS_CACHE and out_wav.is_file():
        try:
            duration = probe_duration(out_wav)
            print(f"[tts] reuse {out_wav.name}")
            return duration
        except Exception as exc:  # noqa: BLE001
            print(f"[tts] invalidate unreadable cache {out_wav.name}: {exc}")
            out_wav.unlink(missing_ok=True)

    if TTS_BACKEND == "jianying-machine":
        source = out_wav.with_suffix(".source.ogg")
        last_error: Exception | None = None
        for attempt in range(1, 4):
            source.unlink(missing_ok=True)
            out_wav.unlink(missing_ok=True)
            try:
                subprocess.run([
                    sys.executable, str(ROOT.parent / "tools" / "machine_voice_tts.py"),
                    "jianying", "--text", text,
                    "--speaker", speaker,
                    "--timeout", "60",
                    "--output", str(source),
                ], check=True)
                processing_filter = JIANYING_PROCESSING_FILTERS[TTS_PROCESSING]
                if TTS_PROCESSING == "character":
                    processing_filter = f"atempo={tempo:.4f},{processing_filter}"
                run([
                    "ffmpeg", "-y", "-v", "error", "-i", str(source),
                    "-af", processing_filter,
                    "-ar", "44100", "-ac", "2", str(out_wav),
                ])
                return probe_duration(out_wav)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                print(f"[tts] {speaker} attempt {attempt}/3 failed: {exc}")
                if attempt < 3:
                    time.sleep(1.5 * (2 ** (attempt - 1)))
        raise RuntimeError(f"TTS failed after 3 attempts for {speaker}: {last_error}")
    else:
        aiff = out_wav.with_suffix(".aiff")
        subprocess.run(
            ["say", "-v", "Tingting", "-r", str(TTS_RATE), "-o", str(aiff), text],
            check=True,
        )
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(aiff),
            "-ar", "44100", "-ac", "2", str(out_wav),
        ])
        aiff.unlink(missing_ok=True)
    return probe_duration(out_wav)


def frame_align(seconds: float) -> float:
    return math.ceil(seconds * FPS) / FPS


def main() -> None:
    global ALL_COMMENTS, COMMENTS, NOTE, RENDER, REUSE_TTS_CACHE
    global TTS_BACKEND, TTS_PROCESSING, TTS_SPEAKER, WORK
    args = parse_args()
    WORK = args.work_dir.expanduser().resolve()
    NOTE = json.loads((WORK / "chosen_note.json").read_text(encoding="utf-8"))
    ALL_COMMENTS = json.loads((WORK / "top_comments.json").read_text(encoding="utf-8"))
    COMMENTS = ALL_COMMENTS[:args.max_comments]
    render_dir = args.render_dir or WORK / "render"
    RENDER = render_dir.expanduser().resolve()
    RENDER.mkdir(parents=True, exist_ok=True)
    TTS_BACKEND = args.voice
    TTS_SPEAKER = args.speaker
    TTS_PROCESSING = args.jianying_processing
    REUSE_TTS_CACHE = args.reuse_tts_cache

    template = build_template()
    template.save(RENDER / "template.png")

    # 段落: [封面, 评论1, 可选子评论1, 评论2, 可选子评论2...], 每段多字幕页。
    segments: list[dict] = []
    title_clean = clean_text(NOTE["title"])
    segments.append({"panel": panel_cover(), "pages": split_pages(title_clean)})
    replies = load_preview_subcomments(COMMENTS) if args.include_subcomments else [None] * len(COMMENTS)
    for i, c in enumerate(COMMENTS):
        segments.append({"panel": panel_comment(c, i), "pages": split_pages(clean_text(c["text"]))})
        reply = replies[i]
        if reply is not None:
            reply_voiceover = f"网友回复，{clean_text(reply['text'])}"
            segments.append({
                "panel": panel_subcomment(c, reply, i),
                "pages": split_pages(reply_voiceover),
            })

    if args.segment_speakers:
        if len(args.segment_speakers) != len(segments):
            raise SystemExit(
                "--segment-speaker 数量必须与实际段落数一致："
                f"当前需要 {len(segments)} 个，收到 {len(args.segment_speakers)} 个"
            )
        segment_speakers = args.segment_speakers
    else:
        segment_speakers = [TTS_SPEAKER] * len(segments)
    if args.segment_tempos:
        if len(args.segment_tempos) != len(segments):
            raise SystemExit(
                "--segment-tempo 数量必须与实际段落数一致："
                f"当前需要 {len(segments)} 个，收到 {len(args.segment_tempos)} 个"
            )
        segment_tempos = args.segment_tempos
    else:
        default_tempo = DEFAULT_CHARACTER_TEMPO if TTS_PROCESSING == "character" else 1.0
        segment_tempos = [default_tempo] * len(segments)
    for index, (segment, speaker, tempo) in enumerate(
        zip(segments, segment_speakers, segment_tempos)
    ):
        segment["speaker"] = speaker
        segment["tempo"] = tempo
        print(f"[voice] s{index} {speaker} tempo={tempo:.2f}")

    # 逐页: 生成清晰/模糊双帧 + 1.5x TTS。视频阶段在两帧间做纵向渐进揭示。
    page_infos = []
    for si, seg in enumerate(segments):
        page_count = len(seg["pages"])
        speaker = seg["speaker"]
        tempo = seg["tempo"]
        cache_key = hashlib.sha256(
            f"{speaker}|{TTS_PROCESSING}|{tempo:.4f}".encode("utf-8")
        ).hexdigest()[:10]
        for pi, page_text in enumerate(seg["pages"]):
            tag = f"s{si}p{pi}"
            audio_tag = (
                f"{tag}_{cache_key}"
                if args.segment_speakers or args.segment_tempos
                else tag
            )
            wav = RENDER / f"{audio_tag}.wav"
            dur = tts(page_text, wav, speaker, tempo)
            still = compose_page(template, seg["panel"], page_text)
            # 示例只有封面两句话按阅读进度由糊变清；评论卡出现后始终保持清晰。
            blur_still = (
                compose_page(template, seg["panel"], page_text, blurred=True)
                if si == 0
                else still.copy()
            )
            png = RENDER / f"{tag}.png"
            blur_png = RENDER / f"{tag}_blur.png"
            still.save(png)
            blur_still.save(blur_png)
            page_dur = frame_align(dur + PAGE_PAD)
            start_fraction = 0.18 + 0.82 * pi / page_count
            end_fraction = 0.18 + 0.82 * (pi + 1) / page_count
            page_infos.append(
                {
                    "seg": si,
                    "png": png,
                    "blur_png": blur_png,
                    "wav": wav,
                    "tts_dur": dur,
                    "dur": page_dur,
                    "text": page_text,
                    "reveal_start": round(PANEL_TOP + PANEL_H * start_fraction),
                    "reveal_end": round(PANEL_TOP + PANEL_H * end_fraction),
                }
            )
            print(
                f"[page] {tag} {speaker}@{tempo:.2f} "
                f"{dur:.2f}s -> {page_dur:.2f}s | "
                f"{page_text}"
            )
        page_infos[-1]["dur"] = frame_align(page_infos[-1]["dur"] + SEGMENT_PAD)

    # 每段: 模糊底图与清晰图按 T 时间变量混合，阅读区从上往下连续解锁。
    seg_files, seg_durs = [], []
    for si in range(len(segments)):
        pages = [p for p in page_infos if p["seg"] == si]
        cmd = ["ffmpeg", "-y", "-v", "error"]
        for p in pages:
            cmd += ["-loop", "1", "-t", f"{p['dur']:.4f}", "-i", str(p["blur_png"])]
            cmd += ["-loop", "1", "-t", f"{p['dur']:.4f}", "-i", str(p["png"])]
        # 评论朗读结束后，用最后一帧做一次快速中心放大；封面段不放大。
        if si > 0:
            cmd += ["-i", str(pages[-1]["png"])]
        n = len(pages)
        fc_lines = []
        page_labels = []
        for pi, p in enumerate(pages):
            start_y = p["reveal_start"]
            travel = p["reveal_end"] - start_y
            boundary = f"{start_y}+{travel}*min(T/{p['dur']:.4f},1)"
            # 直接移动清晰/模糊分界。连续 30fps 推进本身已经平滑，避免像素算术
            # 在分界处生成灰黑色伪影。
            expr = f"if(lte(Y,{boundary}),B,A)"
            label = f"[p{pi}]"
            blur_label = f"[b{pi}]"
            sharp_label = f"[s{pi}]"
            fc_lines.append(f"[{pi * 2}:v]format=gbrp{blur_label}")
            fc_lines.append(f"[{pi * 2 + 1}:v]format=gbrp{sharp_label}")
            fc_lines.append(
                f"{blur_label}{sharp_label}blend=all_expr='{expr}',"
                f"fps={FPS},format=yuv420p{label}"
            )
            page_labels.append(label)
        if si > 0:
            zoom_frames = round(ZOOM_T * FPS)
            zoom_input = n * 2
            fc_lines.append(
                f"[{zoom_input}:v]zoompan="
                f"z='1+0.14*on/{max(zoom_frames - 1, 1)}':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={zoom_frames}:s={W}x{H}:fps={FPS},"
                f"trim=duration={ZOOM_T:.4f},setpts=PTS-STARTPTS,format=yuv420p[z]"
            )
            page_labels.append("[z]")
        fc_lines.append(f"{''.join(page_labels)}concat=n={len(page_labels)}:v=1:a=0[v]")
        seg_mp4 = RENDER / f"seg{si}.mp4"
        cmd += ["-filter_complex", ";".join(fc_lines), "-map", "[v]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "17", str(seg_mp4)]
        run(cmd)
        seg_files.append(seg_mp4)
        seg_durs.append(sum(p["dur"] for p in pages) + (ZOOM_T if si > 0 else 0))
        print(f"[seg] seg{si}.mp4 dur={seg_durs[-1]:.2f}")

    # 示例节奏: 封面 -> 中心火焰爆开 -> 评论/子评论；每段末尾放大并打快门。
    fire_file = build_fire_transition(
        template,
        segments[0]["panel"],
        segments[0]["pages"][-1],
    )
    fire_sfx, camera_sfx = extract_reference_sfx()
    video_files = [seg_files[0], fire_file, *seg_files[1:]]
    video_durs = [seg_durs[0], FIRE_T, *seg_durs[1:]]
    total = sum(video_durs)

    seg_starts = [0.0 for _ in seg_files]
    seg_starts[1] = seg_durs[0] + FIRE_T
    for si in range(2, len(seg_files)):
        seg_starts[si] = seg_starts[si - 1] + seg_durs[si - 1]

    cmd = ["ffmpeg", "-y", "-v", "error"]
    for f in video_files:
        cmd += ["-i", str(f)]
    for p in page_infos:
        cmd += ["-i", str(p["wav"])]
    cmd += ["-i", str(fire_sfx)]
    for _ in range(len(seg_files) - 1):
        cmd += ["-i", str(camera_sfx)]

    fc_lines = [
        "".join(f"[{i}:v]" for i in range(len(video_files)))
        + f"concat=n={len(video_files)}:v=1:a=0,fps={FPS},format=yuv420p[vout]"
    ]

    page_offset_in_seg: dict[int, float] = {}
    amix_ins = []
    for i, p in enumerate(page_infos):
        idx = len(video_files) + i
        off = page_offset_in_seg.get(p["seg"], 0.0)
        abs_start = seg_starts[p["seg"]] + off + 0.08
        page_offset_in_seg[p["seg"]] = off + p["dur"]
        ms = int(abs_start * 1000)
        fc_lines.append(f"[{idx}:a]adelay={ms}|{ms}[a{i}]")
        amix_ins.append(f"[a{i}]")

    sfx_base = len(video_files) + len(page_infos)
    fire_ms = int(seg_durs[0] * 1000)
    fc_lines.append(f"[{sfx_base}:a]volume=0.90,adelay={fire_ms}|{fire_ms}[firea]")
    amix_ins.append("[firea]")
    for ci in range(1, len(seg_files)):
        camera_start = seg_starts[ci] + seg_durs[ci] - ZOOM_T + 0.04
        camera_ms = int(camera_start * 1000)
        label = f"[camera{ci}]"
        fc_lines.append(
            f"[{sfx_base + ci}:a]volume=1.10,adelay={camera_ms}|{camera_ms}{label}"
        )
        amix_ins.append(label)
    fc_lines.append(
        "".join(amix_ins)
        + f"amix=inputs={len(amix_ins)}:normalize=0:dropout_transition=0,"
        + f"alimiter=limit=0.78:level=disabled,apad,atrim=0:{total:.4f}[aout]"
    )
    final = (
        args.output.expanduser().resolve()
        if args.output
        else ROOT / f"KC娱乐_{re.sub(r'[，。？！、 ]', '', title_clean)}.mp4"
    )
    final.parent.mkdir(parents=True, exist_ok=True)
    cmd += [
        "-filter_complex", ";".join(fc_lines),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "17",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(final),
    ]
    run(cmd)
    print(f"[done] {final} total={total:.2f}s")


if __name__ == "__main__":
    main()
