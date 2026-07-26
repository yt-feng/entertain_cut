#!/usr/bin/env python3
"""Validate Tavily fact evidence and DeepSeek title grounding without processing video."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto_kc_entertain import (
    apply_title_audit,
    ask_deepseek,
    audit_deepseek_plan,
    plan_accuracy_issues,
    revise_deepseek_plan,
    search_tavily_fact_evidence,
)
from deepseek_api import deepseek_model


def main() -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key or not tavily_key:
        raise SystemExit("DEEPSEEK_API_KEY and TAVILY_API_KEY GitHub secrets are required")

    fact_items, tavily_usage = search_tavily_fact_evidence(["肖战 击鼓舞台 电影周闭幕式"])
    analysis = {
        "filename": "quality-smoke.mp4",
        "stem": "肖战击鼓舞台",
        "duration": 12.0,
        "width": 1080,
        "height": 1920,
        "orientation": "vertical",
        "source_metadata": {
            "title": "五年了，肖战击鼓舞台再次被翻出，曾在电影周闭幕式演唱中华力量",
            "author": "媒体原创",
            "known_entities": ["肖战"],
            "verified_entities": ["肖战"],
            "primary_celebrities": ["肖战"],
        },
        "fact_check_evidence": {
            "available": bool(fact_items),
            "items": fact_items,
            "tavily_usage": tavily_usage,
        },
        "transcript_polish": {"available": True, "corrections": []},
        "transcript_text": "五年后再看肖战在电影周闭幕式上的击鼓舞台，动作依然很有力量。",
        "transcript": [
            {"start": 0.0, "end": 6.0, "text": "五年后再看肖战在电影周闭幕式上的击鼓舞台"},
            {"start": 6.0, "end": 12.0, "text": "动作依然很有力量"},
        ],
        "visual_text": {"text": "肖战 中华力量"},
        "visual_layout": {"orientation": "vertical"},
    }

    plan = ask_deepseek(api_key, analysis)
    issues = plan_accuracy_issues(plan, analysis)
    audit = audit_deepseek_plan(api_key, analysis, plan, issues)
    plan, audit_issues = apply_title_audit(plan, audit)
    issues = audit_issues + plan_accuracy_issues(plan, analysis)
    if issues:
        plan = revise_deepseek_plan(api_key, analysis, plan, issues)
        issues = plan_accuracy_issues(plan, analysis)
        if not issues:
            final_audit = audit_deepseek_plan(api_key, analysis, plan, [])
            plan, audit_issues = apply_title_audit(plan, final_audit)
            issues = audit_issues + plan_accuracy_issues(plan, analysis)
    if issues:
        raise SystemExit(f"KC title quality smoke failed: {'; '.join(issues)}")

    print(
        json.dumps(
            {
                "status": "ok",
                "deepseek_model": deepseek_model(),
                "title_lines": plan.get("title_lines"),
                "title_anchor": plan.get("title_anchor"),
                "title_evidence": plan.get("title_evidence"),
                "tavily_credits": tavily_usage.get("credits", 0),
                "tavily_items": len(fact_items),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
