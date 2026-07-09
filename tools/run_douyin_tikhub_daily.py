#!/usr/bin/env python3
"""Discover and download recent high-like Douyin entertainment videos via TikHub."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx


ROOT = Path(__file__).resolve().parents[1]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
TIKHUB_VIDEO_SEARCH_URL = "https://api.tikhub.io/api/v1/douyin/search/fetch_video_search_v2"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
PROCESSED_MANIFEST = ROOT / "outputs" / "kc_entertain" / "processed_aweme_ids.json"

HOT_CONTEXT_QUERIES = [
    "内娱 明星 热搜 综艺 新剧",
    "热播剧 演员 综艺 明星 娱乐圈",
    "电影 上映 主演 明星 热议",
]
COMMENTABLE_TERMS = [
    "评论区",
    "网友",
    "热议",
    "争议",
    "吐槽",
    "吵翻",
    "开撕",
    "对比",
    "同框",
    "路人",
    "粉丝",
    "演技",
    "妆造",
    "番位",
    "CP",
    "反差",
    "破防",
    "泪目",
    "笑死",
    "翻车",
    "封神",
    "出圈",
    "名场面",
    "没想到",
]
LOW_SIGNAL_TERMS = [
    "壁纸",
    "卡点",
    "混剪",
    "剪辑教程",
    "调色教程",
    "素材",
    "库存",
    "随拍",
    "饭拍直拍",
    "无水印",
    "搬运",
]
EXPLAINER_TERMS = [
    "娱乐解说",
    "影视解说",
    "电影解说",
    "娱评",
    "锐评",
    "个人观点",
    "我来聊",
    "聊一聊",
    "盘点",
    "爆料",
    "吃瓜",
    "八卦",
    "内娱瓜",
    "揭秘",
    "reaction",
    "React",
]
STAR_CLIP_CUES = [
    "现场",
    "舞台",
    "采访",
    "访谈",
    "综艺",
    "花絮",
    "路透",
    "直播",
    "红毯",
    "演唱会",
    "首映礼",
    "片段",
    "cut",
    "CUT",
    "名场面",
    "剧集",
    "正片",
    "饭拍",
]
KNOWN_ENTITIES = [
    "王一博",
    "肖战",
    "杨紫",
    "赵丽颖",
    "迪丽热巴",
    "刘亦菲",
    "白鹿",
    "赵露思",
    "成毅",
    "檀健次",
    "张凌赫",
    "丁禹兮",
    "田曦薇",
    "虞书欣",
    "杨幂",
    "唐嫣",
    "刘诗诗",
    "胡歌",
    "邓为",
    "龚俊",
    "王鹤棣",
    "张晚意",
    "吴磊",
    "易烊千玺",
    "王俊凯",
    "王源",
    "范丞丞",
    "黄明昊",
    "周深",
    "薛之谦",
    "张艺兴",
    "沈腾",
    "马丽",
    "贾玲",
    "雷佳音",
    "于正",
    "郭敬明",
    "何炅",
    "谢娜",
    "杨迪",
    "宁静",
    "那英",
    "乘风",
    "浪姐",
    "披荆斩棘",
    "奔跑吧",
    "中餐厅",
    "王牌对王牌",
    "花儿与少年",
    "歌手",
    "五十公里桃花坞",
    "无限超越班",
]


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("TIKHUB_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("TIKHUB_API_KEY is required for TikHub source discovery.")

    work_dir = project_path(args.work_dir)
    discovery_dir = work_dir / "discovery"
    selected_dir = work_dir / "selected"
    reports_dir = work_dir / "reports"
    downloads_dir = work_dir / "downloads" / "tikhub"
    for path in (discovery_dir, selected_dir, reports_dir, downloads_dir):
        path.mkdir(parents=True, exist_ok=True)

    reports_dir = work_dir / "reports"
    processed_manifest = project_path(args.processed_manifest)
    processed_ids = load_processed_ids(processed_manifest)
    hot_context = collect_hot_context(args, reports_dir)
    keywords = plan_search_keywords(args, split_terms(args.seed_keywords), hot_context)
    run_info: dict[str, Any] = {
        "provider": "tikhub",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "work_dir": str(work_dir),
        "keywords": keywords,
        "hot_context": hot_context,
        "commands": [],
        "errors": [],
        "requested_limit": args.limit,
        "minimum_selected_videos": args.min_selected_videos,
        "max_search_requests": args.max_search_requests,
        "processed_manifest": str(processed_manifest),
        "processed_id_count": len(processed_ids),
        "quality_rules": {
            "recent_hours": args.recent_hours,
            "min_duration_seconds": args.min_duration_seconds,
            "target_min_duration_seconds": args.target_min_duration_seconds,
            "max_duration_seconds": args.max_duration_seconds,
            "primary_min_likes": args.primary_min_likes,
            "fallback_min_likes": args.fallback_min_likes,
            "tikhub_filter_duration": resolve_duration_filter(args),
        },
    }

    candidates = fetch_candidates(args, api_key, keywords, discovery_dir, run_info)
    selected = select_candidates(
        candidates,
        max(args.limit, args.limit * max(1, args.download_candidate_multiplier)),
        args.recent_hours,
        args.primary_min_likes,
        args.fallback_min_likes,
        args.min_duration_seconds,
        args.target_min_duration_seconds,
        args.max_duration_seconds,
        args.must_include_terms,
        args.exclude_terms,
        processed_ids,
        hot_context,
    )
    selected = deepseek_candidate_review(args, selected, hot_context, run_info)
    write_reports(reports_dir, keywords, candidates, selected, run_info)

    if not selected:
        print("TikHub returned no selected candidates. Reports were still written.")
        return 0

    downloaded_ids = download_selected(args, selected, downloads_dir, selected_dir, run_info)
    successful_ids = selected_aweme_ids(selected_dir)
    if successful_ids:
        selected = [item for item in selected if str(item.get("aweme_id") or "") in successful_ids][: args.limit]
        rewrite_selected_dir(selected_dir, selected)
        update_processed_manifest(processed_manifest, selected, args, run_info)
    record_selected_files(selected_dir, run_info)
    run_info["downloaded_ids"] = sorted(downloaded_ids)
    write_reports(reports_dir, keywords, candidates, selected, run_info)

    selected_file_count = count_selected_files(selected_dir)
    print(f"TikHub selected files: {selected_file_count}/{args.limit} (minimum {args.min_selected_videos})", flush=True)
    if selected_file_count < min(max(1, args.limit), max(1, args.min_selected_videos)):
        run_info["minimum_not_met"] = (
            f"Only {selected_file_count}/{args.limit} selected videos downloaded; "
            f"minimum for publishing is {args.min_selected_videos}."
        )
        write_reports(reports_dir, keywords, candidates, selected, run_info)
    print(f"Reports: {reports_dir}")
    print(f"Selected copies: {selected_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=ROOT / "work" / "douyin_tikhub_daily")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--min-selected-videos", type=int, default=5)
    parser.add_argument("--recent-hours", type=int, default=720)
    parser.add_argument("--primary-min-likes", type=int, default=10_000)
    parser.add_argument("--fallback-min-likes", type=int, default=1_000)
    parser.add_argument("--min-duration-seconds", type=int, default=0)
    parser.add_argument("--target-min-duration-seconds", type=int, default=60)
    parser.add_argument("--max-duration-seconds", type=int, default=300)
    parser.add_argument("--download-candidate-multiplier", type=int, default=4)
    parser.add_argument("--max-search-requests", type=int, default=5)
    parser.add_argument("--pages-per-keyword", type=int, default=1)
    parser.add_argument("--tikhub-filter-duration", default="auto", help="TikHub filter_duration; auto, 0, 0-1, 1-5, or 5-10000.")
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--download-timeout-seconds", type=int, default=120)
    parser.add_argument("--download-max-urls", type=int, default=3)
    parser.add_argument("--processed-manifest", default=str(PROCESSED_MANIFEST))
    parser.add_argument("--output-date", default="")
    parser.add_argument("--hot-context", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--hot-context-max-items", type=int, default=24)
    parser.add_argument("--deepseek-candidate-review", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--deepseek-candidate-review-count", type=int, default=30)
    parser.add_argument("--seed-keywords", default="")
    parser.add_argument("--must-include-terms", default="")
    parser.add_argument("--exclude-terms", default="")
    return parser.parse_args()


def fetch_candidates(
    args: argparse.Namespace,
    api_key: str,
    keywords: list[str],
    discovery_dir: Path,
    run_info: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    request_count = 0
    max_search_requests = max(0, int(args.max_search_requests))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "kc-entertain-daily/1.0",
    }
    timeout = httpx.Timeout(connect=15, read=args.request_timeout_seconds, write=20, pool=15)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for keyword in keywords:
            if max_search_requests and request_count >= max_search_requests:
                break
            cursor = 0
            search_id = ""
            backtrace = ""
            for page in range(max(1, args.pages_per_keyword)):
                if max_search_requests and request_count >= max_search_requests:
                    break
                payload = {
                    "keyword": keyword,
                    "cursor": cursor,
                    "sort_type": "1",
                    "publish_time": resolve_publish_time_filter(args.recent_hours),
                    "filter_duration": resolve_duration_filter(args),
                    "content_type": "1",
                    "search_id": search_id,
                    "backtrace": backtrace,
                }
                try:
                    request_count += 1
                    response = client.post(TIKHUB_VIDEO_SEARCH_URL, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    write_json(discovery_dir / f"tikhub_{safe_slug(keyword)}_{page + 1}.json", data)
                    page_items = 0
                    for aweme in iter_aweme_infos(data):
                        item = normalize_aweme(aweme, keyword, discovery_dir)
                        aweme_id = str(item.get("aweme_id") or "")
                        key = aweme_id or str(item.get("url") or "")
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        candidates.append(item)
                        page_items += 1
                    run_info.setdefault("tikhub_search", []).append(
                        {
                            "keyword": keyword,
                            "page": page + 1,
                            "count": page_items,
                            "cursor": cursor,
                            "publish_time": payload["publish_time"],
                            "filter_duration": payload["filter_duration"],
                        }
                    )
                    next_cursor = find_first_value(data, {"cursor"})
                    next_search_id = find_first_value(data, {"search_id"})
                    next_backtrace = find_first_value(data, {"backtrace"})
                    if next_cursor is None or int_or_zero(next_cursor) == cursor:
                        break
                    cursor = int_or_zero(next_cursor)
                    search_id = str(next_search_id or search_id or "")
                    backtrace = str(next_backtrace or backtrace or "")
                except Exception as exc:  # noqa: BLE001
                    message = f"TikHub search failed keyword={keyword!r} page={page + 1}: {exc}"
                    run_info["errors"].append(message)
                    print(message, flush=True)
                    break
    return candidates


def iter_aweme_infos(data: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            aweme = value.get("aweme_info")
            if isinstance(aweme, dict):
                found.append(aweme)
            elif looks_like_aweme(value):
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return found


def looks_like_aweme(value: dict[str, Any]) -> bool:
    return bool(value.get("aweme_id") and (value.get("video") or value.get("statistics")))


def normalize_aweme(aweme: dict[str, Any], source_keyword: str, source_dir: Path) -> dict[str, Any]:
    stats = first_dict(aweme.get("statistics"), aweme.get("stats"))
    author = first_dict(aweme.get("author"))
    video = first_dict(aweme.get("video"))
    aweme_id = str(aweme.get("aweme_id") or aweme.get("id") or aweme.get("item_id") or "")
    title = str(aweme.get("desc") or aweme.get("title") or aweme.get("caption") or "").strip()
    share_url = (
        aweme.get("share_url")
        or nested_get(aweme, ["share_info", "share_url"])
        or nested_get(aweme, ["share_info", "url"])
        or ""
    )
    if not share_url and aweme_id:
        share_url = f"https://www.douyin.com/video/{aweme_id}"
    return {
        "aweme_id": aweme_id,
        "title": title,
        "url": str(share_url),
        "download_urls": extract_video_urls(aweme),
        "duration_ms": int_or_zero(video.get("duration") or aweme.get("duration")),
        "like_count": int_or_zero(stats.get("digg_count") or stats.get("like_count")),
        "comment_count": int_or_zero(stats.get("comment_count")),
        "share_count": int_or_zero(stats.get("share_count")),
        "collect_count": int_or_zero(stats.get("collect_count")),
        "play_count": int_or_zero(stats.get("play_count")),
        "create_time": int_or_zero(aweme.get("create_time")),
        "create_time_iso": timestamp_iso(int_or_zero(aweme.get("create_time"))),
        "author": str(author.get("nickname") or author.get("unique_id") or ""),
        "source_keyword": source_keyword,
        "source_file": str(source_dir),
        "provider": "tikhub",
    }


def extract_video_urls(aweme: dict[str, Any]) -> list[str]:
    video = first_dict(aweme.get("video"))
    urls: list[str] = []

    def add_addr(addr: Any) -> None:
        if not isinstance(addr, dict):
            return
        value = addr.get("url_list") or addr.get("url_list_264") or addr.get("url_list_265")
        if isinstance(value, list):
            urls.extend(str(url) for url in value if url)
        elif isinstance(value, str):
            urls.append(value)

    for key in ("play_addr_h264", "play_addr", "download_addr"):
        add_addr(video.get(key))
    for bit_rate in video.get("bit_rate") or []:
        if isinstance(bit_rate, dict):
            add_addr(bit_rate.get("play_addr"))
            add_addr(bit_rate.get("play_addr_265"))
    return dedupe_keep_order([url for url in urls if url.startswith(("http://", "https://"))])


def select_candidates(
    candidates: list[dict[str, Any]],
    limit: int,
    recent_hours: int,
    primary_min_likes: int,
    fallback_min_likes: int,
    min_duration_seconds: int,
    target_min_duration_seconds: int,
    max_duration_seconds: int,
    must_include_terms: str,
    exclude_terms: str,
    processed_ids: set[str],
    hot_context: dict[str, Any],
) -> list[dict[str, Any]]:
    scoped = candidates
    if processed_ids:
        scoped = [item for item in scoped if candidate_identity(item).isdisjoint(processed_ids)]
    # Duration is a soft ranking preference. Very short videos can still win when
    # engagement and topic fit are strong.
    if max_duration_seconds > 0:
        max_duration_ms = max_duration_seconds * 1000
        scoped = [
            item
            for item in scoped
            if not item.get("duration_ms") or int_or_zero(item.get("duration_ms")) <= max_duration_ms
        ]
    include_terms = split_terms(must_include_terms)
    exclude_terms_list = split_terms(exclude_terms)
    if exclude_terms_list:
        scoped = [item for item in scoped if not matches_any_term(item, exclude_terms_list)]
    if include_terms:
        included = [item for item in scoped if matches_any_term(item, include_terms)]
        scoped = included if included else scoped

    recent = []
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    for item in scoped:
        created = int_or_zero(item.get("create_time"))
        if not created or recent_hours <= 0 or 0 <= now - created <= recent_hours * 3600:
            recent.append(item)
    scoped = recent if recent else scoped

    quality_pool = [item for item in scoped if int_or_zero(item.get("like_count")) >= fallback_min_likes]
    primary_pool = [item for item in quality_pool if int_or_zero(item.get("like_count")) >= primary_min_likes]
    ranked_pool = primary_pool if len(primary_pool) >= max(1, limit) else quality_pool
    ranked_pool = [item for item in ranked_pool if not likely_face_explainer(item)]
    hot_terms = [str(term) for term in hot_context.get("terms", []) if str(term).strip()]
    anchored_pool = [
        item
        for item in ranked_pool
        if matched_known_entities(item, hot_terms)
        or int_or_zero(item.get("like_count")) >= primary_min_likes * 5
        or int_or_zero(item.get("comment_count")) >= 100
    ]
    if len(anchored_pool) >= max(1, min(limit, 5)):
        ranked_pool = anchored_pool

    for item in ranked_pool:
        score_candidate(item, hot_terms, primary_min_likes, target_min_duration_seconds)
    return sorted(
        ranked_pool,
        key=lambda item: (
            float(item.get("quality_score") or 0),
            int_or_zero(item.get("like_count")),
            int_or_zero(item.get("comment_count")) + int_or_zero(item.get("share_count")),
            int_or_zero(item.get("play_count")),
        ),
        reverse=True,
    )[: max(0, limit)]


def collect_hot_context(args: argparse.Namespace, reports_dir: Path) -> dict[str, Any]:
    context: dict[str, Any] = {"available": False, "terms": [], "items": [], "errors": [], "sources": []}
    if not args.hot_context:
        return context

    max_items = max(1, int(args.hot_context_max_items))
    timeout = httpx.Timeout(connect=10, read=25, write=15, pool=10)
    headers = {"User-Agent": "kc-entertain-hot-context/1.0"}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        context["items"].extend(fetch_hotspot_assistant_context(client, max_items, context))
        for query in HOT_CONTEXT_QUERIES:
            if len(context["items"]) >= max_items:
                break
            context["items"].extend(fetch_bing_context(client, query, max_items - len(context["items"]), context))
            if len(context["items"]) >= max_items:
                break
            context["items"].extend(fetch_gdelt_context(client, query, max_items - len(context["items"]), context))
        if len(context["items"]) < max_items:
            for query in HOT_CONTEXT_QUERIES[:2]:
                if len(context["items"]) >= max_items:
                    break
                context["items"].extend(fetch_jina_search_context(client, query, max_items - len(context["items"]), context))

    context["items"] = dedupe_hot_items(context["items"])[:max_items]
    context["terms"] = extract_hot_terms(context["items"])[:20]
    context["available"] = bool(context["items"])
    write_json(reports_dir / "hot_context.json", context)
    return context


def fetch_hotspot_assistant_context(
    client: httpx.Client,
    max_items: int,
    context: dict[str, Any],
) -> list[dict[str, str]]:
    url = os.environ.get("HOTSPOT_ASSISTANT_API_URL", "").strip() or os.environ.get("HOTSPOT_API_URL", "").strip()
    if not url:
        return []
    headers = {}
    key = os.environ.get("HOTSPOT_ASSISTANT_API_KEY", "").strip() or os.environ.get("HOTSPOT_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"category": "entertainment", "window": "30d", "limit": max_items}
    try:
        response = client.post(url, json=payload, headers=headers)
        if response.status_code in {404, 405}:
            response = client.get(url, params=payload, headers=headers)
        response.raise_for_status()
        items = normalize_hot_payload(response.json(), "hotspot_assistant")[:max_items]
        if items:
            context["sources"].append("hotspot_assistant")
        return items
    except Exception as exc:  # noqa: BLE001
        context["errors"].append({"source": "hotspot_assistant", "error": str(exc)})
        return []


def fetch_bing_context(
    client: httpx.Client,
    query: str,
    max_items: int,
    context: dict[str, Any],
) -> list[dict[str, str]]:
    key = os.environ.get("BING_SEARCH_API_KEY", "").strip() or os.environ.get("BING_SUBSCRIPTION_KEY", "").strip()
    if not key or max_items <= 0:
        return []
    endpoint = os.environ.get("BING_SEARCH_ENDPOINT", "https://api.bing.microsoft.com/v7.0/search").strip()
    headers = {"Ocp-Apim-Subscription-Key": key}
    params = {"q": query, "mkt": "zh-CN", "count": min(10, max_items), "freshness": "Month", "textDecorations": False}
    try:
        response = client.get(endpoint, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        context["errors"].append({"source": "bing", "query": query, "error": str(exc)})
        return []
    items = []
    for value in nested_get(data, ["webPages", "value"]) or []:
        if not isinstance(value, dict):
            continue
        items.append(
            {
                "source": "bing",
                "query": query,
                "title": str(value.get("name") or "")[:160],
                "snippet": str(value.get("snippet") or "")[:420],
                "url": str(value.get("url") or "")[:260],
            }
        )
        if len(items) >= max_items:
            break
    if items:
        context["sources"].append("bing")
    return items


def fetch_gdelt_context(
    client: httpx.Client,
    query: str,
    max_items: int,
    context: dict[str, Any],
) -> list[dict[str, str]]:
    if max_items <= 0:
        return []
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": min(10, max_items),
        "timespan": "30d",
        "sort": "HybridRel",
    }
    try:
        response = client.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        context["errors"].append({"source": "gdelt", "query": query, "error": str(exc)})
        return []
    items = []
    for value in data.get("articles") or []:
        if not isinstance(value, dict):
            continue
        items.append(
            {
                "source": "gdelt",
                "query": query,
                "title": str(value.get("title") or "")[:160],
                "snippet": str(value.get("seendate") or value.get("domain") or "")[:420],
                "url": str(value.get("url") or "")[:260],
            }
        )
        if len(items) >= max_items:
            break
    if items:
        context["sources"].append("gdelt")
    return items


def fetch_jina_search_context(
    client: httpx.Client,
    query: str,
    max_items: int,
    context: dict[str, Any],
) -> list[dict[str, str]]:
    if max_items <= 0:
        return []
    try:
        response = client.get(f"https://s.jina.ai/{quote(query)}")
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        context["errors"].append({"source": "jina_search", "query": query, "error": str(exc)})
        return []
    items = parse_search_markdown(response.text, query, "jina_search")[:max_items]
    if items:
        context["sources"].append("jina_search")
    return items


def parse_search_markdown(text: str, query: str, source: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    blocks = re.split(r"\n\s*\d+\.\s+", text)
    for block in blocks:
        title_match = re.search(r"Title:\s*(.+)", block)
        url_match = re.search(r"URL:\s*(.+)", block)
        snippet_match = re.search(r"Snippet:\s*(.+)", block, flags=re.S)
        if not title_match:
            continue
        items.append(
            {
                "source": source,
                "query": query,
                "title": normalize_space(title_match.group(1))[:160],
                "snippet": normalize_space(snippet_match.group(1) if snippet_match else "")[:420],
                "url": normalize_space(url_match.group(1) if url_match else "")[:260],
            }
        )
        if len(items) >= 8:
            break
    return items


def normalize_hot_payload(data: Any, source: str) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            title = value.get("title") or value.get("name") or value.get("word") or value.get("keyword") or value.get("topic")
            snippet = value.get("summary") or value.get("desc") or value.get("description") or value.get("snippet") or ""
            url = value.get("url") or value.get("link") or ""
            if title:
                values.append(
                    {
                        "source": source,
                        "query": "hotspot_assistant",
                        "title": str(title)[:160],
                        "snippet": str(snippet)[:420],
                        "url": str(url)[:260],
                    }
                )
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return values


def dedupe_hot_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    seen = set()
    for item in items:
        key = (item.get("title", ""), item.get("url", ""))
        if key in seen or not normalize_space(str(item.get("title") or item.get("snippet") or "")):
            continue
        seen.add(key)
        result.append(item)
    return result


def extract_hot_terms(items: list[dict[str, str]]) -> list[str]:
    terms: list[str] = []
    joined = "\n".join(f"{item.get('title', '')} {item.get('snippet', '')}" for item in items)
    for entity in KNOWN_ENTITIES:
        if entity in joined:
            terms.append(entity)
    terms.extend(re.findall(r"《([^》]{2,12})》", joined))
    terms.extend(re.findall(r"#([^#\s，。！？、]{2,12})", joined))
    for match in re.findall(r"[\u4e00-\u9fff]{2,8}(?:开播|定档|杀青|路透|热播|收官|官宣|热议)", joined):
        terms.append(match)
    return dedupe_keep_order([normalize_space(term).strip("《》#：:，。") for term in terms if len(term.strip()) >= 2])


def plan_search_keywords(args: argparse.Namespace, seeds: list[str], hot_context: dict[str, Any]) -> list[str]:
    keywords: list[str] = []
    for term in hot_context.get("terms", []) or []:
        term = normalize_space(str(term))
        if not term:
            continue
        if term in KNOWN_ENTITIES:
            keywords.append(f"{term} 热议")
        elif len(term) <= 8:
            keywords.append(f"{term} 明星")
    keywords.extend(seeds)
    if not keywords:
        keywords.extend(["明星 评论区", "热播剧 演员", "综艺 名场面", "娱乐圈 高赞", "明星 争议"])
    return dedupe_keep_order(keywords)[: max(1, int(args.max_search_requests) or 5)]


def score_candidate(
    item: dict[str, Any],
    hot_terms: list[str],
    primary_min_likes: int,
    target_min_duration_seconds: int,
) -> None:
    text = candidate_text(item)
    likes = int_or_zero(item.get("like_count"))
    comments = int_or_zero(item.get("comment_count"))
    shares = int_or_zero(item.get("share_count"))
    plays = int_or_zero(item.get("play_count"))
    duration = duration_seconds(item)
    known = matched_known_entities(item, hot_terms)
    hot_matches = [term for term in hot_terms if term and term in text]
    comment_terms = [term for term in COMMENTABLE_TERMS if term in text]
    low_signal = [term for term in LOW_SIGNAL_TERMS if term in text]
    explainer_terms = [term for term in EXPLAINER_TERMS if term.lower() in text.lower()]
    clip_cues = [term for term in STAR_CLIP_CUES if term in text]
    comment_ratio = comments / max(1, likes)

    score = math.log10(max(likes, 1)) * 18
    score += math.log10(max(comments, 1)) * 12
    score += math.log10(max(shares, 1)) * 6
    score += math.log10(max(plays, 1)) * 2
    score += min(18, comment_ratio * 180)
    if likes >= primary_min_likes:
        score += 14
    if known:
        score += 18 + min(10, len(known) * 3)
    if hot_matches:
        score += 16 + min(12, len(hot_matches) * 4)
    if comment_terms:
        score += 10 + min(12, len(comment_terms) * 3)
    if duration:
        if duration >= target_min_duration_seconds:
            score += 8
        elif duration >= 35:
            score += 2
        else:
            score -= 8
    if low_signal:
        score -= 18
    if explainer_terms and not clip_cues:
        score -= 42
    elif explainer_terms:
        score -= 16
    if clip_cues:
        score += 10

    item["quality_score"] = round(score, 3)
    item["known_entities"] = known[:8]
    item["hot_context_matches"] = hot_matches[:8]
    item["commentability_terms"] = comment_terms[:8]
    item["low_signal_terms"] = low_signal[:8]
    item["explainer_terms"] = explainer_terms[:8]
    item["star_clip_cues"] = clip_cues[:8]
    if explainer_terms:
        item["clip_type"] = "likely_face_explainer"
    elif clip_cues:
        item["clip_type"] = "likely_star_clip"
    else:
        item["clip_type"] = "metadata_entertainment_clip"


def deepseek_candidate_review(
    args: argparse.Namespace,
    selected: list[dict[str, Any]],
    hot_context: dict[str, Any],
    run_info: dict[str, Any],
) -> list[dict[str, Any]]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not args.deepseek_candidate_review or not api_key or not selected:
        return selected[: review_return_count(args)]
    review_items = selected[: max(1, int(args.deepseek_candidate_review_count))]
    compact_items = [
        {
            "aweme_id": item.get("aweme_id"),
            "title": item.get("title"),
            "author": item.get("author"),
            "likes": item.get("like_count"),
            "comments": item.get("comment_count"),
            "shares": item.get("share_count"),
            "duration_seconds": duration_seconds(item),
            "create_time": item.get("create_time_iso"),
            "known_entities": item.get("known_entities", []),
            "hot_context_matches": item.get("hot_context_matches", []),
            "commentability_terms": item.get("commentability_terms", []),
            "explainer_terms": item.get("explainer_terms", []),
            "star_clip_cues": item.get("star_clip_cues", []),
            "clip_type": item.get("clip_type", ""),
            "quality_score": item.get("quality_score"),
        }
        for item in review_items
    ]
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are KC娱乐's cloud source-selection editor. "
                    "Choose videos with real entertainment heat, recognizable stars/shows/films, and comment potential. "
                    "Prefer 10k+ likes, allow 1k+ likes only when the topic is clearly strong. "
                    "Reject face-to-camera bloggers/commentators explaining entertainment news. "
                    "Do not reward unknown low-context clips, generic compilations, wallpaper/card videos, or dummy reversal bait. "
                    "Use only the provided hot-context/search evidence and metadata; do not invent names."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "goal": "Pick daily KC娱乐 source videos. Need 5 new videos when possible.",
                        "selection_rules": [
                            "至少千赞，优先万赞。",
                            "明星/综艺/影视实体越明确越好。",
                            "能刺激评论区讨论的优先：争议、反差、对比、演技/妆造/番位/CP、路人评价、名场面。",
                            "只要娱乐明星/综艺/影视切片，不要单个博主露脸讲解、娱评、盘点、吃瓜解说。",
                            "标题或描述像模板、卡点、壁纸、无水印素材的降权。",
                            "时长只是软偏好，不是硬性淘汰。",
                        ],
                        "hot_context_terms": hot_context.get("terms", []),
                        "hot_context_items": hot_context.get("items", [])[:10],
                        "candidates": compact_items,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        "temperature": 0.15,
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(connect=15, read=90, write=20, pool=15)) as client:
            response = client.post(
                DEEPSEEK_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        report = parse_json_object(content)
    except Exception as exc:  # noqa: BLE001
        run_info.setdefault("errors", []).append(f"DeepSeek candidate review failed: {exc}")
        return selected[: review_return_count(args)]

    by_id = {str(item.get("aweme_id") or ""): item for item in selected}
    review_map = {}
    for value in report.get("items", []) or []:
        if not isinstance(value, dict):
            continue
        aweme_id = str(value.get("aweme_id") or "")
        if aweme_id:
            review_map[aweme_id] = value
    for aweme_id, review in review_map.items():
        item = by_id.get(aweme_id)
        if not item:
            continue
        editor_score = float_or_zero(review.get("editor_score"))
        item["deepseek_editor_score"] = editor_score
        item["deepseek_comment_hook"] = normalize_space(str(review.get("comment_hook") or ""))[:80]
        item["deepseek_reason"] = normalize_space(str(review.get("reason") or ""))[:160]
        verified = review.get("verified_entities")
        if isinstance(verified, list):
            item["verified_entities"] = [normalize_space(str(entity)) for entity in verified if normalize_space(str(entity))][:8]
        if bool(review.get("discard")):
            item["quality_score"] = float(item.get("quality_score") or 0) - 80
        else:
            item["quality_score"] = float(item.get("quality_score") or 0) + editor_score * 0.45
    run_info["deepseek_candidate_review"] = report
    return sorted(
        selected,
        key=lambda item: (
            float(item.get("quality_score") or 0),
            float(item.get("deepseek_editor_score") or 0),
            int_or_zero(item.get("like_count")),
        ),
        reverse=True,
    )[: review_return_count(args)]


def review_return_count(args: argparse.Namespace) -> int:
    return max(0, max(int(args.limit), int(args.limit) * max(1, int(args.download_candidate_multiplier))))


def candidate_text(item: dict[str, Any]) -> str:
    return normalize_space(
        " ".join(
            str(item.get(key) or "")
            for key in ("title", "author", "source_keyword", "aweme_id", "url")
        )
    )


def matched_known_entities(item: dict[str, Any], hot_terms: list[str]) -> list[str]:
    text = candidate_text(item)
    entities = [entity for entity in KNOWN_ENTITIES if entity in text]
    entities.extend(term for term in hot_terms if len(term) >= 2 and term in text)
    return dedupe_keep_order(entities)


def likely_face_explainer(item: dict[str, Any]) -> bool:
    text = candidate_text(item).lower()
    return any(term.lower() in text for term in EXPLAINER_TERMS)


def likely_star_clip(item: dict[str, Any]) -> bool:
    text = candidate_text(item)
    return any(term in text for term in STAR_CLIP_CUES)


def candidate_identity(item: dict[str, Any]) -> set[str]:
    values = {str(item.get("aweme_id") or "").strip(), str(item.get("url") or "").strip()}
    return {value for value in values if value}


def resolve_publish_time_filter(recent_hours: int) -> str:
    if recent_hours <= 0:
        return "0"
    if recent_hours <= 24:
        return "1"
    if recent_hours <= 24 * 7:
        return "7"
    if recent_hours <= 24 * 180:
        return "180"
    return "0"


def resolve_duration_filter(args: argparse.Namespace) -> str:
    value = normalize_space(str(args.tikhub_filter_duration or "auto"))
    if value != "auto":
        return value
    return "0"


def load_processed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    values: set[str] = set()
    if isinstance(data, dict):
        for item in data.get("items", []):
            if isinstance(item, dict):
                values.update(candidate_identity(item))
        for value in data.get("aweme_ids", []):
            values.add(str(value))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                values.update(candidate_identity(item))
            else:
                values.add(str(item))
    return {value for value in values if value}


def update_processed_manifest(
    path: Path,
    selected: list[dict[str, Any]],
    args: argparse.Namespace,
    run_info: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, Any]] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for item in data.get("items", []) if isinstance(data, dict) else []:
            if isinstance(item, dict):
                aweme_id = str(item.get("aweme_id") or "")
                if aweme_id:
                    existing[aweme_id] = item
    selected_at = dt.datetime.now(dt.timezone.utc).isoformat()
    for item in selected:
        aweme_id = str(item.get("aweme_id") or "")
        if not aweme_id:
            continue
        existing[aweme_id] = {
            "aweme_id": aweme_id,
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "like_count": item.get("like_count", 0),
            "comment_count": item.get("comment_count", 0),
            "share_count": item.get("share_count", 0),
            "duration_ms": item.get("duration_ms", 0),
            "create_time_iso": item.get("create_time_iso", ""),
            "known_entities": item.get("known_entities", []),
            "verified_entities": item.get("verified_entities", []),
            "quality_score": item.get("quality_score", 0),
            "output_date": args.output_date,
            "selected_at": selected_at,
        }
    payload = {
        "updated_at": selected_at,
        "count": len(existing),
        "items": sorted(existing.values(), key=lambda value: str(value.get("selected_at", "")), reverse=True),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    run_info["processed_manifest_updated"] = {"path": str(path), "count": len(existing)}


def download_selected(
    args: argparse.Namespace,
    selected: list[dict[str, Any]],
    downloads_dir: Path,
    selected_dir: Path,
    run_info: dict[str, Any],
) -> set[str]:
    downloaded: set[str] = set()
    stats: list[dict[str, Any]] = []
    run_info["tikhub_download"] = stats
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
    }
    timeout = httpx.Timeout(connect=20, read=60, write=60, pool=20)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for idx, item in enumerate(selected, 1):
            if count_selected_files(selected_dir) >= args.limit:
                break
            aweme_id = str(item.get("aweme_id") or f"rank{idx}")
            urls = [url for url in item.get("download_urls") or [] if isinstance(url, str)]
            urls = urls[: max(1, args.download_max_urls)]
            if not urls:
                stats.append({"aweme_id": aweme_id, "status": "no_download_url"})
                continue
            for url_idx, url in enumerate(urls, 1):
                target = downloads_dir / f"{idx:02d}_{aweme_id}.mp4"
                temp_target = target.with_suffix(".mp4.part")
                started = time.monotonic()
                bytes_written = 0
                try:
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        with temp_target.open("wb") as handle:
                            for chunk in response.iter_bytes():
                                if not chunk:
                                    continue
                                if time.monotonic() - started > args.download_timeout_seconds:
                                    raise TimeoutError("TikHub CDN download timed out")
                                handle.write(chunk)
                                bytes_written += len(chunk)
                    if bytes_written <= 0:
                        raise ValueError("downloaded empty file")
                    temp_target.replace(target)
                    selected_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, selected_dir / target.name)
                    downloaded.add(aweme_id)
                    stats.append(
                        {
                            "aweme_id": aweme_id,
                            "status": "downloaded",
                            "url_index": url_idx,
                            "bytes": bytes_written,
                            "path": str(target),
                        }
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    if temp_target.exists():
                        temp_target.unlink()
                    stats.append({"aweme_id": aweme_id, "status": "failed", "url_index": url_idx, "error": str(exc)})
            if aweme_id not in downloaded:
                run_info["errors"].append(f"TikHub download failed aweme_id={aweme_id}")
    return downloaded


def write_reports(
    reports_dir: Path,
    keywords: list[str],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    run_info: dict[str, Any],
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    write_json(reports_dir / "keywords.json", keywords)
    write_json(reports_dir / "candidates.json", candidates)
    write_json(reports_dir / "selected.json", selected)
    write_json(reports_dir / "run_info.json", run_info)
    with (reports_dir / "selected.csv").open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "rank",
            "aweme_id",
            "like_count",
            "comment_count",
            "share_count",
            "play_count",
            "duration_ms",
            "quality_score",
            "known_entities",
            "verified_entities",
            "hot_context_matches",
            "commentability_terms",
            "clip_type",
            "deepseek_editor_score",
            "deepseek_comment_hook",
            "create_time_iso",
            "author",
            "source_keyword",
            "title",
            "url",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for idx, item in enumerate(selected, 1):
            row = {key: item.get(key, "") for key in fieldnames}
            row["rank"] = idx
            for key in ("known_entities", "verified_entities", "hot_context_matches", "commentability_terms"):
                if isinstance(row.get(key), list):
                    row[key] = "|".join(str(value) for value in row[key])
            writer.writerow(row)
    with (reports_dir / "summary.md").open("w", encoding="utf-8") as handle:
        handle.write("# TikHub Douyin Daily\n\n")
        handle.write(f"- Keywords: {', '.join(keywords)}\n")
        handle.write(f"- Candidates: {len(candidates)}\n")
        handle.write(f"- Selected: {len(selected)}\n")
        selected_files = run_info.get("selected_files")
        if isinstance(selected_files, list):
            handle.write(f"- Selected files: {len(selected_files)}\n")
        handle.write("\n")
        for idx, item in enumerate(selected, 1):
            handle.write(
                f"{idx}. {item.get('title') or item.get('aweme_id')} "
                f"likes={item.get('like_count')} comments={item.get('comment_count')} "
                f"score={item.get('quality_score')} duration={duration_seconds(item)}s "
                f"type={item.get('clip_type', '')} keyword={item.get('source_keyword')}\n"
                f"   {item.get('url')}\n"
            )


def record_selected_files(selected_dir: Path, run_info: dict[str, Any]) -> None:
    files = []
    for path in sorted(selected_dir.glob("*")):
        if path.is_file():
            files.append({"name": path.name, "bytes": path.stat().st_size})
    run_info["selected_files"] = files


def count_selected_files(selected_dir: Path) -> int:
    return sum(1 for path in selected_dir.glob("*") if path.is_file())


def selected_aweme_ids(selected_dir: Path) -> set[str]:
    ids: set[str] = set()
    for path in selected_dir.glob("*"):
        if not path.is_file():
            continue
        match = re.search(r"(\d{15,})", path.name)
        if match:
            ids.add(match.group(1))
    return ids


def rewrite_selected_dir(selected_dir: Path, selected: list[dict[str, Any]]) -> None:
    temp_dir = selected_dir / "_final"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    for idx, item in enumerate(selected, 1):
        aweme_id = str(item.get("aweme_id") or "")
        matches = [path for path in selected_dir.glob("*") if path.is_file() and (not aweme_id or aweme_id in path.name)]
        if not matches:
            continue
        source = matches[0]
        shutil.copy2(source, temp_dir / f"{idx:02d}_{aweme_id or source.stem}{source.suffix}")
    for path in selected_dir.glob("*"):
        if path.is_file():
            path.unlink()
    for path in temp_dir.glob("*"):
        shutil.move(str(path), selected_dir / path.name)
    temp_dir.rmdir()


def first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def nested_get(value: dict[str, Any], keys: list[str]) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def find_first_value(data: Any, keys: set[str]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        for value in data.values():
            found = find_first_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_first_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def matches_any_term(item: dict[str, Any], terms: list[str]) -> bool:
    text = " ".join(str(item.get(key) or "") for key in ("title", "author", "source_keyword", "aweme_id"))
    return any(term in text for term in terms)


def split_terms(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", value).strip("_")
    return slug[:40] or "keyword"


def parse_json_object(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        data = json.loads(match.group(0))
    return data if isinstance(data, dict) else {}


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def duration_seconds(item: dict[str, Any]) -> int:
    return int_or_zero(item.get("duration_ms")) // 1000


def timestamp_iso(value: int) -> str:
    if not value:
        return ""
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
