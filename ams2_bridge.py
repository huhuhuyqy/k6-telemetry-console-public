"""Local HTTP bridge for the K6 demo's multi-game telemetry sources.

Uses only the Python standard library. The browser cannot open Windows named
shared memory or receive game UDP directly, so this process exposes a small
JSON endpoint alongside the static K6 demo files.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from acevo_bridge import AcevoReader
from game_sources import build_extra_readers


MAP_NAME = "$pcars2$"
FILE_MAP_READ = 0x0004


class ParticipantInfo(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("mIsActive", ctypes.c_uint8),
        ("mName", ctypes.c_char * 64),
        ("mWorldPosition", ctypes.c_float * 3),
        ("mCurrentLapDistance", ctypes.c_float),
        ("mRacePosition", ctypes.c_uint32),
        ("mLapsCompleted", ctypes.c_uint32),
        ("mCurrentLap", ctypes.c_uint32),
        ("mCurrentSector", ctypes.c_int32),
    ]


class SharedMemoryPrefix(ctypes.Structure):
    """The stable pCARS2/AMS2 structure prefix through mSequenceNumber."""

    _pack_ = 4
    _fields_ = [
        ("mVersion", ctypes.c_uint32),
        ("mBuildVersionNumber", ctypes.c_uint32),
        ("mGameState", ctypes.c_uint32),
        ("mSessionState", ctypes.c_uint32),
        ("mRaceState", ctypes.c_uint32),
        ("mViewedParticipantIndex", ctypes.c_int32),
        ("mNumParticipants", ctypes.c_int32),
        ("mParticipantInfo", ParticipantInfo * 64),
        ("mUnfilteredThrottle", ctypes.c_float),
        ("mUnfilteredBrake", ctypes.c_float),
        ("mUnfilteredSteering", ctypes.c_float),
        ("mUnfilteredClutch", ctypes.c_float),
        ("mCarName", ctypes.c_char * 64),
        ("mCarClassName", ctypes.c_char * 64),
        ("mLapsInEvent", ctypes.c_uint32),
        ("mTrackLocation", ctypes.c_char * 64),
        ("mTrackVariation", ctypes.c_char * 64),
        ("mTrackLength", ctypes.c_float),
        ("mNumSectors", ctypes.c_int32),
        ("mLapInvalidated", ctypes.c_uint8),
        ("_timingPadding", ctypes.c_uint8 * 3),
        ("mTimingValues", ctypes.c_float * 21),
        ("mHighestFlagColour", ctypes.c_uint32),
        ("mHighestFlagReason", ctypes.c_uint32),
        ("mPitMode", ctypes.c_uint32),
        ("mPitSchedule", ctypes.c_uint32),
        ("mCarFlags", ctypes.c_uint32),
        ("mOilTempCelsius", ctypes.c_float),
        ("mOilPressureKPa", ctypes.c_float),
        ("mWaterTempCelsius", ctypes.c_float),
        ("mWaterPressureKPa", ctypes.c_float),
        ("mFuelPressureKPa", ctypes.c_float),
        ("mFuelLevel", ctypes.c_float),
        ("mFuelCapacity", ctypes.c_float),
        ("mSpeed", ctypes.c_float),
        ("mRpm", ctypes.c_float),
        ("mMaxRPM", ctypes.c_float),
        ("mBrake", ctypes.c_float),
        ("mThrottle", ctypes.c_float),
        ("mClutch", ctypes.c_float),
        ("mSteering", ctypes.c_float),
        ("mGear", ctypes.c_int32),
        ("mNumGears", ctypes.c_int32),
        ("mOdometerKM", ctypes.c_float),
        ("mAntiLockActive", ctypes.c_uint8),
        ("_collisionPadding", ctypes.c_uint8 * 3),
        ("mLastOpponentCollisionIndex", ctypes.c_int32),
        ("mLastOpponentCollisionMagnitude", ctypes.c_float),
        ("mBoostActive", ctypes.c_uint8),
        ("_boostPadding", ctypes.c_uint8 * 3),
        ("mBoostAmount", ctypes.c_float),
        ("mMotionValues", ctypes.c_float * (7 * 3)),
        ("mTyreFlags", ctypes.c_uint32 * 4),
        ("mTerrain", ctypes.c_uint32 * 4),
        ("mTyreValues", ctypes.c_float * (16 * 4)),
        ("mCrashState", ctypes.c_uint32),
        ("mAeroDamage", ctypes.c_float),
        ("mEngineDamage", ctypes.c_float),
        ("mWeatherValues", ctypes.c_float * 7),
        ("mSequenceNumber", ctypes.c_uint32),
    ]


def _decode_c_string(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")


class AMS2Reader:
    def __init__(self) -> None:
        self._handle: int | None = None
        self._view: int | None = None
        self._last_sequence: int | None = None
        self._last_sequence_change = 0.0
        self._kernel32 = None
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
            kernel32.OpenFileMappingW.restype = ctypes.c_void_p
            kernel32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
            kernel32.MapViewOfFile.restype = ctypes.c_void_p
            kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
            kernel32.UnmapViewOfFile.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            self._kernel32 = kernel32

    def close(self) -> None:
        if self._kernel32 is not None and self._view:
            self._kernel32.UnmapViewOfFile(self._view)
        if self._kernel32 is not None and self._handle:
            self._kernel32.CloseHandle(self._handle)
        self._view = None
        self._handle = None

    def _open(self) -> bool:
        if self._view:
            return True
        if self._kernel32 is None:
            return False
        handle = self._kernel32.OpenFileMappingW(FILE_MAP_READ, False, MAP_NAME)
        if not handle:
            return False
        view = self._kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, ctypes.sizeof(SharedMemoryPrefix))
        if not view:
            self._kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self._view = view
        return True

    def _read_consistent(self) -> SharedMemoryPrefix | None:
        if not self._open() or not self._view:
            return None
        size = ctypes.sizeof(SharedMemoryPrefix)
        for _ in range(5):
            first = SharedMemoryPrefix.from_buffer_copy(ctypes.string_at(self._view, size))
            if first.mSequenceNumber & 1:
                time.sleep(0.001)
                continue
            second = SharedMemoryPrefix.from_buffer_copy(ctypes.string_at(self._view, size))
            if first.mSequenceNumber == second.mSequenceNumber and not (second.mSequenceNumber & 1):
                return second
        return None

    def snapshot(self) -> dict[str, object]:
        if self._kernel32 is None:
            return {"source": "ams2", "connected": False, "status": "unsupported_os", "message": "AMS2 shared memory requires Windows."}
        page = self._read_consistent()
        if page is None:
            return {
                "source": "ams2",
                "connected": False,
                "status": "waiting",
                "message": "等待 AMS2：请启动游戏并把共享内存设为 Project CARS 2。",
            }

        values = (page.mRpm, page.mMaxRPM, page.mBrake)
        if not 8 <= page.mVersion <= 100 or not all(math.isfinite(value) for value in values):
            self.close()
            return {"source": "ams2", "connected": False, "status": "invalid", "message": "共享内存格式无效，请选择 Project CARS 2 模式。"}

        now = time.monotonic()
        if page.mSequenceNumber != self._last_sequence:
            self._last_sequence = page.mSequenceNumber
            self._last_sequence_change = now
        stale = self._last_sequence_change > 0 and now - self._last_sequence_change > 2.0

        max_rpm = max(0.0, min(float(page.mMaxRPM), 30000.0))
        rpm = max(0.0, min(float(page.mRpm), max(max_rpm * 1.25, 30000.0)))
        # Front-end pages can retain the last car's RPM/flag values.  Game
        # state is the authoritative gate; max RPM is not a reliable menu
        # detector after returning from a session.
        # Only GAME_INGAME_PLAYING (2) represents player-controlled live
        # driving.  Pause, setup menus, restarting and replay can retain RPM
        # and flag values and must not keep an old lighting cue active.
        session_live = page.mGameState == 2
        status = "stale" if session_live and stale else "live" if session_live else "menu"
        effects_live = status == "live"
        message = (
            "AMS2 已连接，请进入或恢复驾驶。"
            if not session_live
            else "AMS2 数据已暂停或游戏已退出。"
            if stale
            else "AMS2 实时遥测"
        )
        car_flags = int(page.mCarFlags)
        tyre_values = [float(value) for value in page.mTyreValues]
        tyre_temp_max = max([value for value in tyre_values[12:16] if math.isfinite(value)] + [0.0])
        brake_temp_max = max([value for value in tyre_values[40:44] if math.isfinite(value)] + [0.0])
        return {
            "source": "ams2",
            "connected": not session_live or not stale,
            "status": status,
            "message": message,
            "version": int(page.mVersion),
            "build": int(page.mBuildVersionNumber),
            "sequence": int(page.mSequenceNumber),
            "gameState": int(page.mGameState),
            "sessionState": int(page.mSessionState),
            "raceState": int(page.mRaceState),
            "rpm": rpm,
            "maxRpm": max_rpm,
            "gear": int(page.mGear),
            "numGears": int(page.mNumGears),
            "brake": max(0.0, min(float(page.mBrake), 1.0)),
            "throttle": max(0.0, min(float(page.mThrottle), 1.0)),
            # AMS2 exposes ABS directly.  The shared-memory page does not
            # publish a separate TC/DRS/indicator action flag, so those cues
            # stay false instead of being guessed from unrelated values.
            "absActive": effects_live and (bool(page.mAntiLockActive) or bool(car_flags & (1 << 4))),
            "tcActive": effects_live and bool(car_flags & (1 << 6)),
            "drsAvailable": False,
            "drsActive": False,
            "shiftUpHint": effects_live and max_rpm >= 1000.0 and rpm >= max_rpm * 0.96,
            "shiftDownHint": False,
            "wrongWay": False,
            "damage": [
                max(0.0, min(float(page.mAeroDamage), 1.0)),
                max(0.0, min(float(page.mEngineDamage), 1.0)),
            ] if effects_live else [],
            "flag": int(page.mHighestFlagColour) if effects_live else 0,
            # The reason value is meaningful only while a flag colour is
            # active; stale non-zero reasons must not trigger a flag warning.
            "globalFlag": int(page.mHighestFlagReason) if effects_live and page.mHighestFlagColour else 0,
            "pitLimiter": effects_live and bool(car_flags & (1 << 3)),
            "brakeTempMax": brake_temp_max if effects_live else 0.0,
            "tireTempMax": tyre_temp_max if effects_live else 0.0,
            "headlights": effects_live and bool(car_flags & (1 << 0)),
            "mainLightStage": 1 if effects_live and bool(car_flags & (1 << 0)) else 0,
            "warningLights": effects_live and bool(car_flags & (1 << 2)),
            "speedKph": max(0.0, float(page.mSpeed) * 3.6),
            "car": _decode_c_string(bytes(page.mCarName)),
            "carClass": _decode_c_string(bytes(page.mCarClassName)),
        }


class DemoHandler(SimpleHTTPRequestHandler):
    reader: AMS2Reader
    acevo_reader: AcevoReader
    extra_readers: dict[str, object]
    root: str

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=self.root, **kwargs)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/telemetry":
            query = parse_qs(urlparse(self.path).query)
            source = query.get("source", ["ams2"])[0].lower()
            if source in {"acevo", "ac-evo", "evo"}:
                selected_reader = self.acevo_reader
            elif source in self.extra_readers:
                selected_reader = self.extra_readers[source]
            else:
                selected_reader = self.reader
            payload = json.dumps(selected_reader.snapshot(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "http://localhost:8765")
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def end_headers(self) -> None:
        if urlparse(self.path).path != "/api/telemetry":
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        if urlparse(self.path).path != "/api/telemetry":
            super().log_message(fmt, *args)


class DemoServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR can let an old static server and this bridge both
    # bind the same port, randomly routing requests to the wrong process.
    allow_reuse_address = False


def start_parent_watchdog(parent_pid: int | None) -> None:
    """Exit the bridge if the Electron main process disappears.

    This is independent of Electron's normal shutdown handlers, so crashes,
    forced exits and portable-launcher teardown cannot orphan the bridge.
    """
    if not parent_pid or parent_pid <= 0:
        return

    def watch_windows_parent() -> None:
        synchronize = 0x00100000
        wait_forever = 0xFFFFFFFF
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait_for_single_object.restype = ctypes.c_uint32
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]

        handle = open_process(synchronize, False, parent_pid)
        if not handle:
            if ctypes.get_last_error() == error_invalid_parameter:
                os._exit(0)
            return
        try:
            wait_for_single_object(handle, wait_forever)
        finally:
            close_handle(handle)
        os._exit(0)

    def watch_posix_parent() -> None:
        while os.getppid() == parent_pid:
            time.sleep(0.5)
        os._exit(0)

    target = watch_windows_parent if os.name == "nt" else watch_posix_parent
    threading.Thread(target=target, name="electron-parent-watchdog", daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="AMS2 / AC-family / LMU / Forza telemetry bridge and K6 demo server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path, default=None, help="directory containing index.html, app.js and style.css")
    parser.add_argument("--parent-pid", type=int, default=None, help="exit automatically when this process ends")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    start_parent_watchdog(args.parent_pid)

    reader = AMS2Reader()
    acevo_reader = AcevoReader()
    extra_readers = build_extra_readers()
    if args.self_test:
        offsets = {
            "size": ctypes.sizeof(SharedMemoryPrefix),
            "rpm": SharedMemoryPrefix.mRpm.offset,
            "maxRpm": SharedMemoryPrefix.mMaxRPM.offset,
            "brake": SharedMemoryPrefix.mBrake.offset,
            "gear": SharedMemoryPrefix.mGear.offset,
            "sequence": SharedMemoryPrefix.mSequenceNumber.offset,
        }
        print(json.dumps({
            "ams2": {"offsets": offsets, "snapshot": reader.snapshot()},
            "acevo": acevo_reader.snapshot(),
            "extra": {name: extra_reader.snapshot() for name, extra_reader in extra_readers.items()},
        }, ensure_ascii=False, indent=2))
        reader.close()
        acevo_reader.close()
        for extra_reader in extra_readers.values():
            extra_reader.close()
        return 0

    root = str((args.root or Path(__file__).resolve().parent).resolve())
    DemoHandler.reader = reader
    DemoHandler.acevo_reader = acevo_reader
    DemoHandler.extra_readers = extra_readers
    DemoHandler.root = root
    server = DemoServer(("127.0.0.1", args.port), DemoHandler)
    print(f"K6 Shift Light Demo: http://localhost:{args.port}/")
    print("AMS2: Options > System > Shared Memory > Project CARS 2")
    print("AC EVO 0.8: Local\\acevo_pmf_physics + Local\\acevo_pmf_graphics")
    print("AC: Local\\acpmf_physics + Local\\acpmf_graphics + Local\\acpmf_static")
    print("ACC: same mappings, ACC layout")
    print("FH5/FH6: UDP Data Out on 127.0.0.1:5300 (override FORZA_UDP_PORT)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        reader.close()
        acevo_reader.close()
        for extra_reader in extra_readers.values():
            extra_reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
