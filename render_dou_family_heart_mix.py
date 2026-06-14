#!/usr/bin/env python3
"""Render a KC entertainment-style Dou family song comparison montage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import render_star_english_mix as base


base.WORK_DIR = Path("work/dou_family")
base.RENDER_DIR = base.WORK_DIR / "render"
base.CLIP_DIR = base.WORK_DIR / "clips"
base.OUTPUT = Path("KC娱乐_基因遗传的神奇像不像父亲母亲与女儿DontBreakMyHeart.mp4")
base.NAV_ITEMS = ["窦唯", "王菲", "窦靖童"]
base.TITLE_MAIN = "基因遗传太神奇"
base.TITLE_SUB = "像不像？"
base.LOWER_RIBBON = "同曲对照 · 像不像"
base.TITLE_HIGHLIGHTS = [
    "No.1",
    "No.2",
    "No.3",
    "窦唯",
    "王菲",
    "窦靖童",
    "父亲",
    "母亲",
    "女儿",
    "基因",
    "像不像",
    "Don't Break My Heart",
]
base.TITLE_MAIN_HIGHLIGHTS = ["基因", "神奇"]
base.TITLE_SUB_HIGHLIGHTS = ["像不像"]
base.TOP_TAG = "同曲名场面"
base.STICKER_TOPIC = "像吗"
base.NAV_X0 = 360
base.NAV_ITEM_W = 112
base.NAV_GAP = 10
base.NAV_FONT_SIZE = 22
base.MAIN_TOP_GRADIENT_H = 172
base.MAIN_TOP_GRADIENT_START_ALPHA = 176
base.MAIN_TOP_GRADIENT_END_ALPHA = 56
base.MAIN_TOP_MASK_H = 108
base.MAIN_TOP_MASK_ALPHA = 82


SEGMENTS: list[dict[str, Any]] = [
    {
        "kind": "card",
        "duration": 1.15,
        "title": "基因遗传太神奇",
        "subtitle": "父亲母亲与女儿同唱一首歌",
        "badge": "KC娱乐",
        "name": "intro",
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.1 窦唯",
        "subtitle": "父亲原版先来",
        "badge": "父亲",
        "name": "card_douwei",
    },
    {
        "kind": "clip",
        "name": "douwei_original",
        "person": "No.1 窦唯",
        "source": Path("待混剪/窦唯.mp4"),
        "start": 154.0,
        "duration": 12.0,
        "crop": {"top": 202, "bottom": 230, "left": 70, "right": 190},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.4,
                "zh": "父亲原版一出来",
                "en": "The original version sets the tone first.",
                "zh_highlights": ["父亲", "原版"],
                "en_highlights": ["original"],
            },
            {
                "start": 3.4,
                "end": 7.4,
                "zh": "冷感摇滚的味道很明显",
                "en": "Cool, restrained, and unmistakably rock.",
                "zh_highlights": ["冷感", "摇滚"],
                "en_highlights": ["rock"],
            },
            {
                "start": 7.4,
                "end": 12.0,
                "zh": "这个松弛劲真的很难复制",
                "en": "That ease is hard to copy.",
                "zh_highlights": ["松弛", "复制"],
                "en_highlights": ["hard to copy"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.2 王菲",
        "subtitle": "母亲舞台版接上",
        "badge": "母亲",
        "name": "card_wangfei",
    },
    {
        "kind": "clip",
        "name": "wangfei_live",
        "person": "No.2 王菲",
        "source": Path("待混剪/王菲.mp4"),
        "start": 165.0,
        "duration": 12.0,
        "crop": {"top": 0, "bottom": 0, "left": 0, "right": 300},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.6,
                "zh": "母亲版本又是另一种轻盈",
                "en": "Her version turns it into something lighter.",
                "zh_highlights": ["母亲", "轻盈"],
                "en_highlights": ["lighter"],
            },
            {
                "start": 3.6,
                "end": 7.4,
                "zh": "开口就有王菲的辨识度",
                "en": "That voice is instantly recognizable.",
                "zh_highlights": ["王菲", "辨识度"],
                "en_highlights": ["recognizable"],
            },
            {
                "start": 7.4,
                "end": 12.0,
                "zh": "空灵里也带着一点冷感",
                "en": "Airy, but still cool around the edges.",
                "zh_highlights": ["空灵", "冷感"],
                "en_highlights": ["Airy", "cool"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.3 窦靖童",
        "subtitle": "女儿一开口就懂了",
        "badge": "女儿",
        "name": "card_doujingtong",
    },
    {
        "kind": "clip",
        "name": "doujingtong_cover",
        "person": "No.3 窦靖童",
        "source": Path("待混剪/窦靖童.mp4"),
        "start": 121.0,
        "duration": 12.0,
        "crop": {"top": 0, "bottom": 0, "left": 150, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.4,
                "zh": "女儿一接，DNA真的动了",
                "en": "Then the daughter comes in and the resemblance clicks.",
                "zh_highlights": ["女儿", "DNA"],
                "en_highlights": ["daughter", "resemblance"],
            },
            {
                "start": 3.4,
                "end": 7.3,
                "zh": "声线、表情、松弛感都能对上",
                "en": "The tone, face, and ease all line up.",
                "zh_highlights": ["声线", "松弛感"],
                "en_highlights": ["tone", "ease"],
            },
            {
                "start": 7.3,
                "end": 12.0,
                "zh": "但又完全是自己的味道",
                "en": "Still, she keeps her own color.",
                "zh_highlights": ["自己", "味道"],
                "en_highlights": ["own color"],
            },
        ],
    },
]


def main() -> None:
    base.SEGMENTS = SEGMENTS
    base.main()


if __name__ == "__main__":
    main()
