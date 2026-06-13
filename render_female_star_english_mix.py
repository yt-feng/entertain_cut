#!/usr/bin/env python3
"""Render a KC entertainment-style female-star English-speaking montage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import render_star_english_mix as base


base.WORK_DIR = Path("work/female_mix")
base.RENDER_DIR = base.WORK_DIR / "render"
base.CLIP_DIR = base.WORK_DIR / "clips"
base.OUTPUT = Path("KC娱乐_女明星说英语你Pick谁.mp4")
base.NAV_ITEMS = ["刘亦菲", "汤唯", "迪丽热巴", "关晓彤"]
base.TITLE_MAIN = "女明星说英语"
base.TITLE_SUB = "你Pick谁？"
base.LOWER_RIBBON = "英语名场面 · 你来Pick"
base.TITLE_HIGHLIGHTS = [
    "No.1",
    "No.2",
    "No.3",
    "No.4",
    "刘亦菲",
    "汤唯",
    "迪丽热巴",
    "关晓彤",
    "英语",
    "Pick",
]


SEGMENTS: list[dict[str, Any]] = [
    {
        "kind": "card",
        "duration": 1.15,
        "title": "女明星说英语",
        "subtitle": "你Pick谁？",
        "badge": "KC娱乐",
        "name": "intro",
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.1 刘亦菲",
        "subtitle": "温柔谈内在美",
        "badge": "第一位",
        "name": "card_liuyifei",
    },
    {
        "kind": "clip",
        "name": "liuyifei",
        "person": "No.1 刘亦菲",
        "source": Path("待混剪/1 刘亦菲.mp4"),
        "start": 35.2,
        "duration": 14.9,
        "crop": {"top": 0, "bottom": 0, "left": 180, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.5,
                "zh": "自信并不来自外界",
                "en": "Confidence doesn't really come from outside.",
                "zh_highlights": ["自信", "外界"],
                "en_highlights": ["Confidence", "outside"],
            },
            {
                "start": 3.5,
                "end": 8.95,
                "zh": "它是更深层的东西",
                "en": "It's something more, something beyond what we see.",
                "zh_highlights": ["更深层"],
                "en_highlights": ["something more", "beyond"],
            },
            {
                "start": 8.95,
                "end": 14.9,
                "zh": "情感、无畏和安定，也是一种美",
                "en": "A woman with emotion, fearlessness and stillness is beautiful.",
                "zh_highlights": ["无畏", "美"],
                "en_highlights": ["emotion", "fearlessness", "stillness"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.2 汤唯",
        "subtitle": "领奖英文很真诚",
        "badge": "第二位",
        "name": "card_tangwei",
    },
    {
        "kind": "clip",
        "name": "tangwei",
        "person": "No.2 汤唯",
        "source": Path("待混剪/2 汤唯.mov"),
        "start": 0.0,
        "duration": 13.2,
        "crop": {"top": 0, "bottom": 0, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 3.85,
                "zh": "还在学韩语，所以用英语发言",
                "en": "Since I'm still learning Korean, I want to speak English.",
                "zh_highlights": ["英语"],
                "en_highlights": ["speak English"],
            },
            {
                "start": 3.85,
                "end": 8.9,
                "zh": "第一次受邀来到青龙电影节",
                "en": "This is the first time I've been invited to the Blue Dragon Film Awards.",
                "zh_highlights": ["第一次", "青龙电影节"],
                "en_highlights": ["first time", "Blue Dragon"],
            },
            {
                "start": 8.9,
                "end": 13.2,
                "zh": "第一次站上这个舞台",
                "en": "It's also the first time I stand on this stage.",
                "zh_highlights": ["舞台"],
                "en_highlights": ["this stage"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.3 迪丽热巴",
        "subtitle": "英文介绍新疆服饰",
        "badge": "第三位",
        "name": "card_dilraba",
    },
    {
        "kind": "clip",
        "name": "dilraba_hello",
        "person": "No.3 迪丽热巴",
        "source": Path("待混剪/3 迪丽热巴.mp4"),
        "start": 0.0,
        "duration": 2.64,
        "crop": {"top": 0, "bottom": 0, "left": 0, "right": 300},
        "subtitles": [
            {
                "start": 0.0,
                "end": 2.64,
                "zh": "先用英语自然打招呼",
                "en": "Hello, we're here. Good, good.",
                "zh_highlights": ["英语"],
                "en_highlights": ["Hello", "Good"],
            },
        ],
    },
    {
        "kind": "clip",
        "name": "dilraba_intro",
        "person": "No.3 迪丽热巴",
        "source": Path("待混剪/3 迪丽热巴.mp4"),
        "start": 5.8,
        "duration": 1.88,
        "crop": {"top": 0, "bottom": 0, "left": 0, "right": 300},
        "subtitles": [
            {
                "start": 0.0,
                "end": 1.88,
                "zh": "介绍自己是中国演员",
                "en": "I'm an actress in China.",
                "zh_highlights": ["中国演员"],
                "en_highlights": ["actress", "China"],
            },
        ],
    },
    {
        "kind": "clip",
        "name": "dilraba_xinjiang",
        "person": "No.3 迪丽热巴",
        "source": Path("待混剪/3 迪丽热巴.mp4"),
        "start": 11.68,
        "duration": 2.3,
        "crop": {"top": 0, "bottom": 0, "left": 0, "right": 300},
        "subtitles": [
            {
                "start": 0.0,
                "end": 2.3,
                "zh": "这是来自中国新疆的裙子",
                "en": "This is an Atlas dress from Xinjiang.",
                "zh_highlights": ["中国新疆"],
                "en_highlights": ["Xinjiang"],
            },
        ],
    },
    {
        "kind": "clip",
        "name": "dilraba_traditional",
        "person": "No.3 迪丽热巴",
        "source": Path("待混剪/3 迪丽热巴.mp4"),
        "start": 17.68,
        "duration": 4.0,
        "crop": {"top": 0, "bottom": 0, "left": 0, "right": 300},
        "subtitles": [
            {
                "start": 0.0,
                "end": 2.0,
                "zh": "这条裙子很漂亮",
                "en": "This is a beautiful dress.",
                "zh_highlights": ["漂亮"],
                "en_highlights": ["beautiful dress"],
            },
            {
                "start": 2.0,
                "end": 4.0,
                "zh": "也很传统",
                "en": "Very traditional.",
                "zh_highlights": ["传统"],
                "en_highlights": ["traditional"],
            },
        ],
    },
    {
        "kind": "card",
        "duration": 0.82,
        "title": "No.4 关晓彤",
        "subtitle": "联合国英文发言",
        "badge": "第四位",
        "name": "card_guanxiaotong",
    },
    {
        "kind": "clip",
        "name": "guanxiaotong",
        "person": "No.4 关晓彤",
        "source": Path("待混剪/4 关晓彤.mp4"),
        "start": 0.0,
        "duration": 10.2,
        "crop": {"top": 0, "bottom": 0, "left": 0, "right": 0},
        "subtitles": [
            {
                "start": 0.0,
                "end": 5.2,
                "zh": "无论男孩女孩，都值得机会",
                "en": "Everyone, boy or girl, HIV positive or negative,",
                "zh_highlights": ["机会"],
                "en_highlights": ["boy or girl"],
            },
            {
                "start": 5.2,
                "end": 10.2,
                "zh": "都该实现真正的潜能",
                "en": "deserves a chance and opportunity to fulfill their true potential.",
                "zh_highlights": ["潜能"],
                "en_highlights": ["true potential"],
            },
        ],
    },
]


def main() -> None:
    base.SEGMENTS = SEGMENTS
    base.main()


if __name__ == "__main__":
    main()
