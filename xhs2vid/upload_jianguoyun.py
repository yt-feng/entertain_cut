#!/usr/bin/env python3
"""Upload a dated batch of MP4 files to Jianguoyun WebDAV.

The destination layout is ``/<remote-root>/YYYY-MM-DD/<category>/``. Nested folders in
``--source-dir`` are preserved.  Credentials are read only from environment
variables and are never included in log output or the optional local manifest.
JSON evidence remains local unless ``--include-json`` is explicitly supplied.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime
import http.client
import json
import mimetypes
import os
from pathlib import Path
import re
import ssl
import sys
import time
from typing import Callable, Iterable, Iterator
from urllib.parse import quote, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


DEFAULT_WEBDAV_URL = "https://dav.jianguoyun.com/dav/"
DEFAULT_REMOTE_ROOT = "我的坚果云/KC Desk Notes/Ops"
DEFAULT_CATEGORY = "Portal 娱乐"
VIDEO_SUFFIXES = {".mp4"}
RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
COLLECTION_OK_STATUS_CODES = {200, 201, 204, 301, 302, 405}
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class WebDAVUploadError(RuntimeError):
    """A safe-to-print WebDAV upload error (never contains credentials)."""


def beijing_today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def validate_date(value: str) -> str:
    if not DATE_PATTERN.fullmatch(value):
        raise ValueError("--date must use YYYY-MM-DD")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("--date must be a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError("--date must use zero-padded YYYY-MM-DD")
    return value


def remote_root_parts(value: str) -> tuple[str, ...]:
    parts = tuple(part for part in value.strip().strip("/").split("/") if part)
    if not parts:
        raise ValueError("--remote-root must not be empty")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("--remote-root must not contain '.' or '..' segments")
    return parts


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("JIANGUOYUN_WEBDAV_URL must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError(
            "JIANGUOYUN_WEBDAV_URL must not embed credentials; use the credential env vars"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("JIANGUOYUN_WEBDAV_URL must not contain a query or fragment")
    path = parsed.path.rstrip("/") + "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def join_webdav_url(base_url: str, segments: Iterable[str]) -> str:
    parsed = urlsplit(base_url)
    encoded = "/".join(quote(segment, safe="") for segment in segments)
    path = parsed.path.rstrip("/")
    if encoded:
        path += "/" + encoded
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def logical_remote_path(segments: Iterable[str], *, directory: bool = False) -> str:
    path = "/" + "/".join(segments)
    return path + ("/" if directory else "")


def discover_files(
    source_dir: Path,
    *,
    excluded: Path | None = None,
    include_json: bool = False,
) -> list[Path]:
    source_dir = source_dir.expanduser().resolve()
    excluded_resolved = excluded.expanduser().resolve() if excluded else None
    allowed_suffixes = VIDEO_SUFFIXES | ({".json"} if include_json else set())
    files: list[Path] = []
    for path in source_dir.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in allowed_suffixes:
            continue
        if excluded_resolved is not None and path.resolve() == excluded_resolved:
            continue
        files.append(path)
    return sorted(files, key=lambda path: path.relative_to(source_dir).as_posix())


def _file_chunks(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                return
            yield chunk


@dataclass(frozen=True)
class HTTPResult:
    status_code: int
    headers: dict[str, str]
    content: bytes


class JianguoyunWebDAV:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        app_password: str,
        max_attempts: int = 3,
        timeout_seconds: float = 90.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not username or not app_password:
            raise ValueError("Jianguoyun username and app password are required")
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self.base_url = normalize_base_url(base_url)
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        token = base64.b64encode(
            f"{username}:{app_password}".encode("utf-8")
        ).decode("ascii")
        self._authorization = f"Basic {token}"
        self._known_collections: set[tuple[str, ...]] = set()

    def close(self) -> None:
        # A fresh connection is used per request so a partially read Range GET
        # can always be closed without keeping an unusable pooled connection.
        return None

    def __enter__(self) -> "JianguoyunWebDAV":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        segments: tuple[str, ...],
        *,
        accepted: set[int],
        headers: dict[str, str] | None = None,
        content_factory: Callable[[], object] | None = None,
        response_body_limit: int = 1024 * 1024,
    ) -> HTTPResult:
        url = join_webdav_url(self.base_url, segments)
        logical_path = logical_remote_path(segments)
        last_message = "request failed"
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._request_once(
                    method,
                    url,
                    headers=headers,
                    content=content_factory() if content_factory else None,
                    response_body_limit=response_body_limit,
                )
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                last_message = f"{type(exc).__name__}: transport failure"
            else:
                if response.status_code in accepted:
                    return response
                last_message = f"HTTP {response.status_code}"
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    raise WebDAVUploadError(
                        f"{method} {logical_path} failed: {last_message}"
                    )
            if attempt < self.max_attempts:
                self.sleep(0.5 * (2 ** (attempt - 1)))
        raise WebDAVUploadError(
            f"{method} {logical_path} failed after {self.max_attempts} attempts: "
            f"{last_message}"
        )

    def _request_once(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        content: object | None,
        response_body_limit: int,
    ) -> HTTPResult:
        parsed = urlsplit(url)
        port = parsed.port
        if parsed.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname,
                port=port,
                timeout=self.timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                parsed.hostname, port=port, timeout=self.timeout_seconds
            )
        request_path = parsed.path or "/"
        request_headers = {
            "Authorization": self._authorization,
            "User-Agent": "entertain-cut-jianguoyun-uploader/1.0",
            **(headers or {}),
        }
        if isinstance(content, (bytes, bytearray, memoryview)) and not any(
            key.lower() == "content-length" for key in request_headers
        ):
            request_headers["Content-Length"] = str(len(content))
        try:
            connection.putrequest(method, request_path)
            for key, value in request_headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            if isinstance(content, (bytes, bytearray, memoryview)):
                connection.send(content)
            elif content is not None:
                for chunk in content:  # type: ignore[union-attr]
                    connection.send(chunk)
            response = connection.getresponse()
            response_headers = {
                key.lower(): value for key, value in response.getheaders()
            }
            body = (
                b""
                if method == "HEAD" or response_body_limit <= 0
                else response.read(response_body_limit)
            )
            return HTTPResult(response.status, response_headers, body)
        finally:
            connection.close()

    def ensure_collections(self, segments: tuple[str, ...]) -> None:
        for length in range(1, len(segments) + 1):
            collection = segments[:length]
            if collection in self._known_collections:
                continue
            self._request(
                "MKCOL", collection, accepted=COLLECTION_OK_STATUS_CODES
            )
            self._known_collections.add(collection)

    def upload_file(self, local_path: Path, remote_segments: tuple[str, ...]) -> int:
        local_path = local_path.resolve()
        expected_size = local_path.stat().st_size
        self.ensure_collections(remote_segments[:-1])
        content_type = (
            mimetypes.guess_type(local_path.name)[0]
            or "application/octet-stream"
        )
        self._request(
            "PUT",
            remote_segments,
            accepted={200, 201, 204},
            headers={
                "Content-Type": content_type,
                "Content-Length": str(expected_size),
            },
            content_factory=lambda: _file_chunks(local_path),
        )
        if local_path.stat().st_size != expected_size:
            raise WebDAVUploadError(
                f"local file changed during upload: {local_path.name}"
            )
        actual_size = self.remote_size(remote_segments)
        if actual_size != expected_size:
            raise WebDAVUploadError(
                f"size verification failed for {logical_remote_path(remote_segments)}: "
                f"local={expected_size}, remote={actual_size}"
            )
        return actual_size

    @staticmethod
    def _content_length(response: HTTPResult) -> int | None:
        raw = response.headers.get("content-length", "").strip()
        if raw.isdigit():
            return int(raw)
        return None

    def remote_size(self, segments: tuple[str, ...]) -> int:
        # HEAD is the cheapest check and is supported by Jianguoyun in normal use.
        response = self._request(
            "HEAD",
            segments,
            accepted={200, 204, 405, 501},
        )
        if response.status_code in {200, 204}:
            size = self._content_length(response)
            # Jianguoyun can answer HEAD with ``Content-Length: 0`` even for
            # a non-empty WebDAV resource. Treat zero as inconclusive and ask
            # PROPFIND for DAV:getcontentlength before rejecting the upload.
            if size is not None and size > 0:
                return size

        # Some WebDAV servers expose the size only as DAV:getcontentlength.
        propfind_body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<d:propfind xmlns:d="DAV:"><d:prop>'
            b"<d:getcontentlength/></d:prop></d:propfind>"
        )
        response = self._request(
            "PROPFIND",
            segments,
            accepted={200, 207, 405, 501},
            headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
            content_factory=lambda: propfind_body,
        )
        if response.status_code in {200, 207}:
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                root = None
            if root is not None:
                for element in root.iter():
                    if element.tag.rsplit("}", 1)[-1] == "getcontentlength":
                        text = (element.text or "").strip()
                        if text.isdigit():
                            return int(text)

        # A one-byte range request avoids downloading the full video when HEAD
        # and PROPFIND are unavailable.
        response = self._request(
            "GET",
            segments,
            accepted={200, 206, 416},
            headers={"Range": "bytes=0-0"},
            response_body_limit=1,
        )
        content_range = response.headers.get("content-range", "")
        match = re.search(r"/(\d+)$", content_range)
        if match:
            return int(match.group(1))
        if response.status_code == 200:
            size = self._content_length(response)
            if size is not None:
                return size
        if response.status_code == 416:
            match = re.search(r"\*/(\d+)$", content_range)
            if match:
                return int(match.group(1))
        raise WebDAVUploadError(
            f"could not verify remote size for {logical_remote_path(segments)}"
        )


def upload_directory(
    *,
    source_dir: Path,
    remote_root: str,
    date: str,
    dry_run: bool,
    uploader: JianguoyunWebDAV | None = None,
    excluded_manifest: Path | None = None,
    include_json: bool = False,
    category: str = "",
) -> dict:
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source_dir}")
    date = validate_date(date)
    root_parts = remote_root_parts(remote_root)
    category = category.strip()
    if category in {".", ".."} or "/" in category or "\\" in category:
        raise ValueError("--category must be one folder name")
    destination = (*root_parts, date, *((category,) if category else ()))
    files = discover_files(
        source_dir,
        excluded=excluded_manifest,
        include_json=include_json,
    )
    records: list[dict] = []

    if not dry_run and uploader is None:
        raise ValueError("uploader is required unless --dry-run is used")
    if uploader is not None and not dry_run:
        uploader.ensure_collections(destination)

    for local_path in files:
        relative = local_path.relative_to(source_dir)
        remote_segments = (*destination, *relative.parts)
        record = {
            "source": relative.as_posix(),
            "remote_path": logical_remote_path(remote_segments),
            "size": local_path.stat().st_size,
            "status": "planned" if dry_run else "pending",
        }
        print(
            f"[{'plan' if dry_run else 'upload'}] {record['source']} -> "
            f"{record['remote_path']} ({record['size']} bytes)"
        )
        if not dry_run:
            try:
                assert uploader is not None
                verified_size = uploader.upload_file(local_path, remote_segments)
                record["status"] = "verified"
                record["verified_size"] = verified_size
                print(f"[verified] {record['remote_path']} ({verified_size} bytes)")
            except (OSError, WebDAVUploadError) as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
                print(f"[failed] {record['remote_path']}: {exc}", file=sys.stderr)
        records.append(record)

    failed_count = sum(record["status"] == "failed" for record in records)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "date": date,
        "remote_root": logical_remote_path(root_parts, directory=True),
        "remote_directory": logical_remote_path(destination, directory=True),
        "category": category,
        "source_dir": str(source_dir),
        "dry_run": dry_run,
        "status": (
            "empty"
            if not records
            else "failed"
            if failed_count
            else "planned"
            if dry_run
            else "completed"
        ),
        "file_count": len(records),
        "verified_count": sum(
            record["status"] == "verified" for record in records
        ),
        "failed_count": failed_count,
        "files": records,
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    print(f"[manifest] {path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Date batch directory containing MP4/JSON files.",
    )
    parser.add_argument(
        "--remote-root",
        default=os.environ.get("JIANGUOYUN_REMOTE_ROOT") or DEFAULT_REMOTE_ROOT,
        help=f"Remote WebDAV root folder (default: {DEFAULT_REMOTE_ROOT}).",
    )
    parser.add_argument(
        "--category",
        default=DEFAULT_CATEGORY,
        help="Category inside the date folder (default: Portal 娱乐); empty omits it.",
    )
    parser.add_argument(
        "--date",
        default=beijing_today(),
        help="Destination date folder in YYYY-MM-DD (default: Beijing today).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List planned files and paths without credentials or network calls.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Write a local JSON result manifest; it is not uploaded by this run.",
    )
    parser.add_argument(
        "--include-json",
        action="store_true",
        help="Also upload JSON evidence files; by default only MP4 videos are uploaded.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        date = validate_date(args.date)
        if args.dry_run:
            manifest = upload_directory(
                source_dir=args.source_dir,
                remote_root=args.remote_root,
                date=date,
                dry_run=True,
                excluded_manifest=args.manifest,
                include_json=args.include_json,
                category=args.category,
            )
        else:
            base_url = (
                os.environ.get("JIANGUOYUN_WEBDAV_URL", "").strip()
                or DEFAULT_WEBDAV_URL
            )
            username = (
                os.environ.get("JIANGUOYUN_USERNAME", "")
                or os.environ.get("JIANGUOYUN_WEBDAV_USERNAME", "")
            )
            app_password = (
                os.environ.get("JIANGUOYUN_APP_PASSWORD", "")
                or os.environ.get("JIANGUOYUN_WEBDAV_PASSWORD", "")
            )
            if not username.strip() or not app_password:
                raise ValueError(
                    "Jianguoyun WebDAV username and app password are required"
                )
            with JianguoyunWebDAV(
                base_url=base_url,
                username=username,
                app_password=app_password,
            ) as uploader:
                manifest = upload_directory(
                    source_dir=args.source_dir,
                    remote_root=args.remote_root,
                    date=date,
                    dry_run=False,
                    uploader=uploader,
                    excluded_manifest=args.manifest,
                    include_json=args.include_json,
                    category=args.category,
                )
        if args.manifest:
            write_manifest(args.manifest, manifest)
    except (FileNotFoundError, OSError, ValueError, WebDAVUploadError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    if manifest["status"] == "empty":
        print("[error] no MP4 or JSON files found", file=sys.stderr)
        return 3
    if manifest["failed_count"]:
        return 1
    print(
        f"[done] {manifest['status']}: {manifest['file_count']} file(s) -> "
        f"{manifest['remote_directory']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

