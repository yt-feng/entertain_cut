#!/usr/bin/env python3
"""Build GateX daily market-hotspot topic cards from source snapshots.

The adapter is deliberately separate from KC entertainment generation. It may
collect market discovery signals, score and cluster them, and POST short topic
cards to the GateX Private Desk. It cannot start, approve, draft, or publish a
report; a GateX administrator explicitly sends a selected card to Report Studio.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DOUYIN_DOWNLOADER_REF = "79a932c17aab25fccd2e47ce2281361fabd864fa"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
TIKHUB_URL = "https://api.tikhub.io/api/v1/douyin/search/fetch_video_search_v2"
GATEX_INTAKE_URL = "https://gatex.fund/api/integrations/intelligence/intake"
DEFAULT_GDELT_QUERIES = [
    '"artificial intelligence" infrastructure',
    'semiconductor robotics data centre',
    'China Middle East technology industry',
    'energy infrastructure digital economy',
]
DEFAULT_MARKET_TERMS = [
    "artificial intelligence", "ai", "data centre", "data center", "semiconductor",
    "robotics", "cloud", "compute", "power grid", "energy", "infrastructure",
    "technology", "industrial", "supply chain", "market", "company", "policy",
    "人工智能", "算力", "数据中心", "半导体", "机器人", "云计算", "电网", "能源",
    "基础设施", "科技", "产业", "供应链", "市场", "公司", "政策",
]
FINANCE_COPY_REPLACEMENTS = [
    (re.compile(r"\binvestment opportunities\b", re.I), "market developments"),
    (re.compile(r"\binvestment opportunity\b", re.I), "market development"),
    (re.compile(r"\binvestment outlook\b", re.I), "market outlook"),
    (re.compile(r"\bcapital markets?\b", re.I), "company and industry landscape"),
    (re.compile(r"\btrading signals?\b", re.I), "market indicators"),
]
SOURCE_QUALITY = {
    "official": 1.0, "report": 0.9, "news": 0.82, "gdelt": 0.76,
    "douyin_hot_board": 0.56, "tikhub": 0.58, "hotspot_assistant": 0.6,
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: Any, fallback: dt.datetime | None = None) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
        except (ValueError, OSError):
            return fallback
    text = str(value or "").strip()
    if not text:
        return fallback
    if re.fullmatch(r"\d{14}", text):
        try:
            return dt.datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return fallback
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)
    except ValueError:
        return fallback


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def compact(value: Any, maximum: int = 800) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def normalize_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", compact(value).lower()).strip()


def term_matches(haystack: Any, term: Any) -> bool:
    normalized_haystack = normalize_text(haystack)
    normalized_term = normalize_text(term)
    if not normalized_term:
        return False
    if re.fullmatch(r"[a-z0-9 ]+", normalized_term):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])",
            normalized_haystack,
        ) is not None
    return normalized_term in normalized_haystack


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def content_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_url(value: Any) -> str:
    text = compact(value, 2000)
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
        query = [
            (key, item)
            for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"from", "source", "spm", "fbclid", "gclid", "share_token"}
        ]
        host = (parsed.hostname or "").lower().removeprefix("www.")
        port = f":{parsed.port}" if parsed.port else ""
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        return urllib.parse.urlunsplit((parsed.scheme.lower(), host + port, path, urllib.parse.urlencode(sorted(query)), ""))
    except (ValueError, TypeError):
        return text


def sanitized_research_copy(value: str) -> str:
    result = compact(value)
    for pattern, replacement in FINANCE_COPY_REPLACEMENTS:
        result = pattern.sub(replacement, result)
    return compact(result)


def finance_terms(value: str) -> list[str]:
    found: list[str] = []
    for pattern, _ in FINANCE_COPY_REPLACEMENTS:
        found.extend(match.group(0).lower() for match in pattern.finditer(value))
    return unique(found)


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = compact(value, 180)
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def request_json(url: str, *, params: dict[str, Any] | None = None, payload: Any = None,
                 headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"User-Agent": "gatex-intelligence-trend-intake/1.0", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=request_headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def validate_gatex_intake_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname != "gatex.fund"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/api/integrations/intelligence/intake"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("GateX intake URL is not allowed") from exc
    return GATEX_INTAKE_URL


def post_gatex_intake(url: str, payload: dict[str, Any], token: str, timeout: int = 30) -> None:
    target = validate_gatex_intake_url(url)
    request = urllib.request.Request(
        target,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "gatex-intelligence-trend-intake/1.0",
        },
        method="POST",
    )
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        if not 200 <= int(response.status) < 300:
            raise OSError(f"GateX intake returned HTTP {response.status}")


def write_downloader_config(path: Path, output_dir: Path) -> None:
    cookie = os.environ.get("DOUYIN_COOKIE", "").strip()
    lines = [
        "link:", f'path: {json.dumps(str(output_dir), ensure_ascii=False)}', "mode:", "  - post",
        "number:", "  post: 0", "thread: 1", "retry_times: 2", "rate_limit: 2",
        "database: false", "folderstyle: true", "music: false", "cover: false",
        "avatar: false", "json: true", "comments:", "  enabled: false",
        "transcript:", "  enabled: false", "browser_fallback:", "  enabled: false",
    ]
    if cookie:
        lines.append("cookie: " + json.dumps(cookie, ensure_ascii=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def record_collector_run(results: list[dict[str, Any]] | None, *, source: str,
                         configured: bool, attempted: int, succeeded: int,
                         signal_count: int) -> None:
    if results is None:
        return
    results.append({
        "source": source,
        "configured": configured,
        "attempted": max(0, int(attempted)),
        "succeeded": max(0, int(succeeded)),
        "signalCount": max(0, int(signal_count)),
    })


def collect_douyin_hot_board(downloader_dir: Path | None, work_dir: Path, limit: int,
                              observed_at: dt.datetime, errors: list[dict[str, str]],
                              collector_runs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if not downloader_dir:
        record_collector_run(collector_runs, source="douyin_hot_board", configured=False,
                             attempted=0, succeeded=0, signal_count=0)
        return []
    if not (downloader_dir / "run.py").exists():
        errors.append({"source": "douyin_hot_board", "error": "downloader run.py is missing"})
        record_collector_run(collector_runs, source="douyin_hot_board", configured=True,
                             attempted=1, succeeded=0, signal_count=0)
        return []
    output_dir = work_dir / "douyin"
    config_path = work_dir / "douyin-config.yml"
    write_downloader_config(config_path, output_dir)
    try:
        completed = subprocess.run(
            ["python3", str(downloader_dir / "run.py"), "-c", str(config_path),
             "--hot-board", str(limit), "-p", str(output_dir), "--show-warnings"],
            cwd=downloader_dir, check=False, timeout=90, text=True,
        )
        succeeded = int(completed.returncode == 0)
        if not succeeded:
            errors.append({"source": "douyin_hot_board", "error": f"collector exit={completed.returncode}"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append({"source": "douyin_hot_board", "error": str(exc)})
        record_collector_run(collector_runs, source="douyin_hot_board", configured=True,
                             attempted=1, succeeded=0, signal_count=0)
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted((output_dir / "hot_board").glob("*.jsonl"))[-2:]:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            title = compact(item.get("word") or item.get("sentence") or item.get("title") or item.get("keyword"), 240)
            if not title:
                continue
            rank = int(item.get("position") or item.get("rank") or len(rows) + 1)
            hot_value = number(item.get("hot_value") or item.get("hot_score") or item.get("view_count"))
            raw_url = compact(item.get("url") or item.get("share_url"), 2000)
            rows.append(signal(
                source_id="douyin-hot-board", source_kind="douyin_hot_board",
                external_id=f"douyin-hot:{stable_hash(title)}", title=title, summary=compact(item.get("word_cover_title") or ""),
                url=raw_url if raw_url.startswith("http") else "",
                publisher="Douyin hot search", observed_at=observed_at, rank=rank,
                metrics={"views": hot_value}, metadata={"hotValue": hot_value, "collectorRef": DOUYIN_DOWNLOADER_REF},
            ))
    rows = dedupe_signals(rows)
    record_collector_run(collector_runs, source="douyin_hot_board", configured=True,
                         attempted=1, succeeded=succeeded, signal_count=len(rows))
    return rows


def collect_gdelt(queries: list[str], observed_at: dt.datetime, errors: list[dict[str, str]],
                  collector_runs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    succeeded = 0
    for query in queries:
        try:
            data = request_json(GDELT_URL, params={
                "query": query, "mode": "ArtList", "format": "json", "maxrecords": 25,
                "timespan": "24h", "sort": "HybridRel",
            })
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append({"source": "gdelt", "query": query, "error": str(exc)})
            continue
        succeeded += 1
        for item in data.get("articles") or []:
            title = compact(item.get("title"), 240)
            url = canonical_url(item.get("url"))
            if not title:
                continue
            rows.append(signal(
                source_id="gdelt-doc", source_kind="gdelt", external_id=f"gdelt:{stable_hash(url or title)}",
                title=title, summary=compact(item.get("domain") or item.get("sourcecountry") or ""), url=url,
                publisher=compact(item.get("domain") or "GDELT indexed source", 160),
                published_at=parse_time(item.get("seendate")), observed_at=observed_at,
                metadata={"query": query, "sourceCountry": item.get("sourcecountry"), "language": item.get("language")},
            ))
    rows = dedupe_signals(rows)
    record_collector_run(collector_runs, source="gdelt", configured=True,
                         attempted=len(queries), succeeded=succeeded, signal_count=len(rows))
    return rows


def collect_hotspot_assistant(observed_at: dt.datetime, errors: list[dict[str, str]],
                              collector_runs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    url = os.environ.get("HOTSPOT_ASSISTANT_API_URL", "").strip()
    if not url:
        record_collector_run(collector_runs, source="hotspot_assistant", configured=False,
                             attempted=0, succeeded=0, signal_count=0)
        return []
    headers: dict[str, str] = {}
    key = os.environ.get("HOTSPOT_ASSISTANT_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        data = request_json(url, payload={"category": "business-technology", "window": "24h", "limit": 50}, headers=headers)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        errors.append({"source": "hotspot_assistant", "error": str(exc)})
        record_collector_run(collector_runs, source="hotspot_assistant", configured=True,
                             attempted=1, succeeded=0, signal_count=0)
        return []
    rows: list[dict[str, Any]] = []
    for item in walk_records(data):
        title = compact(item.get("title") or item.get("name") or item.get("word") or item.get("topic"), 240)
        if not title:
            continue
        url_value = canonical_url(item.get("url") or item.get("link"))
        rows.append(signal(
            source_id="hotspot-assistant", source_kind="hotspot_assistant",
            external_id=compact(item.get("id"), 160) or f"hotspot:{stable_hash(url_value or title)}",
            title=title, summary=compact(item.get("summary") or item.get("description") or item.get("snippet")),
            url=url_value, publisher=compact(item.get("publisher") or item.get("source") or "Hotspot assistant", 160),
            published_at=parse_time(item.get("publishedAt") or item.get("published_at") or item.get("date")),
            observed_at=observed_at, rank=int(number(item.get("rank"))) or None,
            metrics={"views": number(item.get("hotValue") or item.get("hot_value") or item.get("score"))},
            metadata={"provider": "configured_hotspot_assistant"},
        ))
    rows = dedupe_signals(rows)
    record_collector_run(collector_runs, source="hotspot_assistant", configured=True,
                         attempted=1, succeeded=1, signal_count=len(rows))
    return rows


def collect_tikhub(terms: list[str], observed_at: dt.datetime, errors: list[dict[str, str]],
                    collector_runs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    key = os.environ.get("TIKHUB_API_KEY", "").strip()
    if not key:
        errors.append({"source": "tikhub", "error": "TIKHUB_API_KEY is not configured"})
        record_collector_run(collector_runs, source="tikhub", configured=True,
                             attempted=1, succeeded=0, signal_count=0)
        return []
    rows: list[dict[str, Any]] = []
    selected_terms = terms[:4]
    succeeded = 0
    for term in selected_terms:
        try:
            data = request_json(
                TIKHUB_URL,
                payload={"keyword": term, "cursor": 0, "sort_type": "1", "publish_time": "1", "filter_duration": "0", "content_type": "1"},
                headers={"Authorization": f"Bearer {key}"},
            )
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append({"source": "tikhub", "query": term, "error": str(exc)})
            continue
        succeeded += 1
        for item in walk_records(data):
            if not item.get("aweme_id") or not (item.get("statistics") or item.get("video")):
                continue
            stats = item.get("statistics") or {}
            aweme_id = str(item.get("aweme_id"))
            rows.append(signal(
                source_id="tikhub-douyin-search", source_kind="tikhub", external_id=aweme_id,
                title=compact(item.get("desc") or item.get("title"), 240),
                summary="", url=f"https://www.douyin.com/video/{aweme_id}",
                publisher=compact((item.get("author") or {}).get("nickname") or "Douyin", 160),
                published_at=parse_time(item.get("create_time")), observed_at=observed_at,
                metrics={
                    "views": number(stats.get("play_count")), "likes": number(stats.get("digg_count")),
                    "comments": number(stats.get("comment_count")), "shares": number(stats.get("share_count")),
                }, metadata={"query": term},
            ))
    rows = dedupe_signals(rows)
    record_collector_run(collector_runs, source="tikhub", configured=True,
                         attempted=len(selected_terms), succeeded=succeeded, signal_count=len(rows))
    return rows


def walk_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_records(child)


def number(value: Any) -> float:
    try:
        result = float(value or 0)
        return result if math.isfinite(result) and result > 0 else 0
    except (TypeError, ValueError):
        return 0


def signal(*, source_id: str, source_kind: str, external_id: str, title: str, summary: str,
           url: str, publisher: str, observed_at: dt.datetime, published_at: dt.datetime | None = None,
           rank: int | None = None, metrics: dict[str, float] | None = None,
           metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_text(title)
    return {
        "sourceId": source_id, "sourceKind": source_kind, "externalId": external_id,
        "identityKey": f"{source_id}:{external_id}", "title": compact(title, 240),
        "normalizedTitle": normalized, "summary": compact(summary), "url": canonical_url(url),
        "publisher": compact(publisher, 160), "publishedAt": iso(published_at) if published_at else None,
        "observedAt": iso(observed_at), "rank": rank, "metrics": metrics or {},
        "contentHash": content_sha256(f"{normalized}|{compact(summary)}"),
        "qualityScore": SOURCE_QUALITY.get(source_kind, 0.45), "metadata": metadata or {},
    }


def dedupe_signals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["identityKey"]
        if key not in result or row["observedAt"] > result[key]["observedAt"]:
            result[key] = row
    return list(result.values())


def tokens(value: str) -> set[str]:
    result: set[str] = set()
    for part in re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]+", normalize_text(value)):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            if len(part) <= 2:
                result.add(part)
            result.update(part[index:index + 2] for index in range(len(part) - 1))
        else:
            result.add(part)
    return result


def similarity(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(1, len(left | right))


def metric_weight(metrics: dict[str, Any]) -> float:
    return (number(metrics.get("views")) * 0.02 + number(metrics.get("likes")) +
            number(metrics.get("comments")) * 4 + number(metrics.get("shares")) * 6)


def enrich_with_history(rows: list[dict[str, Any]], state: dict[str, Any], now: dt.datetime,
                        terms: list[str], half_life_hours: float) -> None:
    observations = state.setdefault("observations", {})
    for row in rows:
        previous = observations.get(row["identityKey"]) or {}
        previous_time = parse_time(previous.get("lastSeenAt"))
        hours = max(1 / 6, (now - previous_time).total_seconds() / 3600) if previous_time else 0
        delta = max(0, metric_weight(row["metrics"]) - metric_weight(previous.get("metrics") or {}))
        engagement_velocity = min(1.0, math.log10(1 + delta / hours) / 5) if hours else 0
        previous_rank, current_rank = number(previous.get("rank")), number(row.get("rank"))
        rank_velocity = min(1.0, (previous_rank - current_rank) / max(5, previous_rank) / hours * 6) if hours and previous_rank and current_rank else 0
        row["velocity"] = round(max(0.0, engagement_velocity, rank_velocity), 4)
        evidence_time = parse_time(row.get("publishedAt")) or parse_time(previous.get("firstSeenAt")) or now
        age_hours = max(0, (now - evidence_time).total_seconds() / 3600)
        row["decay"] = round(math.exp(-math.log(2) * age_hours / max(1, half_life_hours)), 4)
        row["engagement"] = round(min(1.0, math.log10(1 + metric_weight(row["metrics"])) / 7), 4)
        haystack = normalize_text(f"{row['title']} {row['summary']}")
        matches = [term for term in terms if term_matches(haystack, term)]
        row["matchedTerms"] = unique(matches)
        row["relevance"] = round(min(1.0, len(matches) * 0.35), 4)
        first_seen = previous.get("firstSeenAt") or row["observedAt"]
        observations[row["identityKey"]] = {
            "firstSeenAt": first_seen, "lastSeenAt": row["observedAt"],
            "metrics": row["metrics"], "rank": row.get("rank"), "title": row["title"],
        }
        row["firstSeenAt"] = first_seen
        row["lastSeenAt"] = row["observedAt"]
    cutoff = now - dt.timedelta(days=14)
    state["observations"] = {
        key: value for key, value in observations.items()
        if (parse_time(value.get("lastSeenAt"), cutoff) or cutoff) >= cutoff
    }
    state["updatedAt"] = iso(now)


def cluster_rows(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for row in sorted(rows, key=lambda item: (item["observedAt"], item["identityKey"]), reverse=True):
        row_tokens = tokens(f"{row['title']} {row['summary']}")
        matching: list[list[dict[str, Any]]] = []
        for cluster in clusters:
            cluster_tokens = set().union(*(tokens(f"{item['title']} {item['summary']}") for item in cluster))
            if any(item["contentHash"] == row["contentHash"] or (item["url"] and item["url"] == row["url"]) for item in cluster):
                matching.append(cluster)
            elif similarity(cluster_tokens, row_tokens) >= 0.52:
                matching.append(cluster)
        if matching:
            target = matching[0]
            target.append(row)
            # A bridge can connect clusters discovered in either arrival order.
            # Merge every match so one topic cannot retain multiple IDs.
            for sibling in matching[1:]:
                target.extend(sibling)
                clusters.remove(sibling)
        else:
            clusters.append([row])
    return clusters


def source_count(cluster: list[dict[str, Any]]) -> int:
    kinds = {item["sourceKind"] for item in cluster}
    domains = {urllib.parse.urlsplit(item["url"]).hostname for item in cluster if item["url"]}
    publishers = {normalize_text(item["publisher"]) for item in cluster if item["publisher"]}
    return max(len(kinds), len(domains), len(publishers))


def cluster_member_signature(item: dict[str, Any]) -> str:
    topics = sorted({normalize_text(term) for term in item["matchedTerms"] if normalize_text(term)})
    title_tokens = sorted(tokens(item["title"]))[:12]
    material = "|".join(topics + title_tokens) or item["identityKey"]
    return stable_hash(material)


def cluster_fingerprint(cluster: list[dict[str, Any]], aliases: dict[str, str]) -> str:
    signatures = sorted({cluster_member_signature(item) for item in cluster})
    known = sorted({
        aliases[signature]
        for signature in signatures
        if re.fullmatch(r"trend-[0-9a-f]{16}", str(aliases.get(signature) or ""))
    })
    anchor = min(cluster, key=lambda item: (item["firstSeenAt"], item["identityKey"]))
    cluster_id = known[0] if known else f"trend-{cluster_member_signature(anchor)}"
    for signature, prior in list(aliases.items()):
        if prior in known:
            aliases[signature] = cluster_id
    for signature in signatures:
        aliases[signature] = cluster_id
    return cluster_id


def build_proposals(rows: list[dict[str, Any]], now: dt.datetime, minimum_score: float,
                    minimum_sources: int,
                    cluster_aliases: dict[str, str] | None = None) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    aliases = cluster_aliases if cluster_aliases is not None else {}
    for cluster in cluster_rows(rows):
        representative = min(cluster, key=lambda item: (
            -(item["relevance"] * .45 + item["qualityScore"] * .35 + item["decay"] * .2),
            item["identityKey"],
        ))
        diversity_count = source_count(cluster)
        relevance = max(item["relevance"] for item in cluster)
        velocity = max(item["velocity"] for item in cluster)
        decay = max(item["decay"] for item in cluster)
        engagement = max(item["engagement"] for item in cluster)
        quality = sum(item["qualityScore"] for item in cluster) / len(cluster)
        diversity = min(1.0, diversity_count * .35)
        total = relevance * 30 + velocity * 20 + decay * 15 + diversity * 15 + engagement * 10 + quality * 10
        review_state = "proposed" if total >= minimum_score and diversity_count >= minimum_sources and decay >= .25 else "observing"
        first_seen = min(item["firstSeenAt"] for item in cluster)
        last_seen = max(item["lastSeenAt"] for item in cluster)
        cluster_id = cluster_fingerprint(cluster, aliases)
        public_title = sanitized_research_copy(representative["title"])
        sources, evidence = [], []
        for item in sorted(cluster, key=lambda value: value["qualityScore"] * value["decay"], reverse=True)[:12]:
            sources.append({
                "kind": item["sourceKind"], "url": item["url"], "title": item["title"],
                "publisher": item["publisher"], "publishedAt": item["publishedAt"],
                "excerpt": item["summary"] or item["title"], "contentHash": item["contentHash"],
                "qualityScore": round(item["qualityScore"], 4), "relevanceScore": item["relevance"],
                "metadata": {"sourceId": item["sourceId"], "externalId": item["externalId"],
                             "firstSeenAt": item["firstSeenAt"], "lastSeenAt": item["lastSeenAt"],
                             "rank": item.get("rank"), "metrics": item["metrics"]},
            })
            evidence.append({
                **({"sourceUrl": item["url"]} if item["url"] else {}),
                "sourceExternalId": item["externalId"],
                "claimId": f"trend-evidence-{stable_hash(item['identityKey'] + item['contentHash'])}",
                "excerpt": item["summary"] or item["title"],
                "confidence": round(min(1.0, item["qualityScore"] * .6 + item["decay"] * .25 + item["relevance"] * .15), 4),
                "status": "corroborated" if diversity_count >= 2 else "observed",
                "metadata": {"observedAt": item["lastSeenAt"], "sourceKind": item["sourceKind"]},
            })
        proposal = {
            "schema": "gatex-intelligence-intake/v1", "channelKey": "market-trend-daily",
            "externalId": cluster_id,
            "idempotencyKey": f"{cluster_id}:{last_seen[:10]}",
            "topic": {
                "title": public_title,
                "brief": sanitized_research_copy(
                    f"Why it is moving now: {len(sources)} source records tracked this development "
                    f"between {first_seen[:10]} and {last_seen[:10]}. Use the linked evidence to "
                    "decide whether to generate a timely GateX analysis in Report Studio."
                ),
                "industry": "Market & Industry", "language": "en", "accessScope": "member",
                "priority": "P0" if total >= 78 else "P1" if total >= 62 else "P2",
                "provenanceType": "trend_proposal",
                "contentMode": "hotspot_topic_card",
            },
            "triggerDraft": False,
            "metadata": {
                "productionMethod": "market_trend_daily",
                "scanCadence": "daily",
                "nextAction": "generate_in_report_studio",
                "autoPublish": False,
            },
            "sources": sources, "evidence": evidence,
            "trend": {
                "firstSeenAt": first_seen, "lastSeenAt": last_seen, "velocity": round(velocity, 4),
                "decay": round(decay, 4), "cluster": cluster_id, "reviewState": review_state,
                "score": {"total": round(total, 2), "relevance": relevance, "velocity": velocity,
                          "freshness": decay, "sourceDiversity": diversity, "engagement": engagement,
                          "sourceQuality": round(quality, 4)},
                "independentSourceCount": diversity_count,
                "financeBoundaryTerms": unique(term for item in cluster for term in finance_terms(f"{item['title']} {item['summary']}")),
            },
        }
        proposals.append(proposal)
    return sorted(proposals, key=lambda item: item["trend"]["score"]["total"], reverse=True)


def post_proposals(proposals: list[dict[str, Any]], errors: list[dict[str, str]]) -> dict[str, int]:
    url = os.environ.get("GATEX_INTELLIGENCE_INTAKE_URL", "").strip()
    token = os.environ.get("GATEX_INTELLIGENCE_INTAKE_SECRET", "").strip()
    if not url or not token:
        return {"configured": 0, "attempted": 0, "accepted": 0}
    target = validate_gatex_intake_url(url)
    accepted = 0
    for proposal in proposals:
        try:
            post_gatex_intake(target, proposal, token, timeout=30)
            accepted += 1
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append({"source": "gatex_intake", "externalId": proposal["externalId"], "error": str(exc)})
    return {"configured": 1, "attempted": len(proposals), "accepted": accepted}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=Path("work/gatex_intelligence_trends"))
    parser.add_argument("--state", type=Path, default=Path("work/gatex_intelligence_trends/state/trend-history.json"))
    parser.add_argument("--output", type=Path, default=Path("work/gatex_intelligence_trends/proposals.json"))
    parser.add_argument("--downloader-dir", type=Path)
    parser.add_argument("--douyin-limit", type=int, default=100)
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument("--fixture", type=Path, help="Raw-signal fixture used for deterministic QA.")
    parser.add_argument("--enable-tikhub", action="store_true", help="Opt-in: TikHub requests are billable.")
    parser.add_argument("--post-if-configured", action="store_true")
    parser.add_argument(
        "--require-successful-collector",
        action="store_true",
        help="Fail without advancing history when no configured collector succeeds and no proposal is produced.",
    )
    parser.add_argument("--now", default="")
    parser.add_argument("--minimum-score", type=float, default=55)
    parser.add_argument("--minimum-sources", type=int, default=2)
    parser.add_argument("--half-life-hours", type=float, default=18)
    return parser.parse_args()


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback


def main() -> int:
    args = parse_args()
    now = parse_time(args.now) or utc_now()
    work_dir = args.work_dir.resolve()
    state_path = args.state.resolve()
    output_path = args.output.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, str]] = []
    collector_runs: list[dict[str, Any]] = []
    rows = load_json(args.fixture.resolve(), []) if args.fixture else []
    if isinstance(rows, dict):
        rows = rows.get("signals") or []
    if args.fixture:
        record_collector_run(collector_runs, source="fixture", configured=True,
                             attempted=1, succeeded=1, signal_count=len(rows))
    if not args.skip_network:
        rows.extend(collect_douyin_hot_board(args.downloader_dir.resolve() if args.downloader_dir else None,
                                            work_dir, args.douyin_limit, now, errors, collector_runs))
        rows.extend(collect_gdelt(DEFAULT_GDELT_QUERIES, now, errors, collector_runs))
        rows.extend(collect_hotspot_assistant(now, errors, collector_runs))
        if args.enable_tikhub:
            rows.extend(collect_tikhub(["人工智能 产业", "半导体 产业", "机器人 产业", "数据中心 算力"],
                                       now, errors, collector_runs))
    rows = dedupe_signals(rows)
    market_terms = unique(DEFAULT_MARKET_TERMS + [part for part in os.environ.get("GATEX_TREND_TERMS", "").split(",") if part.strip()])
    rows = [row for row in rows if any(term_matches(f"{row['title']} {row['summary']}", term) for term in market_terms)]
    state = load_json(state_path, {"schema": "gatex-intelligence-trend-state/v1", "observations": {}})
    enrich_with_history(rows, state, now, market_terms, args.half_life_hours)
    cluster_aliases = state.setdefault("clusterAliases", {})
    if not isinstance(cluster_aliases, dict):
        raise ValueError("trend cluster alias history is invalid")
    proposals = build_proposals(
        rows, now, args.minimum_score, max(1, args.minimum_sources), cluster_aliases
    )
    delivery = post_proposals(proposals, errors) if args.post_if_configured else {"configured": 0, "attempted": 0, "accepted": 0}
    collector_success_count = sum(
        int(item["succeeded"] > 0) for item in collector_runs if item["configured"]
    )
    collector_failure = bool(
        args.require_successful_collector and not proposals and collector_success_count == 0
    )
    delivery_failure = delivery["attempted"] != delivery["accepted"]
    history_saved = not collector_failure and not delivery_failure
    if history_saved:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    payload = {
        "schema": "gatex-intelligence-trend-batch/v1", "generatedAt": iso(now),
        "sourceSignalCount": len(rows), "proposalCount": len(proposals),
        "proposedCount": sum(item["trend"]["reviewState"] == "proposed" for item in proposals),
        "observingCount": sum(item["trend"]["reviewState"] == "observing" for item in proposals),
        "collectorRuns": collector_runs, "collectorSuccessCount": collector_success_count,
        "collectorFailure": collector_failure, "historySaved": history_saved,
        "clusterAliasCount": len(cluster_aliases),
        "delivery": delivery, "errors": errors, "proposals": proposals,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work_dir / "raw_signals.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work_dir / "run_summary.json").write_text(json.dumps({key: value for key, value in payload.items() if key != "proposals"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "signals": len(rows), "proposals": len(proposals), "delivery": delivery, "errors": len(errors)}))
    return 1 if collector_failure or delivery_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
