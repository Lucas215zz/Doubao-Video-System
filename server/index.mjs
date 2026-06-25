import express from 'express';
import multer from 'multer';
import chokidar from 'chokidar';
import fs from 'node:fs/promises';
import fsSync from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';

const ROOT = process.cwd();
const DATA_DIR = path.join(ROOT, 'data');
const UPLOADS_DIR = path.join(ROOT, 'uploads');
const OUTPUTS_DIR = path.join(ROOT, 'outputs');
const WATCHED_DIR = path.join(ROOT, 'watched-downloads');
const DIST_DIR = path.join(ROOT, 'dist');
const DB_FILE = path.join(DATA_DIR, 'store.json');
const PORT = Number(process.env.PORT || 5174);
const VIDEO_EXTENSIONS = new Set(['.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv']);
const DAY_LIMIT = 5;

const defaultDownloadDir = WATCHED_DIR;
const now = () => new Date().toISOString();
const todayKey = () => new Date().toISOString().slice(0, 10);
const id = (prefix) => `${prefix}_${crypto.randomBytes(8).toString('hex')}`;

const upload = multer({
  storage: multer.diskStorage({
    destination: async (_req, _file, cb) => {
      try {
        await ensureDir(UPLOADS_DIR);
        cb(null, UPLOADS_DIR);
      } catch (error) {
        cb(error);
      }
    },
    filename: (_req, file, cb) => {
      const ext = path.extname(file.originalname);
      cb(null, `${Date.now()}-${crypto.randomBytes(6).toString('hex')}${ext}`);
    },
  }),
});

let store = createEmptyStore();
let watcher;

await bootstrap();

const app = express();
app.use(express.json({ limit: '2mb' }));
app.use('/uploads', express.static(UPLOADS_DIR));
app.use('/outputs', express.static(OUTPUTS_DIR));

app.get('/api/state', (_req, res) => {
  res.json(viewState());
});

app.patch('/api/settings', async (req, res) => {
  const downloadDir = String(req.body.downloadDir || '').trim();
  if (!downloadDir) {
    return res.status(400).json({ error: '下载目录不能为空' });
  }

  store.settings.downloadDir = path.resolve(downloadDir);
  store.settings.updatedAt = now();
  await save();
  await restartWatcher();
  res.json(viewState());
});

app.post('/api/accounts', async (req, res) => {
  const name = String(req.body.name || '').trim();
  if (!name) {
    return res.status(400).json({ error: '账号名称不能为空' });
  }

  const account = normalizeAccount({
    id: id('acct'),
    name,
    browserPath: String(req.body.browserPath || '').trim(),
    profilePath: String(req.body.profilePath || '').trim(),
    note: String(req.body.note || '').trim(),
    dailyLimit: Number(req.body.dailyLimit || DAY_LIMIT),
    usageByDate: {},
    createdAt: now(),
    updatedAt: now(),
  });

  store.accounts.unshift(account);
  await save();
  res.json(viewState());
});

app.patch('/api/accounts/:accountId', async (req, res) => {
  const account = store.accounts.find((item) => item.id === req.params.accountId);
  if (!account) {
    return res.status(404).json({ error: '账号不存在' });
  }

  account.name = String(req.body.name ?? account.name).trim();
  account.browserPath = String(req.body.browserPath ?? account.browserPath).trim();
  account.profilePath = String(req.body.profilePath ?? account.profilePath).trim();
  account.note = String(req.body.note ?? account.note).trim();
  account.dailyLimit = Math.max(1, Number(req.body.dailyLimit ?? account.dailyLimit) || DAY_LIMIT);
  account.updatedAt = now();
  await save();
  res.json(viewState());
});

app.delete('/api/accounts/:accountId', async (req, res) => {
  store.accounts = store.accounts.filter((item) => item.id !== req.params.accountId);
  await save();
  res.json(viewState());
});

app.post('/api/accounts/:accountId/open', async (req, res) => {
  const account = store.accounts.find((item) => item.id === req.params.accountId);
  if (!account) {
    return res.status(404).json({ error: '账号不存在' });
  }

  try {
    await openBrowserForAccount(account);
    res.json({ ok: true });
  } catch (error) {
    res.status(400).json({ error: error.message || '无法打开浏览器' });
  }
});

app.post('/api/accounts/:accountId/usage', async (req, res) => {
  const account = store.accounts.find((item) => item.id === req.params.accountId);
  if (!account) {
    return res.status(404).json({ error: '账号不存在' });
  }

  const date = todayKey();
  const delta = Number(req.body.delta || 0);
  account.usageByDate[date] = Math.max(0, (account.usageByDate[date] || 0) + delta);
  account.updatedAt = now();
  await save();
  res.json(viewState());
});

app.post('/api/tasks', upload.single('reference'), async (req, res) => {
  const title = String(req.body.title || '').trim();
  const prompt = String(req.body.prompt || '').trim();
  if (!title || !prompt) {
    return res.status(400).json({ error: '标题和提示词不能为空' });
  }

  const task = normalizeTask({
    id: id('task'),
    title,
    prompt,
    accountId: String(req.body.accountId || ''),
    status: String(req.body.status || 'queued'),
    referencePath: req.file ? toPublicUploadPath(req.file.filename) : '',
    referenceName: req.file?.originalname || '',
    outputPath: '',
    outputName: '',
    notes: String(req.body.notes || '').trim(),
    createdAt: now(),
    updatedAt: now(),
    submittedAt: '',
    completedAt: '',
  });

  store.tasks.unshift(task);
  await save();
  res.json(viewState());
});

app.patch('/api/tasks/:taskId', async (req, res) => {
  const task = store.tasks.find((item) => item.id === req.params.taskId);
  if (!task) {
    return res.status(404).json({ error: '任务不存在' });
  }

  const previousStatus = task.status;
  const nextStatus = String(req.body.status ?? task.status);
  task.title = String(req.body.title ?? task.title).trim();
  task.prompt = String(req.body.prompt ?? task.prompt).trim();
  task.accountId = String(req.body.accountId ?? task.accountId);
  task.status = nextStatus;
  task.notes = String(req.body.notes ?? task.notes).trim();
  task.updatedAt = now();

  if (previousStatus !== 'submitted' && nextStatus === 'submitted' && !task.submittedAt) {
    task.submittedAt = now();
    const account = store.accounts.find((item) => item.id === task.accountId);
    if (account) {
      account.usageByDate[todayKey()] = (account.usageByDate[todayKey()] || 0) + 1;
      account.updatedAt = now();
    }
  }

  if (nextStatus === 'done' && !task.completedAt) {
    task.completedAt = now();
  }

  await save();
  res.json(viewState());
});

app.delete('/api/tasks/:taskId', async (req, res) => {
  store.tasks = store.tasks.filter((item) => item.id !== req.params.taskId);
  await save();
  res.json(viewState());
});

app.post('/api/tasks/:taskId/open-reference', async (req, res) => {
  const task = store.tasks.find((item) => item.id === req.params.taskId);
  if (!task?.referencePath) {
    return res.status(404).json({ error: '任务没有参考图' });
  }

  try {
    await openPath(path.join(ROOT, task.referencePath.replace(/^\//, '')));
    res.json({ ok: true });
  } catch (error) {
    res.status(400).json({ error: error.message || '无法打开参考图' });
  }
});

app.post('/api/inbox/scan', async (_req, res) => {
  await scanDownloads();
  res.json(viewState());
});

app.delete('/api/inbox/:inboxId', async (req, res) => {
  store.inbox = store.inbox.filter((item) => item.id !== req.params.inboxId);
  await save();
  res.json(viewState());
});

app.post('/api/inbox/:inboxId/archive', async (req, res) => {
  const inboxItem = store.inbox.find((item) => item.id === req.params.inboxId);
  const task = store.tasks.find((item) => item.id === req.body.taskId);
  if (!inboxItem || !task) {
    return res.status(404).json({ error: '视频或任务不存在' });
  }

  try {
    const account = store.accounts.find((item) => item.id === task.accountId);
    const archived = await archiveVideo(inboxItem, task, account);
    task.outputPath = archived.publicPath;
    task.outputName = archived.fileName;
    task.status = 'done';
    task.completedAt = now();
    task.updatedAt = now();
    store.inbox = store.inbox.filter((item) => item.id !== inboxItem.id);
    await save();
    res.json(viewState());
  } catch (error) {
    res.status(400).json({ error: error.message || '归档失败' });
  }
});

if (fsSync.existsSync(DIST_DIR)) {
  app.use(express.static(DIST_DIR));
  app.get(/.*/, (_req, res) => {
    res.sendFile(path.join(DIST_DIR, 'index.html'));
  });
}

app.listen(PORT, () => {
  console.log(`Video workbench API listening at http://localhost:${PORT}`);
  console.log(`Watching downloads: ${store.settings.downloadDir}`);
});

async function bootstrap() {
  await ensureDir(DATA_DIR);
  await ensureDir(UPLOADS_DIR);
  await ensureDir(OUTPUTS_DIR);

  if (!fsSync.existsSync(DB_FILE)) {
    await save();
  } else {
    const raw = await fs.readFile(DB_FILE, 'utf8');
    store = normalizeStore(JSON.parse(raw));
  }

  await scanDownloads();
  await restartWatcher();
}

function createEmptyStore() {
  return {
    settings: {
      downloadDir: defaultDownloadDir,
      updatedAt: now(),
    },
    accounts: [],
    tasks: [],
    inbox: [],
  };
}

function normalizeStore(input) {
  return {
    settings: {
      downloadDir: input?.settings?.downloadDir || defaultDownloadDir,
      updatedAt: input?.settings?.updatedAt || now(),
    },
    accounts: Array.isArray(input?.accounts) ? input.accounts.map(normalizeAccount) : [],
    tasks: Array.isArray(input?.tasks) ? input.tasks.map(normalizeTask) : [],
    inbox: Array.isArray(input?.inbox) ? input.inbox.map(normalizeInbox) : [],
  };
}

function normalizeAccount(input) {
  return {
    id: input.id || id('acct'),
    name: input.name || '未命名账号',
    browserPath: input.browserPath || '',
    profilePath: input.profilePath || '',
    note: input.note || '',
    dailyLimit: Math.max(1, Number(input.dailyLimit || DAY_LIMIT)),
    usageByDate: input.usageByDate || {},
    createdAt: input.createdAt || now(),
    updatedAt: input.updatedAt || now(),
  };
}

function normalizeTask(input) {
  return {
    id: input.id || id('task'),
    title: input.title || '未命名任务',
    prompt: input.prompt || '',
    accountId: input.accountId || '',
    status: ['queued', 'submitted', 'waiting_download', 'done'].includes(input.status)
      ? input.status
      : 'queued',
    referencePath: input.referencePath || '',
    referenceName: input.referenceName || '',
    outputPath: input.outputPath || '',
    outputName: input.outputName || '',
    notes: input.notes || '',
    createdAt: input.createdAt || now(),
    updatedAt: input.updatedAt || now(),
    submittedAt: input.submittedAt || '',
    completedAt: input.completedAt || '',
  };
}

function normalizeInbox(input) {
  return {
    id: input.id || id('inbox'),
    filePath: input.filePath || '',
    fileName: input.fileName || '',
    size: Number(input.size || 0),
    createdAt: input.createdAt || now(),
    detectedAt: input.detectedAt || now(),
  };
}

function viewState() {
  const today = todayKey();
  return {
    ...store,
    today,
    accounts: store.accounts.map((account) => ({
      ...account,
      todayUsed: account.usageByDate[today] || 0,
      remainingToday: Math.max(0, account.dailyLimit - (account.usageByDate[today] || 0)),
    })),
  };
}

async function save() {
  await ensureDir(DATA_DIR);
  const tmp = `${DB_FILE}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(store, null, 2), 'utf8');
  await fs.rename(tmp, DB_FILE);
}

async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

function toPublicUploadPath(fileName) {
  return `/uploads/${fileName}`;
}

async function restartWatcher() {
  if (watcher) {
    await watcher.close();
  }

  const dir = store.settings.downloadDir;
  try {
    await ensureDir(dir);
  } catch (error) {
    console.warn(`Cannot watch download directory: ${dir}`, error.message);
    return;
  }

  watcher = chokidar.watch(dir, {
    ignoreInitial: true,
    depth: 0,
    awaitWriteFinish: {
      stabilityThreshold: 1500,
      pollInterval: 250,
    },
  });

  watcher.on('add', async (filePath) => {
    await addInboxFile(filePath);
  });
}

async function scanDownloads() {
  try {
    await ensureDir(store.settings.downloadDir);
  } catch (error) {
    console.warn(`Cannot access download directory: ${store.settings.downloadDir}`, error.message);
    return;
  }

  let entries = [];
  try {
    entries = await fs.readdir(store.settings.downloadDir, { withFileTypes: true });
  } catch (error) {
    console.warn(`Cannot read download directory: ${store.settings.downloadDir}`, error.message);
    return;
  }

  for (const entry of entries) {
    if (!entry.isFile()) continue;
    await addInboxFile(path.join(store.settings.downloadDir, entry.name), false);
  }
  await save();
}

async function addInboxFile(filePath, shouldSave = true) {
  const ext = path.extname(filePath).toLowerCase();
  if (!VIDEO_EXTENSIONS.has(ext) || filePath.endsWith('.crdownload')) {
    return;
  }

  if (store.inbox.some((item) => path.resolve(item.filePath) === path.resolve(filePath))) {
    return;
  }

  try {
    const stat = await fs.stat(filePath);
    if (!stat.isFile()) return;
    store.inbox.unshift(
      normalizeInbox({
        id: id('inbox'),
        filePath,
        fileName: path.basename(filePath),
        size: stat.size,
        createdAt: stat.birthtime?.toISOString?.() || now(),
        detectedAt: now(),
      }),
    );
    if (shouldSave) {
      await save();
    }
  } catch {
    // File can disappear while the browser is still moving it; the next scan will catch it.
  }
}

async function archiveVideo(inboxItem, task, account) {
  const date = todayKey();
  const accountName = safeName(account?.name || '未指定账号');
  const taskName = safeName(task.title);
  const ext = path.extname(inboxItem.fileName) || '.mp4';
  const targetDir = path.join(OUTPUTS_DIR, date, accountName);
  await ensureDir(targetDir);

  let fileName = `${taskName}${ext}`;
  let targetPath = path.join(targetDir, fileName);
  let index = 2;
  while (fsSync.existsSync(targetPath)) {
    fileName = `${taskName}-${index}${ext}`;
    targetPath = path.join(targetDir, fileName);
    index += 1;
  }

  await fs.rename(inboxItem.filePath, targetPath);
  return {
    fileName,
    publicPath: `/${path.relative(ROOT, targetPath).replaceAll(path.sep, '/')}`,
  };
}

function safeName(input) {
  return String(input || 'untitled')
    .trim()
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, '_')
    .replace(/\s+/g, ' ')
    .slice(0, 80);
}

async function openBrowserForAccount(account) {
  const targetUrl = 'https://www.doubao.com/';
  if (account.browserPath) {
    const args = [];
    if (account.profilePath) {
      await ensureDir(account.profilePath);
      args.push(`--user-data-dir=${account.profilePath}`);
    }
    args.push(targetUrl);
    spawn(account.browserPath, args, {
      detached: true,
      stdio: 'ignore',
    }).unref();
    return;
  }

  await openUrl(targetUrl);
}

function openUrl(url) {
  return new Promise((resolve, reject) => {
    const child = spawn('cmd', ['/c', 'start', '', url], {
      detached: true,
      stdio: 'ignore',
    });
    child.on('error', reject);
    child.on('spawn', resolve);
    child.unref();
  });
}

function openPath(filePath) {
  return new Promise((resolve, reject) => {
    const child = spawn('cmd', ['/c', 'start', '', filePath], {
      detached: true,
      stdio: 'ignore',
    });
    child.on('error', reject);
    child.on('spawn', resolve);
    child.unref();
  });
}
