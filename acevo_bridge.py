"""Small, dependency-free Assetto Corsa EVO shared-memory reader.

The game publishes three Windows named mappings.  This bridge only needs the
physics and graphics pages for the K6 effects, so it reads the documented
800-byte physics page and the 4,900-byte graphics page.  All values are
returned as JSON-friendly primitives and missing mappings are reported as a
normal ``waiting`` state instead of creating a fake mapping.

The offsets mirror the public AC EVO shared-memory layout used by current
0.8-era telemetry tools.  Keep the parser deliberately defensive: the game
is early access and a layout change should produce a clear status message,
not a browser crash.
"""

from __future__ import annotations

import ctypes
import math
import struct
import time
from typing import Optional

from game_sources import TransientBooleanEvent


FILE_MAP_READ = 0x0004
PHYSICS_MAP = "Local\\acevo_pmf_physics"
GRAPHICS_MAP = "Local\\acevo_pmf_graphics"
PHYSICS_SIZE = 800
GRAPHICS_SIZE = 8192  # the current documented struct is about 4.9 KiB


class _NamedMapping:
    """Open an existing mapping without accidentally creating a new one."""

    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.size = size
        self.handle: int | None = None
        self.view: int | None = None
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("Assetto Corsa EVO shared memory requires Windows")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.OpenFileMappingW.restype = ctypes.c_void_p
        kernel32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
        kernel32.MapViewOfFile.restype = ctypes.c_void_p
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.UnmapViewOfFile.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        self.kernel32 = kernel32

        handle = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
        if not handle:
            error = ctypes.get_last_error()
            if error == 2:
                raise FileNotFoundError(name)
            raise OSError(error, ctypes.FormatError(error), name)
        view = kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, size)
        if not view:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, ctypes.FormatError(error), name)
        self.handle = handle
        self.view = view

    def read(self) -> bytes:
        if not self.view:
            raise OSError("shared-memory view is closed")
        return ctypes.string_at(self.view, self.size)

    def close(self) -> None:
        if self.view:
            self.kernel32.UnmapViewOfFile(self.view)
            self.view = None
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _f32(data: bytes, offset: int, default: float = 0.0) -> float:
    try:
        value = struct.unpack_from("<f", data, offset)[0]
    except (struct.error, TypeError):
        return default
    return float(value) if math.isfinite(value) else default


def _i32(data: bytes, offset: int, default: int = 0) -> int:
    try:
        return int(struct.unpack_from("<i", data, offset)[0])
    except (struct.error, TypeError):
        return default


def _u16(data: bytes, offset: int, default: int = 0) -> int:
    try:
        return int(struct.unpack_from("<H", data, offset)[0])
    except (struct.error, TypeError):
        return default


def _i16(data: bytes, offset: int, default: int = 0) -> int:
    try:
        return int(struct.unpack_from("<h", data, offset)[0])
    except (struct.error, TypeError):
        return default


def _u8(data: bytes, offset: int) -> int:
    return data[offset] if 0 <= offset < len(data) else 0


def _i8(data: bytes, offset: int, default: int = 0) -> int:
    try:
        return int(struct.unpack_from("<b", data, offset)[0])
    except (struct.error, TypeError):
        return default


def _f32_array(data: bytes, offset: int, count: int) -> list[float]:
    return [_f32(data, offset + index * 4) for index in range(count)]


def _text(data: bytes, offset: int, length: int) -> str:
    raw = data[offset:offset + length].split(b"\0", 1)[0]
    return raw.decode("ascii", errors="ignore").strip()


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


class AcevoReader:
    """Read AC EVO 0.8 telemetry and expose K6-relevant event flags."""

    def __init__(self) -> None:
        self._physics: Optional[_NamedMapping] = None
        self._graphics: Optional[_NamedMapping] = None
        self._last_packet: tuple[int, int] | None = None
        self._last_change = 0.0
        self._lap_invalid_event = TransientBooleanEvent()

    def close(self) -> None:
        for mapping in (self._physics, self._graphics):
            if mapping:
                mapping.close()
        self._physics = None
        self._graphics = None
        self._lap_invalid_event.reset()

    def _open(self) -> bool:
        if self._physics is None:
            try:
                self._physics = _NamedMapping(PHYSICS_MAP, PHYSICS_SIZE)
            except (FileNotFoundError, OSError):
                self._physics = None
        if self._graphics is None:
            try:
                self._graphics = _NamedMapping(GRAPHICS_MAP, GRAPHICS_SIZE)
            except (FileNotFoundError, OSError):
                self._graphics = None
        return self._physics is not None and self._graphics is not None

    def snapshot(self) -> dict[str, object]:
        if not self._open():
            return {
                "source": "ac-evo",
                "connected": False,
                "status": "waiting",
                "message": "等待 AC EVO：请启动游戏并进入驾驶界面。",
            }

        try:
            physics = self._physics.read()  # type: ignore[union-attr]
            graphics = self._graphics.read()  # type: ignore[union-attr]
        except (OSError, ValueError):
            self.close()
            return {
                "source": "ac-evo",
                "connected": False,
                "status": "waiting",
                "message": "AC EVO 共享内存暂时不可读，正在重连。",
            }

        physics_packet = _i32(physics, 0)
        graphics_packet = _i32(graphics, 0)
        packet = (physics_packet, graphics_packet)
        if packet != self._last_packet:
            self._last_packet = packet
            self._last_change = time.monotonic()
        stale = self._last_change > 0 and time.monotonic() - self._last_change > 2.0

        rpm = max(0.0, _i32(physics, 20))
        max_rpm = max(1000.0, _i32(physics, 588))
        if not math.isfinite(max_rpm) or max_rpm > 30000:
            max_rpm = 8000.0
        rpm_percent = _clamp(_f32(graphics, 72), 0.0, 1.0)
        if rpm_percent <= 0.0:
            rpm_percent = _clamp(rpm / max_rpm, 0.0, 1.0)

        brake = _clamp(_f32(physics, 8), 0.0, 1.0)
        throttle = _clamp(_f32(physics, 4), 0.0, 1.0)
        speed_kph = max(0.0, _f32(physics, 28))
        tc_intensity = _clamp(_f32(physics, 204), 0.0, 1.0)
        abs_intensity = _clamp(_f32(physics, 252), 0.0, 1.0)
        tc_active = bool(_i32(physics, 672)) or tc_intensity > 0.01 or bool(_u8(graphics, 45))
        abs_active = bool(_i32(physics, 676)) or abs_intensity > 0.01 or bool(_u8(graphics, 46))

        brake_temps = _f32_array(physics, 348, 4)
        tire_temps = _f32_array(physics, 152, 4)
        slip = _f32_array(physics, 640, 4)
        wheel_lock = []
        for wheel_index in range(4):
            wheel_base = 220 + wheel_index * 256
            wheel_lock.append(bool(_u8(graphics, wheel_base + 4)))

        wheels: list[dict[str, object]] = []
        for index, wheel_base in enumerate((220, 476, 732, 988)):
            wheels.append({
                "id": ("FL", "FR", "RL", "RR")[index],
                "pressure": max(0.0, _f32(graphics, wheel_base + 8)),
                "temperature": max(0.0, _f32(graphics, wheel_base + 12, tire_temps[index])),
                "temperatureInner": max(0.0, _f32(graphics, wheel_base + 24)),
                "temperatureMiddle": max(0.0, _f32(graphics, wheel_base + 28)),
                "temperatureOuter": max(0.0, _f32(graphics, wheel_base + 32)),
                "brakeTemperature": max(0.0, _f32(graphics, wheel_base + 16, brake_temps[index])),
                "slip": abs(slip[index]),
                "lock": wheel_lock[index],
            })

        max_brake_temp = max(brake_temps + [wheel["brakeTemperature"] for wheel in wheels])
        max_tire_temp = max(tire_temps + [wheel["temperature"] for wheel in wheels])
        damage = [
            _clamp(_f32(graphics, 1260 + index * 4), 0.0, 1.0)
            for index in range(5)
        ]
        # graphics.instrumentation (documented 128-byte block at offset 1488)
        # exposes exactly the vehicle controls that are visible in EVO's car
        # showroom and in the cockpit: wiper stage, indicators, hazards,
        # warning lights, rain lights and headlight visibility.
        instrumentation = {
            "mainLightStage": _u8(graphics, 1488),
            "specialLightStage": _u8(graphics, 1489),
            "cockpitLightStage": _u8(graphics, 1490),
            "wiperStage": _u8(graphics, 1491),
            "rainLights": bool(_u8(graphics, 1492)),
            "indicatorLeft": bool(_u8(graphics, 1493)),
            "indicatorRight": bool(_u8(graphics, 1494)),
            "flashingLights": bool(_u8(graphics, 1495)),
            "warningLights": bool(_u8(graphics, 1496)),
            "headlights": bool(_u8(graphics, 1514)),
        }
        raw_lap_valid = bool(_u8(graphics, 3121))
        lap_invalid = self._lap_invalid_event.update(
            raw_active=not raw_lap_valid,
            eligible=not stale and speed_kph > 5.0,
        )
        car = _text(graphics, 3086, 33)
        packet_status = "stale" if stale else "live"
        return {
            "source": "ac-evo",
            "connected": not stale,
            "status": packet_status,
            "message": "AC EVO 实时遥测" if not stale else "AC EVO 数据已暂停或游戏已退出。",
            "version": "0.8",
            "packet": physics_packet,
            "rpm": rpm,
            "rpmPercent": rpm_percent,
            "maxRpm": max_rpm,
            "gear": _i32(physics, 16, _i16(graphics, 68)),
            "brake": brake,
            "throttle": throttle,
            "clutch": _clamp(_f32(physics, 364), 0.0, 1.0),
            "speedKph": speed_kph,
            "car": car,
            "shiftUpHint": bool(_u8(graphics, 43)) or rpm_percent >= 0.96,
            "shiftDownHint": bool(_u8(graphics, 44)),
            "tcActive": tc_active,
            "tcIntensity": tc_intensity,
            "absActive": abs_active,
            "absIntensity": abs_intensity,
            "pitLimiter": bool(_i32(physics, 248)) or bool(_u8(graphics, 1910)),
            "drsAvailable": bool(_i32(physics, 340)) or bool(_u8(graphics, 53)),
            "drsActive": bool(_i32(physics, 344)) or bool(_u8(graphics, 30 + 1872)),
            "ersCharging": bool(_i32(physics, 332)) or bool(_u8(graphics, 51)) or bool(_u8(graphics, 54)),
            "ersHeat": bool(_i32(physics, 328)) or bool(_u8(graphics, 1900)),
            "ersOvertake": bool(_u8(graphics, 1901)),
            "ersCharge": _clamp(_f32(graphics, 1248), 0.0, 1.0),
            "ersDeployCapped": bool(_u8(graphics, 55)),
            "ersChargeCapped": bool(_u8(graphics, 56)),
            "wrongWay": bool(_u8(graphics, 52)),
            # AC EVO reports zero both for an invalid lap and for "no lap
            # data" in garages/showrooms.  Only expose a short alert after a
            # valid-to-invalid edge while the car is actually moving.
            "lapInvalid": lap_invalid,
            "lapValidRaw": raw_lap_valid,
            "lastLap": bool(_u8(graphics, 2421)),
            "flag": _i32(graphics, 2404),
            "globalFlag": _i32(graphics, 2408),
            "instrumentation": instrumentation,
            "mainLightStage": instrumentation["mainLightStage"],
            "specialLightStage": instrumentation["specialLightStage"],
            "cockpitLightStage": instrumentation["cockpitLightStage"],
            "wiperStage": instrumentation["wiperStage"],
            "rainLights": instrumentation["rainLights"],
            "indicatorLeft": instrumentation["indicatorLeft"],
            "indicatorRight": instrumentation["indicatorRight"],
            "hazardLights": instrumentation["indicatorLeft"] and instrumentation["indicatorRight"],
            "flashingLights": instrumentation["flashingLights"],
            "warningLights": instrumentation["warningLights"],
            "headlights": instrumentation["headlights"],
            "waterTemp": float(_i8(graphics, 116, round(_f32(physics, 712)))),
            "oilTemp": _f32(graphics, 120),
            "oilPressure": _f32(graphics, 124),
            "fuelLiters": max(0.0, _f32(graphics, 196, _f32(physics, 12))),
            "batteryTemp": _f32(graphics, 1468),
            "batteryVoltage": _f32(graphics, 1472),
            "damage": damage,
            "tyresOut": max(0, _i32(physics, 244)),
            "brakeTempMax": max_brake_temp,
            "tireTempMax": max_tire_temp,
            "wheels": wheels,
        }


__all__ = ["AcevoReader"]
