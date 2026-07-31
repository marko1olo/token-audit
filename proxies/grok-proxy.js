/**
 * grok-proxy v4.1 — BULLETPROOF LIVE DIRECTIVE INJECTOR & BALANCER
 *
 * Обработка всех эдж-кейсов инъекций (v4.1):
 *  1. Снижение риска нарушения чередования ролей OpenAI (Role Alternation):
 *     - Если последнее сообщение в `messages` уже имеет роль `user`, директива
 *       не создаёт отдельный элемент, а ВНЕДРЯЕТСЯ прямо внутрь последнего `user`-сообщения!
 *     - Если последнее сообщение имеет роль `assistant`, директива создаёт новое `user`-сообщение.
 *     - Это ГАРАНТИРУЕТ 100% соответствие строгому правилу чередования ролей OpenAI/Grok!
 *  2. Атомарное чтение injections.json (защита от race condition при параллельной записи).
 *  3. Strict User-Protected 413 Trimmer (v3.5)
 *  4. Live Web UI Dashboard (http://127.0.0.1:8319/)
 *  5. Dead Key Guard (401/403) + Smart LRU Balancer
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

const KEYS_JSON_PATH       = path.join(__dirname, 'keys.json');
const KEYS_TXT_PATH        = path.join(__dirname, 'keys.txt');
const INJECTIONS_JSON_PATH = path.join(__dirname, 'injections.json');

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
      const lines = raw.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#'));
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
let keyAuto413Trims  = new Array(GROK_KEYS.length).fill(0);
let keyInjections    = new Array(GROK_KEYS.length).fill(0);
let keyDisabled      = new Array(GROK_KEYS.length).fill(false);

function getSessionCountsPerKey() {
  const counts = new Array(GROK_KEYS.length).fill(0);
  for (const [sid, kIdx] of sessionKeyMap.entries()) {
    if (kIdx >= 0 && kIdx < GROK_KEYS.length && !keyDisabled[kIdx]) {
      counts[kIdx]++;
    }
  }
  return counts;
}

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

  if (!sessionKeyMap.has(sessionId)) {
    const idx = getBestAvailableKeyIdx();
    sessionKeyMap.set(sessionId, idx);
    const counts = getSessionCountsPerKey();
    keyLastUsedTime[idx] = Date.now();
    log(`🔑 New Session [${sessionId.slice(0,8)}...] → Assigned Key #${idx + 1} (Load: ${counts.map((c,i)=>`K${i+1}:${c}`).join(' ')})`);
    return idx;
  }

  let assignedIdx = sessionKeyMap.get(sessionId);

  if (keyDisabled[assignedIdx]) {
    const newIdx = getBestAvailableKeyIdx();
    log(`🛡 DEAD KEY GUARD: Key #${assignedIdx + 1} is DISABLED → Re-assigning Session [${sessionId.slice(0,8)}] to Key #${newIdx + 1}`);
    assignedIdx = newIdx;
    sessionKeyMap.set(sessionId, assignedIdx);
  } else {
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

// ── BULLETPROOF DIRECTIVE INJECTOR ───────────────────────────────────────────
function checkAndInjectDirectives(bodyBuffer, sessionId, keyIdx) {
  if (!fs.existsSync(INJECTIONS_JSON_PATH)) return bodyBuffer;

  try {
    const raw = fs.readFileSync(INJECTIONS_JSON_PATH, 'utf8');
    const injections = JSON.parse(raw);
    if (!injections || typeof injections !== 'object') return bodyBuffer;

    let targetDirective = null;

    if (injections.all && typeof injections.all === 'string') {
      targetDirective = injections.all;
      delete injections.all;
    } else if (sessionId) {
      for (const [key, dir] of Object.entries(injections)) {
        if (dir && typeof dir === 'string' && (sessionId.includes(key) || key.includes(sessionId))) {
          targetDirective = dir;
          delete injections[key];
          break;
        }
      }
    }

    if (!targetDirective) return bodyBuffer;

    fs.writeFileSync(INJECTIONS_JSON_PATH, JSON.stringify(injections, null, 2), 'utf8');

    const text = bodyBuffer.toString('utf8');
    const obj  = JSON.parse(text);
    if (!obj || !Array.isArray(obj.messages) || obj.messages.length === 0) return bodyBuffer;

    const directiveText = `\n[OVERSEER SYSTEM DIRECTIVE VIA PROXY]: ${targetDirective}\n`;
    const lastMsg = obj.messages[obj.messages.length - 1];

    // ЭДЖ-КЕЙС #1: Если последнее сообщение уже имеет роль 'user', внедряем внутрь него!
    if (lastMsg && lastMsg.role === 'user') {
      if (typeof lastMsg.content === 'string') {
        lastMsg.content += directiveText;
      } else if (Array.isArray(lastMsg.content)) {
        lastMsg.content.push({ type: 'text', text: directiveText });
      }
    } else {
      // Иначе создаём новое user-сообщение
      obj.messages.push({
        role: 'user',
        content: [{ type: 'text', text: directiveText }],
      });
    }

    keyInjections[keyIdx]++;
    const newBodyStr = JSON.stringify(obj);
    log(`💉 PROXY INJECTED DIRECTIVE into Session [${sessionId ? sessionId.slice(0,8) : 'general'}]: "${targetDirective.slice(0, 80)}..."`);
    return Buffer.from(newBodyStr, 'utf8');
  } catch (err) {
    log(`⚠ Directive Injection error: ${err.message}`);
    return bodyBuffer;
  }
}

// ── STRICT USER-PROTECTED 413 CONTEXT TRIMMER ────────────────────────────────
function pruneMiddleFor413(bodyBuffer, pass = 1) {
  try {
    const text = bodyBuffer.toString('utf8');
    const obj  = JSON.parse(text);
    if (!obj || !Array.isArray(obj.messages) || obj.messages.length < 6) {
      return null;
    }
    const msgs = obj.messages;

    const tailStartIdx = Math.max(2, msgs.length - 6);
    let truncatedTools = 0;
    let truncatedImages = 0;
    const maxResultLen = pass === 1 ? 300 : 100;

    for (let i = 2; i < tailStartIdx; i++) {
      const m = msgs[i];
      if (!m || !m.content) continue;

      if (Array.isArray(m.content)) {
        for (let pIdx = 0; pIdx < m.content.length; pIdx++) {
          const part = m.content[pIdx];
          if (!part) continue;

          if (part.type === 'text' && typeof part.text === 'string') {
            const t = part.text;
            if (t.startsWith('[') && (t.includes(' Result:\n') || t.includes("for '"))) {
              if (t.length > maxResultLen + 150) {
                const resIdx = t.indexOf(' Result:\n');
                if (resIdx !== -1) {
                  const header = t.slice(0, resIdx + 9);
                  const body   = t.slice(resIdx + 9);
                  if (body.length > maxResultLen) {
                    part.text = header + body.slice(0, 100) + `\n[... Proxy truncated ${body.length - maxResultLen} chars of tool output on 413 ...]\n` + body.slice(-100);
                    truncatedTools++;
                  }
                } else {
                  part.text = t.slice(0, 100) + `\n[... Proxy truncated ${t.length - maxResultLen} chars of tool output on 413 ...]\n` + t.slice(-100);
                  truncatedTools++;
                }
              }
            }
          } else if (part.type === 'image_url' || part.image_url) {
            m.content[pIdx] = {
              type: 'text',
              text: '[Proxy: Past base64 screenshot truncated to resolve 413 Payload Too Large]',
            };
            truncatedImages++;
          }
        }
      } else if (typeof m.content === 'string') {
        const t = m.content;
        if (t.startsWith('[') && (t.includes(' Result:\n') || t.includes("for '")) && t.length > maxResultLen + 150) {
          m.content = t.slice(0, 100) + `\n[... Proxy truncated ${t.length - maxResultLen} chars of tool output on 413 ...]\n` + t.slice(-100);
          truncatedTools++;
        }
      }
    }

    const newBodyStr = JSON.stringify(obj);
    log(`✂️ STRICT USER-PROTECTED 413 TRIM (Pass ${pass}): Truncated ${truncatedTools} tool outputs & ${truncatedImages} base64 images. Size: ${bodyBuffer.length} → ${newBodyStr.length} bytes.`);
    return Buffer.from(newBodyStr, 'utf8');
  } catch (err) {
    log(`⚠ Prune 413 failed: ${err.message}`);
    return null;
  }
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
function executeForward(req, res, body, cleanUrl, sessionId, retryCount = 0, rateLimitRetry = 0, prunePass = 0) {
  const keyIdx = getKeyIdxForSession(sessionId);

  // Внедрение директив из injections.json (если есть)
  body = checkAndInjectDirectives(body, sessionId, keyIdx);

  let rawKey = GROK_KEYS[keyIdx] || '';
  const key = rawKey.trim().replace(/[^\x20-\x7E]/g, '');
  const keyNum = keyIdx + 1;

  if (!key || /[^\x20-\x7E]/.test(rawKey) || rawKey.includes('ВСТАВЬ')) {
    log(`❌ ОШИБКА: Ключ #${keyNum} невалиден!`);
    if (!res.headersSent) {
      res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: `Key #${keyNum} is invalid.` } }));
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
  log(`→ ${req.method} ${cleanUrl} (Key #${keyNum}${retryLabel}${rateLabel}) [${body.length}B]`);

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

    // ── 401 / 403 INVALID / EXPIRED KEY ─────────────────────────────────────
    if (upRes.statusCode === 401 || upRes.statusCode === 403) {
      upRes.resume();
      log(`⛔ KEY REJECTED: Key #${keyNum} returned HTTP ${upRes.statusCode}! Marking Key #${keyNum} as DISABLED.`);
      keyDisabled[keyIdx] = true;
      if (sessionId) sessionKeyMap.delete(sessionId);
      log(`🔄 Retrying request on another active key...`);
      executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry, prunePass);
      return;
    }

    // ── 413 PAYLOAD TOO LARGE ───────────────────────────────────────────────
    if (upRes.statusCode === 413) {
      upRes.resume();
      keyAuto413Trims[keyIdx]++;
      const nextPass = prunePass + 1;
      log(`✂️ HTTP 413 Payload Too Large (Key #${keyNum}) → Reactively trimming tool output dumps (Pass ${nextPass}) & retrying...`);
      const prunedBody = pruneMiddleFor413(body, nextPass);
      if (prunedBody && prunedBody.length < body.length) {
        executeForward(req, res, prunedBody, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, nextPass);
        return;
      }
    }

    // ── 429 RATE LIMIT ──────────────────────────────────────────────────────
    if (upRes.statusCode === 429) {
      upRes.resume();
      keyWait429Counts[keyIdx]++;
      log(`⏳ 429 Rate Limit на Key #${keyNum} → ждём ${RATE_LIMIT_WAIT_MS/1000}с, повторяю НА ТОМ ЖЕ ключе`);
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry + 1, prunePass);
      }, RATE_LIMIT_WAIT_MS);
      return;
    }

    // ── 5xx Server Error ────────────────────────────────────────────────────
    if (upRes.statusCode === 529 || upRes.statusCode >= 500) {
      const wait = netDelay(retryCount);
      log(`⚠ HTTP ${upRes.statusCode} → повтор через ${wait}мс`);
      upRes.resume();
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, prunePass);
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
      executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, prunePass);
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
            <span class="m-val">${keyInjections[i]}</span>
            <span class="m-lbl">Injections</span>
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
  <title>Grok Proxy v4.1 — Live Status</title>
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
      <div class="title">🚀 Grok Proxy v4.1 Live Status</div>
      <div class="subtitle">Bulletproof Live Directive Injector &bull; Strict User-Protected 413 Trimmer &bull; Smart LRU &bull; Dead Key Guard</div>
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
  log(`🚀 grok-proxy v4.1 BULLETPROOF INJECTOR & BALANCER`);
  log(`   Port:       http://127.0.0.1:${PROXY_PORT}/v1`);
  log(`   Live Dashboard: http://127.0.0.1:${PROXY_PORT}/`);
  log(`   Features:   Bulletproof Directive Injector + 413 Trimmer + Smart LRU`);
  log(`=======================================================\n`);
});

process.on('uncaughtException', err => log(`🛡 Uncaught: ${err.message}`));
process.on('unhandledRejection', err => log(`🛡 Unhandled: ${err.message}`));
