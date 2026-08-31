#!/usr/bin/env python3
"""Replace displayed comment identities with synthetic nicknames and avatars.

The APIMart path creates at most one paid image task per work directory. That image
is requested as a 4x4 avatar atlas, then Pillow crops and varies the atlas into
one local avatar per parent/sub-comment.  If no API key is available, or any
network/API step fails, a locally drawn atlas is used instead.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import random
import secrets
import tempfile
import time
from typing import Any, Callable, Iterator
import urllib.error
import urllib.request
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageEnhance, ImageOps


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEY_FILE = ROOT / "api_key" / "api_mart.txt"
API_BASE_URL = "https://api.apimart.ai"
API_MODEL = "gpt-image-2"
ATLAS_GRID = 4
ATLAS_SIZE = 1024
AVATAR_SIZE = 256

ATLAS_PROMPT = (
    "A square 4 by 4 contact sheet containing sixteen clearly different, "
    "friendly social-media profile avatars. One centered head-and-shoulders "
    "cartoon character in every equal square tile; varied faces, hairstyles, "
    "clothes, colors and moods; clean flat illustration, simple backgrounds, "
    "strong separation between tiles. No text, letters, logos or watermark."
)

NAME_PREFIXES = (
    "半糖", "晚风", "月见", "薄荷", "松栗", "柚子", "白桃", "青禾",
    "南栀", "小满", "初晴", "微光", "拾柒", "云朵", "橘灯", "鹿鸣",
    "星野", "春山", "桃汽", "盐汽", "糯米", "茉白", "浅夏", "乌龙",
    "青柠", "卷卷", "泡芙", "木棉", "雨眠", "桑落", "不晚", "慢热",
)

NAME_NOUNS = (
    "同学", "队长", "饭团", "汽水", "奶盖", "星球", "海盐", "年糕",
    "松饼", "布丁", "飞鸟", "小岛", "云舟", "青豆", "栗子", "团子",
    "鲸鱼", "橘猫", "小熊", "灯塔", "月牙", "风铃", "蘑菇", "花卷",
    "乌梅", "麦芽", "竹影", "山雀", "野梨", "可可", "芋圆", "椰子",
)


@dataclass
class ApiResult:
    image: Image.Image | None
    create_calls: int
    task_id: str | None = None
    reason: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_api_key(path: Path) -> str | None:
    """Read an API key without logging its contents."""
    env_value = os.environ.get("APIMART_API_KEY", "").strip()
    if env_value:
        return env_value
    if not path.is_file():
        return None

    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("APIMART_API_KEY="):
            candidate = candidate.split("=", 1)[1].strip()
        return candidate.strip("\"'") or None
    return None


def safe_failure_reason(exc: Exception, api_key: str | None = None) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, (TimeoutError, timeout_error_type())):
        return "request_timeout"
    if isinstance(exc, urllib.error.URLError):
        return "network_error"
    message = f"{type(exc).__name__}: {exc}"
    if api_key:
        message = message.replace(api_key, "[redacted]")
    return message[:240]


def timeout_error_type() -> type[OSError]:
    # socket.timeout is an alias/subclass that varies slightly between Python
    # versions; keeping the import local avoids exposing another module global.
    import socket

    return socket.timeout


def request_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with open_api_request(request, timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent an Authorization header from following an API redirect."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


API_OPENER = urllib.request.build_opener(NoRedirectHandler())


def open_api_request(request: urllib.request.Request, timeout: float) -> Any:
    return API_OPENER.open(request, timeout=timeout)


def extract_task_id(payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    candidates: list[Any]
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        candidates = [data]
    else:
        candidates = [payload]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        task_id = item.get("task_id") or item.get("id")
        if task_id:
            return str(task_id)
    raise ValueError("APIMart creation response did not contain a task_id")


def extract_image_url(payload: Any) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    result = data.get("result") or {}
    images = result.get("images") or []
    if not images or not isinstance(images[0], dict):
        raise ValueError("APIMart completed task did not contain images")
    urls = images[0].get("url")
    if isinstance(urls, list) and urls:
        return str(urls[0])
    if isinstance(urls, str) and urls:
        return urls
    raise ValueError("APIMart completed task did not contain an image URL")


def normalize_atlas(image: Image.Image) -> Image.Image:
    image.load()
    rgb = image.convert("RGB")
    return ImageOps.fit(
        rgb,
        (ATLAS_SIZE, ATLAS_SIZE),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def create_apimart_atlas(
    api_key: str,
    *,
    timeout_seconds: float,
    poll_interval: float,
    on_task_created: Callable[[str], None] | None = None,
) -> ApiResult:
    """Create exactly one APIMart task, poll it, and download its first image."""
    deadline = time.monotonic() + timeout_seconds
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "entertain-cut-identities/1.0",
    }
    create_calls = 0
    task_id: str | None = None

    try:
        # Intentionally no POST retry: an ambiguous timeout may still have
        # created a billable task upstream.
        create_calls = 1
        creation_payload = request_json(
            f"{API_BASE_URL}/v1/images/generations",
            headers=headers,
            timeout=min(60.0, max(1.0, timeout_seconds)),
            method="POST",
            payload={
                "model": API_MODEL,
                "prompt": ATLAS_PROMPT,
                "size": "1:1",
                "resolution": "1k",
                "n": 1,
                "official_fallback": False,
            },
        )
        task_id = extract_task_id(creation_payload)
        if on_task_created is not None:
            on_task_created(task_id)

        transient_poll_errors = 0
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                status_payload = request_json(
                    f"{API_BASE_URL}/v1/tasks/{task_id}",
                    headers=headers,
                    timeout=min(45.0, max(1.0, remaining)),
                )
                transient_poll_errors = 0
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504}:
                    raise
                transient_poll_errors += 1
                if transient_poll_errors >= 4:
                    raise exc
                continue
            except (TimeoutError, urllib.error.URLError) as exc:
                transient_poll_errors += 1
                if transient_poll_errors >= 4:
                    raise exc
                continue

            data = status_payload.get("data", status_payload)
            status = str(data.get("status", "")).lower() if isinstance(data, dict) else ""
            if status == "completed":
                image_url = extract_image_url(status_payload)
                parsed = urlparse(image_url)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise ValueError("APIMart returned an invalid image URL")

                # Do not attach Authorization when downloading from the image
                # or CDN host.
                download_request = urllib.request.Request(
                    image_url,
                    headers={"User-Agent": "entertain-cut-identities/1.0"},
                    method="GET",
                )
                with urllib.request.urlopen(
                    download_request,
                    timeout=min(60.0, max(1.0, deadline - time.monotonic())),
                ) as download:
                    content = download.read(25 * 1024 * 1024 + 1)
                if len(content) > 25 * 1024 * 1024:
                    raise ValueError("APIMart image exceeded 25 MB")
                image = normalize_atlas(Image.open(BytesIO(content)))
                return ApiResult(
                    image=image,
                    create_calls=create_calls,
                    task_id=task_id,
                )
            if status in {"failed", "cancelled", "canceled"}:
                return ApiResult(
                    image=None,
                    create_calls=create_calls,
                    task_id=task_id,
                    reason=f"task_{status}",
                )
            # APIMart documentation uses both pending/processing and
            # submitted/in_progress across task endpoints; all other
            # nonterminal statuses keep polling until the deadline.

        return ApiResult(
            image=None,
            create_calls=create_calls,
            task_id=task_id,
            reason="poll_timeout",
        )
    except Exception as exc:  # noqa: BLE001 - fallback is the intended behavior
        return ApiResult(
            image=None,
            create_calls=create_calls,
            task_id=task_id,
            reason=safe_failure_reason(exc, api_key),
        )


def local_avatar_tile(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    rng: random.Random,
) -> None:
    left, top, right, bottom = box
    width = right - left
    palette = [
        (255, 217, 225), (212, 235, 255), (224, 245, 218), (255, 235, 190),
        (226, 218, 255), (205, 242, 239), (246, 219, 193), (235, 229, 218),
    ]
    background = rng.choice(palette)
    draw.rectangle(box, fill=background)
    for stripe in range(5):
        alpha_color = tuple(max(0, channel - 10 - stripe * 4) for channel in background)
        y = top + stripe * width // 5
        draw.rectangle((left, y, right, y + width // 12), fill=alpha_color)

    skin = rng.choice([(255, 218, 185), (241, 194, 150), (216, 158, 112), (166, 112, 78)])
    hair = rng.choice([(50, 38, 34), (92, 57, 42), (30, 31, 38), (116, 74, 47), (53, 49, 79)])
    shirt = rng.choice(
        [(239, 92, 112), (62, 151, 209), (64, 177, 132), (244, 156, 69), (130, 104, 199)]
    )

    cx = (left + right) // 2
    head_w = int(width * rng.uniform(0.42, 0.49))
    head_h = int(width * rng.uniform(0.49, 0.56))
    head_top = top + int(width * 0.20)
    head_box = (
        cx - head_w // 2,
        head_top,
        cx + head_w // 2,
        head_top + head_h,
    )
    draw.ellipse(head_box, fill=skin)
    draw.pieslice(
        (head_box[0] - width * 0.03, head_box[1] - width * 0.08,
         head_box[2] + width * 0.03, head_box[1] + head_h * 0.68),
        180,
        360,
        fill=hair,
    )
    if rng.random() < 0.5:
        draw.polygon(
            [(head_box[0], head_top + width * 0.05),
             (cx, head_top - width * 0.07),
             (head_box[2], head_top + width * 0.05)],
            fill=hair,
        )
    eye_y = head_top + int(head_h * 0.52)
    eye_r = max(3, width // 50)
    eye_dx = head_w // 5
    draw.ellipse(
        (cx - eye_dx - eye_r, eye_y - eye_r, cx - eye_dx + eye_r, eye_y + eye_r),
        fill=(42, 40, 43),
    )
    draw.ellipse(
        (cx + eye_dx - eye_r, eye_y - eye_r, cx + eye_dx + eye_r, eye_y + eye_r),
        fill=(42, 40, 43),
    )
    mouth_y = head_top + int(head_h * 0.72)
    draw.arc(
        (
            cx - width * 0.08,
            mouth_y - width * 0.02,
            cx + width * 0.08,
            mouth_y + width * 0.08,
        ),
        10,
        170,
        fill=(130, 65, 70),
        width=max(3, width // 64),
    )
    draw.ellipse(
        (
            left + width * 0.17,
            top + width * 0.73,
            right - width * 0.17,
            bottom + width * 0.22,
        ),
        fill=shirt,
    )


def create_local_atlas(seed: int) -> Image.Image:
    rng = random.Random(seed ^ 0x5A17A5)
    atlas = Image.new("RGB", (ATLAS_SIZE, ATLAS_SIZE), (238, 238, 238))
    draw = ImageDraw.Draw(atlas)
    cell = ATLAS_SIZE // ATLAS_GRID
    for index in range(ATLAS_GRID * ATLAS_GRID):
        row, column = divmod(index, ATLAS_GRID)
        box = (column * cell, row * cell, (column + 1) * cell, (row + 1) * cell)
        local_avatar_tile(draw, box, rng)
    return atlas


def save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


def atlas_cell(atlas: Image.Image, index: int, rng: random.Random) -> Image.Image:
    atlas = normalize_atlas(atlas)
    cell_size = ATLAS_SIZE // ATLAS_GRID
    tile_index = index % (ATLAS_GRID * ATLAS_GRID)
    row, column = divmod(tile_index, ATLAS_GRID)
    inset = int(cell_size * 0.045)
    tile = atlas.crop(
        (
            column * cell_size + inset,
            row * cell_size + inset,
            (column + 1) * cell_size - inset,
            (row + 1) * cell_size - inset,
        )
    )
    centering = (rng.uniform(0.42, 0.58), rng.uniform(0.42, 0.58))
    tile = ImageOps.fit(
        tile,
        (AVATAR_SIZE, AVATAR_SIZE),
        method=Image.Resampling.LANCZOS,
        centering=centering,
    )
    if rng.random() < 0.5:
        tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    tile = ImageEnhance.Color(tile).enhance(rng.uniform(0.82, 1.22))
    tile = ImageEnhance.Contrast(tile).enhance(rng.uniform(0.94, 1.08))
    return tile.convert("RGBA")


def shape_avatar(tile: Image.Image, shape: str, border_color: tuple[int, int, int]) -> Image.Image:
    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    mask_draw = ImageDraw.Draw(mask)
    margin = 7
    bounds = (margin, margin, AVATAR_SIZE - margin - 1, AVATAR_SIZE - margin - 1)
    if shape == "circle":
        mask_draw.ellipse(bounds, fill=255)
    else:
        mask_draw.rounded_rectangle(bounds, radius=36, fill=255)

    output = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), (0, 0, 0, 0))
    output.paste(tile, (0, 0), mask)
    border = ImageDraw.Draw(output)
    if shape == "circle":
        border.ellipse(bounds, outline=border_color + (255,), width=7)
    else:
        border.rounded_rectangle(bounds, radius=36, outline=border_color + (255,), width=7)
    return output


def iter_comment_nodes(comments: list[Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    def visit(node: dict[str, Any], path: str) -> Iterator[tuple[str, dict[str, Any]]]:
        yield path, node
        children = node.get("sub_comments") or []
        if not isinstance(children, list):
            raise ValueError(f"{path}.sub_comments must be a list")
        for child_index, child in enumerate(children):
            if not isinstance(child, dict):
                raise ValueError(f"{path}.sub_comments[{child_index}] must be an object")
            yield from visit(child, f"{path}.sub_comments[{child_index}]")

    for parent_index, parent in enumerate(comments):
        if not isinstance(parent, dict):
            raise ValueError(f"comments[{parent_index}] must be an object")
        yield from visit(parent, f"comments[{parent_index}]")


def make_unique_nicknames(count: int, rng: random.Random) -> list[str]:
    combinations = [prefix + noun for prefix in NAME_PREFIXES for noun in NAME_NOUNS]
    rng.shuffle(combinations)
    names = combinations[:count]
    while len(names) < count:
        names.append(f"{rng.choice(NAME_PREFIXES)}{rng.choice(NAME_NOUNS)}{len(names) + 1:02d}")
    return names


def existing_seed(manifest_path: Path) -> int | None:
    if not manifest_path.is_file():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8")).get("seed")
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def previous_api_create_calls(manifest_path: Path) -> int:
    if not manifest_path.is_file():
        return 0
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        api = manifest.get("api") or {}
        return max(
            int(api.get("create_calls", 0)),
            int(api.get("create_calls_lifetime", 0)),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def claim_api_attempt(path: Path, payload: dict[str, Any]) -> bool:
    """Atomically reserve this work directory's single paid create attempt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return True


def valid_cached_atlas(path: Path) -> Image.Image | None:
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            return normalize_atlas(image)
    except (OSError, ValueError):
        return None


def process_identities(
    work_dir: Path,
    *,
    api_key_file: Path,
    offline: bool,
    seed: int | None,
    timeout_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    comments_path = work_dir / "top_comments.json"
    if not comments_path.is_file():
        raise FileNotFoundError(f"missing comments file: {comments_path}")
    comments = json.loads(comments_path.read_text(encoding="utf-8"))
    if not isinstance(comments, list):
        raise ValueError("top_comments.json must contain a JSON array")

    identities_dir = work_dir / "identities"
    manifest_path = work_dir / "identities_manifest.json"
    attempt_marker_path = identities_dir / "apimart_create_attempt.json"
    prior_create_calls = previous_api_create_calls(manifest_path)
    if attempt_marker_path.is_file():
        prior_create_calls = max(1, prior_create_calls)
    resolved_seed = seed
    if resolved_seed is None:
        resolved_seed = existing_seed(manifest_path)
    if resolved_seed is None:
        resolved_seed = secrets.randbits(63)
    rng = random.Random(resolved_seed)
    nodes = list(iter_comment_nodes(comments))

    generated_base_path = identities_dir / "base_generated.png"
    fallback_base_path = identities_dir / "base_fallback.png"
    atlas = valid_cached_atlas(generated_base_path)
    source = "cached_apimart" if atlas is not None else ""
    api_result = ApiResult(image=None, create_calls=0)

    if nodes and atlas is None and not offline and prior_create_calls < 1:
        api_key = load_api_key(api_key_file)
        if api_key:
            claimed = claim_api_attempt(
                attempt_marker_path,
                {
                    "version": 1,
                    "created_at": utc_now(),
                    "status": "creating",
                    "model": API_MODEL,
                    "seed": resolved_seed,
                },
            )
            if claimed:
                prior_create_calls = 1

                def record_task(task_id: str) -> None:
                    atomic_write_json(
                        attempt_marker_path,
                        {
                            "version": 1,
                            "created_at": utc_now(),
                            "status": "submitted",
                            "model": API_MODEL,
                            "seed": resolved_seed,
                            "task_id": task_id,
                        },
                    )

                api_result = create_apimart_atlas(
                    api_key,
                    timeout_seconds=timeout_seconds,
                    poll_interval=poll_interval,
                    on_task_created=record_task,
                )
                atomic_write_json(
                    attempt_marker_path,
                    {
                        "version": 1,
                        "created_at": utc_now(),
                        "status": (
                            "completed" if api_result.image is not None else "fallback"
                        ),
                        "model": API_MODEL,
                        "seed": resolved_seed,
                        "task_id": api_result.task_id,
                        "reason": api_result.reason,
                    },
                )
                if api_result.image is not None:
                    atlas = api_result.image
                    save_png(atlas, generated_base_path)
                    source = "apimart"
            else:
                prior_create_calls = 1
                api_result.reason = "create_attempt_already_claimed"
        else:
            api_result.reason = "api_key_unavailable"
    elif nodes and atlas is None and not offline and prior_create_calls >= 1:
        api_result.reason = "prior_create_already_attempted"
    elif offline:
        api_result.reason = "offline_mode"

    if nodes and atlas is None:
        atlas = create_local_atlas(resolved_seed)
        save_png(atlas, fallback_base_path)
        source = "local_fallback"

    names = make_unique_nicknames(len(nodes), rng)
    avatar_records: list[dict[str, Any]] = []
    border_palette = [
        (255, 214, 70), (255, 132, 116), (112, 192, 255), (112, 211, 163),
        (173, 143, 255), (255, 174, 75), (84, 205, 205), (237, 132, 189),
    ]
    for index, ((json_path, node), nickname) in enumerate(zip(nodes, names)):
        if atlas is None:  # Only reachable for an empty node list.
            break
        shape = "circle" if index % 2 == 0 else "square"
        tile = atlas_cell(atlas, index, rng)
        avatar = shape_avatar(tile, shape, border_palette[index % len(border_palette)])
        avatar_path = identities_dir / f"avatar_{index + 1:02d}.png"
        save_png(avatar, avatar_path)
        relative_avatar = avatar_path.relative_to(work_dir).as_posix()
        node["nickname"] = nickname
        node["avatar_file"] = relative_avatar
        avatar_records.append(
            {
                "index": index + 1,
                "json_path": json_path,
                "comment_id": str(node.get("comment_id") or ""),
                "nickname": nickname,
                "avatar_file": relative_avatar,
                "shape": shape,
            }
        )

    atomic_write_json(comments_path, comments)
    manifest: dict[str, Any] = {
        "version": 1,
        "generated_at": utc_now(),
        "seed": resolved_seed,
        "comments_file": "top_comments.json",
        "identity_count": len(avatar_records),
        "avatar_size": [AVATAR_SIZE, AVATAR_SIZE],
        "source": source or "none",
        "base_image": (
            generated_base_path.relative_to(work_dir).as_posix()
            if source in {"apimart", "cached_apimart"}
            else fallback_base_path.relative_to(work_dir).as_posix()
            if source == "local_fallback"
            else None
        ),
        "api": {
            "provider": "APIMart",
            "model": API_MODEL,
            "size": "1:1",
            "resolution": "1k",
            "n": 1,
            "official_fallback": False,
            "create_calls": api_result.create_calls,
            "create_calls_lifetime": min(1, prior_create_calls + api_result.create_calls),
            "create_attempt_marker": (
                attempt_marker_path.relative_to(work_dir).as_posix()
                if attempt_marker_path.is_file()
                else None
            ),
            "task_id": api_result.task_id,
            "fallback_reason": api_result.reason,
        },
        "identities": avatar_records,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def run_self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="xhs2vid-identities-") as temporary:
        work_dir = Path(temporary)
        sample = [
            {
                "comment_id": "parent-1",
                "text": "父评论",
                "nickname": "原昵称甲",
                "sub_comments": [
                    {
                        "comment_id": "reply-1",
                        "text": "子评论",
                        "nickname": "原昵称乙",
                    }
                ],
            },
            {"comment_id": "parent-2", "text": "另一条", "nickname": "原昵称丙"},
        ]
        atomic_write_json(work_dir / "top_comments.json", sample)
        manifest = process_identities(
            work_dir,
            api_key_file=work_dir / "must-not-be-read.txt",
            offline=True,
            seed=20260831,
            timeout_seconds=1.0,
            poll_interval=0.1,
        )
        updated = json.loads((work_dir / "top_comments.json").read_text(encoding="utf-8"))
        nodes = list(iter_comment_nodes(updated))
        assert manifest["identity_count"] == 3
        assert manifest["api"]["create_calls"] == 0
        assert manifest["source"] == "local_fallback"
        assert len({node["nickname"] for _, node in nodes}) == 3
        for _, node in nodes:
            relative = Path(node["avatar_file"])
            assert not relative.is_absolute()
            avatar_path = work_dir / relative
            assert avatar_path.is_file()
            with Image.open(avatar_path) as avatar:
                assert avatar.format == "PNG"
                assert avatar.size == (AVATAR_SIZE, AVATAR_SIZE)
                assert avatar.mode == "RGBA"

        # Exercise the complete APIMart protocol with an in-memory urlopen
        # double. This remains fully offline and proves there is one POST only,
        # the expected low-cost parameters are sent, and the bearer token is
        # not forwarded to the download host.
        from unittest.mock import patch

        image_bytes = BytesIO()
        create_local_atlas(9).save(image_bytes, format="PNG")
        counters = {"create": 0, "poll": 0, "download": 0}

        class FakeResponse:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def read(self, _limit: int | None = None) -> bytes:
                return self.payload

        def fake_api_open(request: Any, timeout: float) -> FakeResponse:
            assert timeout > 0
            assert request.get_header("Authorization") == "Bearer self-test-secret"
            url = request.full_url
            if url.endswith("/v1/images/generations"):
                counters["create"] += 1
                assert request.get_method() == "POST"
                sent = json.loads(request.data.decode("utf-8"))
                assert sent == {
                    "model": "gpt-image-2",
                    "prompt": ATLAS_PROMPT,
                    "size": "1:1",
                    "resolution": "1k",
                    "n": 1,
                    "official_fallback": False,
                }
                payload = {"code": 200, "data": [{"status": "submitted", "task_id": "task-test"}]}
                return FakeResponse(json.dumps(payload).encode("utf-8"))
            if url.endswith("/v1/tasks/task-test"):
                counters["poll"] += 1
                payload = {
                    "code": 200,
                    "data": {
                        "status": "completed",
                        "result": {"images": [{"url": ["https://images.test/avatar.png"]}]},
                    },
                }
                return FakeResponse(json.dumps(payload).encode("utf-8"))
            raise AssertionError(f"unexpected APIMart URL in self-test: {url}")

        def fake_download_open(request: Any, timeout: float) -> FakeResponse:
            assert timeout > 0
            assert request.full_url == "https://images.test/avatar.png"
            counters["download"] += 1
            assert request.get_header("Authorization") is None
            return FakeResponse(image_bytes.getvalue())

        with patch(f"{__name__}.open_api_request", side_effect=fake_api_open), patch(
            "urllib.request.urlopen", side_effect=fake_download_open
        ):
            protocol_result = create_apimart_atlas(
                "self-test-secret",
                timeout_seconds=2.0,
                poll_interval=0.001,
            )
        assert protocol_result.image is not None
        assert protocol_result.create_calls == 1
        assert counters == {"create": 1, "poll": 1, "download": 1}

        # A prior create attempt in the work manifest suppresses a second POST
        # on rerun, even when a key is present.
        persisted = json.loads(
            (work_dir / "identities_manifest.json").read_text(encoding="utf-8")
        )
        persisted["api"]["create_calls"] = 1
        persisted["api"]["create_calls_lifetime"] = 1
        atomic_write_json(work_dir / "identities_manifest.json", persisted)
        with patch.dict(os.environ, {"APIMART_API_KEY": "must-not-be-used"}), patch(
            f"{__name__}.open_api_request",
            side_effect=AssertionError("a second APIMart create call was attempted"),
        ):
            rerun = process_identities(
                work_dir,
                api_key_file=work_dir / "unused-key.txt",
                offline=False,
                seed=None,
                timeout_seconds=1.0,
                poll_interval=0.1,
            )
        assert rerun["api"]["create_calls"] == 0
        assert rerun["api"]["create_calls_lifetime"] == 1
        assert rerun["api"]["fallback_reason"] == "prior_create_already_attempted"
        assert rerun["seed"] == 20260831
        print("generate_identities self-test: OK (offline fallback + mocked APIMart protocol)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic nicknames and avatar files for top_comments.json."
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Directory containing top_comments.json.",
    )
    parser.add_argument(
        "--api-key-file",
        type=Path,
        default=DEFAULT_KEY_FILE,
        help=(
            "Fallback key file when APIMART_API_KEY is unset "
            f"(default: {DEFAULT_KEY_FILE})."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("apimart", "local"),
        default="apimart",
        help="Avatar base provider; local is equivalent to --offline.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Never read an API key or access APIMart; draw the base atlas locally.",
    )
    parser.add_argument("--seed", type=int, help="Optional reproducible identity seed.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Maximum APIMart create+poll time in seconds (default: 180).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=4.0,
        help="Task polling interval in seconds (default: 4).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run an isolated offline self-test and exit.",
    )
    args = parser.parse_args()
    if not args.self_test and args.work_dir is None:
        parser.error("--work-dir is required unless --self-test is used")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than zero")
    return args


def main() -> None:
    args = parse_args()
    if args.self_test:
        run_self_test()
        return
    manifest = process_identities(
        args.work_dir,
        api_key_file=args.api_key_file,
        offline=args.offline or args.provider == "local",
        seed=args.seed,
        timeout_seconds=args.timeout,
        poll_interval=args.poll_interval,
    )
    print(
        "[identities] "
        f"updated {manifest['identity_count']} identities; "
        f"source={manifest['source']}; "
        f"APIMart create calls={manifest['api']['create_calls']}"
    )
    if manifest["api"].get("fallback_reason"):
        print(f"[identities] fallback reason: {manifest['api']['fallback_reason']}")


if __name__ == "__main__":
    main()
