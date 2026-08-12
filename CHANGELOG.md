# Changelog

## 0.2.8 - 2026-08-12

- Fixed a display-priority defect which hid vehicle instrumentation whenever the engine RPM was non-zero.
- AC EVO now shows available left/right indicators, hazards, wiper, rain, headlight, special-light and cockpit-light signals while driving.
- ACC, LMU and AMS2 now also show each vehicle-light signal their documented telemetry actually exposes.
- Kept urgent driving feedback ahead of vehicle instrumentation: `ABS > brake > shift`.
- Included the browser edition in this repository under `browser/`, alongside the Electron edition.
- Added tested K6 HID response/snapshot compatibility, exact bridge identity checks, clean-close restoration and build documentation.

## Supported telemetry sources

- AMS2 (Project CARS 2 shared memory)
- Assetto Corsa EVO 0.8 shared memory
- Assetto Corsa shared memory
- Assetto Corsa Competizione shared memory
- Le Mans Ultimate shared memory
- Forza Horizon 5 / 6 UDP Data Out

See [docs/TELEMETRY_SUPPORT.md](docs/TELEMETRY_SUPPORT.md) for signal availability and setup notes.
