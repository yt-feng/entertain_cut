from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import unquote, urlsplit

from xhs2vid.upload_jianguoyun import (
    JianguoyunWebDAV,
    main,
    parse_args,
    upload_directory,
)


class MockWebDAVState:
    def __init__(self) -> None:
        self.collections = {"/dav"}
        self.resources: dict[str, bytes] = {}
        self.raw_paths: list[tuple[str, str]] = []
        self.authorization_headers: list[str] = []
        self.put_attempts = 0
        self.fail_put_count = 0
        self.head_mode = "length"
        self.propfind_mode = "length"
        self.get_attempts = 0


def make_handler(state: MockWebDAVState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _path(self) -> tuple[str, str]:
            raw = urlsplit(self.path).path.rstrip("/") or "/"
            decoded = unquote(raw)
            state.raw_paths.append((self.command, raw))
            auth = self.headers.get("Authorization", "")
            if auth:
                state.authorization_headers.append(auth)
            return raw, decoded

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length)

        def _send(
            self,
            status: int,
            body: bytes = b"",
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def do_MKCOL(self) -> None:  # noqa: N802
            _raw, path = self._path()
            parent = path.rsplit("/", 1)[0] or "/"
            if path in state.collections:
                self._send(405)
            elif parent not in state.collections:
                self._send(409)
            else:
                state.collections.add(path)
                self._send(201)

        def do_PUT(self) -> None:  # noqa: N802
            _raw, path = self._path()
            body = self._body()
            state.put_attempts += 1
            if state.fail_put_count:
                state.fail_put_count -= 1
                self._send(503)
                return
            parent = path.rsplit("/", 1)[0] or "/"
            if parent not in state.collections:
                self._send(409)
                return
            state.resources[path] = body
            self._send(201)

        def do_HEAD(self) -> None:  # noqa: N802
            _raw, path = self._path()
            if path not in state.resources:
                self._send(404)
            elif state.head_mode == "unsupported":
                self._send(405)
            elif state.head_mode == "no-length":
                self.send_response(200)
                self.end_headers()
            elif state.head_mode == "zero":
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                size = len(state.resources[path])
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.end_headers()

        def do_PROPFIND(self) -> None:  # noqa: N802
            _raw, path = self._path()
            self._body()
            if path not in state.resources:
                self._send(404)
                return
            if state.propfind_mode == "unsupported":
                self._send(405)
                return
            size = len(state.resources[path])
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop>'
                f"<d:getcontentlength>{size}</d:getcontentlength>"
                "</d:prop></d:propstat></d:response></d:multistatus>"
            ).encode()
            self._send(207, body, {"Content-Type": "application/xml"})

        def do_GET(self) -> None:  # noqa: N802
            _raw, path = self._path()
            state.get_attempts += 1
            if path not in state.resources:
                self._send(404)
                return
            body = state.resources[path]
            if self.headers.get("Range") == "bytes=0-0" and body:
                self._send(
                    206,
                    body[:1],
                    {"Content-Range": f"bytes 0-0/{len(body)}"},
                )
            elif self.headers.get("Range") == "bytes=0-0" and not body:
                self._send(416, headers={"Content-Range": "bytes */0"})
            else:
                self._send(200, body)

    return Handler


class MockWebDAVServer:
    def __init__(self, state: MockWebDAVState) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/dav/"

    def __enter__(self) -> "MockWebDAVServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class JianguoyunUploadTests(unittest.TestCase):
    def test_category_upload_adds_five_without_touching_regular_five(self) -> None:
        state = MockWebDAVState()
        remote = "/dav/我的坚果云/KC Desk Notes/Ops/2026-09-03/Portal 娱乐"
        regular = {f"{remote}/KC娱乐_{i}.mp4": b"regular" for i in range(5)}
        state.resources.update(regular)
        with tempfile.TemporaryDirectory() as temporary, MockWebDAVServer(state) as server:
            source = Path(temporary)
            for i in range(5):
                (source / f"0{i + 1}_低粉_note{i}.mp4").write_bytes(b"low-follower")
            with self._uploader(server) as uploader:
                result = upload_directory(
                    source_dir=source, remote_root="我的坚果云/KC Desk Notes/Ops",
                    date="2026-09-03", category="Portal 娱乐", dry_run=False, uploader=uploader,
                )
        self.assertEqual(result["verified_count"], 5)
        self.assertEqual(result["remote_directory"], remote.removeprefix("/dav") + "/")
        self.assertEqual(len(state.resources), 10)
        self.assertTrue(all(state.resources[name] == value for name, value in regular.items()))

    def test_category_must_stay_inside_date_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for category in ("..", "../other", "other/folder", "other\\folder"):
                with self.subTest(category=category), self.assertRaisesRegex(ValueError, "one folder name"):
                    upload_directory(source_dir=Path(temporary), remote_root="root", date="2026-09-03", category=category, dry_run=True)

    def test_cli_uses_shared_root_environment_override(self) -> None:
        with mock.patch.dict("os.environ", {"JIANGUOYUN_REMOTE_ROOT": "我的坚果云/custom/Ops"}):
            args = parse_args(["--source-dir", "/tmp/batch"])
        self.assertEqual(args.remote_root, "我的坚果云/custom/Ops")
        self.assertEqual(args.category, "Portal 娱乐")

    def _uploader(
        self, server: MockWebDAVServer, *, attempts: int = 3
    ) -> JianguoyunWebDAV:
        return JianguoyunWebDAV(
            base_url=server.url,
            username="test-user",
            app_password="test-app-password",
            max_attempts=attempts,
            sleep=lambda _seconds: None,
        )

    def test_uploads_nested_files_with_encoded_names_and_verifies_size(self) -> None:
        state = MockWebDAVState()
        with tempfile.TemporaryDirectory() as temporary, MockWebDAVServer(state) as server:
            source = Path(temporary) / "batch"
            (source / "子目录").mkdir(parents=True)
            video = source / "视频 一.mp4"
            metadata = source / "子目录" / "数据.json"
            video.write_bytes(b"video-payload")
            metadata.write_text('{"ok": true}', encoding="utf-8")

            with self._uploader(server) as uploader:
                result = upload_directory(
                    source_dir=source,
                    remote_root="kc娱乐",
                    date="2026-08-31",
                    dry_run=False,
                    uploader=uploader,
                    include_json=True,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verified_count"], 2)
        self.assertEqual(
            state.resources["/dav/kc娱乐/2026-08-31/视频 一.mp4"],
            b"video-payload",
        )
        self.assertEqual(
            state.resources["/dav/kc娱乐/2026-08-31/子目录/数据.json"],
            b'{"ok": true}',
        )
        put_raw_paths = [path for method, path in state.raw_paths if method == "PUT"]
        self.assertTrue(any("%E8%A7%86%E9%A2%91%20%E4%B8%80.mp4" in path for path in put_raw_paths))
        self.assertTrue(any("%E5%AD%90%E7%9B%AE%E5%BD%95" in path for path in put_raw_paths))
        self.assertTrue(state.authorization_headers)
        self.assertTrue(all(value.startswith("Basic ") for value in state.authorization_headers))

    def test_falls_back_to_range_get_when_head_and_propfind_are_unavailable(self) -> None:
        state = MockWebDAVState()
        state.head_mode = "unsupported"
        state.propfind_mode = "unsupported"
        with tempfile.TemporaryDirectory() as temporary, MockWebDAVServer(state) as server:
            source = Path(temporary)
            (source / "item.json").write_bytes(b"{}")
            with self._uploader(server) as uploader:
                result = upload_directory(
                    source_dir=source,
                    remote_root="kc娱乐",
                    date="2026-08-31",
                    dry_run=False,
                    uploader=uploader,
                    include_json=True,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(state.get_attempts, 1)

    def test_falls_back_to_propfind_when_head_reports_false_zero(self) -> None:
        state = MockWebDAVState()
        state.head_mode = "zero"
        with tempfile.TemporaryDirectory() as temporary, MockWebDAVServer(state) as server:
            source = Path(temporary)
            (source / "clip.mp4").write_bytes(b"non-empty-video")
            with self._uploader(server) as uploader:
                result = upload_directory(
                    source_dir=source,
                    remote_root="kc娱乐",
                    date="2026-08-31",
                    dry_run=False,
                    uploader=uploader,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verified_count"], 1)
        self.assertTrue(any(method == "PROPFIND" for method, _path in state.raw_paths))
        self.assertEqual(state.get_attempts, 0)

    def test_retries_a_transient_put_failure(self) -> None:
        state = MockWebDAVState()
        state.fail_put_count = 1
        with tempfile.TemporaryDirectory() as temporary, MockWebDAVServer(state) as server:
            source = Path(temporary)
            (source / "clip.mp4").write_bytes(b"retry-me")
            with self._uploader(server) as uploader:
                result = upload_directory(
                    source_dir=source,
                    remote_root="kc娱乐",
                    date="2026-08-31",
                    dry_run=False,
                    uploader=uploader,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(state.put_attempts, 2)

    def test_dry_run_writes_manifest_without_credentials_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "clip.mp4").write_bytes(b"123")
            (source / "metadata.json").write_text("{}", encoding="utf-8")
            (source / "ignore.txt").write_text("ignore", encoding="utf-8")
            manifest_path = source / "upload-manifest.json"
            output = io.StringIO()
            error = io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                status = main(
                    [
                        "--source-dir",
                        str(source),
                        "--date",
                        "2026-08-31",
                        "--dry-run",
                        "--manifest",
                        str(manifest_path),
                    ]
                )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(manifest["status"], "planned")
        self.assertEqual(manifest["file_count"], 1)
        self.assertEqual(manifest["files"][0]["source"], "clip.mp4")
        self.assertEqual(manifest["remote_directory"], "/我的坚果云/KC Desk Notes/Ops/2026-08-31/Portal 娱乐/")
        self.assertNotIn("test-app-password", output.getvalue())
        self.assertEqual(error.getvalue(), "")


if __name__ == "__main__":
    unittest.main()

