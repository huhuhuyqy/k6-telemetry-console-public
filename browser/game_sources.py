"""Small, dependency-free telemetry readers used by the K6 demo.

The browser only consumes one normalized JSON shape.  This module keeps the
game-specific details in one place:

* AC / ACC: Kunos ``Local\\acpmf_*`` shared memory.
* LMU: the built-in ``LMU_Data`` shared memory interface.
* FH5 / FH6: Forza Data Out UDP (Sled/Dash packets).

Readers deliberately open existing mappings only.  If a game is not running
or has telemetry output disabled, the endpoint reports ``waiting`` instead of
creating a fake mapping or stale data.
"""

from __future__ import annotations

import ctypes
import math
import os
import socket
import struct
import time
from typing import Optional


FILE_MAP_READ = 0x0004


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _finite(value: float, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _cstring(value: bytes | bytearray) -> str:
    return bytes(value).split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def _kunos_gear(raw_gear: int) -> int:
    """Convert Kunos 0=R, 1=N, 2+=forward to -1=R, 0=N, 1+=forward."""
    return int(raw_gear) - 1


class TransientBooleanEvent:
    """Turn a telemetry flag edge into a short, non-latching alert.

    Several games expose lap validity as a level where zero also means "no
    lap yet" in menus and garages.  Ignoring the initial level and requiring
    an eligible false-to-true transition prevents that zero-filled startup
    state from becoming a permanent warning.
    """

    def __init__(self, duration: float = 3.0) -> None:
        self.duration = max(0.1, float(duration))
        self._previous: bool | None = None
        self._active_until = 0.0

    def reset(self) -> None:
        self._previous = None
        self._active_until = 0.0

    def update(self, raw_active: bool, eligible: bool) -> bool:
        if not eligible:
            self.reset()
            return False
        now = time.monotonic()
        active = bool(raw_active)
        if self._previous is False and active:
            self._active_until = now + self.duration
        self._previous = active
        return now < self._active_until


class _NamedMapping:
    """Open an existing Windows named mapping without creating one."""

    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size
        self.handle: int | None = None
        self.view: int | None = None
        self.kernel32 = None
        if os.name != "nt":
            raise OSError("shared memory requires Windows")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.OpenFileMappingW.restype = ctypes.c_void_p
        kernel32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
        kernel32.MapViewOfFile.restype = ctypes.c_void_p
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not handle:
            raise FileNotFoundError(name)
        view = kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, size)
        if not view:
            kernel32.CloseHandle(handle)
            raise OSError(f"cannot map {name}")
        self.kernel32 = kernel32
        self.handle = handle
        self.view = view

    def read(self) -> bytes:
        if not self.view:
            raise OSError("mapping is closed")
        return ctypes.string_at(self.view, self.size)

    def close(self) -> None:
        if self.kernel32 is not None and self.view:
            self.kernel32.UnmapViewOfFile(self.view)
        if self.kernel32 is not None and self.handle:
            self.kernel32.CloseHandle(self.handle)
        self.view = None
        self.handle = None


class _MappedReader:
    source = "unknown"
    label = "遥测"

    def __init__(self) -> None:
        self._last_packet: object = None
        self._last_change = 0.0

    def _live(self, packet: object) -> bool:
        now = time.monotonic()
        if packet != self._last_packet:
            self._last_packet = packet
            self._last_change = now
        return self._last_change == 0.0 or now - self._last_change <= 2.0

    def _waiting(self, message: str) -> dict[str, object]:
        return {"source": self.source, "connected": False, "status": "waiting", "message": message}

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Original Assetto Corsa (AC1)


class _AcPhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32), ("gas", ctypes.c_float), ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float), ("gear", ctypes.c_int32), ("rpms", ctypes.c_int32),
        ("steerAngle", ctypes.c_float), ("speedKmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3), ("accG", ctypes.c_float * 3),
        ("wheelSlip", ctypes.c_float * 4), ("wheelLoad", ctypes.c_float * 4),
        ("wheelsPressure", ctypes.c_float * 4), ("wheelAngularSpeed", ctypes.c_float * 4),
        ("tyreWear", ctypes.c_float * 4), ("tyreDirtyLevel", ctypes.c_float * 4),
        ("tyreCoreTemperature", ctypes.c_float * 4), ("camberRAD", ctypes.c_float * 4),
        ("suspensionTravel", ctypes.c_float * 4), ("drs", ctypes.c_float), ("tc", ctypes.c_float),
        ("heading", ctypes.c_float), ("pitch", ctypes.c_float), ("roll", ctypes.c_float),
        ("cgHeight", ctypes.c_float), ("carDamage", ctypes.c_float * 5),
        ("numberOfTyresOut", ctypes.c_int32), ("pitLimiterOn", ctypes.c_int32),
        ("abs", ctypes.c_float), ("kersCharge", ctypes.c_float), ("kersInput", ctypes.c_float),
        ("autoShifterOn", ctypes.c_int32), ("rideHeight", ctypes.c_float * 2),
        ("turboBoost", ctypes.c_float), ("ballast", ctypes.c_float), ("airDensity", ctypes.c_float),
        ("airTemp", ctypes.c_float), ("roadTemp", ctypes.c_float),
        ("localAngularVel", ctypes.c_float * 3), ("finalFF", ctypes.c_float),
        ("performanceMeter", ctypes.c_float), ("engineBrake", ctypes.c_int32),
        ("ersRecoveryLevel", ctypes.c_int32), ("ersPowerLevel", ctypes.c_int32),
        ("ersHeatCharging", ctypes.c_int32), ("ersIsCharging", ctypes.c_int32),
        ("kersCurrentKJ", ctypes.c_float), ("drsAvailable", ctypes.c_int32),
        ("drsEnabled", ctypes.c_int32), ("brakeTemp", ctypes.c_float * 4),
        ("clutch", ctypes.c_float), ("tyreTempI", ctypes.c_float * 4),
        ("tyreTempM", ctypes.c_float * 4), ("tyreTempO", ctypes.c_float * 4),
        ("isAIControlled", ctypes.c_int32), ("tyreContactPoint", (ctypes.c_float * 3) * 4),
        ("tyreContactNormal", (ctypes.c_float * 3) * 4),
        ("tyreContactHeading", (ctypes.c_float * 3) * 4),
        ("brakeBias", ctypes.c_float), ("localVelocity", ctypes.c_float * 3),
    ]


class _AcGraphics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32), ("status", ctypes.c_int32), ("session", ctypes.c_int32),
        ("currentTime", ctypes.c_wchar * 15), ("lastTime", ctypes.c_wchar * 15),
        ("bestTime", ctypes.c_wchar * 15), ("split", ctypes.c_wchar * 15),
        ("completedLaps", ctypes.c_int32), ("position", ctypes.c_int32),
        ("iCurrentTime", ctypes.c_int32), ("iLastTime", ctypes.c_int32),
        ("iBestTime", ctypes.c_int32), ("sessionTimeLeft", ctypes.c_float),
        ("distanceTraveled", ctypes.c_float), ("isInPit", ctypes.c_int32),
        ("currentSectorIndex", ctypes.c_int32), ("lastSectorTime", ctypes.c_int32),
        ("numberOfLaps", ctypes.c_int32), ("tyreCompound", ctypes.c_wchar * 33),
        ("replayTimeMultiplier", ctypes.c_float), ("normalizedCarPosition", ctypes.c_float),
        ("carCoordinates", ctypes.c_float * 3), ("penaltyTime", ctypes.c_float),
        ("flag", ctypes.c_int32), ("idealLineOn", ctypes.c_int32),
        ("isInPitLane", ctypes.c_int32), ("surfaceGrip", ctypes.c_float),
        ("mandatoryPitDone", ctypes.c_int32), ("windSpeed", ctypes.c_float),
        ("windDirection", ctypes.c_float),
    ]


class _AcStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", ctypes.c_wchar * 15), ("acVersion", ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int32), ("numCars", ctypes.c_int32),
        ("carModel", ctypes.c_wchar * 33), ("track", ctypes.c_wchar * 33),
        ("playerName", ctypes.c_wchar * 33), ("playerSurname", ctypes.c_wchar * 33),
        ("playerNick", ctypes.c_wchar * 33), ("sectorCount", ctypes.c_int32),
        ("maxTorque", ctypes.c_float), ("maxPower", ctypes.c_float), ("maxRpm", ctypes.c_int32),
        ("maxFuel", ctypes.c_float), ("suspensionMaxTravel", ctypes.c_float * 4),
        ("tyreRadius", ctypes.c_float * 4), ("maxTurboBoost", ctypes.c_float),
        ("airTemp", ctypes.c_float), ("roadTemp", ctypes.c_float),
        ("penaltiesEnabled", ctypes.c_int32), ("aidFuelRate", ctypes.c_float),
        ("aidTireRate", ctypes.c_float), ("aidMechanicalDamage", ctypes.c_float),
        ("aidAllowTyreBlankets", ctypes.c_int32), ("aidStability", ctypes.c_float),
        ("aidAutoClutch", ctypes.c_int32), ("aidAutoBlip", ctypes.c_int32),
        ("hasDRS", ctypes.c_int32), ("hasERS", ctypes.c_int32), ("hasKERS", ctypes.c_int32),
        ("kersMaxJ", ctypes.c_float), ("engineBrakeSettingsCount", ctypes.c_int32),
        ("ersPowerControllerCount", ctypes.c_int32), ("trackSPlineLength", ctypes.c_float),
        ("trackConfiguration", ctypes.c_wchar * 33), ("ersMaxJ", ctypes.c_float),
        ("isTimedRace", ctypes.c_int32), ("hasExtraLap", ctypes.c_int32),
        ("carSkin", ctypes.c_wchar * 33), ("reversedGridPositions", ctypes.c_int32),
        ("pitWindowStart", ctypes.c_int32), ("pitWindowEnd", ctypes.c_int32),
    ]


class AssettoCorsaReader(_MappedReader):
    source = "ac"
    label = "Assetto Corsa"
    _maps = (("Local\\acpmf_physics", 1024), ("Local\\acpmf_graphics", 1024), ("Local\\acpmf_static", 1024))

    def __init__(self) -> None:
        super().__init__()
        self._physics: Optional[_NamedMapping] = None
        self._graphics: Optional[_NamedMapping] = None
        self._static: Optional[_NamedMapping] = None
        self._lap_invalid_event = TransientBooleanEvent()

    def close(self) -> None:
        for mapping in (self._physics, self._graphics, self._static):
            if mapping:
                mapping.close()
        self._physics = self._graphics = self._static = None
        self._lap_invalid_event.reset()

    def _open(self) -> bool:
        if self._physics and self._graphics and self._static:
            return True
        try:
            self._physics = _NamedMapping(*self._maps[0])
            self._graphics = _NamedMapping(*self._maps[1])
            self._static = _NamedMapping(*self._maps[2])
            return True
        except (OSError, FileNotFoundError):
            self.close()
            return False

    def snapshot(self) -> dict[str, object]:
        if not self._open():
            return self._waiting("等待 Assetto Corsa：请启动游戏并启用共享内存插件。")
        try:
            ph = _AcPhysics.from_buffer_copy(self._physics.read())  # type: ignore[union-attr]
            gr = _AcGraphics.from_buffer_copy(self._graphics.read())  # type: ignore[union-attr]
            st = _AcStatic.from_buffer_copy(self._static.read())  # type: ignore[union-attr]
        except (OSError, ValueError, ctypes.ArgumentError):
            self.close()
            return self._waiting("Assetto Corsa 共享内存暂时不可读，正在重连。")
        packets_live = self._live((ph.packetId, gr.packetId))
        session_live = int(gr.status) == 2
        live = packets_live and session_live
        status = "live" if live else "menu" if not session_live else "stale"
        message = (
            "Assetto Corsa 实时遥测" if live
            else "Assetto Corsa 已连接，请进入驾驶界面。" if not session_live
            else "Assetto Corsa 数据已暂停。"
        )
        max_rpm = max(1000, int(st.maxRpm))
        rpm = _clamp(float(ph.rpms), 0, max_rpm * 1.25)
        brake = _clamp(ph.brake, 0, 1)
        slip = max(abs(float(v)) for v in ph.wheelSlip)
        return {
            "source": self.source, "connected": live or not session_live, "status": status,
            "message": message,
            "version": "AC1", "packet": int(ph.packetId), "rpm": rpm, "maxRpm": max_rpm,
            "gear": _kunos_gear(ph.gear), "brake": brake, "throttle": _clamp(ph.gas, 0, 1),
            "clutch": _clamp(ph.clutch, 0, 1), "speedKph": max(0, float(ph.speedKmh)),
            "car": st.carModel.strip(), "shiftUpHint": live and max_rpm >= 1000 and rpm >= max_rpm * .96,
            "shiftDownHint": False, "tcActive": False, "absActive": live and bool(brake > .05 and ph.abs > 0 and slip > .1),
            "pitLimiter": live and bool(ph.pitLimiterOn), "drsAvailable": live and bool(ph.drsAvailable),
            "drsActive": live and (bool(ph.drsEnabled) or float(ph.drs) > .5), "wrongWay": False,
            "flag": int(gr.flag) if live else 0, "globalFlag": 0,
            "damage": [float(v) for v in ph.carDamage] if live else [],
            "brakeTempMax": max([float(v) for v in ph.brakeTemp] + [0.0]) if live else 0.0,
            "tireTempMax": max([float(v) for v in ph.tyreCoreTemperature] + [0.0]) if live else 0.0,
            "rainLights": False, "headlights": False, "mainLightStage": 0,
        }


# ---------------------------------------------------------------------------
# Assetto Corsa Competizione


class _AccPhysics(_AcPhysics):
    # ``_AcPhysics`` already contains the complete AC1-compatible prefix,
    # including localVelocity.  ctypes appends subclass fields after the base
    # layout, so only declare the ACC additions here.
    _fields_ = [
        ("P2PActivation", ctypes.c_int32),
        ("P2PStatus", ctypes.c_int32), ("currentMaxRpm", ctypes.c_int32),
        ("mz", ctypes.c_float * 4), ("fx", ctypes.c_float * 4), ("fy", ctypes.c_float * 4),
        ("slipRatio", ctypes.c_float * 4), ("slipAngle", ctypes.c_float * 4),
        ("tcInAction", ctypes.c_int32), ("absInAction", ctypes.c_int32),
        ("suspensionDamage", ctypes.c_float * 4), ("tyreTemp", ctypes.c_float * 4),
        ("waterTemp", ctypes.c_float), ("brakePressure", ctypes.c_float * 4),
        ("frontBrakeCompound", ctypes.c_int32), ("rearBrakeCompound", ctypes.c_int32),
        ("padLife", ctypes.c_float * 4), ("discLife", ctypes.c_float * 4),
        ("ignitionOn", ctypes.c_int32), ("starterEngineOn", ctypes.c_int32),
        ("isEngineRunning", ctypes.c_int32), ("kerbVibration", ctypes.c_float),
        ("slipVibrations", ctypes.c_float), ("gVibrations", ctypes.c_float),
        ("absVibrations", ctypes.c_float),
    ]


class _AccGraphics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int32), ("status", ctypes.c_int32), ("session", ctypes.c_int32),
        ("currentTime", ctypes.c_wchar * 15), ("lastTime", ctypes.c_wchar * 15),
        ("bestTime", ctypes.c_wchar * 15), ("split", ctypes.c_wchar * 15),
        ("completedLaps", ctypes.c_int32), ("position", ctypes.c_int32),
        ("iCurrentTime", ctypes.c_int32), ("iLastTime", ctypes.c_int32), ("iBestTime", ctypes.c_int32),
        ("sessionTimeLeft", ctypes.c_float), ("distanceTraveled", ctypes.c_float),
        ("isInPit", ctypes.c_int32), ("currentSectorIndex", ctypes.c_int32),
        ("lastSectorTime", ctypes.c_int32), ("numberOfLaps", ctypes.c_int32),
        ("tyreCompound", ctypes.c_wchar * 33), ("replayTimeMultiplier", ctypes.c_float),
        ("normalizedCarPosition", ctypes.c_float), ("activeCars", ctypes.c_int32),
        ("carCoordinates", (ctypes.c_float * 3) * 60), ("carID", ctypes.c_int32 * 60),
        ("playerCarID", ctypes.c_int32), ("penaltyTime", ctypes.c_float), ("flag", ctypes.c_int32),
        ("penalty", ctypes.c_int32), ("idealLineOn", ctypes.c_int32), ("isInPitLane", ctypes.c_int32),
        ("surfaceGrip", ctypes.c_float), ("mandatoryPitDone", ctypes.c_int32),
        ("windSpeed", ctypes.c_float), ("windDirection", ctypes.c_float),
        ("isSetupMenuVisible", ctypes.c_int32), ("mainDisplayIndex", ctypes.c_int32),
        ("secondaryDisplayIndex", ctypes.c_int32), ("TC", ctypes.c_int32), ("TCCUT", ctypes.c_int32),
        ("EngineMap", ctypes.c_int32), ("ABS", ctypes.c_int32), ("fuelXLap", ctypes.c_float),
        ("rainLights", ctypes.c_int32), ("flashingLights", ctypes.c_int32), ("lightsStage", ctypes.c_int32),
        ("exhaustTemperature", ctypes.c_float), ("wiperLV", ctypes.c_int32),
        ("driverStintTotalTimeLeft", ctypes.c_int32), ("driverStintTimeLeft", ctypes.c_int32),
        ("rainTyres", ctypes.c_int32), ("sessionIndex", ctypes.c_int32), ("usedFuel", ctypes.c_float),
        ("deltaLapTime", ctypes.c_wchar * 15), ("iDeltaLapTime", ctypes.c_int32),
        ("estimatedLapTime", ctypes.c_wchar * 15), ("iEstimatedLapTime", ctypes.c_int32),
        ("isDeltaPositive", ctypes.c_int32), ("iSplit", ctypes.c_int32), ("isValidLap", ctypes.c_int32),
        ("fuelEstimatedLaps", ctypes.c_float), ("trackStatus", ctypes.c_wchar * 33),
        ("missingMandatoryPits", ctypes.c_int32), ("Clock", ctypes.c_float),
        ("directionLightsLeft", ctypes.c_int32), ("directionLightsRight", ctypes.c_int32),
        ("GlobalYellow", ctypes.c_int32), ("GlobalYellow1", ctypes.c_int32),
        ("GlobalYellow2", ctypes.c_int32), ("GlobalYellow3", ctypes.c_int32),
        ("GlobalWhite", ctypes.c_int32), ("GlobalGreen", ctypes.c_int32),
        ("GlobalChequered", ctypes.c_int32), ("GlobalRed", ctypes.c_int32),
        ("mfdTyreSet", ctypes.c_int32), ("mfdFuelToAdd", ctypes.c_float),
        ("mfdTyrePressureLF", ctypes.c_float), ("mfdTyrePressureRF", ctypes.c_float),
        ("mfdTyrePressureLR", ctypes.c_float), ("mfdTyrePressureRR", ctypes.c_float),
        ("trackGripStatus", ctypes.c_int32), ("rainIntensity", ctypes.c_int32),
        ("rainIntensityIn10min", ctypes.c_int32), ("rainIntensityIn30min", ctypes.c_int32),
        ("currentTyreSet", ctypes.c_int32), ("strategyTyreSet", ctypes.c_int32),
        ("gapAhead", ctypes.c_int32), ("gapBehind", ctypes.c_int32),
    ]


class _AccStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", ctypes.c_wchar * 15), ("acVersion", ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int32), ("numCars", ctypes.c_int32),
        ("carModel", ctypes.c_wchar * 33), ("track", ctypes.c_wchar * 33),
        ("playerName", ctypes.c_wchar * 33), ("playerSurname", ctypes.c_wchar * 33),
        ("playerNick", ctypes.c_wchar * 33), ("sectorCount", ctypes.c_int32),
        ("maxTorque", ctypes.c_float), ("maxPower", ctypes.c_float), ("maxRpm", ctypes.c_int32),
        ("maxFuel", ctypes.c_float), ("suspensionMaxTravel", ctypes.c_float * 4),
        ("tyreRadius", ctypes.c_float * 4), ("maxTurboBoost", ctypes.c_float),
        ("deprecated_1", ctypes.c_float), ("deprecated_2", ctypes.c_float),
        ("penaltiesEnabled", ctypes.c_int32), ("aidFuelRate", ctypes.c_float),
        ("aidTireRate", ctypes.c_float), ("aidMechanicalDamage", ctypes.c_float),
        ("allowTyreBlankets", ctypes.c_float), ("aidStability", ctypes.c_float),
        ("aidAutoClutch", ctypes.c_int32), ("aidAutoBlip", ctypes.c_int32),
        ("hasDRS", ctypes.c_int32), ("hasERS", ctypes.c_int32), ("hasKERS", ctypes.c_int32),
        ("kersMaxJ", ctypes.c_float), ("engineBrakeSettingsCount", ctypes.c_int32),
        ("ersPowerControllerCount", ctypes.c_int32), ("trackSplineLength", ctypes.c_float),
        ("trackConfiguration", ctypes.c_wchar * 33), ("ersMaxJ", ctypes.c_float),
        ("isTimedRace", ctypes.c_int32), ("hasExtraLap", ctypes.c_int32),
        ("carSkin", ctypes.c_wchar * 33),
        ("reversedGridPositions", ctypes.c_int32), ("pitWindowStart", ctypes.c_int32),
        ("pitWindowEnd", ctypes.c_int32), ("isOnline", ctypes.c_int32),
        ("dryTyresName", ctypes.c_wchar * 33), ("wetTyresName", ctypes.c_wchar * 33),
    ]


class AssettoCorsaCompetizioneReader(AssettoCorsaReader):
    source = "acc"
    label = "Assetto Corsa Competizione"
    # ACC's graphics page is larger than the 1024-byte page used by the AC
    # reader (currently 1588 bytes).  Opening the mapping with the inherited
    # fixed size truncated it, so ``from_buffer_copy`` failed even though ACC
    # was publishing all three mappings correctly.
    _maps = (
        ("Local\\acpmf_physics", ctypes.sizeof(_AccPhysics)),
        ("Local\\acpmf_graphics", ctypes.sizeof(_AccGraphics)),
        ("Local\\acpmf_static", ctypes.sizeof(_AccStatic)),
    )

    def snapshot(self) -> dict[str, object]:
        if not self._open():
            return self._waiting("等待 ACC：请启动 PC 版游戏并进入实际驾驶界面（ACC 会自动提供共享内存）。")
        try:
            ph = _AccPhysics.from_buffer_copy(self._physics.read())  # type: ignore[union-attr]
            gr = _AccGraphics.from_buffer_copy(self._graphics.read())  # type: ignore[union-attr]
            st = _AccStatic.from_buffer_copy(self._static.read())  # type: ignore[union-attr]
        except (OSError, ValueError, ctypes.ArgumentError):
            self.close()
            return self._waiting("ACC 共享内存暂时不可读，正在重连。")
        packets_live = self._live((ph.packetId, gr.packetId))
        session_live = int(gr.status) == 2 and not bool(gr.isSetupMenuVisible)
        live = packets_live and session_live
        status = "live" if live else "menu" if not session_live else "stale"
        message = (
            "ACC 实时遥测" if live
            else "ACC 已连接，请进入驾驶界面。" if not session_live
            else "ACC 数据已暂停。"
        )
        max_rpm = max(1000, int(ph.currentMaxRpm or st.maxRpm))
        rpm = _clamp(float(ph.rpm if hasattr(ph, "rpm") else ph.rpms), 0, max_rpm * 1.25)
        brake = _clamp(ph.brake, 0, 1)
        speed_kph = max(0, float(ph.speedKmh))
        lap_invalid = self._lap_invalid_event.update(not bool(gr.isValidLap), live and speed_kph > 5.0)
        return {
            "source": self.source, "connected": live or not session_live, "status": status,
            "message": message, "version": "ACC",
            "packet": int(ph.packetId), "rpm": rpm, "maxRpm": max_rpm, "gear": _kunos_gear(ph.gear),
            "brake": brake, "throttle": _clamp(ph.gas, 0, 1), "clutch": _clamp(ph.clutch, 0, 1),
            "speedKph": speed_kph, "car": st.carModel.strip(),
            "shiftUpHint": live and max_rpm >= 1000 and rpm >= max_rpm * .96, "shiftDownHint": False,
            "tcActive": live and bool(ph.tcInAction), "absActive": live and bool(ph.absInAction),
            "pitLimiter": live and bool(ph.pitLimiterOn), "drsAvailable": live and bool(ph.drsAvailable),
            "drsActive": live and (bool(ph.drsEnabled) or float(ph.drs) > .5),
            "lapInvalid": lap_invalid,
            "flag": int(gr.flag) if live else 0,
            "globalFlag": int(gr.GlobalRed or gr.GlobalYellow) if live else 0,
            "damage": [float(v) for v in ph.carDamage] if live else [],
            "brakeTempMax": max([float(v) for v in ph.brakeTemp] + [0.0]) if live else 0.0,
            "tireTempMax": max([float(v) for v in ph.tyreTemp] + [0.0]) if live else 0.0,
            "rainLights": live and bool(gr.rainLights), "flashingLights": live and bool(gr.flashingLights),
            "headlights": live and bool(gr.lightsStage), "mainLightStage": int(gr.lightsStage) if live else 0,
            "wiperStage": max(0, min(int(gr.wiperLV), 3)) if live else 0,
            "indicatorLeft": live and bool(gr.directionLightsLeft),
            "indicatorRight": live and bool(gr.directionLightsRight),
            "hazardLights": live and bool(gr.directionLightsLeft and gr.directionLightsRight),
        }


# ---------------------------------------------------------------------------
# Le Mans Ultimate built-in shared memory


class _LmuVect3(ctypes.Structure):
    _pack_ = 4
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("z", ctypes.c_double)]


class _LmuWheel(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("mSuspensionDeflection", ctypes.c_double), ("mRideHeight", ctypes.c_double),
        ("mSuspForce", ctypes.c_double), ("mBrakeTemp", ctypes.c_double),
        ("mBrakePressure", ctypes.c_double), ("mRotation", ctypes.c_double),
        ("mLateralPatchVel", ctypes.c_double), ("mLongitudinalPatchVel", ctypes.c_double),
        ("mLateralGroundVel", ctypes.c_double), ("mLongitudinalGroundVel", ctypes.c_double),
        ("mCamber", ctypes.c_double), ("mLateralForce", ctypes.c_double),
        ("mLongitudinalForce", ctypes.c_double), ("mTireLoad", ctypes.c_double),
        ("mGripFract", ctypes.c_double), ("mPressure", ctypes.c_double),
        ("mTemperature", ctypes.c_double * 3), ("mWear", ctypes.c_double),
        ("mTerrainName", ctypes.c_char * 16), ("mSurfaceType", ctypes.c_ubyte),
        ("mFlat", ctypes.c_bool), ("mDetached", ctypes.c_bool),
        ("mStaticUndeflectedRadius", ctypes.c_ubyte), ("mVerticalTireDeflection", ctypes.c_double),
        ("mWheelYLocation", ctypes.c_double), ("mToe", ctypes.c_double),
        ("mTireCarcassTemperature", ctypes.c_double),
        ("mTireInnerLayerTemperature", ctypes.c_double * 3), ("mOptimalTemp", ctypes.c_float),
        ("mCompoundIndex", ctypes.c_ubyte), ("mCompoundType", ctypes.c_ubyte),
        ("mExpansion", ctypes.c_ubyte * 18),
    ]


class _LmuTelemetry(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("mID", ctypes.c_int), ("mDeltaTime", ctypes.c_double), ("mElapsedTime", ctypes.c_double),
        ("mLapNumber", ctypes.c_int), ("mLapStartET", ctypes.c_double),
        ("mVehicleName", ctypes.c_char * 64), ("mTrackName", ctypes.c_char * 64),
        ("mPos", _LmuVect3), ("mLocalVel", _LmuVect3), ("mLocalAccel", _LmuVect3),
        ("mOri", _LmuVect3 * 3), ("mLocalRot", _LmuVect3), ("mLocalRotAccel", _LmuVect3),
        ("mGear", ctypes.c_int), ("mEngineRPM", ctypes.c_double),
        ("mEngineWaterTemp", ctypes.c_double), ("mEngineOilTemp", ctypes.c_double),
        ("mClutchRPM", ctypes.c_double), ("mUnfilteredThrottle", ctypes.c_double),
        ("mUnfilteredBrake", ctypes.c_double), ("mUnfilteredSteering", ctypes.c_double),
        ("mUnfilteredClutch", ctypes.c_double), ("mFilteredThrottle", ctypes.c_double),
        ("mFilteredBrake", ctypes.c_double), ("mFilteredSteering", ctypes.c_double),
        ("mFilteredClutch", ctypes.c_double), ("mSteeringShaftTorque", ctypes.c_double),
        ("mFront3rdDeflection", ctypes.c_double), ("mRear3rdDeflection", ctypes.c_double),
        ("mFrontWingHeight", ctypes.c_double), ("mFrontRideHeight", ctypes.c_double),
        ("mRearRideHeight", ctypes.c_double), ("mDrag", ctypes.c_double),
        ("mFrontDownforce", ctypes.c_double), ("mRearDownforce", ctypes.c_double),
        ("mFuel", ctypes.c_double), ("mEngineMaxRPM", ctypes.c_double),
        ("mScheduledStops", ctypes.c_ubyte), ("mOverheating", ctypes.c_bool),
        ("mDetached", ctypes.c_bool), ("mHeadlights", ctypes.c_bool),
        ("mDentSeverity", ctypes.c_ubyte * 8), ("mLastImpactET", ctypes.c_double),
        ("mLastImpactMagnitude", ctypes.c_double), ("mLastImpactPos", _LmuVect3),
        ("mEngineTorque", ctypes.c_double), ("mCurrentSector", ctypes.c_int),
        ("mSpeedLimiter", ctypes.c_ubyte), ("mMaxGears", ctypes.c_ubyte),
        ("mFrontTireCompoundIndex", ctypes.c_ubyte), ("mRearTireCompoundIndex", ctypes.c_ubyte),
        ("mFuelCapacity", ctypes.c_double), ("mFrontFlapActivated", ctypes.c_ubyte),
        ("mRearFlapActivated", ctypes.c_ubyte), ("mRearFlapLegalStatus", ctypes.c_ubyte),
        ("mIgnitionStarter", ctypes.c_ubyte), ("mFrontTireCompoundName", ctypes.c_char * 18),
        ("mRearTireCompoundName", ctypes.c_char * 18), ("mSpeedLimiterAvailable", ctypes.c_ubyte),
        ("mAntiStallActivated", ctypes.c_ubyte), ("mUnused", ctypes.c_ubyte * 2),
        ("mVisualSteeringWheelRange", ctypes.c_float), ("mRearBrakeBias", ctypes.c_double),
        ("mTurboBoostPressure", ctypes.c_double), ("mPhysicsToGraphicsOffset", ctypes.c_float * 3),
        ("mPhysicalSteeringWheelRange", ctypes.c_float), ("mDeltaBest", ctypes.c_double),
        ("mBatteryChargeFraction", ctypes.c_double), ("mElectricBoostMotorTorque", ctypes.c_double),
        ("mElectricBoostMotorRPM", ctypes.c_double), ("mElectricBoostMotorTemperature", ctypes.c_double),
        ("mElectricBoostWaterTemperature", ctypes.c_double), ("mElectricBoostMotorState", ctypes.c_ubyte),
        ("mLapInvalidated", ctypes.c_bool), ("mABSActive", ctypes.c_bool),
        ("mTCActive", ctypes.c_bool), ("mSpeedLimiterActive", ctypes.c_bool),
        ("mWiperState", ctypes.c_ubyte), ("mTC", ctypes.c_ubyte), ("mTCMax", ctypes.c_ubyte),
        ("mTCSlip", ctypes.c_ubyte), ("mTCSlipMax", ctypes.c_ubyte), ("mTCCut", ctypes.c_ubyte),
        ("mTCCutMax", ctypes.c_ubyte), ("mABS", ctypes.c_ubyte), ("mABSMax", ctypes.c_ubyte),
        ("mMotorMap", ctypes.c_ubyte), ("mMotorMapMax", ctypes.c_ubyte),
        ("mMigration", ctypes.c_ubyte), ("mMigrationMax", ctypes.c_ubyte),
        ("mFrontAntiSway", ctypes.c_ubyte), ("mFrontAntiSwayMax", ctypes.c_ubyte),
        ("mRearAntiSway", ctypes.c_ubyte), ("mRearAntiSwayMax", ctypes.c_ubyte),
        ("mLiftAndCoastProgress", ctypes.c_ubyte), ("mTrackLimitsSteps", ctypes.c_ubyte),
        ("mRegen", ctypes.c_float), ("mStateOfCharge", ctypes.c_float), ("mVirtualEnergy", ctypes.c_float),
        ("mTimeGapCarAhead", ctypes.c_float), ("mTimeGapCarBehind", ctypes.c_float),
        ("mTimeGapPlaceAhead", ctypes.c_float), ("mTimeGapPlaceBehind", ctypes.c_float),
        ("mVehicleModel", ctypes.c_char * 30), ("mVehicleClass", ctypes.c_ubyte),
        ("mVehicleChampionship", ctypes.c_ubyte), ("mExpansion", ctypes.c_ubyte * 20),
        ("mWheels", _LmuWheel * 4),
    ]


class _LmuTelemetryData(ctypes.Structure):
    _pack_ = 4
    _fields_ = [("activeVehicles", ctypes.c_ubyte), ("playerVehicleIdx", ctypes.c_ubyte),
                ("playerHasVehicle", ctypes.c_bool), ("telemInfo", _LmuTelemetry * 104)]


# The fixed-size blocks before telemetry are documented by S397's
# SharedMemoryInterface header.  Keeping the opaque scoring bytes avoids a
# second large scoring struct while preserving the exact telemetry offset.
_LMU_GENERIC_SIZE = 332
_LMU_PATH_SIZE = 1300
# VehicleScoringInfoV01 is 584 bytes with the C++ ``pack(4)`` layout used by
# LMU.  The 548-byte scoring header and 64 KiB stream precede telemetry.
_LMU_SCORING_SIZE = 548 + 12 + (104 * 584) + 65536


class _LmuObject(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("generic", ctypes.c_ubyte * _LMU_GENERIC_SIZE),
        ("paths", ctypes.c_ubyte * _LMU_PATH_SIZE),
        ("scoring", ctypes.c_ubyte * _LMU_SCORING_SIZE),
        ("telemetry", _LmuTelemetryData),
    ]


class LeMansUltimateReader(_MappedReader):
    source = "lmu"
    label = "Le Mans Ultimate"
    _map_names = ("LMU_Data", "Local\\LMU_Data")

    def __init__(self) -> None:
        super().__init__()
        self._mapping: Optional[_NamedMapping] = None
        self._lap_invalid_event = TransientBooleanEvent()

    def close(self) -> None:
        if self._mapping:
            self._mapping.close()
        self._mapping = None
        self._lap_invalid_event.reset()

    def _open(self) -> bool:
        if self._mapping:
            return True
        size = ctypes.sizeof(_LmuObject)
        for name in self._map_names:
            try:
                self._mapping = _NamedMapping(name, size)
                return True
            except (OSError, FileNotFoundError):
                continue
        return False

    def snapshot(self) -> dict[str, object]:
        if not self._open():
            return self._waiting("等待 LMU：请进入赛道并启用游戏的共享内存输出（LMU_Data）。")
        try:
            data = _LmuObject.from_buffer_copy(self._mapping.read())  # type: ignore[union-attr]
        except (OSError, ValueError, ctypes.ArgumentError):
            self.close()
            return self._waiting("LMU 共享内存暂时不可读，正在重连。")
        td = data.telemetry
        if not bool(td.playerHasVehicle) or int(td.playerVehicleIdx) >= 104:
            self._lap_invalid_event.reset()
            return self._waiting("LMU 已连接，请进入驾驶界面。")
        car = td.telemInfo[int(td.playerVehicleIdx)]
        packet = (int(td.activeVehicles), int(td.playerVehicleIdx), round(float(car.mElapsedTime), 3))
        live = self._live(packet)
        max_rpm = max(1000.0, _finite(car.mEngineMaxRPM, 8000.0))
        rpm = _clamp(_finite(car.mEngineRPM), 0.0, max_rpm * 1.25)
        speed_kph = abs(_finite(car.mLocalVel.z)) * 3.6
        temps = [max(0.0, _finite(w.mBrakeTemp)) for w in car.mWheels]
        tire_temps = [max(0.0, _finite(w.mTemperature[1] - 273.15)) for w in car.mWheels]
        lap_invalid = self._lap_invalid_event.update(bool(car.mLapInvalidated), live and speed_kph > 5.0)
        return {
            "source": self.source, "connected": live, "status": "live" if live else "stale",
            "message": "LMU 实时遥测" if live else "LMU 数据已暂停。", "version": "LMU SHM",
            "packet": int(round(car.mElapsedTime * 1000)), "rpm": rpm, "maxRpm": max_rpm,
            "gear": int(car.mGear), "brake": _clamp(car.mUnfilteredBrake, 0, 1),
            "throttle": _clamp(car.mUnfilteredThrottle, 0, 1), "clutch": _clamp(car.mUnfilteredClutch, 0, 1),
            "speedKph": speed_kph, "car": _cstring(car.mVehicleModel) or _cstring(car.mVehicleName),
            "shiftUpHint": live and max_rpm >= 1000 and rpm >= max_rpm * .96, "shiftDownHint": False,
            "tcActive": live and bool(car.mTCActive), "absActive": live and bool(car.mABSActive),
            "pitLimiter": live and bool(car.mSpeedLimiterActive or car.mSpeedLimiter),
            "drsAvailable": live and bool(car.mRearFlapLegalStatus),
            "drsActive": live and bool(car.mRearFlapActivated),
            "lapInvalid": lap_invalid, "wrongWay": False, "flag": 0, "globalFlag": 0,
            "damage": [v / 2.0 for v in car.mDentSeverity] if live else [],
            "brakeTempMax": max(temps + [0.0]) if live else 0.0,
            "tireTempMax": max(tire_temps + [0.0]) if live else 0.0,
            "wiperStage": max(0, min(int(car.mWiperState), 3)) if live else 0,
            "headlights": live and bool(car.mHeadlights),
            "mainLightStage": int(bool(car.mHeadlights)) if live else 0,
        }


# ---------------------------------------------------------------------------
# Forza Horizon 5/6 Data Out (Sled/Dash)


class ForzaUdpReader(_MappedReader):
    _shared_sockets: dict[int, socket.socket] = {}
    _shared_refs: dict[int, int] = {}

    def __init__(self, source: str, port: int = 5300) -> None:
        super().__init__()
        self.source = source
        self.label = "Forza Horizon 5" if source == "fh5" else "Forza Horizon 6"
        self.port = port
        self._socket: socket.socket | None = None
        self._last_packet_bytes = b""

    def close(self) -> None:
        # FH5 and FH6 use the same Data Out format and port.  Keep the actual
        # socket shared so switching the page between the two sources does not
        # leave the first reader holding 5300 and make the second fail to bind.
        if self._socket is not None:
            refs = self._shared_refs.get(self.port, 1) - 1
            if refs <= 0:
                sock = self._shared_sockets.pop(self.port, None)
                self._shared_refs.pop(self.port, None)
                if sock:
                    sock.close()
            else:
                self._shared_refs[self.port] = refs
        self._socket = None

    def _open(self) -> bool:
        if self._socket:
            return True
        shared = self._shared_sockets.get(self.port)
        if shared:
            self._socket = shared
            self._shared_refs[self.port] = self._shared_refs.get(self.port, 0) + 1
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((os.getenv("FORZA_UDP_HOST", "127.0.0.1"), self.port))
            sock.setblocking(False)
            self._shared_sockets[self.port] = sock
            self._shared_refs[self.port] = 1
            self._socket = sock
            return True
        except OSError:
            self.close()
            return False

    @staticmethod
    def _f32(packet: bytes, offset: int, default: float = 0.0) -> float:
        if offset + 4 > len(packet):
            return default
        return _finite(struct.unpack_from("<f", packet, offset)[0], default)

    def _decode(self, packet: bytes) -> dict[str, object] | None:
        # Known Forza layouts: 232B Sled, 311B FM7 Dash, 324B FH4/FH5
        # Dash (323B has also appeared without its unused trailing byte), and
        # 331B current-generation Dash.  The 331B packet returns to the v1
        # dashboard offsets; treating every large packet as FH5 shifts speed,
        # pedals, gear and tyre temperatures by 12 bytes.
        packet_size = len(packet)
        if packet_size not in {232, 311, 323, 324, 331}:
            return None
        race_on = struct.unpack_from("<i", packet, 0)[0]
        max_rpm = max(1000.0, self._f32(packet, 8, 8000))
        rpm = _clamp(self._f32(packet, 16), 0, max_rpm * 1.25)
        slip = max(abs(self._f32(packet, offset)) for offset in (84, 88, 92, 96))
        in_race = race_on == 1

        if packet_size == 232:
            velocity = (self._f32(packet, 32), self._f32(packet, 36), self._f32(packet, 40))
            speed = math.sqrt(sum(component * component for component in velocity)) * 3.6
            version = "Sled (RPM only)"
            throttle = brake = clutch = 0.0
            gear = 0
            tire_temp_max = 0.0
        else:
            horizon_dash = packet_size in {323, 324}
            speed_offset = 256 if horizon_dash else 244
            tire_offsets = (268, 272, 276, 280) if horizon_dash else (256, 260, 264, 268)
            throttle_offset, brake_offset, clutch_offset, gear_offset = (
                (315, 316, 317, 319) if horizon_dash else (303, 304, 305, 307)
            )
            speed = max(0.0, self._f32(packet, speed_offset) * 3.6)
            throttle = _clamp(packet[throttle_offset] / 255.0, 0, 1)
            brake = _clamp(packet[brake_offset] / 255.0, 0, 1)
            clutch = _clamp(packet[clutch_offset] / 255.0, 0, 1)
            gear_raw = packet[gear_offset]
            gear = -1 if gear_raw == 0xFF else int(gear_raw)
            version = "FH4/FH5 Dash" if horizon_dash else "Dash v3" if packet_size == 331 else "Dash v1"
            # The protocol publishes tyre temperatures in Fahrenheit; the UI
            # thresholds used by the other games are Celsius.
            max_fahrenheit = max(self._f32(packet, offset) for offset in tire_offsets)
            tire_temp_max = max(0.0, (max_fahrenheit - 32.0) * 5.0 / 9.0)
        return {
            "source": self.source, "connected": True, "status": "live" if in_race else "menu",
            "message": f"{self.label} UDP 实时遥测" if in_race else f"{self.label} 已连接，等待进入比赛。",
            "version": version, "packet": int(struct.unpack_from("<I", packet, 4)[0]),
            "rpm": rpm, "maxRpm": max_rpm, "gear": gear, "brake": brake,
            "throttle": throttle, "clutch": clutch,
            "speedKph": speed, "car": "Forza Horizon",
            "shiftUpHint": in_race and max_rpm >= 1000 and rpm >= max_rpm * .96,
            "shiftDownHint": False, "tcActive": False,
            "absActive": in_race and bool(brake > .05 and slip > .1), "pitLimiter": False,
            "drsAvailable": False, "drsActive": False, "wrongWay": False,
            "flag": 0, "globalFlag": 0, "damage": [], "brakeTempMax": 0,
            "tireTempMax": tire_temp_max if in_race else 0.0,
            "headlights": False, "mainLightStage": 0,
        }

    def snapshot(self) -> dict[str, object]:
        if not self._open():
            return self._waiting(
                f"等待 {self.label} UDP：请在游戏设置打开 Data Out，目标 127.0.0.1:{self.port}。"
            )
        packet = None
        try:
            while True:
                candidate = self._socket.recv(4096)  # type: ignore[union-attr]
                if candidate:
                    packet = candidate
        except BlockingIOError:
            pass
        except OSError:
            self.close()
            return self._waiting(f"{self.label} UDP 接收失败，正在重连。")
        if packet is None:
            if self._last_packet_bytes and time.monotonic() - self._last_change <= 2:
                decoded = self._decode(self._last_packet_bytes)
                if decoded:
                    decoded["connected"] = True
                    return decoded
            return self._waiting(f"等待 {self.label} UDP：请在游戏设置打开 Data Out，目标 127.0.0.1:{self.port}。")
        decoded = self._decode(packet)
        if decoded is None:
            return self._waiting(
                f"{self.label} UDP 数据包长度不受支持（收到 {len(packet)}B；"
                "支持 232/311/323/324/331B）。"
            )
        self._last_packet_bytes = packet
        self._last_change = time.monotonic()
        return decoded


def build_extra_readers() -> dict[str, _MappedReader]:
    forza_port = int(os.getenv("FORZA_UDP_PORT", "5300"))
    return {
        "ac": AssettoCorsaReader(), "acc": AssettoCorsaCompetizioneReader(),
        "lmu": LeMansUltimateReader(),
        "fh5": ForzaUdpReader("fh5", forza_port), "fh6": ForzaUdpReader("fh6", forza_port),
    }


__all__ = ["AssettoCorsaReader", "AssettoCorsaCompetizioneReader", "LeMansUltimateReader", "ForzaUdpReader", "build_extra_readers"]
