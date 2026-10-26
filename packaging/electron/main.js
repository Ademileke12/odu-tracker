#!/usr/bin/env node
/**
 * ODU Tracker — dedicated Electron shell.
 *
 * Launches the bundled headless Python engine (PyInstaller build of
 * odu_tracker.headless), waits for the local dashboard to answer, then shows
 * the full dashboard in a native window. All state (config, SQLite, cookies)
 * lives in the usual per-user data dir, so this shell co-exists with and
 * replaces the pywebview/Python builds seamlessly.
 *
 * Engine sourcing, in order:
 *   1. bundled:      <resources>/odu-tracker-server/…   (CI / build scripts)
 *   2. cached copy:  <userData>/engine/…                (downloaded once)
 *   3. download:     GitHub Releases on first run (Windows fallback)
 */
'use strict';

const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const START_PORT = 8770;
const PORT_SPAN = 8;               // try 8770..8777 before giving up
const READY_TIMEOUT_MS = 90_000;   // PyInstaller extract + waitress boot
const READY_POLL_MS = 500;
const MAX_BACKEND_RESTARTS = 3;
const ENGINE_FALLBACK_URL =
  'https://github.com/Ademileke12/odu-tracker/releases/latest/download/' +
  'odu-tracker-engine-windows-x64.exe';

// the dashboard is plain HTML/CSS — no need for GPU accel, and this keeps
// the app alive on weak/missing-GPU systems (VMs, old desktops, Wayland
// quirks) instead of aborting with "GPU process isn't usable"
app.disableHardwareAcceleration();

const isPackaged = app.isPackaged;
let backend = null;
let engineExe = null;
let backendPort = 0;
let dashboardUrl = '';
let mainWindow = null;
let quitting = false;
let restarts = 0;
let logStream = null;

/* ------------------------------------------------------------------ utils */

function resourcesDir() {
  return isPackaged ? process.resourcesPath : path.join(__dirname, 'resources');
}

/** Where the bundled Python engine lives (staged by the build scripts). */
function findServerCommand() {
  const dir = resourcesDir();
  const cands =
    process.platform === 'win32'
      ? [
          path.join(dir, 'odu-tracker-server.exe'),
          path.join(dir, 'odu-tracker-server', 'odu-tracker-server.exe'),
        ]
      : [
          path.join(dir, 'odu-tracker-server', 'odu-tracker-server'),
          path.join(dir, 'odu-tracker-server', 'ODU-Tracker'),
        ];
  return cands.find(fs.existsSync) || null;
}

function cachedEnginePath() {
  return path.join(app.getPath('userData'), 'engine', 'odu-tracker-server.exe');
}

/** First bindable port in [start, start+span) on loopback. */
function findFreePort(start, span) {
  return new Promise((resolve, reject) => {
    const probe = (port) => {
      if (port >= start + span) {
        reject(new Error(`no free localhost port in ${start}..${start + span - 1}`));
        return;
      }
      const s = net.createServer();
      s.once('error', () => { try { s.close(); } catch { /* ignore */ } probe(port + 1); });
      s.once('listening', () => s.close(() => resolve(port)));
      s.listen(port, '127.0.0.1');
    };
    probe(start);
  });
}

function logLine(msg) {
  try {
    logStream?.write(`[${new Date().toISOString()}] ${msg}\n`);
  } catch { /* ignore */ }
}

function setLoadStatus(text, pct) {
  try {
    mainWindow?.webContents.executeJavaScript(
      `window.__engineProgress && window.__engineProgress(${pct ?? 'null'}, ${JSON.stringify(text)})`);
  } catch { /* page may be mid-navigation */ }
}

/** Resolve once the server answers an HTTP request (any response is fine). */
function ping() {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port: backendPort, path: '/', timeout: 4000 }, (res) => {
      res.resume();
      resolve(true);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

async function waitForServer(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await ping()) return true;
    if (quitting) return false;
    await new Promise((r) => setTimeout(r, READY_POLL_MS));
  }
  return false;
}

/* ------------------------------------------------------------ engine fetch */

/** Download the engine with manual redirect-following + progress. */
function downloadEngine(dest, onProgress) {
  return new Promise((resolve, reject) => {
    const get = (url, redirects) => {
      if (redirects > 5) return reject(new Error('too many redirects'));
      const req = https.get(url, { headers: { 'User-Agent': 'odu-tracker-electron' } }, (res) => {
        if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
          res.resume();
          return get(new URL(res.headers.location, url).toString(), redirects + 1);
        }
        if (res.statusCode !== 200) {
          res.resume();
          return reject(new Error(`HTTP ${res.statusCode} fetching engine`));
        }
        const total = Number(res.headers['content-length']) || 0;
        let got = 0;
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        const file = fs.createWriteStream(dest);
        res.on('data', (c) => {
          got += c.length;
          if (total) onProgress(Math.min(99, Math.round((got / total) * 100)));
        });
        res.pipe(file);
        file.on('finish', () => file.close(() => resolve(dest)));
        file.on('error', reject);
      });
      req.on('error', reject);
    };
    get(ENGINE_FALLBACK_URL, 0);
  });
}

/** Windows fallback: bundled engine missing → cached copy → one-time download. */
async function ensureEngine() {
  let exe = findServerCommand();
  if (exe) return exe;

  if (process.platform !== 'win32') return null; // posix builds always bundle

  const cached = cachedEnginePath();
  if (fs.existsSync(cached)) return cached;

  setLoadStatus('Downloading the ODU Tracker engine…', 0);
  try {
    await downloadEngine(cached, (pct) => setLoadStatus('Downloading the engine…', pct));
  } catch (err) {
    logLine(`engine download failed: ${err.message}`);
    const r = await dialog.showMessageBox({
      type: 'error',
      title: 'ODU Tracker',
      message: 'The ODU Tracker engine could not be downloaded.',
      detail:
        `${err.message}\n\n` +
        'Check your internet connection and try again. (One-time download; ' +
        'afterwards the app runs fully offline.)',
      buttons: ['Retry', 'Open Releases page', 'Quit'],
      defaultId: 0,
      cancelId: 2,
    });
    if (r.response === 0) return ensureEngine();
    if (r.response === 1) {
      shell.openExternal('https://github.com/Ademileke12/odu-tracker/releases');
      return null;
    }
    return null;
  }
  setLoadStatus('Starting the engine…', null);
  return cached;
}

/* ---------------------------------------------------------------- backend */

function spawnBackend(port, exe) {
  engineExe = exe;
  const logPath = path.join(os.tmpdir(), 'odu-tracker-electron.log');
  logStream = fs.createWriteStream(logPath, { flags: 'a' });
  logLine(`starting engine: ${exe} --port ${port}  (log: ${logPath})`);

  const child = spawn(exe, ['--port', String(port)], {
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
    detached: process.platform !== 'win32', // own process group on POSIX
  });

  child.stdout.on('data', (d) => logLine(`[engine] ${String(d).trim()}`));
  child.stderr.on('data', (d) => logLine(`[engine!] ${String(d).trim()}`));
  child.on('exit', (code, signal) => {
    logLine(`engine exited code=${code} signal=${signal}`);
    if (!quitting && code !== 0 && code !== 3) onBackendCrashed();
  });

  backend = child;
  backendPort = port;
  return child;
}

function killBackend() {
  if (!backend) return;
  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(backend.pid), '/T', '/F'], { windowsHide: true });
    } else {
      try { process.kill(-backend.pid, 'SIGTERM'); } // nuke the process group
      catch { backend.kill('SIGTERM'); }
      setTimeout(() => { try { process.kill(-backend.pid, 'SIGKILL'); } catch { /* gone */ } }, 2500);
    }
  } catch { /* already dead */ }
  backend = null;
}

async function onBackendCrashed() {
  restarts += 1;
  if (restarts > MAX_BACKEND_RESTARTS) {
    dialog.showErrorBox('ODU Tracker', 'The ODU Tracker engine stopped unexpectedly.\n' +
      'Check the log: ' + path.join(os.tmpdir(), 'odu-tracker-electron.log'));
    app.quit();
    return;
  }
  logLine(`restarting engine (attempt ${restarts}/${MAX_BACKEND_RESTARTS})`);
  backend = null;
  try {
    const port = await findFreePort(START_PORT, PORT_SPAN);
    spawnBackend(port, engineExe);
    dashboardUrl = `http://127.0.0.1:${port}/`;
    if (await waitForServer(READY_TIMEOUT_MS)) mainWindow?.loadURL(dashboardUrl);
  } catch { /* surfaced by the next crash */ }
}

/* ------------------------------------------------------------------ window */

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: 'ODU Tracker',
    backgroundColor: '#05070b',
    autoHideMenuBar: true,
    icon: path.join(__dirname, 'build',
      process.platform === 'win32' ? 'icon.ico' : 'icon.png'),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  // load the graceful pre-connect loading page first
  mainWindow.loadFile(path.join(__dirname, 'loading.html'));

  mainWindow.once('ready-to-show', () => mainWindow.show());

  // any link that is not the dashboard itself opens in the system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(dashboardUrl)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

/* ---------------------------------------------------------------- main flow */

async function start() {
  let port;
  try {
    port = await findFreePort(START_PORT, PORT_SPAN);
  } catch (err) {
    dialog.showErrorBox('ODU Tracker', err.message);
    app.exit(3);
    return;
  }

  const exe = await ensureEngine();
  if (!exe) { app.quit(); return; }

  spawnBackend(port, exe);
  dashboardUrl = `http://127.0.0.1:${port}/`;

  const ok = await waitForServer(READY_TIMEOUT_MS);
  if (!ok && !quitting) {
    dialog.showErrorBox('ODU Tracker',
      `The local engine did not come up on port ${port}.\n` +
      'Log: ' + path.join(os.tmpdir(), 'odu-tracker-electron.log'));
    killBackend();
    app.quit();
    return;
  }
  if (!quitting) mainWindow?.loadURL(dashboardUrl);
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  // Another instance already owns the dashboard — just bow out.
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    Menu.setApplicationMenu(null);
    createWindow();
    start();
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => { quitting = true; });
  app.on('will-quit', killBackend);
  app.on('quit', () => { killBackend(); logStream?.end(); });
}
