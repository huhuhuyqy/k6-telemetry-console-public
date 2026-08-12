import {
  COMMAND,
  LED_RAM_ID,
  RAM_READ_CHUNK_SIZE,
  MAX_LIGHTING_RAM_SIZE,
  SerialTaskQueue,
  buildFrame,
  clamp,
  crc32,
  encodeLightingBin,
  lerp,
  parseReadRamChunk,
  parseResponse,
  selectDisplayCue,
  selectCoreCue,
  smoothstep,
  u16,
  u32,
  validateLightingRam,
  validateRestorableLightingSnapshot,
  wrapLightingBin,
} from './runtime-core.mjs';

const FLYDIGI_VENDOR_ID = 0x37d7;
const K6_FILTERS = [
  { vendorId: FLYDIGI_VENDOR_ID, productId: 0x2502, usagePage: 0xffa0 },
];

// Flydigi's current web tool uses RAM 0x04 for the LGHT blob.
// RAM 0x05 is the XMUL mapping/config domain.
const REPORT_FALLBACK_SIZE = 63;
// Flash timings are intentionally separated by cue type.  Indicators/hazards
// need a slower automotive-style cadence, while brake feedback should become
// visibly faster as pedal pressure rises.  The previous single 75/120 ms
// clock made those two cues feel identical and too fast for indicators.
const FLASH_HALF_PERIOD_MS = 120;
const INDICATOR_HALF_PERIOD_MS = 300;
const BRAKE_SLOW_HALF_PERIOD_MS = 300;
const BRAKE_FAST_HALF_PERIOD_MS = 80;
const FLASH_CLOCK_ORIGIN = typeof performance === 'undefined' ? Date.now() : performance.now();
const UI_TICK_MS = 20;

const flashClock = {
  lastNow: FLASH_CLOCK_ORIGIN,
  standard: 0,
  indicator: 0,
  brake: 0,
};

function clockNow() { return typeof performance === 'undefined' ? Date.now() : performance.now(); }
function brakeHalfPeriod() {
  const pressure = clamp(Number(state?.brake) || 0, 0, 100) / 100;
  const eased = smoothstep(pressure);
  return lerp(BRAKE_SLOW_HALF_PERIOD_MS, BRAKE_FAST_HALF_PERIOD_MS, eased);
}
function flashPhaseOn(clock = 'standard') {
  const now = clockNow();
  const elapsed = clamp(now - flashClock.lastNow, 0, 100);
  if (elapsed > 0) {
    flashClock.standard = (flashClock.standard + elapsed / FLASH_HALF_PERIOD_MS) % 2;
    flashClock.indicator = (flashClock.indicator + elapsed / INDICATOR_HALF_PERIOD_MS) % 2;
    flashClock.brake = (flashClock.brake + elapsed / brakeHalfPeriod()) % 2;
    flashClock.lastNow = now;
  }
  return flashClock[clock] < 1;
}

function isK6ControlInterface(device) {
  const productMatches = K6_FILTERS.some((filter) =>
    device.vendorId === filter.vendorId && device.productId === filter.productId,
  );
  return productMatches && (device.collections || []).some((collection) =>
    collection.usagePage === 0xffa0 && (collection.outputReports || []).length > 0,
  );
}

const $ = (selector) => document.querySelector(selector);
const ui = {
  connectButton: $('#connect-button'), connectionDot: $('#connection-dot'), connectionLabel: $('#connection-label'),
  shiftLights: $('#shift-lights'), rpmValue: $('#rpm-value'), rpmSlider: $('#rpm-slider'), rpmPercent: $('#rpm-percent'),
  gearValue: $('#gear-value'), gearControlValue: $('#gear-control-value'), gearUp: $('#gear-up'), gearDown: $('#gear-down'),
  brakeValue: $('#brake-value'), brakeSlider: $('#brake-slider'), brakePercent: $('#brake-percent'), brakeButton: $('#brake-button'),
  dataSource: $('#data-source'), dataSourceState: $('#data-source-state'),
  maxRpm: $('#max-rpm'), shiftRpm: $('#shift-rpm'), autoSweep: $('#auto-sweep'), hardwareOutput: $('#hardware-output'),
  vibrationLink: $('#vibration-link'),
  manualEffectsCard: $('#manual-effects-card'), manualEffectsReset: $('#manual-effects-reset'),
  shiftMessage: $('#shift-message'), lightState: $('#light-state'), packetState: $('#packet-state'), telemetryEvents: $('#telemetry-events'), log: $('#log-output'),
};
const manualEffectControls = Array.from(document.querySelectorAll('[data-manual-field]'));

const state = {
  rpm: 3200, maxRpm: 8000, shiftRpm: 7200, gear: 3, brake: 0, sweepDirection: 1,
  telemetryLive: false, telemetryMaxRpm: 0, telemetry: null, manualTelemetry: null,
};
const lamps = Array.from({ length: 12 }, (_, index) => {
  const lamp = document.createElement('span');
  lamp.className = 'shift-light';
  lamp.style.setProperty('--lamp', index < 6 ? '#20e58b' : index < 9 ? '#ffd13b' : '#ff2a4d');
  ui.shiftLights.append(lamp);
  return lamp;
});

function log(message) {
  const stamp = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  ui.log.textContent = `[${stamp}] ${message}\n${ui.log.textContent}`.slice(0, 9000);
}

const LIVE_SOURCE_NAMES = {
  ams2: 'AMS2', acevo: 'AC EVO', ac: 'Assetto Corsa', acc: 'ACC',
  lmu: 'Le Mans Ultimate', fh5: 'Forza Horizon 5', fh6: 'Forza Horizon 6',
};
function isLiveSource() { return Boolean(LIVE_SOURCE_NAMES[ui.dataSource?.value]); }
function sourceName() { return LIVE_SOURCE_NAMES[ui.dataSource?.value] || '手动模拟器'; }
function formatGear(gear) { return gear < 0 ? 'R' : gear === 0 ? 'N' : String(gear); }

class K6Hid {
  constructor() {
    this.device = null;
    this.reportId = 0;
    this.reportSize = REPORT_FALLBACK_SIZE;
    this.queue = Promise.resolve();
    this.lightingTransactions = new SerialTaskQueue();
    this.reportSummary = [];
    this.initialLightingRam = null;
    this.runtimeWritesEnabled = false;
    this.restorePromise = null;
    this.disconnectListenerInstalled = false;
  }

  get connected() { return Boolean(this.device?.opened); }

  async connect(requestIfMissing = true) {
    if (!navigator.hid) throw new Error('当前浏览器不支持 WebHID，请使用最新版 Chrome 或 Edge。');
    // The same VID/PID exposes multiple HID interfaces. Only 0xFFA0 is the
    // configuration channel; 0xFFEE/report 5 accepts BEGIN but rejects chunks.
    const existing = (await navigator.hid.getDevices()).find(isK6ControlInterface);
    if (!existing && !requestIfMissing) return null;
    const device = existing || (await navigator.hid.requestDevice({ filters: K6_FILTERS }))[0];
    if (!device) throw new Error('没有选择 K6 设备。');
    if (!isK6ControlInterface(device)) {
      throw new Error('选中的不是 K6 配置接口。需要 usagePage 0xFFA0；请移除旧授权后重新选择。');
    }
    if (!device.opened) await device.open();
    this.device = device;
    this.findOutputReport();
    await new Promise((resolve) => setTimeout(resolve, 180));
    if (!this.disconnectListenerInstalled) {
      navigator.hid.addEventListener('disconnect', (event) => {
        if (event.device !== this.device) return;
        this.device = null;
        this.initialLightingRam = null;
        this.runtimeWritesEnabled = false;
        setConnectedUi(false);
      });
      this.disconnectListenerInstalled = true;
    }
    return device;
  }

  findOutputReport() {
    this.reportSummary = (this.device.collections || []).map((collection) => ({
      usagePage: collection.usagePage,
      usage: collection.usage,
      output: (collection.outputReports || []).map((report) => ({
        id: report.reportId || 0,
        items: (report.items || []).map((item) => ({ count: item.reportCount, bits: item.reportSize })),
      })),
      feature: (collection.featureReports || []).map((report) => ({
        id: report.reportId || 0,
        items: (report.items || []).map((item) => ({ count: item.reportCount, bits: item.reportSize })),
      })),
    }));
    for (const collection of this.device.collections || []) {
      if (collection.usagePage !== 0xffa0) continue;
      const report = collection.outputReports?.[0];
      if (!report) continue;
      this.reportId = report.reportId || 0;
      this.reportSize = report.items?.[0]?.reportCount || REPORT_FALLBACK_SIZE;
      return;
    }
    throw new Error('K6 上没有找到 usagePage 0xFFA0 的输出报告。');
  }

  request(command, payload, timeoutMs = 1600) {
    const operation = () => this.requestNow(command, payload, timeoutMs);
    const pending = this.queue.then(operation, operation);
    this.queue = pending.catch(() => {});
    return pending;
  }

  async requestNow(command, payload, timeoutMs) {
    if (!this.connected) throw new Error('K6 未连接。');
    const frame = buildFrame(command, payload);
    if (frame.length > 32) throw new Error(`协议帧过长：${frame.length} 字节。`);
    const report = new Uint8Array(this.reportSize);
    report.set(frame);
    try {
      return await this.sendAndWait(command, report, timeoutMs);
    } catch (error) {
      if (!String(error.message).startsWith('HID_WRITE:')) throw error;
      log(`HID 写入句柄异常，正在重新打开设备：${error.message.slice(10)}`);
      await this.reopen();
      const retryReport = new Uint8Array(this.reportSize);
      retryReport.set(frame);
      try {
        return await this.sendAndWait(command, retryReport, timeoutMs);
      } catch (retryError) {
        if (String(retryError.message).startsWith('HID_WRITE:')) {
          throw new Error(`浏览器无法写入 HID report ${this.reportId}:${this.reportSize}B。请完全退出飞智空间站/相关后台程序并重新插拔接收器。底层错误：${retryError.message.slice(10)}`);
        }
        throw retryError;
      }
    }
  }

  sendAndWait(command, report, timeoutMs) {
    return new Promise((resolve, reject) => {
      let lastUnparsed = '';
      let settled = false;
      const timer = setTimeout(() => {
        const detail = lastUnparsed ? `；收到未识别回包 ${lastUnparsed}` : '';
        finish(new Error(`命令 0x${command.toString(16)} 响应超时${detail}`));
      }, timeoutMs);
      const onReport = (event) => {
        const bytes = new Uint8Array(event.data.buffer);
        const response = parseResponse(bytes, command);
        if (response) finish(null, response);
        else lastUnparsed = [...bytes.slice(0, 32)].map((byte) => byte.toString(16).padStart(2, '0')).join(' ');
      };
      const finish = (error, response) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.device?.removeEventListener('inputreport', onReport);
        error ? reject(error) : resolve(response);
      };
      this.device.addEventListener('inputreport', onReport);
      this.device.sendReport(this.reportId, report).catch((error) => finish(new Error(`HID_WRITE:${error.message}`)));
    });
  }

  async reopen() {
    if (!this.device) throw new Error('K6 设备句柄已丢失。');
    if (this.device.opened) await this.device.close();
    await new Promise((resolve) => setTimeout(resolve, 220));
    await this.device.open();
    this.findOutputReport();
    await new Promise((resolve) => setTimeout(resolve, 220));
  }

  async readLightingRam() {
    let expectedLength = 0;
    let chunkIndex = 0;
    let activeSlot = 0;
    let result = null;
    while (expectedLength === 0 || chunkIndex * RAM_READ_CHUNK_SIZE < expectedLength) {
      const payload = Uint8Array.from([0x00, LED_RAM_ID, chunkIndex, RAM_READ_CHUNK_SIZE]);
      const response = await this.request(COMMAND.READ_RAM, payload);
      const chunk = parseReadRamChunk(response, chunkIndex);
      if (expectedLength === 0) {
        expectedLength = chunk.totalLength;
        activeSlot = chunk.activeSlot;
        result = new Uint8Array(expectedLength);
      } else if (chunk.totalLength !== expectedLength) {
        throw new Error(`灯光 RAM 长度在读取中变化：${chunk.totalLength}/${expectedLength}`);
      }
      const offset = chunkIndex * RAM_READ_CHUNK_SIZE;
      const expectedChunkLength = Math.min(RAM_READ_CHUNK_SIZE, expectedLength - offset);
      if (chunk.data.length !== expectedChunkLength) {
        throw new Error(`灯光读取分块 ${chunkIndex} 长度不一致：${chunk.data.length}/${expectedChunkLength}`);
      }
      result.set(chunk.data, offset);
      chunkIndex += 1;
      if (chunkIndex > Math.ceil(MAX_LIGHTING_RAM_SIZE / RAM_READ_CHUNK_SIZE)) {
        throw new Error('灯光 RAM 读取分块过多，已停止。');
      }
    }
    return { activeSlot, data: result };
  }

  async captureInitialLighting() {
    const snapshot = await this.readLightingRam();
    snapshot.validation = validateRestorableLightingSnapshot(snapshot.data);
    this.initialLightingRam = new Uint8Array(snapshot.data);
    this.runtimeWritesEnabled = true;
    return snapshot;
  }

  async restoreInitialLighting() {
    if (this.restorePromise) return this.restorePromise;
    if (!this.connected || !this.initialLightingRam) return false;
    this.runtimeWritesEnabled = false;
    const snapshot = new Uint8Array(this.initialLightingRam);
    this.restorePromise = this.lightingTransactions.run(async () => {
      await this.abortWrite().catch(() => {});
      await this.writeRawLightingRamNow(snapshot);
      return true;
    }).finally(() => { this.restorePromise = null; });
    return this.restorePromise;
  }

  async close({ restore = true } = {}) {
    if (restore) await this.restoreInitialLighting();
    this.runtimeWritesEnabled = false;
    this.initialLightingRam = null;
    const device = this.device;
    this.device = null;
    if (device?.opened) await device.close();
  }

  async writeSolidColor(color, vibration = false) {
    return this.writeLightingBin(encodeLightingBin(color, vibration));
  }

  async writeLightingBin(bin) {
    const wrapped = wrapLightingBin(bin);
    return this.writeRawLightingRam(wrapped);
  }

  async writeRawLightingRam(wrapped) {
    validateLightingRam(wrapped);
    return this.lightingTransactions.run(() => this.writeRawLightingRamNow(wrapped));
  }

  async writeRawLightingRamNow(wrapped) {
    try {
      await this.writeWrappedLighting(wrapped, 16);
    } catch (firstError) {
      // A failed/old A4 transaction can leave the firmware in a busy state.
      // This is especially relevant after changing the target RAM domain.
      await this.abortWrite().catch(() => {});
      await new Promise((resolve) => setTimeout(resolve, 120));
      try {
        // Some receiver/firmware revisions reject 16-byte A4 chunks with BUSY.
        await this.writeWrappedLighting(wrapped, 8);
      } catch (retryError) {
        await this.abortWrite().catch(() => {});
        throw new Error(`${retryError.message}（已清理事务并用 8B 分块重试；首次错误：${firstError.message}）`);
      }
    }
  }

  async abortWrite() {
    await this.request(COMMAND.WRITE_RAM, Uint8Array.from([0x03]));
  }

  async requestA4Status(payload, stage, attempts = 4) {
    let status = 255;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const ack = await this.request(COMMAND.WRITE_RAM, payload);
      status = ack[1] ?? 255;
      if (status === 0) return ack;
      if (status !== 1 || attempt === attempts - 1) break;
      await new Promise((resolve) => setTimeout(resolve, 50 * (attempt + 1)));
    }
    throw new Error(`${stage}，状态 ${status}`);
  }

  async writeWrappedLighting(wrapped, chunkSize = 16) {
    const checksum = crc32(wrapped);
    await this.requestA4Status(
      Uint8Array.from([0x00, LED_RAM_ID, ...u16(wrapped.length), ...u32(checksum)]),
      '灯光写入开始失败',
    );
    // Every stage already waits for an ACK. A short settle is sufficient and
    // makes telemetry-driven changes much more responsive than the old 70/25 ms gaps.
    await new Promise((resolve) => setTimeout(resolve, 15));
    for (let offset = 0; offset < wrapped.length; offset += chunkSize) {
      const chunk = wrapped.slice(offset, offset + chunkSize);
      await this.requestA4Status(
        Uint8Array.from([0x01, ...u16(offset), chunk.length, ...chunk]),
        `灯光分块写入失败，offset ${offset}，长度 ${chunk.length}`,
      );
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
    const commit = await this.requestA4Status(
      Uint8Array.from([0x02, ...u16(wrapped.length), ...u32(checksum)]),
      '灯光提交失败',
    );
    const finalLength = (commit[4] ?? 0) | ((commit[5] ?? 0) << 8);
    const finalChecksum = (
      (commit[6] ?? 0)
      | ((commit[7] ?? 0) << 8)
      | ((commit[8] ?? 0) << 16)
      | ((commit[9] ?? 0) << 24)
    ) >>> 0;
    if (finalLength && finalLength !== wrapped.length) throw new Error(`灯光提交长度不一致：${finalLength}/${wrapped.length}`);
    if (finalChecksum && finalChecksum !== checksum) {
      throw new Error(`灯光提交 CRC32 不一致：0x${finalChecksum.toString(16)}/0x${checksum.toString(16)}`);
    }
  }
}

const k6 = new K6Hid();
let desiredHardwareColor = null;
let lastHardwareSignature = '';
let hardwarePumpRunning = false;
let hardwareErrorUntil = 0;
let shutdownRequested = false;
let restoreBeforeClosePromise = null;
async function queueHardwareColor(color, label) {
  if (shutdownRequested || !k6.connected || !k6.runtimeWritesEnabled || !ui.hardwareOutput.checked || Date.now() < hardwareErrorUntil) return;
  const vibration = Boolean(ui.vibrationLink?.checked);
  desiredHardwareColor = {
    ...color,
    vibration,
    label,
    signature: `${color.r},${color.g},${color.b},${color.brightness},v${Number(vibration)}`,
  };
  if (hardwarePumpRunning) return;
  hardwarePumpRunning = true;
  try {
    while (desiredHardwareColor && desiredHardwareColor.signature !== lastHardwareSignature) {
      const target = desiredHardwareColor;
      desiredHardwareColor = null;
      ui.packetState.textContent = `写入 ${target.label}…`;
      await k6.writeSolidColor(target, target.vibration);
      lastHardwareSignature = target.signature;
      ui.packetState.textContent = `已发送 ${target.label}`;
      log(`K6 灯带 ← ${target.label} (${target.r}, ${target.g}, ${target.b})${target.vibration ? ' · 震动同步' : ''}`);
    }
  } catch (error) {
    desiredHardwareColor = null;
    hardwareErrorUntil = Date.now() + 1500;
    ui.packetState.textContent = `写入失败：${error.message}`;
    ui.packetState.title = error.message;
    log(`写入失败：${error.message}`);
  } finally {
    hardwarePumpRunning = false;
    if (desiredHardwareColor && desiredHardwareColor.signature !== lastHardwareSignature) queueHardwareColor(desiredHardwareColor, desiredHardwareColor.label);
  }
}

let telemetryRequestRunning = false;
let telemetryGeneration = 0;
let telemetryController = null;
let lastTelemetryStatus = '';

function setSimulatorControlsEnabled(enabled) {
  [ui.rpmSlider, ui.brakeSlider, ui.brakeButton, ui.gearUp, ui.gearDown, ui.autoSweep].forEach((control) => {
    control.disabled = !enabled;
    control.classList.toggle('simulator-disabled', !enabled);
  });
  manualEffectControls.forEach((control) => { control.disabled = !enabled; });
  ui.manualEffectsReset.disabled = !enabled;
  ui.manualEffectsCard.classList.toggle('disabled', !enabled);
  ui.maxRpm.disabled = !enabled;
}

function syncManualTelemetry({ renderNow = true } = {}) {
  const fields = Object.fromEntries(manualEffectControls.map((control) => [
    control.dataset.manualField,
    control.type === 'checkbox' ? control.checked : Number(control.value),
  ]));
  state.manualTelemetry = {
    source: 'manual', connected: true, status: 'live',
    absActive: Boolean(fields.absActive),
    tcActive: Boolean(fields.tcActive),
    shiftUpHint: Boolean(fields.shiftUpHint),
    shiftDownHint: Boolean(fields.shiftDownHint),
    wrongWay: Boolean(fields.wrongWay),
    drsAvailable: Boolean(fields.drsAvailable || fields.drsActive),
    drsActive: Boolean(fields.drsActive),
    pitLimiter: Boolean(fields.pitLimiter),
    indicatorLeft: Boolean(fields.indicatorLeft),
    indicatorRight: Boolean(fields.indicatorRight),
    hazardLights: Boolean(fields.hazardLights || (fields.indicatorLeft && fields.indicatorRight)),
    headlights: Boolean(fields.headlights),
    mainLightStage: fields.headlights ? 1 : 0,
    flashingLights: Boolean(fields.flashingLights),
    rainLights: Boolean(fields.rainLights),
    warningLights: Boolean(fields.warningLights),
    specialLightStage: fields.specialLightStage ? 1 : 0,
    cockpitLightStage: fields.cockpitLightStage ? 1 : 0,
    wiperStage: clamp(Number(fields.wiperStage) || 0, 0, 3),
    flag: fields.flagActive ? 1 : 0,
    globalFlag: 0,
    damage: fields.damageSevere ? [0.75] : [],
    lapInvalid: Boolean(fields.lapInvalid),
    tyresOut: fields.tyresOutActive ? 4 : 0,
    brakeTempMax: fields.brakeOverheat ? 900 : 0,
    tireTempMax: fields.tireOverheat ? 110 : 0,
    ersCharging: Boolean(fields.ersCharging),
    ersOvertake: Boolean(fields.ersOvertake),
    ersHeat: Boolean(fields.ersHeat),
    ersDeployCapped: Boolean(fields.ersDeployCapped),
    ersChargeCapped: Boolean(fields.ersChargeCapped),
  };
  if (renderNow && !isLiveSource()) render();
}

function resetManualTelemetry() {
  manualEffectControls.forEach((control) => {
    if (control.type === 'checkbox') control.checked = false;
    else control.value = control.querySelector?.('option[selected]')?.value || control.options?.[0]?.value || '0';
  });
  syncManualTelemetry();
}

function setTelemetryStatus(text, status) {
  ui.dataSourceState.textContent = text;
  ui.dataSourceState.dataset.status = status;
  if (status !== lastTelemetryStatus) {
    log(`${sourceName()}：${text}`);
    lastTelemetryStatus = status;
  }
}

function applyTelemetry(data) {
  state.telemetryLive = true;
  state.telemetry = data;
  if (data.maxRpm >= 1000 && Math.abs(data.maxRpm - state.telemetryMaxRpm) >= 1) {
    const oldMax = state.telemetryMaxRpm || Number(ui.maxRpm.value) || data.maxRpm;
    const shiftRatio = clamp((Number(ui.shiftRpm.value) || oldMax * .9) / oldMax, .6, .99);
    state.telemetryMaxRpm = data.maxRpm;
    ui.maxRpm.value = String(Math.round(data.maxRpm));
    ui.shiftRpm.value = String(Math.round(data.maxRpm * shiftRatio / 50) * 50);
  }
  state.rpm = Number(data.rpm) || 0;
  state.gear = Number.isFinite(data.gear) ? data.gear : 0;
  state.brake = clamp((Number(data.brake) || 0) * 100, 0, 100);
  const car = data.car ? ` · ${data.car}` : '';
  const version = data.version ? ` · v${data.version}` : '';
  setTelemetryStatus(`${data.message || `${sourceName()} 实时`}${version}${car}`, data.status || 'live');
  render();
}

async function pollTelemetry() {
  if (!isLiveSource() || telemetryRequestRunning) return;
  telemetryRequestRunning = true;
  const source = ui.dataSource.value;
  const generation = telemetryGeneration;
  const controller = new AbortController();
  telemetryController = controller;
  const timeout = setTimeout(() => controller.abort('timeout'), 1200);
  try {
    const response = await fetch(`/api/telemetry?source=${source}`, {
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (generation !== telemetryGeneration || source !== ui.dataSource.value) return;
    if (data.connected && ['live', 'showroom', 'replay'].includes(data.status)) {
      applyTelemetry(data);
    } else {
      state.telemetryLive = false;
      state.rpm = 0;
      state.gear = 0;
      state.brake = 0;
      state.telemetry = null;
      setTelemetryStatus(data.message || `正在等待 ${sourceName()}`, data.status || 'waiting');
      render();
    }
  } catch (error) {
    if (generation !== telemetryGeneration || source !== ui.dataSource.value) return;
    state.telemetryLive = false;
    state.rpm = 0;
    state.gear = 0;
    state.brake = 0;
    state.telemetry = null;
    const timedOut = controller.signal.aborted && controller.signal.reason === 'timeout';
    setTelemetryStatus(
      timedOut ? `${sourceName()} 遥测请求超时` : `遥测桥接服务不可用（${sourceName()}）`,
      'bridge-error',
    );
    render();
  } finally {
    clearTimeout(timeout);
    if (telemetryController === controller) telemetryController = null;
    telemetryRequestRunning = false;
  }
}

function colorForState(normalized, brake, shifting) {
  const brightPhase = flashPhaseOn(brake > 2 ? 'brake' : 'standard');
  if (brake > 2) return brightPhase
    ? { r: 255, g: 0, b: 0, brightness: 100, label: '刹车纯红' }
    : { r: 0, g: 0, b: 0, brightness: 100, label: '刹车闪灭' };
  if (shifting) return brightPhase
    ? { r: 255, g: 0, b: 0, brightness: 100, label: '换挡纯红' }
    : { r: 255, g: 255, b: 255, brightness: 100, label: '换挡闪白' };

  // Continuous RPM palette. Interpolating between nearby anchors avoids the
  // abrupt blue/green/yellow/orange jumps from the previous threshold mapping.
  const stops = [
    { at: 0, r: 15, g: 90, b: 255, brightness: 60 },
    { at: .28, r: 15, g: 225, b: 112, brightness: 76 },
    { at: .58, r: 255, g: 205, b: 16, brightness: 84 },
    { at: .82, r: 255, g: 92, b: 8, brightness: 94 },
    { at: 1, r: 255, g: 0, b: 0, brightness: 100 },
  ];
  let upperIndex = stops.findIndex((stop) => normalized <= stop.at);
  if (upperIndex <= 0) upperIndex = 1;
  const lower = stops[upperIndex - 1];
  const upper = stops[upperIndex] || stops.at(-1);
  const amount = smoothstep(clamp((normalized - lower.at) / (upper.at - lower.at), 0, 1));
  // Quantize slightly so tiny telemetry noise does not start a full HID RAM
  // transaction for a visually indistinguishable one-level RGB change.
  const q = (value) => Math.round(value / 3) * 3;
  return {
    r: clamp(q(lerp(lower.r, upper.r, amount)), 0, 255),
    g: clamp(q(lerp(lower.g, upper.g, amount)), 0, 255),
    b: clamp(q(lerp(lower.b, upper.b, amount)), 0, 255),
    brightness: clamp(Math.round(lerp(lower.brightness, upper.brightness, amount)), 0, 100),
    label: '转速渐变',
  };
}

function flashColor(onColor, offLabel, label, clock = 'standard') {
  const phaseOn = flashPhaseOn(clock);
  return phaseOn
    ? { ...onColor, label, alert: true }
    : { r: 0, g: 0, b: 0, brightness: 100, label: offLabel, alert: true };
}

function telemetryLabels(data) {
  if (!data) return [];
  const labels = [];
  if (data.absActive) labels.push('ABS');
  if (state.brake > 2) labels.push('刹车');
  if (data.shiftUpHint || state.rpm >= state.shiftRpm) labels.push('换挡');
  if (data.shiftDownHint) labels.push('降挡');
  if (data.tcActive) labels.push('TC');
  if (data.wrongWay) labels.push('逆向');
  if (data.drsActive) labels.push('DRS');
  else if (data.drsAvailable) labels.push('DRS可用');
  if (data.hazardLights) labels.push('双闪');
  else {
    if (data.indicatorLeft) labels.push('左转向');
    if (data.indicatorRight) labels.push('右转向');
  }
  if (data.wiperStage > 0) labels.push(`雨刷 ${data.wiperStage}`);
  if (data.headlights || data.mainLightStage > 0) labels.push('大灯');
  if (data.rainLights) labels.push('雨灯');
  if (data.flashingLights) labels.push('远光闪灯');
  if (data.specialLightStage > 0) labels.push('特殊灯');
  if (data.cockpitLightStage > 0) labels.push('座舱灯');
  if (data.flag || data.globalFlag) labels.push('赛道旗帜');
  if (data.damage?.some((value) => Number(value) >= 0.35)) labels.push('损伤');
  if (Number(data.tyresOut) > 0) labels.push(`出界轮胎 ${data.tyresOut}`);
  if (data.ersHeat) labels.push('ERS过热');
  else if (data.ersOvertake) labels.push('ERS超车');
  else if (data.ersCharging) labels.push('ERS充电');
  if (data.ersDeployCapped) labels.push('ERS输出受限');
  if (data.ersChargeCapped) labels.push('ERS充电受限');
  if (data.lapInvalid) labels.push('圈速无效');
  if (data.brakeTempMax > 850) labels.push('刹车过热');
  if (data.tireTempMax > 105) labels.push('轮胎偏热');
  return labels;
}

function hasFlashingTelemetry(data) {
  if (!data) return false;
  return Boolean(
    data.absActive ||
    data.tcActive ||
    data.wrongWay ||
    data.drsActive ||
    data.shiftUpHint ||
    data.shiftDownHint ||
    data.flag || data.globalFlag ||
    data.warningLights ||
    data.hazardLights || data.indicatorLeft || data.indicatorRight ||
    data.flashingLights ||
    data.damage?.some((value) => Number(value) >= 0.55) ||
    data.lapInvalid || Number(data.tyresOut) > 0 ||
    data.ersHeat || data.ersDeployCapped || data.ersChargeCapped || data.ersOvertake ||
    data.brakeTempMax > 850
  );
}

function colorForTelemetry(normalized, data, shifting) {
  if (!data) return colorForState(normalized, state.brake, shifting);
  const brake = state.brake;
  // Urgent driving cues keep their requested priority. Vehicle instrumentation
  // is intentionally above only the ordinary RPM background so an active
  // indicator, wiper or headlight remains visible while driving.
  const cue = selectDisplayCue(normalized, data, brake, shifting);
  if (cue === 'abs') return flashColor({ r: 80, g: 170, b: 255, brightness: 100 }, 'ABS熄灭', 'ABS介入');
  if (cue === 'brake') return flashColor({ r: 255, g: 0, b: 0, brightness: 100 }, '刹车熄灭', '刹车', 'brake');
  if (cue === 'shift-up') return flashColor({ r: 255, g: 0, b: 0, brightness: 100 }, '换挡熄灭', '换挡提示');
  if (cue === 'shift-down') return flashColor({ r: 165, g: 75, b: 255, brightness: 88 }, '降挡熄灭', '降挡提示');
  if (cue === 'warning-lights') return flashColor({ r: 255, g: 50, b: 0, brightness: 92 }, '警告熄灭', '车辆警告灯');
  if (cue === 'hazards') return flashColor({ r: 255, g: 150, b: 0, brightness: 78 }, '双闪熄灭', '双闪', 'indicator');
  if (cue === 'indicator-left') return flashColor({ r: 255, g: 150, b: 0, brightness: 78 }, '左转向熄灭', '左转向', 'indicator');
  if (cue === 'indicator-right') return flashColor({ r: 255, g: 150, b: 0, brightness: 78 }, '右转向熄灭', '右转向', 'indicator');
  if (cue === 'flashing-lights') return flashColor({ r: 255, g: 255, b: 255, brightness: 100 }, '远光熄灭', '远光闪灯');
  if (cue === 'wiper') return { r: 40, g: 150, b: 255, brightness: 45, label: `雨刷 ${data.wiperStage}`, alert: false };
  if (cue === 'rain-lights') return { r: 60, g: 130, b: 255, brightness: 48, label: '雨灯', alert: false };
  if (cue === 'headlights') return { r: 190, g: 210, b: 255, brightness: 40, label: '大灯', alert: false };
  if (cue === 'special-lights') return { r: 45, g: 220, b: 210, brightness: 52, label: '特殊灯', alert: false };
  if (cue === 'cockpit-lights') return { r: 255, g: 185, b: 105, brightness: 36, label: '座舱灯', alert: false };
  if (cue === 'rpm') return colorForState(normalized, 0, false);
  if (cue === 'tc') return flashColor({ r: 40, g: 255, b: 110, brightness: 92 }, 'TC熄灭', 'TC介入');
  if (cue === 'wrong-way') return flashColor({ r: 255, g: 0, b: 0, brightness: 100 }, '逆向熄灭', '逆向警告');
  if (cue === 'drs-active') return flashColor({ r: 0, g: 220, b: 255, brightness: 90 }, 'DRS熄灭', 'DRS开启');
  if (cue === 'drs-available') return { r: 0, g: 165, b: 230, brightness: 70, label: 'DRS可用', alert: false };

  // Secondary vehicle-state cues are deliberately below the requested seven
  // core cues. They are still useful on road cars and in the showroom.
  if (data.damage?.some((value) => Number(value) >= 0.55)) {
    return flashColor({ r: 255, g: 48, b: 0, brightness: 100 }, '损伤熄灭', '严重损伤');
  }
  if (data.flag || data.globalFlag) return flashColor({ r: 255, g: 210, b: 0, brightness: 92 }, '旗帜熄灭', '赛道旗帜');
  if (data.lapInvalid) return flashColor({ r: 255, g: 105, b: 0, brightness: 92 }, '圈速熄灭', '圈速无效');
  if (Number(data.tyresOut) > 0) return flashColor({ r: 255, g: 120, b: 0, brightness: 92 }, '出界熄灭', `${data.tyresOut} 轮出界`);
  if (data.ersHeat || data.ersDeployCapped) {
    return flashColor({ r: 255, g: 56, b: 0, brightness: 100 }, 'ERS熄灭', 'ERS警告');
  }
  if (data.ersChargeCapped) return flashColor({ r: 255, g: 105, b: 0, brightness: 95 }, 'ERS熄灭', 'ERS充电受限');
  if (data.ersOvertake) return flashColor({ r: 170, g: 60, b: 255, brightness: 90 }, 'ERS熄灭', 'ERS超车');
  if (data.pitLimiter) return { r: 255, g: 190, b: 0, brightness: 80, label: '进站限速', alert: false };
  if (data.ersCharging) return { r: 90, g: 80, b: 255, brightness: 68, label: 'ERS充电', alert: false };
  if (data.brakeTempMax > 850) return flashColor({ r: 255, g: 72, b: 0, brightness: 95 }, '刹车温度熄灭', '刹车过热');
  if (data.tireTempMax > 105) return { r: 255, g: 72, b: 0, brightness: 80, label: '轮胎偏热', alert: false };
  return colorForState(normalized, brake, shifting);
}

function render() {
  state.maxRpm = clamp(Number(ui.maxRpm.value) || 8000, 1000, 30000);
  state.shiftRpm = clamp(Number(ui.shiftRpm.value) || Math.round(state.maxRpm * .9), 800, state.maxRpm);
  state.rpm = clamp(state.rpm, 0, state.maxRpm);
  const normalized = clamp(state.rpm / state.maxRpm, 0, 1);
  const lit = clamp(Math.ceil(normalized * lamps.length), 0, lamps.length);
  const telemetryUnavailable = isLiveSource() && !state.telemetryLive;
  const activeTelemetry = isLiveSource() ? state.telemetry : state.manualTelemetry;
  const shifting = !telemetryUnavailable && (state.rpm >= state.shiftRpm || Boolean(activeTelemetry?.shiftUpHint));
  const color = telemetryUnavailable
    ? { r: 0, g: 0, b: 0, brightness: 100, label: `等待 ${sourceName()}`, alert: true }
    : colorForTelemetry(normalized, activeTelemetry, shifting);
  const alertMode = !telemetryUnavailable && (Boolean(color.alert) || state.brake > 2 || shifting);
  const alertLit = color.r > 0 || color.g > 0 || color.b > 0;
  const displayLit = telemetryUnavailable ? 0 : alertMode ? (alertLit ? lamps.length : 0) : lit;
  lamps.forEach((lamp, index) => {
    const normalColor = index < 6 ? '#20e58b' : index < 9 ? '#ffd13b' : '#ff2a4d';
    const alertColor = `rgb(${color.r}, ${color.g}, ${color.b})`;
    lamp.style.setProperty('--lamp', alertMode ? alertColor : normalColor);
    lamp.classList.toggle('on', index < displayLit);
  });
  ui.shiftLights.classList.toggle('redline', shifting);
  ui.rpmValue.textContent = Math.round(state.rpm).toLocaleString('en-US');
  ui.rpmSlider.max = state.maxRpm;
  ui.rpmSlider.value = state.rpm;
  ui.rpmPercent.textContent = `${Math.round(normalized * 100)}%`;
  ui.gearValue.textContent = formatGear(state.gear);
  ui.gearControlValue.textContent = formatGear(state.gear);
  ui.brakeValue.textContent = `${Math.round(state.brake)}%`;
  ui.brakePercent.textContent = `${Math.round(state.brake)}%`;
  ui.brakeSlider.value = state.brake;
  ui.brakeButton.classList.toggle('active', state.brake > 2);
  ui.brakeButton.textContent = state.brake > 2 ? '正在刹车' : '按住刹车（空格）';
  ui.brakeValue.parentElement.classList.toggle('active', state.brake > 2);
  ui.shiftMessage.classList.toggle('active', shifting);
  ui.lightState.textContent = `${color.label} · ${displayLit}/12`;
  ui.telemetryEvents.textContent = telemetryUnavailable
    ? `等待 ${sourceName()}`
    : (telemetryLabels(activeTelemetry).join(' · ') || (isLiveSource() ? '巡航' : '手动模拟'));
  queueHardwareColor(color, color.label);
}

function setConnectedUi(connected, device) {
  ui.connectionDot.classList.toggle('connected', connected);
  ui.connectionLabel.textContent = connected ? `${device?.productName || 'K6'} 已连接` : '未连接 K6';
  ui.connectButton.textContent = connected ? '已连接' : '连接 K6';
  ui.connectButton.disabled = connected;
  if (!connected) ui.packetState.textContent = '等待连接（请点顶部“连接 K6”）';
}

async function connectK6({ requestIfMissing = true, showAlert = true } = {}) {
  ui.connectButton.disabled = true;
  ui.connectButton.textContent = '正在连接…';
  try {
    const device = await k6.connect(requestIfMissing);
    if (!device) {
      setConnectedUi(false);
      return false;
    }
    // Clear a transaction that may have survived a failed previous write.
    await k6.abortWrite().catch((error) => log(`清理旧写入事务未确认：${error.message}`));
    shutdownRequested = false;
    restoreBeforeClosePromise = null;
    try {
      const snapshot = await k6.captureInitialLighting();
      log(`已备份连接时灯效：RAM 0x${LED_RAM_ID.toString(16)}，${snapshot.data.length}B，slot ${snapshot.activeSlot}，格式 ${snapshot.validation.format}`);
    } catch (snapshotError) {
      k6.runtimeWritesEnabled = false;
      ui.packetState.textContent = `保护模式：无法备份原灯效`;
      ui.packetState.title = snapshotError.message;
      log(`未启用灯光输出：无法备份连接时灯效：${snapshotError.message}`);
    }
    setConnectedUi(true, device);
    lastHardwareSignature = '';
    log(`已连接 ${device.productName}，VID/PID ${device.vendorId.toString(16)}/${device.productId.toString(16)}，report ${k6.reportId}:${k6.reportSize}B`);
    log(`HID 描述符：${JSON.stringify(k6.reportSummary)}`);
    render();
    return true;
  } catch (error) {
    setConnectedUi(false);
    log(`连接失败：${error.message}`);
    if (showAlert) alert(error.message);
    return false;
  }
}

async function restoreK6BeforeClose() {
  if (restoreBeforeClosePromise) return restoreBeforeClosePromise;
  shutdownRequested = true;
  desiredHardwareColor = null;
  restoreBeforeClosePromise = (async () => {
    if (!k6.connected) return false;
    try {
      ui.packetState.textContent = '正在恢复连接时灯效…';
      const restored = await k6.restoreInitialLighting();
      if (restored) {
        ui.packetState.textContent = '已恢复连接时灯效';
        log('关闭前已恢复连接时的 K6 灯效。');
      }
      return restored;
    } catch (error) {
      log(`关闭前恢复灯效失败：${error.message}`);
      return false;
    } finally {
      await k6.close({ restore: false }).catch(() => {});
      setConnectedUi(false);
    }
  })();
  return restoreBeforeClosePromise;
}

// Electron waits for this promise before destroying the renderer. Browsers do
// not guarantee enough time for asynchronous WebHID work during tab shutdown,
// so pagehide is best-effort; closing the Electron window is deterministic.
window.__k6RestoreBeforeClose = restoreK6BeforeClose;
window.addEventListener('pagehide', () => { void restoreK6BeforeClose(); });

async function autoConnectAuthorizedK6() {
  try {
    const authorized = (await navigator.hid.getDevices()).some(isK6ControlInterface);
    if (!authorized) {
      log('K6 尚未授权，请点击页面顶部的“连接 K6”。');
      return;
    }
    log('检测到已授权的 K6，正在自动重连…');
    await connectK6({ requestIfMissing: false, showAlert: false });
  } catch (error) {
    log(`K6 自动重连失败：${error.message}，请手动点击“连接 K6”。`);
  }
}

ui.connectButton.addEventListener('click', async () => {
  await connectK6();
});

ui.rpmSlider.addEventListener('input', (event) => { state.rpm = Number(event.target.value); render(); });
ui.brakeSlider.addEventListener('input', (event) => { state.brake = Number(event.target.value); render(); });
ui.maxRpm.addEventListener('change', render);
ui.shiftRpm.addEventListener('change', render);
ui.hardwareOutput.addEventListener('change', () => { lastHardwareSignature = ''; render(); });
ui.vibrationLink.addEventListener('change', () => {
  lastHardwareSignature = '';
  log(`震动同步灯效：${ui.vibrationLink.checked ? '已启用；由固件根据左右马达决定灯区' : '已关闭'}`);
  render();
});
manualEffectControls.forEach((control) => {
  control.addEventListener('change', () => syncManualTelemetry());
});
ui.manualEffectsReset.addEventListener('click', resetManualTelemetry);
ui.dataSource.addEventListener('change', () => {
  telemetryGeneration += 1;
  telemetryController?.abort('source-change');
  const simulator = !isLiveSource();
  setSimulatorControlsEnabled(simulator);
  state.telemetryLive = false;
  state.telemetry = null;
  state.telemetryMaxRpm = 0;
  if (simulator) {
    setTelemetryStatus('手动模拟器', 'simulator');
    render();
  } else {
    ui.autoSweep.checked = false;
    setTelemetryStatus(`正在等待 ${sourceName()}`, 'waiting');
    pollTelemetry();
  }
});
ui.gearUp.addEventListener('click', () => { state.gear = clamp(state.gear + 1, 1, 8); render(); });
ui.gearDown.addEventListener('click', () => { state.gear = clamp(state.gear - 1, 1, 8); render(); });

function setBrake(active) { if (!isLiveSource()) { state.brake = active ? 100 : 0; render(); } }
ui.brakeButton.addEventListener('pointerdown', () => setBrake(true));
ui.brakeButton.addEventListener('pointerup', () => setBrake(false));
ui.brakeButton.addEventListener('pointerleave', () => setBrake(false));
window.addEventListener('keydown', (event) => { if (event.code === 'Space' && !event.repeat) { event.preventDefault(); setBrake(true); } });
window.addEventListener('keyup', (event) => { if (event.code === 'Space') { event.preventDefault(); setBrake(false); } });

setInterval(() => {
  if (isLiveSource()) {
    // Keep the visual/HID flash clock independent of the telemetry polling
    // cadence.  Polling can be 25–40 Hz and would otherwise sample the 120 ms
    // phase at uneven points, especially for showroom-style indicators.
    if (state.rpm >= state.shiftRpm || state.brake > 2 || hasFlashingTelemetry(state.telemetry)) render();
    return;
  }
  if (!ui.autoSweep.checked) {
    if (state.rpm >= state.shiftRpm || state.brake > 2 || hasFlashingTelemetry(state.manualTelemetry)) render();
    return;
  }
  const step = state.maxRpm * .012 * state.sweepDirection;
  state.rpm += step;
  if (state.rpm >= state.maxRpm) state.sweepDirection = -1;
  if (state.rpm <= state.maxRpm * .12) state.sweepDirection = 1;
  render();
}, UI_TICK_MS);

setInterval(pollTelemetry, 40);

if (!navigator.hid) {
  ui.connectButton.disabled = true;
  ui.connectButton.textContent = '浏览器不支持 WebHID';
  log('当前浏览器没有 WebHID。请使用 Chrome 或 Edge，并通过 http://localhost 打开 Demo。');
} else {
  autoConnectAuthorizedK6();
}

syncManualTelemetry({ renderNow: false });
setSimulatorControlsEnabled(!isLiveSource());
pollTelemetry();
render();
