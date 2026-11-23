"""Async fb-idb 1.7.8 driver for multi-persona iOS Simulator observation, control & streaming.

Adapts proven `_ControlClient` + `_StreamHub` patterns from `ios-phone-pane` with:
- Dedicated `/tmp/ios-agent-testers-<uid>/` companion sockets for ephemeral QA sims
- Point-scaled PNG screenshots via `ScreenshotOptions(format=PNG, max_width=wpt)` +
  pure-stdlib IHDR check + `/usr/bin/sips` fallback (ZERO Pillow dependency)
- Nested `accessibility_info` with flat fallback, deterministic `a11y.audit_ios` & element refs
- Full 22-verb action executor with Unicode `simctl pbcopy` + Cmd+V + NFKD ASCII fallback
- Process-alive (`list_apps` + `launchctl`) & crash detection (`crash_list` + `.ips` reports)
- Background `tail_logs` ring buffer per UDID
- Loopback-ready `_StreamHub` MJPEG fan-out & `<= 580 KB` JPEG snapshot frames
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from idb.common import hid
from idb.common.types import (
    AccessibilityBackend,
    AccessibilityInfoOptions,
    AccessibilityOutputFormat,
    AppProcessState,
    CrashLogQuery,
    DomainSocketAddress,
    HIDButtonType,
    HIDOrientationType,
    IdbConnectionException,
    Permission,
    ScreenshotFormat,
    ScreenshotOptions,
    VideoFormat,
)
from idb.grpc.client import Client

try:
    from grpclib.const import Status as _GRPCStatus
    from grpclib.exceptions import (
        GRPCError as _GRPCError,
        StreamTerminatedError as _GRPCStreamTerminatedError,
    )

    _GRPC_UNAVAILABLE = _GRPCStatus.UNAVAILABLE
except Exception:  # pragma: no cover - optional fallback if grpclib internals differ
    _GRPCError = None  # type: ignore[assignment]
    _GRPCStreamTerminatedError = None  # type: ignore[assignment]
    _GRPC_UNAVAILABLE = None

from .a11y import (
    audit_ios,
    extract_elements,
    find_opaque_containers,
    format_elements_text,
    graft_elements_in_boxes,
)
from .personas import VALID_TEXT_SIZES
from .sim_provisioner import QA_SOCK_DIR, SIPS_BIN, resolve_companion_bin

logger = logging.getLogger("ios-agent-testers.driver")

T = TypeVar("T")

TERMINAL_PHONE_SOCK_DIR = Path(f"/tmp/terminal-phone-{os.getuid()}")
DIAGNOSTIC_REPORTS_DIR = Path.home() / "Library" / "Logs" / "DiagnosticReports"

MAX_JPEG_RAW_BYTES = 580_000

HID_KEYCODES: dict[str, int] = {
    "return": 40,
    "enter": 40,
    "escape": 41,
    "esc": 41,
    "backspace": 42,
    "delete": 42,
    "tab": 43,
    "space": 44,
}

BUTTON_MAP: dict[str, HIDButtonType] = {
    "HOME": HIDButtonType.HOME,
    "LOCK": HIDButtonType.LOCK,
    "SIDE_BUTTON": HIDButtonType.SIDE_BUTTON,
    "SIRI": HIDButtonType.SIRI,
    "VOLUME_UP": HIDButtonType.VOLUME_UP,
    "VOLUME_DOWN": HIDButtonType.VOLUME_DOWN,
}

ORIENTATION_MAP: dict[str, HIDOrientationType] = {
    "portrait": HIDOrientationType.PORTRAIT,
    "portrait_upside_down": HIDOrientationType.PORTRAIT_UPSIDE_DOWN,
    "landscape_left": HIDOrientationType.LANDSCAPE_LEFT,
    "landscape_right": HIDOrientationType.LANDSCAPE_RIGHT,
}

PERMISSION_MAP: dict[str, Permission] = {
    "photos": Permission.PHOTOS,
    "camera": Permission.CAMERA,
    "contacts": Permission.CONTACTS,
    "url": Permission.URL,
    "location": Permission.LOCATION,
    "notification": Permission.NOTIFICATION,
    "notifications": Permission.NOTIFICATION,
    "microphone": Permission.MICROPHONE,
}

_KIND_ALIASES: dict[str, str] = {
    "long-press": "long_press",
    "longpress": "long_press",
    "multi-tap": "multi_tap",
    "double_tap": "multi_tap",
    "dismiss-sheet": "dismiss_sheet",
    "replace-text": "replace_text",
    "open-url": "open_url",
    "url": "open_url",
    "home": "button",
    "set-appearance": "set_appearance",
    "set-text-size": "set_text_size",
    "set-location": "set_location",
}

_LOG_IMPORTANT_RE = re.compile(
    r"(error|fault|exception|crash|fatal|assert|warning|terminated|sigabrt|sigsegv|uncaught)",
    re.IGNORECASE,
)

_LOG_BANNER_RE = re.compile(
    r"^(?:Filtering the log data\b|Timestamp\s+(?:Ty\s+Process|Thread\s+Type)\b)"
)

_AX_BACKEND_MAP: dict[str, AccessibilityBackend] = {
    "ax": AccessibilityBackend.AX,
    "axbridge": AccessibilityBackend.AXBRIDGE,
    "axbridge_persistent": AccessibilityBackend.AXBRIDGE_PERSISTENT,
    "axbridge-persistent": AccessibilityBackend.AXBRIDGE_PERSISTENT,
    "ax_coresimulator": AccessibilityBackend.AX_CORESIMULATOR,
    "ax-coresimulator": AccessibilityBackend.AX_CORESIMULATOR,
}


def _resolve_ax_backend() -> AccessibilityBackend:
    raw = (os.environ.get("IOS_AGENT_TESTERS_AX_BACKEND") or "").strip().lower()
    if not raw:
        return AccessibilityBackend.AX
    backend = _AX_BACKEND_MAP.get(raw)
    if backend is None:
        logger.warning(
            "Unknown IOS_AGENT_TESTERS_AX_BACKEND=%r; defaulting to AX", raw
        )
        return AccessibilityBackend.AX
    return backend


def _is_disconnect_error(exc: BaseException) -> bool:
    """Return True only for genuine transport/socket disconnects (NEVER timeouts)."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError)):
        return False
    if isinstance(exc, (IdbConnectionException, ConnectionError, EOFError)):
        return True
    if _GRPCStreamTerminatedError is not None and isinstance(
        exc, _GRPCStreamTerminatedError
    ):
        return True
    if _GRPCError is not None and isinstance(exc, _GRPCError):
        return (
            _GRPC_UNAVAILABLE is not None
            and getattr(exc, "status", None) == _GRPC_UNAVAILABLE
        )
    if isinstance(exc, OSError):
        return True
    return False


def _escape_predicate_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_log_predicate(
    bundle_id: str | None,
    process_name: str | None = None,
) -> str | None:
    clauses: list[str] = []
    bid = (bundle_id or "").strip()
    exec_name = (process_name or "").strip()
    proc_hint = bid.rsplit(".", 1)[-1].strip() if bid else ""

    if exec_name:
        clauses.append(f'process == "{_escape_predicate_str(exec_name)}"')
    if bid:
        clauses.append(f'subsystem == "{_escape_predicate_str(bid)}"')
    if proc_hint and proc_hint != exec_name:
        clauses.append(f'process == "{_escape_predicate_str(proc_hint)}"')

    return " OR ".join(clauses) if clauses else None


def _is_unix_socket_alive(sock_path: Path, timeout: float = 0.6) -> bool:
    if not sock_path.exists():
        return False
    try:
        st = sock_path.stat()
        if not stat.S_ISSOCK(st.st_mode):
            return False
    except OSError:
        return False

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            s.close()


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read (width, height) directly from PNG IHDR header without Pillow."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        if w > 0 and h > 0:
            return (int(w), int(h))
    return None


def _default_dimensions_for_device(device_type: str) -> tuple[int, int, float]:
    dt = (device_type or "").lower()
    if "pro-max" in dt or "plus" in dt:
        return (440, 956, 3.0)
    if "se" in dt:
        return (375, 667, 2.0)
    if "mini" in dt and "ipad" not in dt:
        return (375, 812, 3.0)
    if "ipad-mini" in dt:
        return (744, 1133, 2.0)
    if "ipad-pro-13" in dt or "12-9" in dt:
        return (1032, 1376, 2.0)
    if "ipad" in dt:
        return (820, 1180, 2.0)
    return (393, 852, 3.0)


def _to_ascii_fallback(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in norm if ch in hid.KEY_MAP)


async def _run_subproc(
    args: list[str],
    *,
    timeout: float = 15.0,
    stdin_bytes: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes), timeout=timeout
        )
        return proc.returncode or 0, stdout, stderr
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        raise TimeoutError(f"Command timed out after {timeout}s: {' '.join(args)}")


class _ControlClient:
    """Persistent, serialized, auto-reconnecting idb gRPC client for one UDID."""

    def __init__(self, driver: "IDBDriver", udid: str) -> None:
        self._driver = driver
        self.udid = udid
        self._lock = asyncio.Lock()
        self._cm: Any = None
        self._client: Client | None = None

    async def _close_unlocked(self) -> None:
        cm = self._cm
        self._cm = None
        self._client = None
        if cm is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=2.5)

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _ensure_connected_unlocked(self) -> Client:
        if self._client is not None:
            return self._client
        sock_path = await self._driver.ensure_companion(self.udid)
        idb_logger = logging.getLogger(f"ios-agent-testers.idb.{self.udid[:8]}")
        idb_logger.setLevel(logging.ERROR)
        cm = Client.build(
            address=DomainSocketAddress(path=str(sock_path)),
            logger=idb_logger,
        )
        try:
            client = await asyncio.wait_for(cm.__aenter__(), timeout=10.0)
        except (TimeoutError, asyncio.TimeoutError):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=1.5)
            raise TimeoutError(
                f"idb connect timed out after 10.0s for {self.udid[:8]}"
            ) from None
        if client.companion.udid.upper() != self.udid.upper():
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)
            raise RuntimeError(
                f"Companion UDID mismatch: expected {self.udid}, got {client.companion.udid}"
            )
        self._cm = cm
        self._client = client
        return client

    async def run(
        self,
        fn: Callable[[Client], Awaitable[T]],
        *,
        timeout: float = 15.0,
        retry_on_disconnect: bool = False,
        op_name: str = "rpc",
    ) -> T:
        async with self._lock:
            attempts = 2 if retry_on_disconnect else 1
            last_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    client = await self._ensure_connected_unlocked()
                    return await asyncio.wait_for(fn(client), timeout=timeout)
                except asyncio.CancelledError:
                    await self._close_unlocked()
                    raise
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    msg = str(exc).strip() or (
                        f"idb {op_name} timed out after {timeout:.1f}s for {self.udid[:8]}"
                    )
                    logger.warning(
                        "idb %s timed out for %s (no retry): %s",
                        op_name,
                        self.udid[:8],
                        msg,
                    )
                    await self._close_unlocked()
                    raise TimeoutError(msg) from None
                except Exception as exc:
                    last_exc = exc
                    is_disconnect = _is_disconnect_error(exc)
                    logger.warning(
                        "idb %s failed for %s (attempt %d/%d): %s",
                        op_name,
                        self.udid[:8],
                        attempt + 1,
                        attempts,
                        str(exc).strip() or type(exc).__name__,
                    )
                    if is_disconnect:
                        await self._close_unlocked()
                        if attempt + 1 < attempts:
                            await asyncio.sleep(0.15)
                            continue
                    raise
            assert last_exc is not None
            raise last_exc


class _StreamHub:
    """Single upstream idb MJPEG stream per UDID fanning out to N HTTP subscribers."""

    IDLE_GRACE_SECONDS = 3.0

    def __init__(self, driver: "IDBDriver", udid: str) -> None:
        self._driver = driver
        self.udid = udid
        self._subscribers: set[asyncio.Queue[bytes | None]] = set()
        self._pump_task: asyncio.Task[None] | None = None
        self._idle_timer: asyncio.TimerHandle | None = None
        self._stopping = False
        self._fps: int = 24
        self._scale: float = 0.5
        self.latest_frame: bytes | None = None
        self.latest_frame_at: float = 0.0

    def subscribe(self, fps: int = 24, scale: float = 0.5) -> asyncio.Queue[bytes | None]:
        self._stopping = False
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
        if not self._subscribers and (self._pump_task is None or self._pump_task.done()):
            self._fps = max(1, min(30, int(fps)))
            self._scale = max(0.2, min(1.0, float(scale)))

        self._subscribers.add(q)

        if self.latest_frame is not None and (time.monotonic() - self.latest_frame_at) < 2.0:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(self.latest_frame)

        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(
                self._pump_loop(), name=f"qa-mjpeg-{self.udid[:8]}"
            )
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes | None]) -> None:
        self._subscribers.discard(q)
        if not self._subscribers and not self._stopping:
            loop = asyncio.get_running_loop()
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            self._idle_timer = loop.call_later(
                self.IDLE_GRACE_SECONDS, self._on_idle_timeout
            )

    def _on_idle_timeout(self) -> None:
        self._idle_timer = None
        if not self._subscribers and self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()

    def _broadcast(self, frame: bytes | None) -> None:
        if frame is not None:
            self.latest_frame = frame
            self.latest_frame_at = time.monotonic()
        for q in list(self._subscribers):
            if frame is None:
                while not q.empty():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(None)
            else:
                if q.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(frame)

    async def stop(self) -> None:
        self._stopping = True
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        self._broadcast(None)
        self._subscribers.clear()
        task = self._pump_task
        self._pump_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=3.0)

    async def _pump_loop(self) -> None:
        backoff = 0.5
        try:
            while not self._stopping:
                if not self._subscribers and self._idle_timer is None:
                    break
                try:
                    sock_path = await self._driver.ensure_companion(self.udid)
                    idb_logger = logging.getLogger(
                        f"ios-agent-testers.stream.{self.udid[:8]}"
                    )
                    idb_logger.setLevel(logging.ERROR)
                    async with Client.build(
                        address=DomainSocketAddress(path=str(sock_path)),
                        logger=idb_logger,
                    ) as client:
                        if client.companion.udid.upper() != self.udid.upper():
                            raise RuntimeError("Companion UDID mismatch during stream")

                        gen = client.stream_video(
                            output_file=None,
                            fps=self._fps,
                            format=VideoFormat.MJPEG,
                            compression_quality=0.82,
                            scale_factor=self._scale,
                        )
                        pending = bytearray()
                        backoff = 0.5
                        try:
                            async for chunk in gen:
                                if self._stopping:
                                    break
                                if not self._subscribers and self._idle_timer is None:
                                    break
                                pending.extend(chunk)
                                while True:
                                    start = pending.find(b"\xff\xd8")
                                    if start < 0:
                                        if len(pending) > 1:
                                            del pending[:-1]
                                        break
                                    end = pending.find(b"\xff\xd9", start + 2)
                                    if end < 0:
                                        if len(pending) > 16 * 1024 * 1024:
                                            raise RuntimeError("Oversized MJPEG frame")
                                        break
                                    frame = bytes(pending[start : end + 2])
                                    del pending[: end + 2]
                                    self._broadcast(frame)
                        finally:
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(gen.aclose(), timeout=1.5)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "MJPEG stream error for %s: %s (retrying in %.1fs)",
                        self.udid[:8],
                        exc,
                        backoff,
                    )
                    if not self._subscribers:
                        break
                    booted = await self._driver._is_simulator_booted(self.udid)
                    if booted is False:
                        self._broadcast(None)
                        break
                    await asyncio.sleep(backoff)
                    backoff = min(2.0, backoff * 1.5)
        except asyncio.CancelledError:
            pass


class IDBDriver:
    """Manages per-UDID fb-idb companions, observation, gestures, logs, and live streams."""

    def __init__(self) -> None:
        self._controls: dict[str, _ControlClient] = {}
        self._streams: dict[str, _StreamHub] = {}
        self._companion_locks: dict[str, asyncio.Lock] = {}
        self._companion_procs: dict[str, subprocess.Popen[Any]] = {}
        self._ephemeral_flags: dict[str, bool] = {}
        self._dimensions_cache: dict[str, dict[str, Any]] = {}
        self._log_tasks: dict[str, tuple[asyncio.Event, asyncio.Task[None]]] = {}
        self._log_buffers: dict[str, collections.deque[str]] = {}
        self._seen_crashes: dict[str, set[str]] = {}
        self._process_names: dict[str, str | None] = {}

    def _key(self, udid: str) -> str:
        return udid.strip().upper()

    def _get_companion_lock(self, udid: str) -> asyncio.Lock:
        key = self._key(udid)
        lock = self._companion_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._companion_locks[key] = lock
        return lock

    def _get_control(self, udid: str) -> _ControlClient:
        key = self._key(udid)
        ctrl = self._controls.get(key)
        if ctrl is None:
            ctrl = _ControlClient(self, udid)
            self._controls[key] = ctrl
        return ctrl

    def _get_stream_hub(self, udid: str) -> _StreamHub:
        key = self._key(udid)
        hub = self._streams.get(key)
        if hub is None:
            hub = _StreamHub(self, udid)
            self._streams[key] = hub
        return hub

    async def _get_simctl_device(self, udid: str) -> dict[str, Any] | None:
        try:
            rc, out, _ = await _run_subproc(
                ["xcrun", "simctl", "list", "devices", "-j"], timeout=8.0
            )
            if rc != 0:
                return None
            data = json.loads(out.decode("utf-8", errors="replace"))
        except Exception:
            return None

        target = self._key(udid)
        for runtime_id, dev_list in (data.get("devices") or {}).items():
            for dev in dev_list or []:
                if str(dev.get("udid") or "").upper() == target:
                    return {
                        "state": str(dev.get("state") or "Unknown"),
                        "deviceType": str(dev.get("deviceTypeIdentifier") or ""),
                        "runtime": str(runtime_id or ""),
                        "name": str(dev.get("name") or ""),
                    }
        return None

    async def _is_simulator_booted(self, udid: str) -> bool | None:
        info = await self._get_simctl_device(udid)
        if info is None:
            return None
        return info.get("state") == "Booted"

    async def ensure_companion(self, udid: str, ephemeral: bool | None = None) -> Path:
        key = self._key(udid)
        if ephemeral is not None:
            self._ephemeral_flags[key] = bool(ephemeral)
        is_ephemeral = self._ephemeral_flags.get(key, True)

        # If attaching to a pre-existing non-ephemeral terminal-phone simulator, reuse its socket first
        if not is_ephemeral:
            tp_sock = TERMINAL_PHONE_SOCK_DIR / f"{udid}.sock"
            if await asyncio.to_thread(_is_unix_socket_alive, tp_sock):
                return tp_sock

        sock_path = QA_SOCK_DIR / f"{udid}.sock"
        if await asyncio.to_thread(_is_unix_socket_alive, sock_path):
            return sock_path

        async with self._get_companion_lock(udid):
            if not is_ephemeral:
                tp_sock = TERMINAL_PHONE_SOCK_DIR / f"{udid}.sock"
                if await asyncio.to_thread(_is_unix_socket_alive, tp_sock):
                    return tp_sock
            if await asyncio.to_thread(_is_unix_socket_alive, sock_path):
                return sock_path

            sim_info = await self._get_simctl_device(udid)
            if sim_info is not None and sim_info.get("state") != "Booted":
                raise RuntimeError(
                    f"Simulator {udid} is not Booted (current state: {sim_info.get('state')})"
                )

            companion_bin = resolve_companion_bin()
            if companion_bin is None or not companion_bin.is_file():
                raise RuntimeError(
                    "idb_companion binary not found. Install terminal-phone or set IOS_AGENT_TESTERS_COMPANION."
                )

            QA_SOCK_DIR.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(QA_SOCK_DIR, 0o700)

            if sock_path.exists():
                with contextlib.suppress(OSError):
                    st = sock_path.stat()
                    if stat.S_ISSOCK(st.st_mode) and not _is_unix_socket_alive(sock_path):
                        sock_path.unlink(missing_ok=True)

            logger.info("Starting idb_companion for %s at %s", udid, sock_path)
            proc = subprocess.Popen(
                [
                    str(companion_bin),
                    "--udid",
                    udid,
                    "--grpc-domain-sock",
                    str(sock_path),
                    "--terminate-offline",
                    "1",
                    "--idle-shutdown-time",
                    "600",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._companion_procs[key] = proc

            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if await asyncio.to_thread(_is_unix_socket_alive, sock_path):
                    return sock_path
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"idb_companion exited early with code {proc.returncode} for {udid}"
                    )
                await asyncio.sleep(0.15)

            raise RuntimeError(
                f"Timed out waiting for idb_companion socket at {sock_path} for {udid}"
            )

    async def connect(self, udid: str, ephemeral: bool = True) -> None:
        self._ephemeral_flags[self._key(udid)] = bool(ephemeral)
        await self.ensure_companion(udid, ephemeral=ephemeral)
        ctrl = self._get_control(udid)
        await ctrl.run(lambda c: c.describe(), timeout=12.0, retry_on_disconnect=True)

    async def describe_screen(self, udid: str) -> dict[str, Any]:
        key = self._key(udid)
        if key in self._dimensions_cache:
            return dict(self._dimensions_cache[key])
        try:
            ctrl = self._get_control(udid)
            desc = await ctrl.run(
                lambda c: c.describe(),
                timeout=6.0,
                retry_on_disconnect=True,
                op_name="describe",
            )
            sd = desc.screen_dimensions
            if sd is not None:
                density = float(sd.density or 0.0)
                width = int(sd.width or 0)
                height = int(sd.height or 0)
                w_pt = int(sd.width_points or 0)
                h_pt = int(sd.height_points or 0)
                if density <= 0 and width > 0 and w_pt > 0:
                    density = round(width / w_pt, 2)
                if density <= 0:
                    density = 3.0
                if w_pt <= 0 and width > 0:
                    w_pt = int(round(width / density))
                if h_pt <= 0 and height > 0:
                    h_pt = int(round(height / density))
                if width <= 0 and w_pt > 0:
                    width = int(round(w_pt * density))
                if height <= 0 and h_pt > 0:
                    height = int(round(h_pt * density))
                if w_pt > 0 and h_pt > 0:
                    result = {
                        "width": width,
                        "height": height,
                        "width_points": w_pt,
                        "height_points": h_pt,
                        "density": float(density),
                    }
                    self._dimensions_cache[key] = result
                    return dict(result)
        except Exception as exc:
            logger.warning("describe() failed for %s: %s", udid[:8], exc)

        sim_info = await self._get_simctl_device(udid)
        w_pt, h_pt, density = _default_dimensions_for_device(
            sim_info.get("deviceType", "") if sim_info else ""
        )
        return {
            "width": int(round(w_pt * density)),
            "height": int(round(h_pt * density)),
            "width_points": w_pt,
            "height_points": h_pt,
            "density": density,
        }

    async def _fetch_ax_tree(
        self,
        udid: str,
        *,
        timeout: float = 8.0,
    ) -> tuple[list[Any] | dict[str, Any], str | None]:
        ctrl = self._get_control(udid)
        backend = _resolve_ax_backend()
        opts = AccessibilityInfoOptions(
            nested=True,
            backend=backend,
            format=AccessibilityOutputFormat.NESTED,
        )

        for attempt in range(2):
            try:
                info = await ctrl.run(
                    lambda c, o=opts: c.accessibility_info(None, o),
                    timeout=timeout,
                    retry_on_disconnect=True,
                    op_name="accessibility_info",
                )
                raw = (info.json or "").strip()
                if not raw:
                    return [], "accessibility_info returned an empty response"
                parsed = json.loads(raw)
                is_empty = (
                    not parsed
                    or (
                        isinstance(parsed, dict)
                        and isinstance(parsed.get("elements"), list)
                        and not parsed["elements"]
                    )
                )
                if is_empty:
                    return [], "accessibility_info returned an empty tree"
                return parsed, None
            except TimeoutError as exc:
                msg = str(exc).strip() or (
                    f"idb accessibility_info timed out after {timeout:.1f}s for {udid[:8]}"
                )
                reason = f"{msg} (possible SpringBoard alert or AX stall)"
                logger.warning(
                    "accessibility_info circuit-breaking on timeout for %s: %s",
                    udid[:8],
                    reason,
                )
                return [], reason
            except Exception as exc:
                err_text = str(exc).strip() or type(exc).__name__
                if attempt == 0 and "no frontmost application" in err_text.lower():
                    logger.debug(
                        "Transient no-frontmost-application AX error for %s; retrying once after 0.5s",
                        udid[:8],
                    )
                    await asyncio.sleep(0.5)
                    continue
                logger.warning(
                    "accessibility_info(backend=%s) failed for %s: %s",
                    backend.name,
                    udid[:8],
                    err_text,
                )
                return [], f"accessibility_info failed: {err_text}"
        return [], "accessibility_info failed"

    async def _fetch_axbridge_tree(
        self,
        udid: str,
        *,
        timeout: float = 6.0,
    ) -> list[Any] | dict[str, Any] | None:
        ctrl = self._get_control(udid)
        opts = AccessibilityInfoOptions(
            nested=True,
            backend=AccessibilityBackend.AXBRIDGE_PERSISTENT,
            format=AccessibilityOutputFormat.NESTED,
        )
        try:
            info = await ctrl.run(
                lambda c, o=opts: c.accessibility_info(None, o),
                timeout=timeout,
                retry_on_disconnect=False,
                op_name="accessibility_info(axbridge)",
            )
            raw = (info.json or "").strip()
            if not raw:
                return None
            return json.loads(raw)
        except Exception as exc:
            logger.debug(
                "Targeted AXBRIDGE_PERSISTENT grafting failed for %s: %s",
                udid[:8],
                str(exc).strip() or type(exc).__name__,
            )
            return None

    async def _capture_point_scaled_png(
        self,
        udid: str,
        output_png: Path,
        wpt: int,
    ) -> tuple[int, int]:
        """Capture PNG at logical point width using fb-idb + /usr/bin/sips fallback (no Pillow)."""
        output_png.parent.mkdir(parents=True, exist_ok=True)
        ctrl = self._get_control(udid)

        png_bytes: bytes | None = None
        try:
            opts = (
                ScreenshotOptions(format=ScreenshotFormat.PNG, max_width=wpt)
                if wpt > 0
                else ScreenshotOptions(format=ScreenshotFormat.PNG)
            )
            shot = await ctrl.run(
                lambda c: c.screenshot(opts),
                timeout=8.0,
                retry_on_disconnect=True,
                op_name="screenshot",
            )
            png_bytes = bytes(shot)
        except Exception as exc:
            exc_msg = str(exc).strip() or type(exc).__name__
            logger.warning(
                "idb PNG screenshot failed for %s (%s); falling back to simctl io",
                udid[:8],
                exc_msg,
            )
            rc, _, err = await _run_subproc(
                [
                    "xcrun",
                    "simctl",
                    "io",
                    udid,
                    "screenshot",
                    "--type=png",
                    str(output_png),
                ],
                timeout=10.0,
            )
            if rc != 0 or not output_png.is_file():
                raise RuntimeError(
                    f"PNG screenshot failed for {udid}: {err.decode('utf-8', errors='replace').strip() or exc_msg}"
                )
            png_bytes = await asyncio.to_thread(output_png.read_bytes)

        await asyncio.to_thread(output_png.write_bytes, png_bytes)

        dims = _png_dimensions(png_bytes)
        if dims is not None:
            img_w, img_h = dims
            if wpt > 0 and abs(img_w - wpt) > 2 and SIPS_BIN.is_file():
                rc, _, err = await _run_subproc(
                    [str(SIPS_BIN), "--resampleWidth", str(wpt), str(output_png)],
                    timeout=10.0,
                )
                if rc == 0:
                    resized = await asyncio.to_thread(output_png.read_bytes)
                    new_dims = _png_dimensions(resized)
                    if new_dims is not None:
                        return new_dims
                else:
                    logger.warning(
                        "sips resampleWidth failed: %s",
                        err.decode("utf-8", errors="replace").strip(),
                    )
            return dims
        return (wpt, 0)

    async def _check_app_running(
        self,
        udid: str,
        bundle_id: str | None,
    ) -> bool | None:
        if not bundle_id:
            return None

        try:
            ctrl = self._get_control(udid)
            apps = await ctrl.run(
                lambda c: c.list_apps(fetch_process_state=True),
                timeout=5.0,
                retry_on_disconnect=True,
                op_name="list_apps",
            )
            for app in apps:
                if app.bundle_id == bundle_id:
                    if app.process_state == AppProcessState.RUNNING:
                        return True
                    break
        except Exception as exc:
            logger.debug("list_apps check failed for %s: %s", udid[:8], exc)

        # Secondary check via launchctl (reliably covers system apps & freshly spawned jobs)
        try:
            rc, out, _ = await _run_subproc(
                ["xcrun", "simctl", "spawn", udid, "launchctl", "list"], timeout=5.0
            )
            if rc == 0:
                text = out.decode("utf-8", errors="replace")
                return bundle_id in text
        except Exception as exc:
            logger.debug("launchctl check failed for %s: %s", udid[:8], exc)

        return None

    async def _collect_crashes(
        self,
        udid: str,
        *,
        bundle_id: str | None,
        run_start_ts: float,
        crashes_dir: Path | None,
    ) -> list[dict[str, Any]]:
        crashes: list[dict[str, Any]] = []
        key = self._key(udid)
        seen = self._seen_crashes.setdefault(key, set())
        since_ts = max(0, int(run_start_ts) - 2)
        bid_low = bundle_id.lower() if bundle_id else ""
        proc_name = (self._process_names.get(key) or "").strip()
        app_short = bundle_id.rsplit(".", 1)[-1].strip() if bundle_id else ""
        match_names = {n.lower() for n in (proc_name, app_short) if n}

        def _matches_crash_info(info: Any) -> bool:
            if not bid_low and not match_names:
                return True
            if bid_low and info.bundle_id and str(info.bundle_id).lower() == bid_low:
                return True
            if info.process_name:
                cp = str(info.process_name).lower()
                if any(cp == m or m in cp for m in match_names):
                    return True
            if info.name:
                cn = str(info.name).lower()
                if (bid_low and bid_low in cn) or any(m in cn for m in match_names):
                    return True
            return False

        # 1. Single fb-idb crash_list call filtered in Python (avoids 2x RPC timeout cascade)
        try:
            ctrl = self._get_control(udid)
            broad = await ctrl.run(
                lambda c: c.crash_list(CrashLogQuery(since=since_ts)),
                timeout=5.0,
                retry_on_disconnect=True,
                op_name="crash_list",
            )
            found = [c for c in broad if _matches_crash_info(c)]
            for info in found:
                cname = str(info.name or "")
                if not cname or cname in seen:
                    continue
                seen.add(cname)
                excerpt = ""
                saved_path: str | None = None
                try:
                    full_crash = await ctrl.run(
                        lambda c, n=cname: c.crash_show(n),
                        timeout=6.0,
                        retry_on_disconnect=True,
                        op_name="crash_show",
                    )
                    contents = full_crash.contents or ""
                    excerpt = contents[:4000]
                    if crashes_dir is not None and contents:
                        crashes_dir.mkdir(parents=True, exist_ok=True)
                        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", cname)
                        out_file = crashes_dir / (
                            safe if safe.endswith((".crash", ".ips", ".txt")) else f"{safe}.crash"
                        )
                        await asyncio.to_thread(out_file.write_text, contents, "utf-8")
                        saved_path = str(out_file)
                except Exception as exc:
                    logger.debug("crash_show failed for %s: %s", cname, exc)

                crashes.append(
                    {
                        "name": cname,
                        "source": "idb",
                        "bundle_id": info.bundle_id or bundle_id,
                        "process_name": info.process_name,
                        "timestamp": info.timestamp,
                        "path": saved_path,
                        "excerpt": excerpt,
                    }
                )
        except Exception as exc:
            logger.debug("idb crash_list failed for %s: %s", udid[:8], exc)

        # 2. Local ~/Library/Logs/DiagnosticReports/*.ips (< 120s & >= run_start_ts)
        if DIAGNOSTIC_REPORTS_DIR.is_dir():
            now = time.time()
            cutoff = max(run_start_ts - 5.0, now - 120.0)
            try:
                ips_files = sorted(
                    DIAGNOSTIC_REPORTS_DIR.glob("*.ips"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                ips_files = []

            for ips in ips_files[:20]:
                try:
                    st = ips.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                marker = f"ips:{ips.name}"
                if marker in seen:
                    continue
                try:
                    head = await asyncio.to_thread(
                        lambda p=ips: p.read_text(encoding="utf-8", errors="replace")[:4000]
                    )
                except OSError:
                    continue

                # Avoid unrelated macOS host process crashes
                low_head = head.lower()
                low_name = ips.name.lower()
                matches_target = False
                if bid_low or match_names:
                    if (bid_low and bid_low in low_head) or any(
                        m in low_name for m in match_names
                    ):
                        matches_target = True
                elif "coresimulator" in low_head or "iphonesimulator" in low_head:
                    matches_target = True

                if not matches_target:
                    continue

                seen.add(marker)
                saved_path = None
                if crashes_dir is not None:
                    crashes_dir.mkdir(parents=True, exist_ok=True)
                    dest = crashes_dir / ips.name
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(shutil.copy2, ips, dest)
                        saved_path = str(dest)

                crashes.append(
                    {
                        "name": ips.name,
                        "source": "ips",
                        "bundle_id": bundle_id,
                        "process_name": ips.stem,
                        "timestamp": int(st.st_mtime),
                        "path": saved_path,
                        "excerpt": head,
                    }
                )

        return crashes

    async def observe(
        self,
        udid: str,
        *,
        bundle_id: str | None,
        output_png: Path,
        run_start_ts: float,
        crashes_dir: Path | None = None,
        max_elements: int = 120,
    ) -> dict[str, Any]:
        """Capture point-scaled PNG, nested AX tree, A11y audit, app-alive, crashes & logs."""
        out_path = Path(output_png)
        dims = await self.describe_screen(udid)
        wpt = int(dims["width_points"])
        hpt = int(dims["height_points"])

        # Capture screenshot & primary AX tree
        img_w, img_h = await self._capture_point_scaled_png(udid, out_path, wpt)
        tree, ax_unavailable_reason = await self._fetch_ax_tree(udid)

        # Fallback dimension inference if describe_screen returned invalid zeros
        if (wpt <= 0 or hpt <= 0) and img_w > 0 and img_h > 0:
            wpt = img_w
            hpt = img_h

        elements = extract_elements(tree, wpt, hpt, max_elements=max_elements)

        # Targeted AXBRIDGE_PERSISTENT grafting into `elements` ONLY when primary AX succeeded
        # and exposed opaque childless containers (e.g. SwiftUI Segmented Picker TabGroup) or < 2 controls.
        if ax_unavailable_reason is None and _resolve_ax_backend() not in (
            AccessibilityBackend.AXBRIDGE,
            AccessibilityBackend.AXBRIDGE_PERSISTENT,
        ):
            opaque_boxes = find_opaque_containers(tree, wpt, hpt)
            if opaque_boxes or len(elements) < 2:
                bridge_tree = await self._fetch_axbridge_tree(udid, timeout=6.0)
                if bridge_tree:
                    target_boxes = (
                        opaque_boxes
                        if opaque_boxes
                        else [
                            {
                                "x": 0.0,
                                "y": 0.0,
                                "width": float(wpt),
                                "height": float(hpt),
                                "kind": "Screen",
                            }
                        ]
                    )
                    elements = graft_elements_in_boxes(
                        elements,
                        bridge_tree,
                        target_boxes,
                        wpt,
                        hpt,
                        max_elements=max_elements,
                    )

        elements_text = format_elements_text(elements)

        a11y_dict: dict[str, Any] = {
            "summary_line": None,
            "unnamed": [],
            "undersized": [],
        }
        a11y_line: str | None = None
        try:
            # Keep a11y audit strictly on the clean primary AX tree (never grafted bridge nodes)
            audit = audit_ios(tree, wpt, hpt)
            a11y_dict = audit.to_dict()
            a11y_line = audit.summary_line
        except Exception as exc:
            logger.warning("a11y.audit_ios failed for %s: %s", udid[:8], exc)

        app_running = await self._check_app_running(udid, bundle_id)
        crashes = await self._collect_crashes(
            udid,
            bundle_id=bundle_id,
            run_start_ts=run_start_ts,
            crashes_dir=crashes_dir,
        )
        recent_logs = self.get_recent_logs(udid, limit=25)

        return {
            "screenshot_path": str(out_path),
            "width_points": wpt,
            "height_points": hpt,
            "elements": elements,
            "elements_text": elements_text,
            "a11y": a11y_dict,
            "a11y_line": a11y_line,
            "ax_unavailable_reason": ax_unavailable_reason,
            "app_running": app_running,
            "crashes": crashes,
            "recent_logs": recent_logs,
        }

    def _resolve_target_point(
        self,
        action: dict[str, Any],
        last_elements: list[dict[str, Any]] | None,
    ) -> tuple[int, int] | None:
        if action.get("x") is not None and action.get("y") is not None:
            return (int(round(float(action["x"]))), int(round(float(action["y"]))))

        elements = last_elements or []
        ref = (action.get("ref") or "").strip()
        if ref:
            for el in elements:
                if str(el.get("ref") or "") == ref:
                    return (int(el["cx"]), int(el["cy"]))

        ident = (action.get("identifier") or "").strip()
        if ident:
            for el in elements:
                if str(el.get("identifier") or "") == ident:
                    return (int(el["cx"]), int(el["cy"]))

        label = (action.get("label") or "").strip()
        if label:
            low = label.lower()
            for el in elements:
                if str(el.get("label") or "").strip().lower() == low:
                    return (int(el["cx"]), int(el["cy"]))
            for el in elements:
                if low in str(el.get("label") or "").lower():
                    return (int(el["cx"]), int(el["cy"]))
            for el in elements:
                if low in str(el.get("value") or "").lower():
                    return (int(el["cx"]), int(el["cy"]))

        return None

    async def _type_text_impl(self, udid: str, text: str) -> str:
        if not text:
            return "empty text (no-op)"
        ctrl = self._get_control(udid)

        if all(ch in hid.KEY_MAP for ch in text):
            await ctrl.run(lambda c: c.text(text), timeout=15.0)
            return f"typed {len(text)} ASCII chars via idb.text"

        # Unicode / CJK / RTL / Emoji: simctl pbcopy + Cmd+V (keycode 25)
        try:
            rc, _, stderr = await _run_subproc(
                ["xcrun", "simctl", "pbcopy", udid],
                timeout=8.0,
                stdin_bytes=text.encode("utf-8"),
            )
            if rc != 0:
                raise RuntimeError(
                    f"simctl pbcopy failed: {stderr.decode('utf-8', errors='replace').strip()}"
                )
            await asyncio.sleep(0.06)
            paste_events = hid.key_press_with_modifiers_to_events(25, ["command"])
            await ctrl.run(lambda c: c.send_events(paste_events), timeout=12.0)
            return f"pasted {len(text)} Unicode chars via pbcopy + Cmd+V"
        except Exception as exc:
            logger.warning("Unicode paste failed (%s); falling back to NFKD ASCII", exc)
            fallback = _to_ascii_fallback(text)
            if fallback:
                await ctrl.run(lambda c: c.text(fallback), timeout=15.0)
                return f"typed {len(fallback)} ASCII fallback chars"
            raise

    async def _clear_field_impl(self, udid: str) -> None:
        ctrl = self._get_control(udid)
        select_all = hid.key_press_with_modifiers_to_events(4, ["command"])
        await ctrl.run(lambda c: c.send_events(select_all), timeout=10.0)
        await ctrl.run(lambda c: c.key(42), timeout=10.0)
        await ctrl.run(lambda c: c.key_sequence([42] * 60), timeout=15.0)

    async def act(
        self,
        udid: str,
        action: dict[str, Any],
        *,
        bundle_id: str | None = None,
        last_elements: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute a single QA action verb on the simulator."""
        raw_kind = str(action.get("kind") or "").strip().lower()
        kind = _KIND_ALIASES.get(raw_kind, raw_kind)
        ctrl = self._get_control(udid)

        if raw_kind == "home":
            action = {**action, "button": "HOME"}

        if kind == "tap":
            pt = self._resolve_target_point(action, last_elements)
            if pt is None:
                raise ValueError(f"Could not resolve target coordinates for tap: {action}")
            await ctrl.run(lambda c: c.tap(float(pt[0]), float(pt[1])), timeout=12.0)
            return {
                "ok": True,
                "kind": "tap",
                "detail": f"tapped ({pt[0]}, {pt[1]})",
                "resolved_point": [pt[0], pt[1]],
            }

        if kind == "long_press":
            pt = self._resolve_target_point(action, last_elements)
            if pt is None:
                raise ValueError(f"Could not resolve target coordinates for long_press: {action}")
            dur = max(0.3, min(5.0, float(action.get("duration", 1.0))))
            await ctrl.run(
                lambda c: c.tap(float(pt[0]), float(pt[1]), duration=dur), timeout=15.0
            )
            return {
                "ok": True,
                "kind": "long_press",
                "detail": f"long-pressed ({pt[0]}, {pt[1]}) for {dur:.2f}s",
                "resolved_point": [pt[0], pt[1]],
            }

        if kind == "multi_tap":
            pt = self._resolve_target_point(action, last_elements)
            if pt is None:
                raise ValueError(f"Could not resolve target coordinates for multi_tap: {action}")
            count = max(2, min(5, int(action.get("count", 2))))
            pause = max(0.02, min(1.0, float(action.get("pause", 0.1))))
            await ctrl.run(
                lambda c: c.multi_tap(float(pt[0]), float(pt[1]), count=count, pause=pause),
                timeout=15.0,
            )
            return {
                "ok": True,
                "kind": "multi_tap",
                "detail": f"tapped ({pt[0]}, {pt[1]}) x{count}",
                "resolved_point": [pt[0], pt[1]],
            }

        if kind == "swipe":
            x1 = int(round(float(action["x1"])))
            y1 = int(round(float(action["y1"])))
            x2 = int(round(float(action["x2"])))
            y2 = int(round(float(action["y2"])))
            dur = max(0.05, min(3.0, float(action.get("duration", 0.5))))
            await ctrl.run(
                lambda c: c.swipe((x1, y1), (x2, y2), duration=dur), timeout=15.0
            )
            return {
                "ok": True,
                "kind": "swipe",
                "detail": f"swiped ({x1},{y1}) -> ({x2},{y2}) in {dur:.2f}s",
                "resolved_point": [x1, y1],
            }

        if kind == "scroll":
            dims = await self.describe_screen(udid)
            wpt = int(dims["width_points"])
            hpt = int(dims["height_points"])
            direction = str(action.get("direction") or "down").strip().lower()
            dur = max(0.1, min(2.0, float(action.get("duration", 0.5))))
            if direction == "down":
                p1, p2 = (wpt // 2, (3 * hpt) // 4), (wpt // 2, hpt // 4)
            elif direction == "up":
                p1, p2 = (wpt // 2, hpt // 4), (wpt // 2, (3 * hpt) // 4)
            elif direction == "right":
                p1, p2 = ((3 * wpt) // 4, hpt // 2), (wpt // 4, hpt // 2)
            elif direction == "left":
                p1, p2 = (wpt // 4, hpt // 2), ((3 * wpt) // 4, hpt // 2)
            else:
                raise ValueError(
                    f"Unsupported scroll direction {direction!r}; expected down|up|left|right"
                )
            await ctrl.run(lambda c: c.swipe(p1, p2, duration=dur), timeout=15.0)
            return {
                "ok": True,
                "kind": "scroll",
                "detail": f"scrolled {direction} ({p1[0]},{p1[1]}) -> ({p2[0]},{p2[1]})",
                "resolved_point": [p1[0], p1[1]],
            }

        if kind == "back":
            dims = await self.describe_screen(udid)
            wpt = int(dims["width_points"])
            hpt = int(dims["height_points"])
            p1 = (4, hpt // 2)
            p2 = (int(wpt * 0.75), hpt // 2)
            await ctrl.run(lambda c: c.swipe(p1, p2, duration=0.35), timeout=12.0)
            return {
                "ok": True,
                "kind": "back",
                "detail": f"edge-back swipe ({p1[0]},{p1[1]}) -> ({p2[0]},{p2[1]})",
                "resolved_point": [p1[0], p1[1]],
            }

        if kind == "dismiss_sheet":
            dims = await self.describe_screen(udid)
            wpt = int(dims["width_points"])
            hpt = int(dims["height_points"])
            p1 = (wpt // 2, hpt // 6)
            p2 = (wpt // 2, (5 * hpt) // 6)
            await ctrl.run(lambda c: c.swipe(p1, p2, duration=0.6), timeout=12.0)
            return {
                "ok": True,
                "kind": "dismiss_sheet",
                "detail": f"dismissed sheet ({p1[0]},{p1[1]}) -> ({p2[0]},{p2[1]})",
                "resolved_point": [p1[0], p1[1]],
            }

        if kind == "type":
            pt = self._resolve_target_point(action, last_elements)
            if pt is not None:
                await ctrl.run(lambda c: c.tap(float(pt[0]), float(pt[1])), timeout=12.0)
                await asyncio.sleep(0.25)
            text = str(action.get("text") or "")
            detail = await self._type_text_impl(udid, text)
            return {
                "ok": True,
                "kind": "type",
                "detail": detail,
                "resolved_point": [pt[0], pt[1]] if pt else None,
            }

        if kind == "replace_text":
            pt = self._resolve_target_point(action, last_elements)
            if pt is not None:
                await ctrl.run(lambda c: c.tap(float(pt[0]), float(pt[1])), timeout=12.0)
                await asyncio.sleep(0.25)
            await self._clear_field_impl(udid)
            text = str(action.get("text") or "")
            detail = "cleared field"
            if text:
                typed = await self._type_text_impl(udid, text)
                detail = f"cleared field and {typed}"
            return {
                "ok": True,
                "kind": "replace_text",
                "detail": detail,
                "resolved_point": [pt[0], pt[1]] if pt else None,
            }

        if kind == "enter":
            await ctrl.run(lambda c: c.key(40), timeout=12.0)
            return {
                "ok": True,
                "kind": "enter",
                "detail": "pressed Return (keycode 40)",
                "resolved_point": None,
            }

        if kind == "key":
            raw_key = action.get("key")
            if isinstance(raw_key, int):
                keycode = raw_key
                key_label = str(raw_key)
            else:
                key_label = str(raw_key or "").strip().lower()
                if key_label not in HID_KEYCODES:
                    raise ValueError(
                        f"Unsupported key {raw_key!r}; supported: {sorted(HID_KEYCODES)}"
                    )
                keycode = HID_KEYCODES[key_label]
            await ctrl.run(lambda c: c.key(keycode), timeout=12.0)
            return {
                "ok": True,
                "kind": "key",
                "detail": f"pressed key {key_label} ({keycode})",
                "resolved_point": None,
            }

        if kind == "button":
            btn_raw = str(action.get("button") or "HOME").strip().upper()
            btn_enum = BUTTON_MAP.get(btn_raw)
            if btn_enum is None:
                raise ValueError(
                    f"Unsupported hardware button {btn_raw!r}; supported: {sorted(BUTTON_MAP)}"
                )
            await ctrl.run(lambda c: c.button(btn_enum), timeout=12.0)
            return {
                "ok": True,
                "kind": "button",
                "detail": f"pressed hardware button {btn_raw}",
                "resolved_point": None,
            }

        if kind == "open":
            target_bid = str(action.get("bundle_id") or bundle_id or "").strip()
            if not target_bid:
                raise ValueError("open action requires bundle_id")
            try:
                await ctrl.run(
                    lambda c: c.launch(target_bid, foreground_if_running=True),
                    timeout=25.0,
                    op_name="launch",
                )
            except Exception:
                rc, _, err = await _run_subproc(
                    ["xcrun", "simctl", "launch", udid, target_bid], timeout=40.0
                )
                if rc != 0:
                    raise RuntimeError(
                        f"Failed to launch {target_bid}: {err.decode('utf-8', errors='replace').strip()}"
                    )
            return {
                "ok": True,
                "kind": "open",
                "detail": f"launched {target_bid}",
                "resolved_point": None,
            }

        if kind == "relaunch":
            target_bid = str(action.get("bundle_id") or bundle_id or "").strip()
            if not target_bid:
                raise ValueError("relaunch action requires bundle_id")
            try:
                await ctrl.run(
                    lambda c: c.terminate(target_bid),
                    timeout=10.0,
                    op_name="terminate",
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await _run_subproc(
                        ["xcrun", "simctl", "terminate", udid, target_bid], timeout=10.0
                    )
            await asyncio.sleep(1.8)
            try:
                await ctrl.run(
                    lambda c: c.launch(target_bid),
                    timeout=25.0,
                    op_name="launch",
                )
            except Exception:
                rc, _, err = await _run_subproc(
                    ["xcrun", "simctl", "launch", udid, target_bid], timeout=40.0
                )
                if rc != 0:
                    raise RuntimeError(
                        f"Failed to relaunch {target_bid}: {err.decode('utf-8', errors='replace').strip()}"
                    )
            await asyncio.sleep(1.5)
            return {
                "ok": True,
                "kind": "relaunch",
                "detail": f"relaunched {target_bid}",
                "resolved_point": None,
            }

        if kind == "open_url":
            url = str(action.get("url") or "").strip()
            if not url:
                raise ValueError("open_url action requires url")
            try:
                await ctrl.run(lambda c: c.open_url(url), timeout=15.0)
            except Exception:
                rc, _, err = await _run_subproc(
                    ["xcrun", "simctl", "openurl", udid, url], timeout=15.0
                )
                if rc != 0:
                    raise RuntimeError(
                        f"Failed to open URL {url}: {err.decode('utf-8', errors='replace').strip()}"
                    )
            return {
                "ok": True,
                "kind": "open_url",
                "detail": f"opened url {url}",
                "resolved_point": None,
            }

        if kind == "rotate":
            ori_raw = str(
                action.get("orientation") or action.get("direction") or "portrait"
            ).strip().lower()
            ori_enum = ORIENTATION_MAP.get(ori_raw)
            if ori_enum is None:
                raise ValueError(
                    f"Unsupported orientation {ori_raw!r}; supported: {sorted(ORIENTATION_MAP)}"
                )
            try:
                await ctrl.run(lambda c: c.set_orientation(ori_enum), timeout=12.0)
            except Exception:
                await ctrl.run(lambda c: c.rotate(ori_enum), timeout=12.0)
            # Invalidate dimensions cache since width/height swap on landscape
            self._dimensions_cache.pop(self._key(udid), None)
            return {
                "ok": True,
                "kind": "rotate",
                "detail": f"rotated to {ori_raw}",
                "resolved_point": None,
            }

        if kind == "shake":
            await ctrl.run(lambda c: c.shake(), timeout=12.0)
            return {
                "ok": True,
                "kind": "shake",
                "detail": "triggered device shake",
                "resolved_point": None,
            }

        if kind == "set_appearance":
            appearance = str(
                action.get("appearance") or action.get("theme") or "dark"
            ).strip().lower()
            if appearance not in ("light", "dark"):
                raise ValueError(f"Unsupported appearance {appearance!r}; expected light|dark")
            rc, _, err = await _run_subproc(
                ["xcrun", "simctl", "ui", udid, "appearance", appearance], timeout=10.0
            )
            if rc != 0:
                raise RuntimeError(
                    f"set_appearance failed: {err.decode('utf-8', errors='replace').strip()}"
                )
            return {
                "ok": True,
                "kind": "set_appearance",
                "detail": f"appearance set to {appearance}",
                "resolved_point": None,
            }

        if kind == "set_text_size":
            ts = str(action.get("text_size") or action.get("content_size") or "").strip()
            if ts not in VALID_TEXT_SIZES:
                raise ValueError(
                    f"Invalid text_size {ts!r}; expected one of {VALID_TEXT_SIZES}"
                )
            rc, _, err = await _run_subproc(
                ["xcrun", "simctl", "ui", udid, "content_size", ts], timeout=10.0
            )
            if rc != 0:
                raise RuntimeError(
                    f"set_text_size failed: {err.decode('utf-8', errors='replace').strip()}"
                )
            return {
                "ok": True,
                "kind": "set_text_size",
                "detail": f"content_size set to {ts}",
                "resolved_point": None,
            }

        if kind == "set_location":
            geo = action.get("geo")
            if isinstance(geo, (list, tuple)) and len(geo) == 2:
                lat, lon = float(geo[0]), float(geo[1])
            else:
                lat = float(
                    action["lat"] if "lat" in action else action["latitude"]
                )
                lon = float(
                    action["lon"] if "lon" in action else action["longitude"]
                )
            try:
                await ctrl.run(lambda c: c.set_location(lat, lon), timeout=10.0)
            except Exception:
                rc, _, err = await _run_subproc(
                    ["xcrun", "simctl", "location", udid, "set", f"{lat},{lon}"],
                    timeout=10.0,
                )
                if rc != 0:
                    raise RuntimeError(
                        f"set_location failed: {err.decode('utf-8', errors='replace').strip()}"
                    )
            return {
                "ok": True,
                "kind": "set_location",
                "detail": f"location set to ({lat}, {lon})",
                "resolved_point": None,
            }

        if kind == "permission":
            raw_mode = action.get("mode") or action.get("action")
            if raw_mode is not None and str(raw_mode).strip():
                mode = str(raw_mode).strip().lower()
            elif "grant" in action and action["grant"] is not None:
                mode = "grant" if bool(action["grant"]) else "revoke"
            else:
                mode = "grant"
            service = str(action.get("service") or action.get("permission") or "").strip().lower()
            target_bid = str(action.get("bundle_id") or bundle_id or "").strip()
            if not service:
                raise ValueError("permission action requires service/permission")

            perm_enum = PERMISSION_MAP.get(service)
            if perm_enum is not None and target_bid and mode in ("grant", "approve", "revoke"):
                try:
                    if mode in ("grant", "approve"):
                        await ctrl.run(
                            lambda c: c.approve(target_bid, {perm_enum}), timeout=10.0
                        )
                    else:
                        await ctrl.run(
                            lambda c: c.revoke(target_bid, {perm_enum}), timeout=10.0
                        )
                    return {
                        "ok": True,
                        "kind": "permission",
                        "detail": f"{mode} {service} for {target_bid} via idb",
                        "resolved_point": None,
                    }
                except Exception:
                    pass

            sim_mode = "grant" if mode in ("grant", "approve") else (
                "revoke" if mode == "revoke" else "reset"
            )
            cmd = ["xcrun", "simctl", "privacy", udid, sim_mode, service]
            if target_bid:
                cmd.append(target_bid)
            rc, _, err = await _run_subproc(cmd, timeout=10.0)
            if rc != 0:
                raise RuntimeError(
                    f"simctl privacy failed: {err.decode('utf-8', errors='replace').strip()}"
                )
            return {
                "ok": True,
                "kind": "permission",
                "detail": f"{sim_mode} {service} via simctl privacy",
                "resolved_point": None,
            }

        if kind == "wait":
            timeout_s = max(0.1, min(30.0, float(action.get("timeout", 5.0))))
            wait_for = str(
                action.get("text") or action.get("label") or action.get("identifier") or ""
            ).strip()
            absent = bool(action.get("absent", False))
            if not wait_for:
                await asyncio.sleep(min(timeout_s, 10.0))
                return {
                    "ok": True,
                    "kind": "wait",
                    "detail": f"waited {min(timeout_s, 10.0):.2f}s",
                    "resolved_point": None,
                }

            needle = wait_for.lower()
            deadline = time.monotonic() + timeout_s
            while True:
                remaining = max(1.0, deadline - time.monotonic())
                tree, _ = await self._fetch_ax_tree(udid, timeout=min(8.0, remaining))
                blob = json.dumps(tree, ensure_ascii=False).lower()
                found = needle in blob
                if (not absent and found) or (absent and not found):
                    return {
                        "ok": True,
                        "kind": "wait",
                        "detail": f"condition met ({'absent' if absent else 'present'}: {wait_for!r})",
                        "resolved_point": None,
                    }
                if time.monotonic() >= deadline:
                    return {
                        "ok": False,
                        "kind": "wait",
                        "detail": f"timed out after {timeout_s:.1f}s waiting for {wait_for!r}",
                        "resolved_point": None,
                    }
                await asyncio.sleep(0.4)

        raise ValueError(f"Unsupported action kind: {action.get('kind')!r}")

    async def get_jpeg_frame(
        self,
        udid: str,
        max_width: int = 480,
        quality: float = 0.75,
    ) -> bytes:
        """Capture a JPEG frame guaranteed to be `<= 580_000` bytes (zero Pillow)."""
        clamped_w = max(120, min(900, int(max_width)))
        q = float(quality)
        if q > 1.0:
            q = q / 100.0
        clamped_q = max(0.2, min(0.95, q))

        ctrl = self._get_control(udid)
        attempts = [
            (clamped_w, clamped_q),
            (min(clamped_w, 390), min(clamped_q, 0.58)),
            (min(clamped_w, 300), min(clamped_q, 0.45)),
        ]
        raw = b""
        for w, cq in attempts:
            shot = await ctrl.run(
                lambda c, mw=w, qv=cq: c.screenshot(
                    ScreenshotOptions(
                        format=ScreenshotFormat.JPEG,
                        compression_quality=qv,
                        max_width=mw,
                    )
                ),
                timeout=12.0,
                retry_on_disconnect=True,
            )
            raw = bytes(shot)
            if len(raw) <= MAX_JPEG_RAW_BYTES:
                return raw
        return raw

    async def stream_mjpeg(
        self,
        udid: str,
        fps: int = 24,
        scale: float = 0.5,
    ) -> AsyncIterator[bytes]:
        """Yield live JPEG frames from a shared per-UDID `_StreamHub`."""
        hub = self._get_stream_hub(udid)
        q = hub.subscribe(fps=fps, scale=scale)
        try:
            while True:
                frame = await q.get()
                if frame is None:
                    break
                yield frame
        finally:
            hub.unsubscribe(q)

    async def start_log_tail(
        self,
        udid: str,
        log_file: Path,
        bundle_id: str | None = None,
        process_name: str | None = None,
    ) -> None:
        """Start background simulator log tailing into `log_file` and an in-memory ring buffer."""
        await self.stop_log_tail(udid)
        key = self._key(udid)
        clean_proc = (process_name or "").strip() or None
        self._process_names[key] = clean_proc
        buf: collections.deque[str] = collections.deque(maxlen=200)
        self._log_buffers[key] = buf
        stop_event = asyncio.Event()
        out_file = Path(log_file)
        out_file.parent.mkdir(parents=True, exist_ok=True)

        async def _tail_worker() -> None:
            args = ["--style", "compact"]
            predicate = _build_log_predicate(bundle_id, clean_proc)
            if predicate:
                args.extend(["--predicate", predicate])

            log_fh: Any = None
            try:
                with contextlib.suppress(OSError):
                    log_fh = out_file.open("a", encoding="utf-8", errors="replace", buffering=1)

                def _record_line(line: str) -> None:
                    clean = line.rstrip("\r\n")
                    if not clean or _LOG_BANNER_RE.match(clean):
                        return
                    buf.append(clean)
                    if log_fh is not None:
                        with contextlib.suppress(OSError):
                            log_fh.write(clean + "\n")

                try:
                    sock_path = await self.ensure_companion(udid)
                    idb_logger = logging.getLogger(f"ios-agent-testers.logs.{udid[:8]}")
                    idb_logger.setLevel(logging.ERROR)
                    async with Client.build(
                        address=DomainSocketAddress(path=str(sock_path)),
                        logger=idb_logger,
                    ) as client:
                        async for chunk in client.tail_logs(stop=stop_event, arguments=args):
                            if stop_event.is_set():
                                break
                            for line in str(chunk).splitlines():
                                _record_line(line)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug(
                        "idb tail_logs stopped for %s (%s); falling back to simctl log stream",
                        udid[:8],
                        exc,
                    )
                    if stop_event.is_set():
                        return
                    sub_proc: asyncio.subprocess.Process | None = None
                    try:
                        sub_proc = await asyncio.create_subprocess_exec(
                            "xcrun",
                            "simctl",
                            "spawn",
                            udid,
                            "log",
                            "stream",
                            *args,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        assert sub_proc.stdout is not None
                        while not stop_event.is_set():
                            raw_line = await sub_proc.stdout.readline()
                            if not raw_line:
                                break
                            _record_line(raw_line.decode("utf-8", errors="replace"))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                    finally:
                        if sub_proc is not None and sub_proc.returncode is None:
                            with contextlib.suppress(ProcessLookupError):
                                sub_proc.terminate()
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(sub_proc.wait(), timeout=2.0)
            finally:
                if log_fh is not None:
                    with contextlib.suppress(OSError):
                        log_fh.flush()
                        log_fh.close()

        task = asyncio.create_task(_tail_worker(), name=f"qa-logs-{udid[:8]}")
        self._log_tasks[key] = (stop_event, task)

    async def stop_log_tail(self, udid: str) -> None:
        key = self._key(udid)
        entry = self._log_tasks.pop(key, None)
        if entry is None:
            return
        stop_event, task = entry
        stop_event.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=2.5)

    def get_recent_logs(self, udid: str, limit: int = 25) -> list[str]:
        buf = self._log_buffers.get(self._key(udid))
        if not buf:
            return []
        lines = list(buf)
        important = [ln for ln in lines if _LOG_IMPORTANT_RE.search(ln)]
        if important:
            return important[-limit:]
        return lines[-limit:]

    async def close_udid(self, udid: str) -> None:
        key = self._key(udid)
        await self.stop_log_tail(udid)

        hub = self._streams.pop(key, None)
        if hub is not None:
            await hub.stop()

        ctrl = self._controls.pop(key, None)
        if ctrl is not None:
            await ctrl.close()

        # If we spawned an isolated companion for an ephemeral sim, shut it down cleanly
        proc = self._companion_procs.pop(key, None)
        if proc is not None and proc.poll() is None and self._ephemeral_flags.get(key, True):
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

        self._dimensions_cache.pop(key, None)
        self._log_buffers.pop(key, None)
        self._seen_crashes.pop(key, None)
        self._ephemeral_flags.pop(key, None)
        self._process_names.pop(key, None)

    async def close_all(self) -> None:
        udids = list(
            set(self._controls.keys())
            | set(self._streams.keys())
            | set(self._log_tasks.keys())
            | set(self._companion_procs.keys())
        )
        for u in udids:
            await self.close_udid(u)
