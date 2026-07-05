#!/usr/bin/env python3
"""Discover recent high-like Douyin entertainment videos and package them as KC clips."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
DEFAULT_SEED_KEYWORDS = ",".join(
    [
        "明星",
        "娱乐 明星",
        "娱乐圈",
        "内娱",
        "综艺 明星",
        "电视剧 明星",
        "电影 明星",
        "红毯",
        "演唱会",
        "短剧 明星",
    ]
)
DEFAULT_MUST_INCLUDE_TERMS = ",".join(
    [
        "明星",
        "演员",
        "艺人",
        "歌手",
        "爱豆",
        "偶像",
        "内娱",
        "娱乐圈",
        "女明星",
        "男明星",
        "女星",
        "男星",
        "综艺",
        "电视剧",
        "影视",
        "影评",
        "电影解说",
        "短剧",
        "演唱会",
        "唱歌",
        "歌单",
        "禁唱",
        "彩排",
        "演跳会",
        "红毯",
        "百花奖",
        "剧集",
        "好剧",
        "热剧",
        "舞台",
        "名场面",
        "爆笑",
        "访谈",
        "采访",
        "嘉宾",
        "主持",
        "跑男",
        "奔跑吧",
        "中餐厅",
        "浪姐",
        "乘风",
        "披荆斩棘",
        "小姐不熙娣",
        "快乐老家",
        "经典老歌",
        "欧美音乐",
        "音乐",
        "好剧推荐",
        "盗墓笔记",
        "南部档案",
        "十日终焉",
        "全民目击",
        "破冰行动",
        "主演",
        "主创",
        "代言",
        "品牌代言人",
        "王俊凯",
        "薛之谦",
        "左手指月",
        "张韶涵",
        "宁静",
        "那英",
        "王一博",
        "肖战",
        "白鹿",
        "赵露思",
        "范丞丞",
        "黄明昊",
        "杨迪",
        "詹青云",
        "庞颖",
        "戴军",
        "李静",
        "孙浩",
        "白敬亭",
        "张国强",
        "杨紫",
        "赵丽颖",
        "迪丽热巴",
        "易烊千玺",
        "刘亦菲",
        "于正",
    ]
)
DEFAULT_EXCLUDE_TERMS = ",".join(
    [
        "游戏",
        "永劫无间",
        "王者荣耀",
        "和平精英",
        "原神",
        "二次元",
        "画画",
        "青年艺术家计划",
        "未来导演扶持计划",
        "AI创作",
        "AIGC",
        "AI影像",
        "AI短剧",
        "AI漫剧",
        "原创故事",
        "剪辑技巧",
        "后期教程",
        "vlog技巧",
        "电影感",
        "高考",
        "财经",
        "科普",
        "英语口语",
        "外贸",
        "省钱",
        "营销思维",
        "商业思维",
        "防溺水",
        "滑雪",
        "冷泡茶",
        "地铁",
        "物理课",
        "化学实验",
        "机甲",
        "职场",
        "生活",
    ]
)


def main() -> int:
    args = parse_args()
    run_id = args.run_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    work_root = project_path(args.work_root)
    run_dir = project_path(args.run_dir) if args.run_dir else work_root / "runs" / run_id
    output_dir = project_path(args.output_dir) if args.output_dir else ROOT / "outputs" / "kc_entertain" / run_id
    kc_work_dir = project_path(args.kc_work_dir)
    downloader_dir = project_path(args.downloader_dir)
    python_bin = resolve_python()
    source_provider = resolve_source_provider(args.provider)
    summary: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source_provider": source_provider,
        "limit": args.limit,
        "min_selected_videos": args.min_selected_videos,
        "recent_hours": args.recent_hours,
        "python": python_bin,
        "run_dir": str(run_dir),
        "selected_dir": str(run_dir / "selected"),
        "output_dir": str(output_dir),
        "commands": [],
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    if source_provider == "tikhub":
        discovery_cmd = [
            python_bin,
            str(ROOT / "tools" / "run_douyin_tikhub_daily.py"),
            "--work-dir",
            str(run_dir),
            "--limit",
            str(args.limit),
            "--min-selected-videos",
            str(args.min_selected_videos),
            "--recent-hours",
            str(args.recent_hours),
            "--max-duration-seconds",
            str(args.max_duration_seconds),
            "--download-candidate-multiplier",
            str(args.download_candidate_multiplier),
            "--pages-per-keyword",
            str(args.tikhub_pages_per_keyword),
            "--request-timeout-seconds",
            str(args.tikhub_request_timeout_seconds),
            "--download-timeout-seconds",
            str(args.tikhub_download_timeout_seconds),
            "--download-max-urls",
            str(args.tikhub_download_max_urls),
            "--seed-keywords",
            args.seed_keywords,
            "--must-include-terms",
            args.must_include_terms,
            "--exclude-terms",
            args.exclude_terms,
        ]
    else:
        ensure_downloader(downloader_dir, install_deps=args.install_downloader_deps, python_bin=python_bin)
        discovery_cmd = [
            python_bin,
            str(ROOT / "tools" / "run_douyin_entertain_free.py"),
            "--downloader-dir",
            str(downloader_dir),
            "--work-dir",
            str(run_dir),
            "--limit",
            str(args.limit),
            "--recent-hours",
            str(args.recent_hours),
            "--max-duration-seconds",
            str(args.max_duration_seconds),
            "--search-max",
            str(args.search_max),
            "--feed-pages",
            str(args.feed_pages),
            "--download-candidate-multiplier",
            str(args.download_candidate_multiplier),
            "--min-selected-videos",
            str(args.min_selected_videos),
            "--downloader-link-timeout-seconds",
            str(args.downloader_link_timeout_seconds),
            "--downloader-concurrency",
            str(args.downloader_concurrency),
            "--downloader-timeout-seconds",
            str(args.downloader_timeout_seconds),
            "--direct-download-timeout-seconds",
            str(args.direct_download_timeout_seconds),
            "--direct-download-max-urls",
            str(args.direct_download_max_urls),
            "--yt-dlp-timeout-seconds",
            str(args.yt_dlp_timeout_seconds),
            "--seed-keywords",
            args.seed_keywords,
            "--must-include-terms",
            args.must_include_terms,
            "--exclude-terms",
            args.exclude_terms,
        ]
        discovery_cmd.append("--direct-search" if args.direct_search else "--no-direct-search")
        if args.browser_keywords:
            discovery_cmd.extend(["--browser-keywords", str(args.browser_keywords)])
        if args.direct_download:
            discovery_cmd.append("--direct-download")
        if args.yt_dlp_download:
            discovery_cmd.append("--yt-dlp-download")
    run(discovery_cmd, summary)

    selected_dir = run_dir / "selected"
    selected_files = sorted(path for path in selected_dir.glob("*") if path.suffix.lower() in VIDEO_EXTENSIONS)
    summary["selected_file_count"] = len(selected_files)
    summary["selected_files"] = [str(path) for path in selected_files]
    mirror_latest(run_dir, work_root / "latest")

    if args.search_only or args.skip_kc:
        write_summary(run_dir, summary)
        print(f"Discovery complete. Selected videos: {len(selected_files)}")
        print(f"Run directory: {run_dir}")
        return 0

    if not selected_files:
        summary["kc_skipped"] = "no selected videos"
        write_summary(run_dir, summary)
        print("No selected videos were downloaded; KC packaging skipped.")
        print(f"Reports: {run_dir / 'reports'}")
        return 3

    minimum_selected = min(max(1, int(args.limit)), max(1, int(args.min_selected_videos)))
    if len(selected_files) < minimum_selected:
        summary["kc_skipped"] = f"only {len(selected_files)} selected videos; minimum is {minimum_selected}"
        write_summary(run_dir, summary)
        print(
            f"Only {len(selected_files)}/{args.limit} selected videos were downloaded; "
            f"minimum for KC publishing is {minimum_selected}. KC packaging skipped.",
            flush=True,
        )
        print(f"Reports: {run_dir / 'reports'}")
        return 3

    output_dir.mkdir(parents=True, exist_ok=True)
    kc_cmd = [
        python_bin,
        str(ROOT / "auto_kc_entertain.py"),
        "--input-dir",
        str(selected_dir),
        "--metadata-file",
        str(run_dir / "reports" / "selected.json"),
        "--output-dir",
        str(output_dir),
        "--work-dir",
        str(kc_work_dir),
        "--encoder",
        args.encoder,
        "--threads",
        str(args.threads),
    ]
    if args.force:
        kc_cmd.append("--force")
    if args.force_fallback:
        kc_cmd.append("--force-fallback")
    run(kc_cmd, summary)

    outputs_list = kc_work_dir / "last_run_outputs.txt"
    kc_outputs = []
    if outputs_list.exists():
        kc_outputs = [line.strip() for line in outputs_list.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary["kc_output_count"] = len(kc_outputs)
    summary["kc_outputs"] = kc_outputs
    write_summary(run_dir, summary)
    print(f"KC outputs: {len(kc_outputs)}")
    print(f"Run directory: {run_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--provider", choices=["auto", "free", "tikhub"], default=os.environ.get("KC_SOURCE_PROVIDER", "auto"))
    parser.add_argument("--recent-hours", type=int, default=24)
    parser.add_argument("--max-duration-seconds", type=int, default=300)
    parser.add_argument("--search-max", type=int, default=30)
    parser.add_argument("--feed-pages", type=int, default=60)
    parser.add_argument("--download-candidate-multiplier", type=int, default=4)
    parser.add_argument("--min-selected-videos", type=int, default=7)
    parser.add_argument("--downloader-link-timeout-seconds", type=int, default=60)
    parser.add_argument("--downloader-concurrency", type=int, default=4)
    parser.add_argument("--downloader-timeout-seconds", type=int, default=1800)
    parser.add_argument("--direct-download-timeout-seconds", type=int, default=45)
    parser.add_argument("--direct-download-max-urls", type=int, default=1)
    parser.add_argument("--yt-dlp-timeout-seconds", type=int, default=60)
    parser.add_argument("--tikhub-pages-per-keyword", type=int, default=1)
    parser.add_argument("--tikhub-request-timeout-seconds", type=int, default=45)
    parser.add_argument("--tikhub-download-timeout-seconds", type=int, default=120)
    parser.add_argument("--tikhub-download-max-urls", type=int, default=3)
    parser.add_argument("--seed-keywords", default=DEFAULT_SEED_KEYWORDS)
    parser.add_argument("--must-include-terms", default=DEFAULT_MUST_INCLUDE_TERMS)
    parser.add_argument("--exclude-terms", default=DEFAULT_EXCLUDE_TERMS)
    parser.add_argument("--direct-download", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--yt-dlp-download", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--direct-search", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--browser-keywords", type=int, default=0)
    parser.add_argument("--downloader-dir", type=Path, default=Path("/tmp/douyin-downloader"))
    parser.add_argument("--work-root", type=Path, default=ROOT / "work" / "douyin_free_daily")
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--kc-work-dir", type=Path, default=ROOT / "work" / "auto_kc_douyin_free")
    parser.add_argument("--encoder", choices=["auto", "videotoolbox", "libx264"], default="libx264")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-fallback", action="store_true")
    parser.add_argument("--search-only", action="store_true", help="Download selected videos and skip KC packaging.")
    parser.add_argument("--skip-kc", action="store_true", help="Alias for skipping KC packaging after download.")
    parser.add_argument("--install-downloader-deps", action="store_true")
    return parser.parse_args()


def resolve_source_provider(provider: str) -> str:
    if provider != "auto":
        return provider
    return "tikhub" if os.environ.get("TIKHUB_API_KEY", "").strip() else "free"


def ensure_downloader(downloader_dir: Path, *, install_deps: bool, python_bin: str) -> None:
    if not downloader_dir.exists():
        downloader_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/jiji262/douyin-downloader.git", str(downloader_dir)],
            check=True,
        )
    if not (downloader_dir / "run.py").exists():
        raise SystemExit(f"Douyin downloader is not usable: {downloader_dir}")
    if install_deps:
        subprocess.run([python_bin, "-m", "pip", "install", "-r", str(downloader_dir / "requirements.txt")], check=True)
        subprocess.run([python_bin, "-m", "pip", "install", "pillow", "httpx", "yt-dlp"], check=True)


def resolve_python() -> str:
    candidates = [
        os.getenv("KC_PYTHON", "").strip(),
        str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"),
        sys.executable,
        shutil.which("python3.12") or "",
        shutil.which("python3.11") or "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists() and python_version_ok(str(path)):
            return str(path)
    return sys.executable


def python_version_ok(python_bin: str) -> bool:
    try:
        result = subprocess.run(
            [python_bin, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def run(command: list[str], summary: dict[str, Any]) -> None:
    printable = " ".join(str(part) for part in command)
    print(f"+ {printable}", flush=True)
    summary["commands"].append(printable)
    subprocess.run(command, cwd=str(ROOT), check=True)


def mirror_latest(run_dir: Path, latest_dir: Path) -> None:
    if latest_dir.exists() or latest_dir.is_symlink():
        if latest_dir.is_symlink() or latest_dir.is_file():
            latest_dir.unlink()
        else:
            shutil.rmtree(latest_dir)
    shutil.copytree(run_dir, latest_dir, dirs_exist_ok=True)


def write_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    (run_dir / "kc_daily_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Douyin Free KC Daily",
        "",
        f"- Run: {summary.get('run_id')}",
        f"- Selected videos: {summary.get('selected_file_count', 0)}",
        f"- KC outputs: {summary.get('kc_output_count', 0)}",
        f"- Selected dir: `{summary.get('selected_dir')}`",
        f"- Output dir: `{summary.get('output_dir')}`",
    ]
    (run_dir / "kc_daily_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def project_path(path: Path) -> Path:
    path = Path(path).expanduser()
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())