const { app, BrowserWindow, session } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const http = require('node:http');
const path = require('node:path');
const { version: BRIDGE_VERSION } = require('./package.json');
const { matchesBridgeHealth } = require('./bridge-health.cjs');

const BRIDGE_PORT = Number(process.env.K6_ELECTRON_PORT || 8766);
const SMOKE_TEST_CLOSE = process.env.K6_ELECTRON_SMOKE_TEST_CLOSE === '1';
const LIGHT_RESTORE_TIMEOUT_MS = 8000;
const FLYDIGI_VENDOR_ID = 0x37d7;
const FLYDIGI_K6_PRODUCT_ID = 0x2502;
const BRIDGE_TOKEN = randomBytes(24).toString('hex');

// The HID scheduler runs in the renderer.  Chromium normally reduces timer
// frequency when another exclusive-fullscreen window fully occludes Electron,
// which made the K6 animation stall whenever a game covered this window.
app.commandLine.appendSwitch('disable-background-timer-throttling');
app.commandLine.appendSwitch('disable-renderer-backgrounding');
app.commandLine.appendSwitch('disable-backgrounding-occluded-windows');

let mainWindow = null;
let bridgeProcess = null;
let quitInProgress = false;
let allowWindowClose = false;
let shutdownPromise = null;
let bridgeFailure = null;
let hidPermissionsConfigured = false;

function isAllowedOrigin(origin) {
  try {
    const url = new URL(origin);
    return url.hostname === '127.0.0.1' && Number(url.port) === BRIDGE_PORT;
  } catch {
    return false;
  }
}

function isK6Device(device) {
  return device?.vendorId === FLYDIGI_VENDOR_ID && device?.productId === FLYDIGI_K6_PRODUCT_ID;
}

function configureHidPermissions() {
  if (hidPermissionsConfigured) return;
  const ses = session.defaultSession;
  ses.setPermissionCheckHandler((_webContents, permission, requestingOrigin) => (
    permission === 'hid' && isAllowedOrigin(requestingOrigin)
  ));
  ses.setDevicePermissionHandler((details) => (
    details.deviceType === 'hid' && isAllowedOrigin(details.origin) && isK6Device(details.device)
  ));
  ses.on('select-hid-device', (event, details, callback) => {
    event.preventDefault();
    const device = (details.deviceList || []).find(isK6Device);
    callback(device?.deviceId || '');
  });
  hidPermissionsConfigured = true;
}

function bridgeExecutable() {
  const executable = process.platform === 'win32' ? 'k6-telemetry-bridge.exe' : 'k6-telemetry-bridge';
  return path.join(__dirname, 'bridge', 'k6-telemetry-bridge', executable);
}

function startBridge() {
  bridgeFailure = null;
  const executable = bridgeExecutable();
  const args = [
    '--port', String(BRIDGE_PORT),
    '--root', __dirname,
    '--parent-pid', String(process.pid),
    '--bridge-token', BRIDGE_TOKEN,
    '--bridge-version', BRIDGE_VERSION,
  ];
  const child = spawn(executable, args, {
    cwd: __dirname,
    windowsHide: true,
    stdio: 'ignore',
  });
  bridgeProcess = child;
  child.on('error', (error) => {
    bridgeFailure = `Unable to start telemetry bridge: ${error.message}`;
    console.error(bridgeFailure);
  });
  child.on('exit', (code, signal) => {
    if (bridgeProcess === child) bridgeProcess = null;
    if (!quitInProgress && (code || signal)) bridgeFailure = `Telemetry bridge exited (${code ?? signal})`;
    if (!quitInProgress && (code || signal)) console.error(`Telemetry bridge exited (${code ?? signal})`);
  });
  return child;
}

// Stop synchronously: an asynchronous taskkill can be terminated together
// with Electron before it has had a chance to clean up the bridge.
function stopBridge() {
  const child = bridgeProcess;
  bridgeProcess = null;
  if (!child || !child.pid) return;

  if (process.platform === 'win32') {
    spawnSync('taskkill.exe', ['/pid', String(child.pid), '/t', '/f'], {
      windowsHide: true,
      stdio: 'ignore',
      timeout: 3000,
    });
  } else {
    child.kill('SIGTERM');
  }
}

function exitApplication() {
  if (!quitInProgress) {
    quitInProgress = true;
    stopBridge();
  }
  app.exit(0);
}

function waitForRendererRestore(windowToClose) {
  if (!windowToClose || windowToClose.isDestroyed()) return Promise.resolve(false);
  const restore = windowToClose.webContents.executeJavaScript(
    'window.__k6RestoreBeforeClose ? window.__k6RestoreBeforeClose() : false',
    true,
  ).catch((error) => {
    console.error(`Unable to restore K6 lighting before close: ${error.message}`);
    return false;
  });
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      console.error(`K6 lighting restore timed out after ${LIGHT_RESTORE_TIMEOUT_MS}ms`);
      resolve(false);
    }, LIGHT_RESTORE_TIMEOUT_MS);
    restore.then((result) => {
      clearTimeout(timer);
      resolve(result);
    });
  });
}

function beginGracefulShutdown() {
  if (shutdownPromise) return shutdownPromise;
  const windowToClose = mainWindow;
  shutdownPromise = (async () => {
    await waitForRendererRestore(windowToClose);
    allowWindowClose = true;
    quitInProgress = true;
    stopBridge();
    if (windowToClose && !windowToClose.isDestroyed()) windowToClose.destroy();
    app.exit(0);
  })();
  return shutdownPromise;
}

function waitForBridge(child, retries = 80) {
  return new Promise((resolve, reject) => {
    const probe = () => {
      if (bridgeFailure || child.exitCode !== null) {
        reject(new Error(bridgeFailure || `Telemetry bridge exited (${child.exitCode})`));
        return;
      }
      const request = http.get({ hostname: '127.0.0.1', port: BRIDGE_PORT, path: '/api/health' }, (response) => {
        const chunks = [];
        response.on('data', (chunk) => chunks.push(chunk));
        response.on('end', () => {
          try {
            const health = JSON.parse(Buffer.concat(chunks).toString('utf8'));
            const matches = response.statusCode === 200 && matchesBridgeHealth(health, {
              version: BRIDGE_VERSION,
              token: BRIDGE_TOKEN,
              childPid: child.pid,
              parentPid: process.pid,
            });
            if (!matches) {
              reject(new Error(`端口 ${BRIDGE_PORT} 已被其他或旧版服务占用。`));
              return;
            }
            resolve();
          } catch {
            reject(new Error(`端口 ${BRIDGE_PORT} 返回的不是 K6 遥测桥接服务。`));
          }
        });
      });
      request.setTimeout(300, () => request.destroy());
      request.on('error', () => {
        if (bridgeFailure || child.exitCode !== null) {
          reject(new Error(bridgeFailure || `Telemetry bridge exited (${child.exitCode})`));
        } else if (retries <= 0) reject(new Error(`遥测桥接服务未能在端口 ${BRIDGE_PORT} 启动`));
        else {
          retries -= 1;
          setTimeout(probe, 100);
        }
      });
    };
    probe();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1240,
    height: 900,
    minWidth: 900,
    minHeight: 680,
    backgroundColor: '#f3f6fa',
    title: 'K6 Telemetry Console',
    autoHideMenuBar: true,
    show: !SMOKE_TEST_CLOSE,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  mainWindow.loadURL(`http://127.0.0.1:${BRIDGE_PORT}/`);
  if (SMOKE_TEST_CLOSE) {
    setTimeout(() => {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
    }, 800);
  }
  mainWindow.on('close', (event) => {
    if (allowWindowClose) return;
    event.preventDefault();
    void beginGracefulShutdown();
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

async function boot() {
  configureHidPermissions();
  const child = startBridge();
  await waitForBridge(child);
  createWindow();
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });
  app.whenReady().then(boot).catch((error) => {
    console.error(error);
    app.quit();
  });
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0 && !bridgeProcess) {
      boot().catch((error) => console.error(error));
    }
  });
  app.on('before-quit', (event) => {
    if (!allowWindowClose && mainWindow && !mainWindow.isDestroyed()) {
      event.preventDefault();
      void beginGracefulShutdown();
      return;
    }
    quitInProgress = true;
    stopBridge();
  });
  app.on('will-quit', stopBridge);
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') exitApplication();
  });
}

// Last-resort synchronous cleanup for exits initiated outside Electron's
// normal application lifecycle.
process.on('exit', stopBridge);
