"""Pure-asyncio loopback HTTP streaming & artifact server for ios-agent-testers.

Binds strictly to 127.0.0.1:0 with a 192-bit URL-safe token verified via
``hmac.compare_digest``. Serves:
- ``GET/HEAD /health``
- ``GET/HEAD /stream?udid=...&fps=...&scale=...&token=...`` (MJPEG live stream)
- ``GET/HEAD /snapshot?udid=...&max_width=...&quality=...&token=...`` (JPEG frame)
- ``GET/HEAD /artifact?run=...&persona=...&file=...&token=...`` (static PNG/MP4/JSON/MD/TXT)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

logger = logging.getLogger("ios-agent-testers.http")

CHUNK_SIZE = 64 * 1024

MIME_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".ips": "text/plain; charset=utf-8",
}

RUN_LEVEL_ARTIFACTS: frozenset[str] = frozenset(
    {
        "SUMMARY.md",
        "summary.json",
        "COMPARE.md",
        "compare.json",
        "bug_report.md",
        "manifest.json",
        "state.json",
    }
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def guess_mime_type(path: Path) -> str:
    return MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _format_exc(exc: BaseException) -> str:
    msg = str(exc).strip()
    if msg:
        return msg
    if isinstance(exc, TimeoutError):
        return "TimeoutError: operation timed out"
    return type(exc).__name__


def _has_traversal(value: str) -> bool:
    if "\x00" in value or "\\" in value:
        return True
    if value.startswith("/") or value.startswith("~"):
        return True
    parts = [p for p in value.split("/") if p]
    return any(p in (".", "..") for p in parts)


def _read_file_slice(path: Path, start: int, length: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(start)
        return fh.read(length)


class LoopbackQAServer:
    """Minimal pure-asyncio loopback HTTP server for MJPEG, snapshots, and run artifacts."""

    def __init__(
        self,
        driver: Any,
        store: Any,
        *,
        host: str = "127.0.0.1",
        store_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.driver = driver
        self.store = store
        self._store_resolver = store_resolver
        self.token = secrets.token_urlsafe(24)  # 192 bits of entropy
        self.host = host
        self.port: int = 0
        self._server: asyncio.AbstractServer | None = None
        self._active_clients: set[asyncio.Task[None]] = set()

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stream_url(self, udid: str, *, fps: int = 24, scale: float = 0.5) -> str:
        q_udid = quote(udid, safe="")
        return (
            f"{self.origin}/stream?udid={q_udid}&fps={fps}&scale={scale}"
            f"&token={self.token}"
        )

    def snapshot_url(
        self, udid: str, *, max_width: int = 480, quality: float = 0.75
    ) -> str:
        q_udid = quote(udid, safe="")
        return (
            f"{self.origin}/snapshot?udid={q_udid}&max_width={max_width}"
            f"&quality={quality}&token={self.token}"
        )

    def artifact_url(self, run_id: str, persona_id: str, rel_file: str) -> str:
        q_run = quote(run_id, safe="")
        q_persona = quote(persona_id, safe="")
        q_file = quote(rel_file, safe="/")
        return (
            f"{self.origin}/artifact?run={q_run}&persona={q_persona}"
            f"&file={q_file}&token={self.token}"
        )

    def _get_store(self, run_id: str) -> Any:
        if self._store_resolver is not None:
            resolved = self._store_resolver(run_id)
            if resolved is not None:
                return resolved
        return self.store

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection_wrapper,
            host=self.host,
            port=0,
        )
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("Failed to bind loopback QA HTTP server")
        self.port = int(sockets[0].getsockname()[1])
        logger.info("Loopback QA HTTP server listening on %s", self.origin)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            self._server = None
        for task in list(self._active_clients):
            task.cancel()
        for task in list(self._active_clients):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=1.0)
        self._active_clients.clear()

    async def _handle_connection_wrapper(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._active_clients.add(task)
        try:
            await self._handle_connection(reader, writer)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.debug("HTTP client handler error: %s", _format_exc(exc))
        finally:
            if task is not None:
                self._active_clients.discard(task)
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

    def _verify_token(self, provided_token: str) -> bool:
        return bool(provided_token) and hmac.compare_digest(provided_token, self.token)

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
        *,
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        header_lines = [
            f"HTTP/1.1 {status} {reason}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store, no-cache, must-revalidate, max-age=0",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers: Range",
            "X-Content-Type-Options: nosniff",
            "Connection: close",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                header_lines.append(f"{k}: {v}")
        header_bytes = ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii")
        writer.write(header_bytes)
        if not head_only and body:
            writer.write(body)
        await asyncio.wait_for(writer.drain(), timeout=5.0)

    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        payload: dict[str, Any],
        *,
        head_only: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await self._send_response(writer, status, reason, body, head_only=head_only)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            return

        lines = raw_head.decode("latin-1", errors="replace").split("\r\n")
        if not lines or not lines[0]:
            return
        parts = lines[0].split(" ", 2)
        if len(parts) < 2:
            await self._send_json(writer, 400, "Bad Request", {"ok": False, "error": "bad request"})
            return

        method = parts[0].upper()
        target = parts[1]

        req_headers: dict[str, str] = {}
        for hline in lines[1:]:
            if ":" in hline:
                hk, hv = hline.split(":", 1)
                req_headers[hk.strip().lower()] = hv.strip()

        if method == "OPTIONS":
            await self._send_response(writer, 204, "No Content", b"", content_type="text/plain")
            return

        if method not in ("GET", "HEAD"):
            await self._send_json(
                writer, 405, "Method Not Allowed", {"ok": False, "error": "method not allowed"}
            )
            return

        head_only = method == "HEAD"
        split = urlsplit(target)
        path = split.path
        qs = parse_qs(split.query, keep_blank_values=True)
        provided_token = (qs.get("token") or [""])[0]

        if path == "/health":
            if "token" in qs and not self._verify_token(provided_token):
                await self._send_json(
                    writer,
                    403,
                    "Forbidden",
                    {"ok": False, "error": "invalid token"},
                    head_only=head_only,
                )
                return
            await self._send_json(writer, 200, "OK", {"ok": True}, head_only=head_only)
            return

        # Every non-health endpoint strictly requires the 192-bit session token.
        if not self._verify_token(provided_token):
            await self._send_json(
                writer,
                403,
                "Forbidden",
                {"ok": False, "error": "invalid or missing token"},
                head_only=head_only,
            )
            return

        if path == "/snapshot":
            await self._handle_snapshot(writer, qs, head_only=head_only)
            return

        if path == "/stream":
            await self._handle_stream(writer, qs, head_only=head_only)
            return

        if path == "/artifact":
            await self._handle_artifact(writer, qs, req_headers, head_only=head_only)
            return

        await self._send_json(
            writer, 404, "Not Found", {"ok": False, "error": "not found"}, head_only=head_only
        )

    async def _handle_snapshot(
        self,
        writer: asyncio.StreamWriter,
        qs: dict[str, list[str]],
        *,
        head_only: bool,
    ) -> None:
        udid = (qs.get("udid") or [""])[0].strip()
        if not udid:
            await self._send_json(
                writer,
                400,
                "Bad Request",
                {"ok": False, "error": "missing udid parameter"},
                head_only=head_only,
            )
            return

        try:
            max_width = int((qs.get("max_width") or ["480"])[0])
        except ValueError:
            max_width = 480
        try:
            quality = float((qs.get("quality") or ["0.75"])[0])
        except ValueError:
            quality = 0.75
        max_width = max(120, min(1200, max_width))
        quality = max(0.2, min(0.95, quality))

        try:
            jpeg_bytes = await self.driver.get_jpeg_frame(
                udid, max_width=max_width, quality=quality
            )
        except Exception as exc:
            await self._send_json(
                writer,
                503,
                "Service Unavailable",
                {"ok": False, "error": _format_exc(exc)},
                head_only=head_only,
            )
            return

        await self._send_response(
            writer,
            200,
            "OK",
            jpeg_bytes,
            content_type="image/jpeg",
            head_only=head_only,
        )

    async def _handle_stream(
        self,
        writer: asyncio.StreamWriter,
        qs: dict[str, list[str]],
        *,
        head_only: bool,
    ) -> None:
        udid = (qs.get("udid") or [""])[0].strip()
        if not udid:
            await self._send_json(
                writer,
                400,
                "Bad Request",
                {"ok": False, "error": "missing udid parameter"},
                head_only=head_only,
            )
            return

        try:
            fps = int((qs.get("fps") or ["24"])[0])
        except ValueError:
            fps = 24
        try:
            scale = float((qs.get("scale") or ["0.5"])[0])
        except ValueError:
            scale = 0.5
        fps = max(1, min(30, fps))
        scale = max(0.2, min(1.0, scale))

        header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
            "Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n"
            "Pragma: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")

        if head_only:
            writer.write(header)
            await asyncio.wait_for(writer.drain(), timeout=5.0)
            return

        stream_obj = self.driver.stream_mjpeg(udid, fps=fps, scale=scale)
        ait = aiter(stream_obj)
        try:
            # Wait for the first frame before sending 200 OK so invalid/offline
            # UDIDs immediately return 503 instead of a hanging stream.
            try:
                first_frame = await asyncio.wait_for(anext(ait), timeout=10.0)
            except StopAsyncIteration:
                await self._send_json(
                    writer,
                    503,
                    "Service Unavailable",
                    {"ok": False, "error": f"stream ended before first frame for {udid}"},
                )
                return
            except TimeoutError:
                await self._send_json(
                    writer,
                    503,
                    "Service Unavailable",
                    {
                        "ok": False,
                        "error": f"TimeoutError: timed out waiting for first stream frame for {udid}",
                    },
                )
                return
            except Exception as exc:
                await self._send_json(
                    writer,
                    503,
                    "Service Unavailable",
                    {"ok": False, "error": _format_exc(exc)},
                )
                return

            writer.write(header)
            await asyncio.wait_for(writer.drain(), timeout=5.0)

            frame: bytes | None = first_frame
            while frame is not None:
                part = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )
                writer.write(part)
                await asyncio.wait_for(writer.drain(), timeout=5.0)
                try:
                    frame = await asyncio.wait_for(anext(ait), timeout=20.0)
                except (StopAsyncIteration, TimeoutError):
                    break
        except (ConnectionResetError, BrokenPipeError, TimeoutError, asyncio.CancelledError):
            pass
        finally:
            aclose = getattr(ait, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    await aclose()

    def _resolve_artifact_safely(
        self, run_id: str, persona_id: str, rel_file: str
    ) -> Path:
        if (
            _has_traversal(run_id)
            or "/" in run_id
            or not _SAFE_ID_RE.match(run_id)
            or _has_traversal(rel_file)
        ):
            raise ValueError("path traversal or invalid identifier rejected")

        store = self._get_store(run_id)
        runs_root = Path(store.runs_root).resolve()

        if persona_id:
            if _has_traversal(persona_id) or "/" in persona_id or not _SAFE_ID_RE.match(persona_id):
                raise ValueError("invalid persona identifier rejected")
            resolved = Path(store.resolve_artifact_path(run_id, persona_id, rel_file)).resolve()
        else:
            if rel_file not in RUN_LEVEL_ARTIFACTS:
                raise ValueError("persona parameter required for non-run-level artifact")
            run_dir = Path(store.run_dir(run_id)).resolve()
            if not run_dir.is_relative_to(runs_root):
                raise ValueError("run directory escapes runs_root")
            resolved = (run_dir / rel_file).resolve()
            if not resolved.is_relative_to(run_dir):
                raise ValueError("artifact escapes run directory")
            if not resolved.is_file():
                raise FileNotFoundError(f"artifact not found: {rel_file}")

        if not resolved.is_relative_to(runs_root):
            raise ValueError("resolved artifact escapes runs_root")
        if not resolved.is_file():
            raise FileNotFoundError(f"artifact not found: {rel_file}")
        return resolved

    async def _handle_artifact(
        self,
        writer: asyncio.StreamWriter,
        qs: dict[str, list[str]],
        req_headers: dict[str, str],
        *,
        head_only: bool,
    ) -> None:
        run_id = (qs.get("run") or [""])[0].strip()
        persona_id = (qs.get("persona") or [""])[0].strip()
        rel_file = (qs.get("file") or [""])[0].strip()

        if not run_id or not rel_file:
            await self._send_json(
                writer,
                400,
                "Bad Request",
                {"ok": False, "error": "missing run or file parameter"},
                head_only=head_only,
            )
            return

        # Missing persona on a non-run-level file with no traversal -> 400.
        if not persona_id and not _has_traversal(rel_file) and rel_file not in RUN_LEVEL_ARTIFACTS:
            await self._send_json(
                writer,
                400,
                "Bad Request",
                {"ok": False, "error": "missing persona parameter"},
                head_only=head_only,
            )
            return

        try:
            artifact_path = await asyncio.to_thread(
                self._resolve_artifact_safely, run_id, persona_id, rel_file
            )
        except ValueError as exc:
            await self._send_json(
                writer,
                403,
                "Forbidden",
                {"ok": False, "error": _format_exc(exc)},
                head_only=head_only,
            )
            return
        except FileNotFoundError as exc:
            await self._send_json(
                writer,
                404,
                "Not Found",
                {"ok": False, "error": _format_exc(exc)},
                head_only=head_only,
            )
            return

        file_size = artifact_path.stat().st_size
        content_type = guess_mime_type(artifact_path)

        # Support single byte-range requests (needed for smooth HTML5 <video> MP4 playback).
        range_header = req_headers.get("range", "").strip()
        start = 0
        end = file_size - 1
        status = 200
        reason = "OK"
        extra_headers: dict[str, str] = {"Accept-Ranges": "bytes"}

        if range_header and file_size > 0:
            match = _RANGE_RE.match(range_header)
            if match:
                s_str, e_str = match.group(1), match.group(2)
                if s_str and e_str:
                    start, end = int(s_str), min(int(e_str), file_size - 1)
                elif s_str:
                    start, end = int(s_str), file_size - 1
                elif e_str:
                    suffix = int(e_str)
                    start = max(0, file_size - suffix)
                    end = file_size - 1
                else:
                    start, end = file_size, 0

                if start > end or start >= file_size:
                    await self._send_response(
                        writer,
                        416,
                        "Range Not Satisfiable",
                        b"",
                        content_type="text/plain",
                        head_only=head_only,
                        extra_headers={"Content-Range": f"bytes */{file_size}"},
                    )
                    return

                status = 206
                reason = "Partial Content"
                extra_headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        total_length = max(0, end - start + 1) if file_size > 0 else 0
        header_lines = [
            f"HTTP/1.1 {status} {reason}",
            f"Content-Type: {content_type}",
            f"Content-Length: {total_length}",
            "Cache-Control: no-store, no-cache, must-revalidate, max-age=0",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers: Range",
            "X-Content-Type-Options: nosniff",
            "Connection: close",
        ]
        for k, v in extra_headers.items():
            header_lines.append(f"{k}: {v}")
        writer.write(("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout=5.0)

        if head_only or total_length == 0:
            return

        offset = start
        remaining = total_length
        while remaining > 0:
            chunk_len = min(CHUNK_SIZE, remaining)
            chunk = await asyncio.to_thread(_read_file_slice, artifact_path, offset, chunk_len)
            if not chunk:
                break
            writer.write(chunk)
            await asyncio.wait_for(writer.drain(), timeout=10.0)
            offset += len(chunk)
            remaining -= len(chunk)
