// AI 直播 A 线：本地服务（静态页 + /say 合成播报 + SSE 推送）
// 零 npm 依赖，只用 Node 内置模块；TTS 用已装的 python edge_tts 命令行。
import http from 'node:http';
import { spawn } from 'node:child_process';
import { createReadStream } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC = path.join(ROOT, 'public');
const TMP = path.join(ROOT, 'tmp');
const PORT = Number(process.env.PORT || 8788);
const VOICE = process.env.TTS_VOICE || 'zh-CN-XiaoxuanNeural';
const RATE = process.env.TTS_RATE || '+0%';
const PYTHON = process.env.PYTHON || 'python';
const MAX_CHARS = Number(process.env.MAX_CHARS || 300);
const TTS_TIMEOUT_MS = Number(process.env.TTS_TIMEOUT_MS || 45000);

await fs.mkdir(TMP, { recursive: true });

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.mp3': 'audio/mpeg',
  '.wav': 'audio/wav'
};

const sseClients = new Set();
const danmakuRing = [];
const queue = [];
let busy = false;
let seq = 0;
let currentChild = null;

function log() {
  const args = Array.prototype.slice.call(arguments);
  console.log.apply(console, [new Date().toISOString()].concat(args));
}

function broadcast(obj) {
  const payload = 'data: ' + JSON.stringify(obj) + '\n\n';
  for (const res of sseClients) {
    try {
      res.write(payload);
    } catch (e) {
      sseClients.delete(res);
    }
  }
}

// 把文本交给 edge_tts 合成音频；node 侧不处理音频，直接交给浏览器播放
function runTts(textFile, mediaFile) {
  return new Promise(function (resolve, reject) {
    const child = spawn(PYTHON, [
      '-m', 'edge_tts',
      '--file', textFile,
      '--voice', VOICE,
      '--rate', RATE,
      '--write-media', mediaFile
    ], { windowsHide: true, env: Object.assign({}, process.env, { PYTHONUTF8: '1' }) });
    currentChild = child;
    let err = '';
    child.stderr.on('data', function (d) { err += d.toString(); });
    const timer = setTimeout(function () {
      try { child.kill(); } catch (e) {}
      reject(new Error('TTS 超时 ' + TTS_TIMEOUT_MS + 'ms'));
    }, TTS_TIMEOUT_MS);
    child.on('error', function (e) {
      clearTimeout(timer);
      reject(new Error('无法启动 python（检查 PATH）：' + e.message));
    });
    child.on('close', function (code) {
      clearTimeout(timer);
      currentChild = null;
      if (code === 0) resolve();
      else reject(new Error('edge_tts 退出码 ' + code + '：' + err.trim().slice(-400)));
    });
  });
}

async function doSay(job) {
  const id = String(++seq) + '-' + Date.now();
  const base = 'say-' + id;
  const textFile = path.join(TMP, base + '.txt');
  const mediaFile = path.join(TMP, base + '.mp3');
  await fs.writeFile(textFile, job.text, 'utf8');
  await runTts(textFile, mediaFile);
  const st = await fs.stat(mediaFile);
  broadcast({
    type: 'say',
    id: id,
    text: job.text,
    url: '/audio/' + base + '.mp3',
    bytes: st.size,
    voice: VOICE,
    at: Date.now()
  });
  log('[say]', id, st.size + 'B', JSON.stringify(job.text.slice(0, 40)));
  return id;
}

async function pump() {
  if (busy) return;
  busy = true;
  while (queue.length) {
    const job = queue.shift();
    try {
      const id = await doSay(job);
      job.resolve({ ok: true, id: id });
    } catch (e) {
      log('[say:error]', e.message);
      job.reject(e);
      broadcast({ type: 'error', message: e.message });
    }
  }
  busy = false;
}

function enqueue(text, voice) {
  return new Promise(function (resolve, reject) {
    queue.push({ text: text, voice: voice, resolve: resolve, reject: reject });
    pump();
  });
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store'
  });
  res.end(body);
}

function readBody(req, limit) {
  return new Promise(function (resolve, reject) {
    let size = 0;
    const chunks = [];
    req.on('data', function (c) {
      size += c.length;
      if (size > limit) {
        reject(new Error('body 过大'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', function () { resolve(Buffer.concat(chunks).toString('utf8')); });
    req.on('error', reject);
  });
}

async function serveStatic(res, urlPath) {
  const rel = urlPath === '/' ? '/puppet.html' : urlPath;
  const abs = path.join(PUBLIC, path.normalize(rel).replace(/^([.][.][\\/])+/, ''));
  if (!abs.startsWith(PUBLIC)) {
    res.writeHead(403).end('forbidden');
    return;
  }
  try {
    const st = await fs.stat(abs);
    if (!st.isFile()) throw new Error('not file');
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(abs).toLowerCase()] || 'application/octet-stream',
      'Content-Length': st.size,
      'Cache-Control': 'no-store'
    });
    createReadStream(abs).pipe(res);
  } catch (e) {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('404 ' + rel);
  }
}

async function serveAudio(res, urlPath) {
  const name = path.basename(urlPath);
  const abs = path.join(TMP, name);
  try {
    const st = await fs.stat(abs);
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(abs).toLowerCase()] || 'application/octet-stream',
      'Content-Length': st.size,
      'Cache-Control': 'no-store',
      'Accept-Ranges': 'bytes'
    });
    createReadStream(abs).pipe(res);
  } catch (e) {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('404 audio');
  }
}

const server = http.createServer(async function (req, res) {
  const u = new URL(req.url, 'http://127.0.0.1');
  const p = u.pathname;

  if (p === '/health') {
    return json(res, 200, {
      ok: true,
      port: PORT,
      voice: VOICE,
      rate: RATE,
      queue: queue.length,
      busy: busy,
      sseClients: sseClients.size
    });
  }

  if (p === '/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no'
    });
    res.write('retry: 2000' + '\n\n');
    res.write('data: ' + JSON.stringify({ type: 'hello', voice: VOICE }) + '\n\n');
    sseClients.add(res);
    const hb = setInterval(function () {
      try { res.write(': ping' + '\n\n'); } catch (e) { clearInterval(hb); }
    }, 15000);
    req.on('close', function () {
      clearInterval(hb);
      sseClients.delete(res);
    });
    return;
  }

  if (p === '/say') {
    let text = '';
    let voice = VOICE;
    if (req.method === 'POST') {
      try {
        const raw = await readBody(req, 64 * 1024);
        const body = raw ? JSON.parse(raw) : {};
        text = String(body.text || '').trim();
        if (body.voice) voice = String(body.voice);
      } catch (e) {
        return json(res, 400, { ok: false, error: '请求体不是合法 JSON：' + e.message });
      }
    } else {
      text = String(u.searchParams.get('text') || '').trim();
    }
    if (!text) return json(res, 400, { ok: false, error: 'text 不能为空' });
    if (text.length > MAX_CHARS) text = text.slice(0, MAX_CHARS);
    try {
      const r = await enqueue(text, voice);
      return json(res, 200, r);
    } catch (e) {
      return json(res, 500, { ok: false, error: e.message });
    }
  }

  if (p === '/danmaku') {
    let user = '';
    let dtext = '';
    try {
      const raw = await readBody(req, 16 * 1024);
      const body = raw ? JSON.parse(raw) : {};
      user = String(body.user || '观众').slice(0, 24);
      dtext = String(body.text || '').trim().slice(0, 120);
    } catch (e) {
      return json(res, 400, { ok: false, error: '请求体不是合法 JSON' });
    }
    if (!dtext) return json(res, 400, { ok: false, error: 'text 不能为空' });
    const item = { type: 'danmaku', user: user, text: dtext, at: Date.now() };
    danmakuRing.push(item);
    if (danmakuRing.length > 20) danmakuRing.shift();
    broadcast(item);
    log('[danmaku]', user + ': ' + dtext.slice(0, 30));
    return json(res, 200, { ok: true });
  }

  if (p === '/favicon.ico') { res.writeHead(204).end(); return; }

  if (p.startsWith('/audio/')) return serveAudio(res, p);

  return serveStatic(res, p);
});

server.listen(PORT, '127.0.0.1', function () {
  log('[ai-live] http://127.0.0.1:' + PORT + '/puppet.html  voice=' + VOICE + ' rate=' + RATE);
});

process.on('SIGINT', function () {
  log('[ai-live] 关闭中');
  if (currentChild) { try { currentChild.kill(); } catch (e) {} }
  for (const res of sseClients) { try { res.end(); } catch (e) {} }
  server.close(function () { process.exit(0); });
  setTimeout(function () { process.exit(0); }, 1500);
});
