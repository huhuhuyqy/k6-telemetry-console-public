# Telemetry support and signal availability

## Lighting priority

The K6 hardware path currently controls the complete light strip as one RGB effect. It does not claim real-time per-LED control.

Urgent driving feedback always wins:

`ABS > brake > shift`

Available vehicle instrumentation is then displayed before the ordinary RPM background: warning lights, hazards, indicators, flashing/high beam, wiper, rain lights, headlights, special lights and cockpit lights. The normal racing sequence remains `RPM > TC > wrong-way > DRS` when no higher-priority vehicle instrumentation is active.

## Source matrix

| Game | Transport | Vehicle-light signals read from documented/observed data | Notes |
| --- | --- | --- | --- |
| AMS2 | Project CARS 2 shared memory | Headlight and warning bits where the game exposes them | Enable `Options > System > Shared Memory > Project CARS 2`. TC is not guessed from an undocumented car-flag bit. |
| AC EVO 0.8 | `Local\\acevo_pmf_*` shared memory | Indicators, hazards, wiper, rain lights, headlights, flashing/high beam, special and cockpit lights | The bridge distinguishes driving, replay and showroom. Instrumentation remains available when the game publishes it. |
| Assetto Corsa | `Local\\acpmf_*` shared memory | None currently confirmed | RPM, brake, ABS and gear effects remain available. |
| ACC | Kunos shared memory | Indicators, hazards, wiper, rain lights, headlights and flashing lights | Enter a driving session so the shared-memory pages are created. |
| LMU | `LMU_Data` shared memory | Wiper and headlights | No documented indicator/hazard fields are mapped. |
| FH5 / FH6 | UDP Data Out at `127.0.0.1:5300` | None currently confirmed | Configure UDP Data Out in the game. RPM, brake, ABS and gear effects remain available. |

Unknown fields are intentionally treated as unavailable rather than inferred. This prevents false light effects when a game changes an undocumented memory layout.

## Run options

### Electron (recommended)

The Electron version provides deterministic K6 lighting restoration when its window closes:

```powershell
pnpm install --frozen-lockfile
pnpm run build:bridge
pnpm run dev
```

Build the one-file Windows portable application with:

```powershell
pnpm run dist:portable
```

### Browser edition

The complete browser edition lives in `browser/` and requires Chrome or Edge for WebHID:

```powershell
Set-Location .\browser
powershell -ExecutionPolicy Bypass -File .\start-demo.ps1
```

Then open `http://localhost:8765/`, choose a source and select **Connect K6**. Browser shutdown can only make a best-effort asynchronous restoration attempt; use Electron when deterministic close restoration is required.
