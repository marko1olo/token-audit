/**
 * grok-proxy v2.3 — SMART BALANCER & STICKY RETRY
 *
 * Локальный прокси для Cline / Roo-Code под Grok API (tunnel.rue.onl).
 *
 * Особенности v2.3:
 *  1. Умная балансировка диалогов по ключам:
 *     - Новая сессия выбирает ключ с НАИМЕНЬШИМ количеством активных сессий (при равенстве — LRU).
 *     - Если 2 диалога случайно оказались на одном ключе, а 3-й ключ пустует —
 *       прокси АВТОМАТИЧЕСКИ ПЕРЕНОСИТ диалог на свободный ключ на следующем запросе!
 *  2. Сохраняет STICKY status для равномерно распределенных диалогов.
 *  3. 429 → ждём 20с → повторяем НА ТОМ ЖЕ ключе без вылета ошибок.
 *  4. Защита от не-ASCII / кириллицы в ключах.
 */

const http  = require('http');
const https = require('https');
const zlib  = require('zlib');
const { URL } = require('url');

const PROXY_PORT = 8319;
const UPSTREAM   = 'https://tunnel.rue.onl';

// ── ВСТАВЬ СВОИ КЛЮЧИ СЮДА ───────────────────────────────────────────────────
const GROK_KEYS = [
  'pk_ВСТАВЬ_СВОЙ_КЛЮЧ_1_СЮДА',
  'pk_ВСТАВЬ_СВОЙ_КЛЮЧ_2_СЮДА',
];
// ─────────────────────────────────────────────────────────────────────────────

const RATE_LIMIT_WAIT_MS = 20000;

// ── SMART LOAD BALANCER & STICKY SESSIONS ────────────────────────────────────
const sessionKeyMap = new Map();
const keyLastUsedTime = new Array(GROK_KEYS.length).fill(0);

function getSessionCountsPerKey() {
  const counts = new Array(GROK_KEYS.length).fill(0);
  for (const [sid, kIdx] of sessionKeyMap.entries()) {
    if (kIdx >= 0 && kIdx < GROK_KEYS.length) counts[kIdx]++;
  }
  return counts;
}

function getBestAvailableKeyIdx() {
  const counts = getSessionCountsPerKey();
  let minCount = Math.min(...counts);
  const candidates = [];
  for (let i = 0; i < GROK_KEYS.length; i++) {
    if (counts[i] === minCount) candidates.push(i);
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

  if (!sessionKeyMap.has(sessionId)) {
    const idx = getBestAvailableKeyIdx();
    sessionKeyMap.set(sessionId, idx);
    const counts = getSessionCountsPerKey();
    keyLastUsedTime[idx] = Date.now();
    log(`🔑 New Session [${sessionId.slice(0,8)}...] -> Assigned Key #${idx + 1} (Load: ${counts.map((c,i)=>`K${i+1}:${c}`).join(' ')})`);
    return idx;
  }

  let assignedIdx = sessionKeyMap.get(sessionId);
  const counts = getSessionCountsPerKey();

  // Автоматическая ребалансировка если наш ключ перегружен (>1 сессии), а рядом есть абсолютно свободный ключ (0 сессий)
  if (counts[assignedIdx] > 1) {
    const freeIdx = counts.indexOf(0);
    if (freeIdx !== -1) {
      const oldIdx = assignedIdx;
      assignedIdx = freeIdx;
      sessionKeyMap.set(sessionId, assignedIdx);
      const newCounts = getSessionCountsPerKey();
      log(`⚖️ SMART REBALANCE: Session [${sessionId.slice(0,8)}...] moved from Key #${oldIdx + 1} (${counts[oldIdx]} sessions) -> Key #${assignedIdx + 1} (0 sessions)! New Load: ${newCounts.map((c,i)=>`K${i+1}:${c}`).join(' ')}`);
    }
  }

  keyLastUsedTime[assignedIdx] = Date.now();
  return assignedIdx;
}

const NET_DELAYS_MS = [2000, 2000, 4000, 8000, 15000, 30000];
const netDelay = (n) => Math.round(NET_DELAYS_MS[Math.min(n, NET_DELAYS_MS.length - 1)] * (0.75 + Math.random() * 0.5));

const httpsAgent = new https.Agent({ keepAlive: true, maxSockets: 50, maxFreeSockets: 4, keepAliveMsecs: 5000 });

function log(...args) { process.stdout.write(`[${new Date().toISOString()}] ${args.join(' ')}\n`); }

function executeForward(req, res, body, cleanUrl, sessionId, retryCount = 0, rateLimitRetry = 0) {
  const keyIdx = getKeyIdxForSession(sessionId);
  let rawKey = GROK_KEYS[keyIdx] || '';
  const key = rawKey.trim().replace(/[^\x20-\x7E]/g, '');
  const keyNum = keyIdx + 1;

  if (!key || /[^\x20-\x7E]/.test(rawKey) || rawKey.includes('ВСТАВЬ')) {
    log(`❌ ОШИБКА: Ключ #${keyNum} невалиден или содержит не-ASCII символы / кириллицу!`);
    log(`👉 Замени '${rawKey.slice(0, 30)}...' на реальный API-ключ в GROK_KEYS в grok-proxy.js`);
    if (!res.headersSent) {
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: `Key #${keyNum} is invalid or contains placeholder/cyrillic text. Set real API keys in GROK_KEYS array inside grok-proxy.js` } }));
    }
    return;
  }

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

  const upUrl = new URL(cleanUrl, UPSTREAM);
  const upReq = https.request({
    hostname: upUrl.hostname, port: 443, path: upUrl.pathname + (upUrl.search || ''),
    method: req.method, headers: upHeaders, agent: httpsAgent,
  }, (upRes) => {
    log(`↑ HTTP ${upRes.statusCode} (Key #${keyNum})`);

    if (upRes.statusCode === 429) {
      upRes.resume();
      log(`⏳ 429 Rate Limit -> ждём 20с, повторяем на том же ключе (попытка #${rateLimitRetry + 1})`);
      setTimeout(() => {
        if (!res.writableEnded && !res.destroyed) executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry + 1);
      }, RATE_LIMIT_WAIT_MS);
      return;
    }

    if (upRes.statusCode === 529 || upRes.statusCode >= 500) {
      const wait = netDelay(retryCount);
      upRes.resume();
      setTimeout(() => {
        if (!res.writableEnded && !res.destroyed) executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry);
      }, wait);
      return;
    }

    if (upRes.statusCode !== 200) {
      const parts = [];
      upRes.on('data', c => parts.push(c));
      upRes.on('end', () => {
        const buf = Buffer.concat(parts);
        const decode = upRes.headers['content-encoding'] === 'gzip' 
          ? (b, cb) => zlib.gunzip(b, (e, r) => cb(e ? b.toString('utf8') : r.toString('utf8')))
          : (b, cb) => cb(b.toString('utf8'));
        decode(buf, text => {
          log(`💥 ERROR ${upRes.statusCode}: ${text.slice(0, 300)}`);
          if (!res.headersSent) {
            const outBuf = Buffer.from(text, 'utf8');
            const h = { ...upRes.headers }; delete h['content-encoding']; h['content-length'] = String(outBuf.length);
            res.writeHead(upRes.statusCode, h); res.end(outBuf);
          }
        });
      });
      return;
    }

    res.writeHead(upRes.statusCode, upRes.headers);
    upRes.pipe(res);
  });

  upReq.on('socket', (sock) => {
    if (sock.connecting) { sock.setTimeout(15000); sock.once('connect', () => sock.setTimeout(90000)); } 
    else { sock.setTimeout(90000); }
  });
  upReq.on('timeout', () => { upReq.destroy(new Error('socket timeout')); });
  upReq.on('error', err => {
    const wait = netDelay(retryCount);
    setTimeout(() => {
      if (!res.writableEnded && !res.destroyed) executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry);
    }, wait);
  });
  upReq.write(body);
  upReq.end();
}

const server = http.createServer((req, res) => {
  if ((req.method === 'HEAD' || req.method === 'GET') && (req.url === '/' || req.url === '')) {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ status: 'ok', keys: GROK_KEYS.length }));
    return;
  }
  const chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => {
    const body = Buffer.concat(chunks);
    let sessionId = req.headers['x-session-id'] || req.headers['x-claude-code-session-id'] || null;
    if (body.length > 0) {
      try {
        const obj = JSON.parse(body.toString('utf8'));
        if (!sessionId && obj.metadata?.user_id) {
          try { const uid = JSON.parse(obj.metadata.user_id); if (uid.session_id) sessionId = uid.session_id; } catch {}
        }
      } catch {}
    }
    executeForward(req, res, body, req.url, sessionId);
  });
});

server.listen(PROXY_PORT, '127.0.0.1', () => {
  log(`🚀 Proxy v2.3 SMART BALANCER running on http://127.0.0.1:${PROXY_PORT}/v1`);
});
