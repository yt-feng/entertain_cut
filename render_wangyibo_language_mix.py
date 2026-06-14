#!/usr/bin/env python3
"""Render a KC entertainment-style Wang Yibo multi-language montage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import render_star_english_mix as base


base.WORK_DIR = Path("work/wangyibo_languages")
base.RENDER_DIR = base.WORK_DIR / "render"
base.CLIP_DIR = base.WORK_DIR / "clips"
base.OUTPUT = Path("KC娱乐_日语韩语英语法语方言全能语言小天才王一博.mp4")
base.NAV_ITEMS = ["日语", "韩语", "英语", "法语", "方言"]
base.TITLE_MAIN = "五种语言名场面"
base.TITLE_SUB = "王一博全能小天才"
base.LOWER_RIBBON = "五种语言 · 王一博"
base.TITLE_HIGHLIGHTS = [
    "No.1",
    "No.2",
    "No.3",
    "No.4",
    "No.5",
    "日语",
    "韩语",
    "英语",
    "法语",
    "方言",
    "王一博",
    "语言",
]
base.TITLE_MAIN_HIGHLIGHTS = ["日语", "韩语", "英语", "法语", "方言"]
base.TITLE_SUB_HIGHLIGHTS = ["全能", "语言", "王一博"]
base.TOP_TAG = "语言名场面"
base.STICKER_TOPIC = "语言"
base.NAV_X0 = 344
base.NAV_ITEM_W = 76
base.NAV_GAP = 6
base.NAV_FONT_SIZE = 20
base.MAIN_TOP_GRADIENT_H = 172
base.MAIN_TOP_GRADIENT_START_ALPHA = 176
base.MAIN_TOP_GRADIENT_END_ALPHA = 56
base.MAIN_TOP_MASK_H = 108
base.MAIN_TOP_MASK_ALPHA = 82


SEGMENTS: list[dict[str, Any]] = [
    {
        "kind": "card",
        "duration": 1.15,
        "title": "全能语言小天才王一博",
        "subtitle": "日语 韩语 英语 法语 方言",
        "badge": "KC娱乐",
        "name": "intro",
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.1 日语",
        "subtitle": "日语台词一秒入戏",
        "badge": "第一种",
        "name": "card_japanese",
    },
    {
        "kind": "clip",
        "name": "wangyibo_japanese",
        "person": "No.1 日语",
        "source": Path("待混剪/王一博日语.mp4"),
        "start": 29.0,
        "duration": 8.6,
        "crop": {"top": 600, "bottom": 596, "left": 180, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.15,
                "zh": "日语台词一秒入戏",
                "en": "Japanese line delivery, straight into character.",
                "zh_highlights": ["日语", "入戏"],
                "en_highlights": ["Japanese"],
            },
            {
                "start": 3.15,
                "end": 6.25,
                "zh": "低声开口，压迫感就来了",
                "en": "The low voice sets the mood.",
                "zh_highlights": ["压迫感"],
                "en_highlights": ["low voice"],
            },
            {
                "start": 6.25,
                "end": 8.6,
                "zh": "这句台词真的有画面感",
                "en": "Take it off.",
                "zh_highlights": ["台词"],
                "en_highlights": ["Take it off"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.2 韩语",
        "subtitle": "采访回答很自然",
        "badge": "第二种",
        "name": "card_korean",
    },
    {
        "kind": "clip",
        "name": "wangyibo_korean",
        "person": "No.2 韩语",
        "source": Path("待混剪/王一博韩语.mp4"),
        "start": 36.0,
        "duration": 12.0,
        "crop": {"top": 0, "bottom": 90, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.05,
                "zh": "第一次来到巴西",
                "en": "First time coming to Brazil.",
                "zh_highlights": ["第一次", "巴西"],
                "en_highlights": ["First time"],
            },
            {
                "start": 3.05,
                "end": 5.35,
                "zh": "粉丝很亲切，真的感谢",
                "en": "The fans were so kind. Thank you.",
                "zh_highlights": ["粉丝", "感谢"],
                "en_highlights": ["fans", "Thank you"],
            },
            {
                "start": 5.35,
                "end": 8.45,
                "zh": "明天的演出非常期待",
                "en": "I'm really looking forward to tomorrow's show.",
                "zh_highlights": ["演出", "期待"],
                "en_highlights": ["looking forward"],
            },
            {
                "start": 8.45,
                "end": 12.0,
                "zh": "个人舞台也准备好了",
                "en": "There is also a solo stage prepared.",
                "zh_highlights": ["个人舞台"],
                "en_highlights": ["solo stage"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.3 英语",
        "subtitle": "英文旁白氛围感拉满",
        "badge": "第三种",
        "name": "card_english",
    },
    {
        "kind": "clip",
        "name": "wangyibo_english",
        "person": "No.3 英语",
        "source": Path("待混剪/王一博英语.mp4"),
        "start": 0.0,
        "duration": 14.2,
        "crop": {"top": 0, "bottom": 70, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 4.8,
                "zh": "想离开城市生活",
                "en": "I want to leave the city life behind.",
                "zh_highlights": ["城市生活"],
                "en_highlights": ["city life"],
            },
            {
                "start": 4.8,
                "end": 9.9,
                "zh": "走进自然，寻找未知",
                "en": "go deep into nature in search of something untamed.",
                "zh_highlights": ["自然", "未知"],
                "en_highlights": ["nature", "untamed"],
            },
            {
                "start": 9.9,
                "end": 14.2,
                "zh": "纯粹又神秘",
                "en": "pure and mysterious.",
                "zh_highlights": ["神秘"],
                "en_highlights": ["mysterious"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.4 法语",
        "subtitle": "法语口播挑战",
        "badge": "第四种",
        "name": "card_french",
    },
    {
        "kind": "clip",
        "name": "wangyibo_french",
        "person": "No.4 法语",
        "source": Path("待混剪/王一博法语"),
        "start": 14.0,
        "duration": 10.0,
        "crop": {"top": 0, "bottom": 260, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.1,
                "zh": "法语口播挑战上线",
                "en": "French line challenge.",
                "zh_highlights": ["法语"],
                "en_highlights": ["French"],
            },
            {
                "start": 3.1,
                "end": 6.6,
                "zh": "越读越有节奏",
                "en": "Fast, crisp, and surprisingly smooth.",
                "zh_highlights": ["节奏"],
                "en_highlights": ["smooth"],
            },
            {
                "start": 6.6,
                "end": 10.0,
                "zh": "表情管理也在线",
                "en": "The delivery stays in control.",
                "zh_highlights": ["表情管理"],
                "en_highlights": ["delivery"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.5 方言",
        "subtitle": "河南话也拿捏",
        "badge": "第五种",
        "name": "card_dialect",
    },
    {
        "kind": "clip",
        "name": "wangyibo_dialect",
        "person": "No.5 方言",
        "source": Path("待混剪/河南话王一博.mp4"),
        "start": 7.5,
        "duration": 6.8,
        "crop": {"top": 180, "bottom": 260, "left": 0, "right": 0, "fit": "contain"},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.15,
                "zh": "再在路口蹲着说话",
                "en": "Keep chatting at the street corner...",
                "zh_highlights": ["路口", "说话"],
                "en_highlights": ["street corner"],
            },
            {
                "start": 3.15,
                "end": 6.8,
                "zh": "就请你吹免费空调",
                "en": "and enjoy the free air conditioning.",
                "zh_highlights": ["免费空调"],
                "en_highlights": ["free air conditioning"],
            },
        ],
    },
]


def main() -> None:
    base.SEGMENTS = SEGMENTS
    base.main()


if __name__ == "__main__":
    main()
