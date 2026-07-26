#!/usr/bin/env python3
"""Make one tiny DeepSeek JSON request for GitHub Actions validation."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_api import deepseek_model, request_deepseek_json


def main() -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY GitHub secret is missing")

    result = request_deepseek_json(
        api_key,
        [
            {
                "role": "system",
                "content": "Return one valid JSON object only. Do not add commentary.",
            },
            {
                "role": "user",
                "content": 'Reply with exactly this JSON shape: {"status":"ok","purpose":"kc-entertain-smoke"}',
            },
        ],
        temperature=0,
        max_tokens=256,
        timeout=45,
    )
    if result.get("status") != "ok":
        raise SystemExit(f"DeepSeek JSON smoke returned an unexpected object: {result}")
    print(f"DeepSeek API smoke passed with model {deepseek_model()}")


if __name__ == "__main__":
    main()
