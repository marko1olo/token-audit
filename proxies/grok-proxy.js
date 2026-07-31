/**
 * grok-proxy v3.0 — ENTERPRISE LIVE BALANCER & STICKY RETRY
 *
 * Что нового в v3.0:
 *  1. Live Web UI Dashboard на http://127.0.0.1:8319/ — живой мониторинг нагрузок ключей,
 *     активных сессий, 429 ошибок и статусов ключей в реальном времени.
 *  2. Поддержка внешнего файла keys.json / keys.txt рядом с прокси (авто-перезагрузка ключей).
 *  3. Dead Key Guard (401/403): автоматическое отключение скомпрометированных/истёкших
 *     ключей и мгновенный перенос сессий на здоровые ключи.
 *  4. Smart Load Balancer (v2.3): наименьшая нагрузка -> LRU -> авто-устранение коллизий.
 *  5. Infinite 429 Wait-and-Retry (20с пауза, сохранение prompt cache).
 */

const http  = require('http');
const https = require('https');
const zlib  = require('zlib');
const fs    = require('fs');
const path  = require('path');
const { URL } = require('url');

const PROXY_PORT = 8319;
const UPSTREAM   = 'https://tunnel.rue.onl';

// ── 3 DEFAULT GROK КЛЮЧА ─────────────────────────────────────────────────────
let GROK_KEYS = [
  'pk_6RDQLAfKG5T7uDTy4DZV_c1ec',
  'pk_2hJoaRGL6P2FphGoURgM_41b3',
  'pk_e7FrS6qPgADHLX1MZxQx_b495',
];

// Внешний файл ключей
const KEYS_JSON_PATH = path.join(__dirname, 'keys.json');
const KEYS_TXT_PATH  = path.join(__dirname, 'keys.txt');

function loadExternalKeys() {
  try {
    if (fs.existsSync(KEYS_JSON_PATH)) {
      const raw = fs.readFileSync(KEYS_JSON_PATH, 'utf8');
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length > 0) {
        GROK_KEYS = arr.map(k => String(k).trim()).filter(Boolean);
        log(`🔑 Загружено ${GROK_KEYS.length} ключей из keys.json`);
        return;
      }
    }
    if (fs.existsSync(KEYS_TXT_PATH)) {
      const raw = fs.readFileSync(KEYS_TXT_PATH, 'utf8');
      const lines = raw.split('\n').map(l => l.trim()).filter(l => l && !l.startswith('#'));
      if (lines.length > 0) {
        GROK_KEYS = lines;
        log(`🔑 Загружено ${GROK_KEYS.length} ключей из keys.txt`);
        return;
      }
    }
  } catch (err) {
    log(`⚠ Ошибка чтения внешних ключей: ${err.message}`);
  }
}

loadExternalKeys();

// ── СОСТОЯНИЕ КЛЮЧЕЙ И СЕССИЙ ────────────────────────────────────────────────
const sessionKeyMap = new Map();
let keyLastUsedTime = new Array(GROK_KEYS.length).fill(0);
let keyReqCounts    = new Array(GROK_KEYS.length).fill(0);
let keyWait429Counts = new Array(GROK_KEYS.length).fill(0);
let keyDisabled     = new Array(GROK_KEYS.length).fill(false);

function resetStateForNewKeys() {
  keyLastUsedTime  = new Array(GROK_KEYS.length).fill(0);
  keyReqCounts     = new Array(GROK_KEYS.length).fill(0);
  keyWait429Counts = new Array(GROK_KEYS.length).fill(0);
  keyDisabled      = new Array(GROK_KEYS.length).fill(false);
}

// Посчитать количество привязанных сессий к каждому ключу
function getSessionCountsPerKey() {
  const counts = new Array(GROK_KEYS.length).fill(0);
  for (const [sid, kIdx] of sessionKeyMap.entries()) {
    if (kIdx >= 0 && kIdx < GROK_KEYS.length && !keyDisabled[kIdx]) {
      counts[kIdx]++;
    }
  }
  return counts;
}

// Найти лучший доступный ключ (не disabled, минимально загружен, LRU)
function getBestAvailableKeyIdx() {
  const counts = getSessionCountsPerKey();
  let minCount = Infinity;
  let candidates = [];

  for (let i = 0; i < GROK_KEYS.length; i++) {
    if (keyDisabled[i]) continue;
    if (counts[i] < minCount) {
      minCount = counts[i];
      candidates = [i];
    } else if (counts[i] === minCount) {
      candidates.push(i);
    }
  }

  if (candidates.length === 0) {
    // Если все ключи почему-то disabled — сбрасываем блокировку
    log(`⚠ Все ключи disabled! Сбрасываю аварийную блокировку.`);
    keyDisabled.fill(false);
    return 0;
  }

  let bestIdx = candidates[0];
  let oldestTime = keyLastUsedTime[bestIdx];
  for (const idx of candidates) {
    if (keyLastUsedTime[idx] < oldestTime) {
      oldestTime = keyLastUsedTime[idx];
      bestIdx = idx;
    }
  }
  return bestIdx;
}

function getKeyIdxForSession(sessionId) {
  if (!sessionId) {
    const idx = getBestAvailableKeyIdx();
    keyLastUsedTime[idx] = Date.now();
    return idx;
  }

  // 1. Новая сессия — сопоставляем свободный ключ
  if (!sessionKeyMap.has(sessionId)) {
    const idx = getBestAvailableKeyIdx();
    sessionKeyMap.set(sessionId, idx);
    const counts = getSessionCountsPerKey();
    keyLastUsedTime[idx] = Date.now();
    log(`🔑 New Session [${sessionId.slice(0,8)}...] → Assigned Key #${idx + 1} (Load: ${counts.map((c,i)=>`K${i+1}:${c}`).join(' ')})`);
    return idx;
  }

  // 2. Существующая сессия
  let assignedIdx = sessionKeyMap.get(sessionId);

  // Если прошлый ключ сессии был заблокирован (401/403), даем новый живой ключ!
  if (keyDisabled[assignedIdx]) {
    const newIdx = getBestAvailableKeyIdx();
    log(`🛡 DEAD KEY GUARD: Key #${assignedIdx + 1} is DISABLED → Re-assigning Session [${sessionId.slice(0,8)}] to Key #${newIdx + 1}`);
    assignedIdx = newIdx;
    sessionKeyMap.set(sessionId, assignedIdx);
  } else {
    // Ребалансировка если наш ключ перегружен (>1 сессии), а рядом есть абсолютно свободный ключ (0 сессий)
    const counts = getSessionCountsPerKey();
    if (counts[assignedIdx] > 1) {
      const freeIdx = counts.indexOf(0);
      if (freeIdx !== -1 && !keyDisabled[freeIdx]) {
        const oldIdx = assignedIdx;
        assignedIdx = freeIdx;
        sessionKeyMap.set(sessionId, assignedIdx);
        const newCounts = getSessionCountsPerKey();
        log(`⚖️ SMART REBALANCE: Session [${sessionId.slice(0,8)}...] moved from Key #${oldIdx + 1} (${counts[oldIdx]} sessions) → Key #${assignedIdx + 1} (0 sessions)! New Load: ${newCounts.map((c,i)=>`K${i+1}:${c}`).join(' ')}`);
      }
    }
  }

  keyLastUsedTime[assignedIdx] = Date.now();
  return assignedIdx;
}

const RATE_LIMIT_WAIT_MS = 20000;
const NET_DELAYS_MS = [2000, 2000, 4000, 8000, 15000, 30000];
const netDelay = (n) => Math.round(NET_DELAYS_MS[Math.min(n, NET_DELAYS_MS.length - 1)] * (0.75 + Math.random() * 0.5));

const httpsAgent = new https.Agent({
  keepAlive: true,
  maxSockets: 50,
  maxFreeSockets: 4,
  keepAliveMsecs: 5000,
});

let netFailStreak = 0;
function noteNetFailure(reason) {
  netFailStreak++;
  if (netFailStreak >= 3) {
    log(`🔄 ${netFailStreak} сетевых сбоя подряд (${reason}) — сбрасываю пул сокетов`);
    try { httpsAgent.destroy(); } catch {}
    netFailStreak = 0;
  }
}

function log(...args) {
  process.stdout.write(`[${new Date().toISOString()}] ${args.join(' ')}\n`);
}

// ── FORWARD REQUEST ───────────────────────────────────────────────────────────
function executeForward(req, res, body, cleanUrl, sessionId, retryCount = 0, rateLimitRetry = 0) {
  const keyIdx = getKeyIdxForSession(sessionId);
  let rawKey = GROK_KEYS[keyIdx] || '';
  const key = rawKey.trim().replace(/[^\x20-\x7E]/g, '');
  const keyNum = keyIdx + 1;

  if (!key || /[^\x20-\x7E]/.test(rawKey) || rawKey.includes('ВСТАВЬ')) {
    log(`❌ ОШИБКА: Ключ #${keyNum} невалиден или содержит не-ASCII символы / кириллицу!`);
    log(`👉 Замени '${rawKey.slice(0, 30)}...' на реальный API-ключ в GROK_KEYS.`);
    if (!res.headersSent) {
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: `Key #${keyNum} is invalid or contains non-ASCII characters.` } }));
    }
    return;
  }

  keyReqCounts[keyIdx]++;

  const upHeaders = {};
  for (const [k, v] of Object.entries(req.headers)) {
    if (['host','content-length','authorization','x-api-key'].includes(k)) continue;
    upHeaders[k] = v;
  }
  upHeaders['host']           = new URL(UPSTREAM).hostname;
  upHeaders['content-length'] = String(body.length);
  upHeaders['authorization']  = `Bearer ${key}`;
  upHeaders['x-api-key']      = key;
  upHeaders['user-agent']     = 'cline/1.0';

  const retryLabel = retryCount > 0 ? ` NET-RETRY#${retryCount}` : '';
  const rateLabel  = rateLimitRetry > 0 ? ` 429-WAIT#${rateLimitRetry}` : '';
  log(`→ ${req.method} ${cleanUrl} (Key #${keyNum}${retryLabel}${rateLabel})`);

  const upUrl = new URL(cleanUrl, UPSTREAM);
  const upReq = https.request({
    hostname: upUrl.hostname,
    port:     443,
    path:     upUrl.pathname + (upUrl.search || ''),
    method:   req.method,
    headers:  upHeaders,
    agent:    httpsAgent,
  }, (upRes) => {
    log(`↑ HTTP ${upRes.statusCode} (Key #${keyNum})`);

    // ── 401 / 403 INVALID / EXPIRED KEY → DISABLE KEY & RE-ASSIGN ───────────
    if (upRes.statusCode === 401 || upRes.statusCode === 403) {
      upRes.resume();
      log(`⛔ KEY REJECTED: Key #${keyNum} returned HTTP ${upRes.statusCode}! Marking Key #${keyNum} as DISABLED.`);
      keyDisabled[keyIdx] = true;
      if (sessionId) sessionKeyMap.delete(sessionId);
      log(`🔄 Retrying request on another active key...`);
      executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry);
      return;
    }

    // ── 429 RATE LIMIT: ждём 20с, тот же ключ ────────────────────────────────
    if (upRes.statusCode === 429) {
      upRes.resume();
      keyWait429Counts[keyIdx]++;
      log(`⏳ 429 Rate Limit на Key #${keyNum} → ждём ${RATE_LIMIT_WAIT_MS/1000}с, повторяю НА ТОМ ЖЕ ключе (попытка #${rateLimitRetry + 1})`);
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry + 1);
      }, RATE_LIMIT_WAIT_MS);
      return;
    }

    // ── 5xx Server Error: backoff, тот же ключ ────────────────────────────────
    if (upRes.statusCode === 529 || upRes.statusCode >= 500) {
      const wait = netDelay(retryCount);
      log(`⚠ HTTP ${upRes.statusCode} (server error) → повтор через ${wait}мс`);
      upRes.resume();
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry);
      }, wait);
      return;
    }

    // ── 4xx Ошибки клиентов ──────────────────────────────────────────────────
    if (upRes.statusCode !== 200) {
      const parts = [];
      upRes.on('data', c => parts.push(c));
      upRes.on('end', () => {
        const buf = Buffer.concat(parts);
        const decode = upRes.headers['content-encoding'] === 'gzip'
          ? (b, cb) => zlib.gunzip(b, (err, r) => cb(err ? b.toString('utf8') : r.toString('utf8')))
          : (b, cb) => cb(b.toString('utf8'));
        decode(buf, text => {
          log(`💥 ERROR ${upRes.statusCode}: ${text.slice(0, 300)}`);
          if (!res.headersSent) {
            const outBuf = Buffer.from(text, 'utf8');
            const headers = { ...upRes.headers };
            delete headers['content-encoding'];
            headers['content-length'] = String(outBuf.length);
            res.writeHead(upRes.statusCode, headers);
            res.end(outBuf);
          }
        });
      });
      return;
    }

    // ── 200 OK ───────────────────────────────────────────────────────────────
    netFailStreak = 0;
    res.writeHead(upRes.statusCode, upRes.headers);
    upRes.pipe(res);
    upRes.on('error', err => log(`⚠ upRes pipe error: ${err.message}`));
  });

  upReq.on('socket', (sock) => {
    if (sock.connecting) {
      sock.setTimeout(15000);
      sock.once('connect', () => sock.setTimeout(90000));
    } else {
      sock.setTimeout(90000);
    }
  });

  upReq.on('timeout', () => {
    log(`⏱ socket timeout → destroy & retry`);
    upReq.destroy(new Error('socket timeout'));
  });

  upReq.on('error', err => {
    if (/ECONNRESET|ETIMEDOUT|EPIPE|ENETUNREACH|EHOSTUNREACH|socket timeout|ECONNREFUSED/i.test(err.message || '')) {
      noteNetFailure(err.message);
    }
    const wait = netDelay(retryCount);
    log(`✗ upstream error (Key #${keyNum}): ${err.message} → retry in ${wait}мс`);
    setTimeout(() => {
      if (res.writableEnded || res.destroyed) return;
      executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry);
    }, wait);
  });

  upReq.write(body);
  upReq.end();
}

// ── LIVE HTML STATUS DASHBOARD PAGE (GET /) ──────────────────────────────────
function renderHtmlDashboard() {
  const counts = getSessionCountsPerKey();
  const now = Date.now();

  const keyCards = GROK_KEYS.map((k, i) => {
    const maskedKey = k.length > 10 ? `${k.slice(0,6)}...${k.slice(-4)}` : k;
    const idleSec = keyLastUsedTime[i] > 0 ? Math.round((now - keyLastUsedTime[i]) / 1000) : '—';
    const statusClass = keyDisabled[i] ? 'disabled' : 'active';
    const statusText  = keyDisabled[i] ? '⛔ DISABLED (401/403)' : '✅ ACTIVE';

    return `
      <div class="card ${statusClass}">
        <div class="card-header">
          <span class="key-title">🔑 Key #${i + 1}</span>
          <span class="badge ${statusClass}">${statusText}</span>
        </div>
        <div class="key-code">${maskedKey}</div>
        <div class="metrics">
          <div class="m-item">
            <span class="m-val">${counts[i]}</span>
            <span class="m-lbl">Active Sessions</span>
          </div>
          <div class="m-item">
            <span class="m-val">${keyReqCounts[i]}</span>
            <span class="m-lbl">Total Requests</span>
          </div>
          <div class="m-item">
            <span class="m-val">${keyWait429Counts[i]}</span>
            <span class="m-lbl">429 Retries</span>
          </div>
          <div class="m-item">
            <span class="m-val">${idleSec === '—' ? 'Never' : idleSec + 's ago'}</span>
            <span class="m-lbl">Last Active</span>
          </div>
        </div>
      </div>
    `;
  }).join('');

  const activeSessionsList = Array.from(sessionKeyMap.entries()).map(([sid, kIdx]) => {
    return `<li><code>${sid}</code> &rarr; <span class="key-tag">Key #${kIdx + 1}</span></li>`;
  }).join('') || '<li class="empty">No active dialogues connected yet.</li>';

  return `<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="3">
  <title>Grok Proxy v3.0 — Live Status</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; background: #0b0f19; color: #e2e8f0; padding: 24px; }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid #1e293b; padding-bottom: 16px; }
    .title { font-size: 24px; font-weight: 700; color: #38bdf8; display: flex; align-items: center; gap: 10px; }
    .subtitle { font-size: 13px; color: #94a3b8; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 32px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 18px; transition: all 0.2s; }
    .card.disabled { border-color: #ef4444; background: #2c1517; }
    .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
    .key-title { font-weight: 700; font-size: 16px; color: #f8fafc; }
    .key-code { font-size: 12px; font-family: monospace; color: #64748b; background: #0f172a; padding: 4px 8px; border-radius: 6px; margin-bottom: 14px; word-break: break-all; }
    .badge { font-size: 11px; padding: 3px 8px; border-radius: 20px; font-weight: 600; }
    .badge.active { background: #065f46; color: #34d399; }
    .badge.disabled { background: #991b1b; color: #fca5a5; }
    .metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .m-item { background: #0f172a; padding: 10px; border-radius: 8px; text-align: center; }
    .m-val { font-size: 18px; font-weight: 700; color: #38bdf8; display: block; }
    .m-lbl { font-size: 10px; color: #94a3b8; text-transform: uppercase; margin-top: 2px; }
    .panel { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 20px; }
    .panel-title { font-size: 16px; font-weight: 600; margin-bottom: 12px; color: #f8fafc; }
    ul { list-style: none; }
    li { padding: 8px 12px; border-bottom: 1px solid #334155; font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
    li.empty { color: #64748b; font-style: italic; border: none; }
    .key-tag { background: #0284c7; color: #fff; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  </style>
</head>
<body>
  <div class="header">
    <div>
      <div class="title">🚀 Grok Proxy v3.0 Live Status</div>
      <div class="subtitle">Wait-and-Retry (20s) &bull; Smart LRU Balancer &bull; Dead Key Guard</div>
    </div>
    <div style="text-align: right;">
      <div style="font-size: 12px; color: #34d399;">● LIVE (Auto-refresh 3s)</div>
      <div style="font-size: 11px; color: #64748b;">${new Date().toLocaleTimeString()}</div>
    </div>
  </div>

  <div class="grid">
    ${keyCards}
  </div>

  <div class="panel">
    <div class="panel-title">💬 Connected Dialogues / Sessions (${sessionKeyMap.size})</div>
    <ul>
      ${activeSessionsList}
    </ul>
  </div>
</body>
</html>`;
}

// ── SERVER ────────────────────────────────────────────────────────────────────
const server = http.createServer((req, res) => {
  res.on('error', err => log(`⚠ client res error: ${err.message}`));

  if ((req.method === 'HEAD' || req.method === 'GET') && (req.url === '/' || req.url === '')) {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(renderHtmlDashboard());
    return;
  }

  const chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => {
    const body = Buffer.concat(chunks);
    const cleanUrl = req.url;

    let sessionId = req.headers['x-session-id'] || req.headers['x-claude-code-session-id'] || null;
    if (body.length > 0) {
      try {
        const obj = JSON.parse(body.toString('utf8'));
        if (!sessionId && obj.metadata?.user_id) {
          try {
            const uid = JSON.parse(obj.metadata.user_id);
            if (uid.session_id) sessionId = uid.session_id;
          } catch {}
        }
        let totalChars = 0;
        if (Array.isArray(obj.messages)) {
          for (const m of obj.messages) {
            if (typeof m.content === 'string') totalChars += m.content.length;
            else if (Array.isArray(m.content)) for (const p of m.content) { if (p.text) totalChars += p.text.length; }
          }
        }
        log(`📦 model=${obj.model} msgs=${obj.messages?.length || 0} ~${Math.round(totalChars/4).toLocaleString()} tok${sessionId ? ' sid='+sessionId.slice(0,8) : ''}`);
      } catch {}
    }

    executeForward(req, res, body, cleanUrl, sessionId);
  });
});

server.listen(PROXY_PORT, '127.0.0.1', () => {
  log(`=======================================================`);
  log(`🚀 grok-proxy v3.0 ENTERPRISE LIVE BALANCER`);
  log(`   Port:       http://127.0.0.1:${PROXY_PORT}/v1`);
  log(`   Live Dashboard: http://127.0.0.1:${PROXY_PORT}/`);
  log(`   Upstream:   ${UPSTREAM}`);
  log(`   Keys:       ${GROK_KEYS.length} ключа (Smart LRU + Dead Key Guard)`);
  log(`   On 429:     ждём ${RATE_LIMIT_WAIT_MS/1000}с → тот же ключ (бесконечно)`);
  log(`   On 401/403: замена ключа на лету без краша сессии`);
  log(`=======================================================`);
  log(`   В Cline ставь:`);
  log(`   Base URL: http://127.0.0.1:${PROXY_PORT}/v1`);
  log(`   API Key:  any-key`);
  log(`   Model:    grok-4.5`);
  log(`=======================================================\n`);
});

process.on('uncaughtException', err => log(`🛡 Uncaught: ${err.message}`));
process.on('unhandledRejection', err => log(`🛡 Unhandled: ${err.message}`));
