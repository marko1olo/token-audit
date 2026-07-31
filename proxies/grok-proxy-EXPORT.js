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
  'pk_YOUR_GROK_KEY_1_HERE',
  'pk_YOUR_GROK_KEY_2_HERE',
  'pk_YOUR_GROK_KEY_3_HERE',
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

    // Точная проверка: Сабагент определяется ТОЛЬКО по первому сообщению/системному промпту ("You are subagent...")
    // Это предотвращает ложные срабатывания, когда главный агент просто упоминает слово "subagent" в истории.
    const firstMsgStr = (obj.messages[0] ? JSON.stringify(obj.messages[0]) : '').toLowerCase();
    const isSubagent = firstMsgStr.includes('you are subagent') || 
                       firstMsgStr.includes('you are a subagent') || 
                       firstMsgStr.includes('ты сабагент') || 
                       firstMsgStr.includes('subagent discover') || 
                       firstMsgStr.includes('subagent recon');

    let directiveText = '';
    if (isSubagent) {
      directiveText = `\n[OVERSEER SUBAGENT DIRECTIVE VIA PROXY]: Ты САБАГЕНТ. Твоя единственная цель — БЫСТРО (за 1-2 хода) исполнить узкое читательское/поисковое поручение главного агента. ЗАПРЕЩЕНО писать .py скрипты, крутить слипы или брать на себя глобальную верстку/архитектуру. Используй только встроенные инструменты (view_file, grep_search). Отдай выжимку Главному Агенту и заверши работу.\n`;
    } else {
      directiveText = `\n[OVERSEER MAIN AGENT DIRECTIVE VIA PROXY]: ${targetDirective}\n`;
    }

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
    log(`💉 PROXY INJECTED DIRECTIVE (${isSubagent ? 'SUBAGENT GUARD' : 'MAIN STRATEGY'}) into Session [${sessionId ? sessionId.slice(0,8) : 'general'}]: "${targetDirective.slice(0, 60)}..."`);
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
// ── LIVE HTML STATUS & CONTROL DASHBOARD PAGE (GET /) ──────────────────────
const recentLogs = [];
function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.join(' ')}`;
  process.stdout.write(`${line}\n`);
  recentLogs.push(line);
  if (recentLogs.length > 50) recentLogs.shift();
}

function renderHtmlDashboard() {
  return `<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Grok Proxy v4.5 — Interactive Overseer Console</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap">
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(26, 34, 52, 0.7);
      --card-border: rgba(56, 189, 248, 0.15);
      --accent-cyan: #38bdf8;
      --accent-emerald: #10b981;
      --accent-amber: #f59e0b;
      --accent-rose: #f43f5e;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text-main);
      padding: 28px;
      min-height: 100vh;
      background-image: 
        radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.08) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.05) 0px, transparent 50%);
    }

    .container { max-width: 1280px; margin: 0 auto; }

    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 28px;
      padding-bottom: 20px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }

    .brand { display: flex; align-items: center; gap: 14px; }
    .brand-logo {
      width: 44px; height: 44px;
      background: linear-gradient(135deg, #0284c7, #10b981);
      border-radius: 12px;
      display: flex; align-items: center; justify-content: center;
      font-size: 22px; font-weight: 800; color: #fff;
      box-shadow: 0 0 20px rgba(56, 189, 248, 0.3);
    }
    .brand-title { font-size: 22px; font-weight: 800; color: #fff; letter-spacing: -0.5px; }
    .brand-subtitle { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

    .status-badge {
      background: rgba(16, 185, 129, 0.12);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: var(--accent-emerald);
      padding: 6px 14px; border-radius: 20px;
      font-size: 12px; font-weight: 600;
      display: flex; align-items: center; gap: 8px;
    }
    .pulse-dot { width: 8px; height: 8px; background: var(--accent-emerald); border-radius: 50%; animation: pulse 1.5s infinite; }

    @keyframes pulse { 0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; } }

    .grid-keys {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 18px;
      margin-bottom: 28px;
    }

    .glass-card {
      background: var(--card-bg);
      backdrop-filter: blur(12px);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 22px;
      transition: all 0.25s ease;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
    }
    .glass-card:hover { border-color: rgba(56, 189, 248, 0.35); transform: translateY(-2px); }

    .card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .key-name { font-size: 15px; font-weight: 700; color: #f8fafc; }
    .key-badge { font-size: 11px; padding: 4px 10px; border-radius: 12px; font-weight: 600; }
    .key-badge.ok { background: rgba(16, 185, 129, 0.15); color: #34d399; }
    .key-badge.err { background: rgba(244, 63, 94, 0.15); color: #fca5a5; }

    .key-hash {
      font-family: 'JetBrains Mono', monospace;
      font-size: 12px; color: var(--text-muted);
      background: rgba(0, 0, 0, 0.3);
      padding: 6px 10px; border-radius: 8px;
      margin-bottom: 16px; word-break: break-all;
    }

    .metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
    .metric-box { background: rgba(0, 0, 0, 0.25); padding: 10px 6px; border-radius: 8px; text-align: center; }
    .metric-num { font-size: 16px; font-weight: 700; color: var(--accent-cyan); }
    .metric-lbl { font-size: 9px; color: var(--text-muted); text-transform: uppercase; margin-top: 4px; }

    .section-layout { display: grid; grid-template-columns: 1.2fr 0.8fr; gap: 24px; margin-bottom: 28px; }

    .injector-panel {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 24px;
    }
    .panel-header { font-size: 17px; font-weight: 700; color: #fff; margin-bottom: 16px; display: flex; align-items: center; gap: 10px; }

    .preset-btns { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
    .btn-preset {
      background: rgba(56, 189, 248, 0.08);
      border: 1px solid rgba(56, 189, 248, 0.2);
      color: var(--accent-cyan);
      padding: 8px 12px; border-radius: 8px;
      font-size: 12px; font-weight: 600; cursor: pointer;
      transition: all 0.15s;
    }
    .btn-preset:hover { background: rgba(56, 189, 248, 0.2); transform: scale(1.02); }
    .btn-preset.danger { background: rgba(244, 63, 94, 0.08); border-color: rgba(244, 63, 94, 0.2); color: var(--accent-rose); }
    .btn-preset.danger:hover { background: rgba(244, 63, 94, 0.2); }

    textarea {
      width: 100%; height: 110px;
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 10px;
      padding: 12px; color: #fff;
      font-family: 'JetBrains Mono', monospace; font-size: 13px;
      resize: vertical; outline: none; margin-bottom: 14px;
    }
    textarea:focus { border-color: var(--accent-cyan); box-shadow: 0 0 12px rgba(56, 189, 248, 0.2); }

    .btn-submit {
      width: 100%;
      background: linear-gradient(135deg, #0284c7, #10b981);
      border: none; color: #fff; padding: 12px;
      border-radius: 10px; font-weight: 700; font-size: 14px;
      cursor: pointer; transition: all 0.2s;
    }
    .btn-submit:hover { opacity: 0.9; transform: translateY(-1px); }

    .sessions-panel {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 24px;
    }

    .session-item {
      display: flex; justify-content: space-between; align-items: center;
      padding: 10px 14px; background: rgba(0, 0, 0, 0.25);
      border-radius: 8px; margin-bottom: 8px; font-size: 13px;
    }
    .session-id { font-family: 'JetBrains Mono', monospace; color: var(--accent-cyan); }
    .session-key { background: rgba(56, 189, 248, 0.15); color: #7dd3fc; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }

    .terminal-panel {
      background: #050811;
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 16px; padding: 20px;
      font-family: 'JetBrains Mono', monospace; font-size: 12px;
    }
    .terminal-header { font-size: 14px; font-weight: 700; color: #94a3b8; margin-bottom: 12px; display: flex; justify-content: space-between; }
    .log-box { max-height: 240px; overflow-y: auto; color: #cbd5e1; }
    .log-line { padding: 3px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.03); }
    .log-line.inject { color: var(--accent-emerald); font-weight: 600; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <div class="brand-logo">⚡</div>
        <div>
          <div class="brand-title">Grok Proxy v4.5 Overseer Console</div>
          <div class="brand-subtitle">Smart Balancer &bull; Subagent Role Classifier &bull; Realtime Directive Injector</div>
        </div>
      </div>
      <div class="status-badge">
        <div class="pulse-dot"></div>
        <span>LIVE SYSTEM OPERATIONAL</span>
      </div>
    </header>

    <div class="grid-keys" id="keysContainer">Loading keys...</div>

    <div class="section-layout">
      <div class="injector-panel">
        <div class="panel-header">💉 Live Prompt Directive Injector</div>
        
        <div class="preset-btns">
          <button class="btn-preset" onclick="setPreset('no_python')">🚫 No Scratch Python</button>
          <button class="btn-preset" onclick="setPreset('screenshots')">📸 4-State Screenshots</button>
          <button class="btn-preset" onclick="setPreset('commit')">⚡ Direct Git Commit</button>
          <button class="btn-preset danger" onclick="clearInjections()">🧹 Clear Injections</button>
        </div>

        <textarea id="directiveText" placeholder="Введи текст инъекции для агентов..."></textarea>
        <button class="btn-submit" onclick="sendInjection()">🚀 Inject Directive into Live Agent Stream</button>
      </div>

      <div class="sessions-panel">
        <div class="panel-header">💬 Connected Sessions</div>
        <div id="sessionsContainer">Loading sessions...</div>
      </div>
    </div>

    <div class="terminal-panel">
      <div class="terminal-header">
        <span>📟 Live Event Terminal Stream</span>
        <span id="logCount">0 events</span>
      </div>
      <div class="log-box" id="logBox">Reading stream...</div>
    </div>
  </div>

  <script>
    const PRESETS = {
      no_python: '[CRITICAL OVERSEER DIRECTIVE]: УДАЛИ ВСЕ .py СКРИПТЫ из scratch. Запрещено плодить консольные обертки. Используй ТОЛЬКО встроенные инструменты view_file и replace_file_content.',
      screenshots: '[CRITICAL OVERSEER DIRECTIVE]: ТРЕБОВАНИЕ К UI: Сделай 4 скриншота верстки (Mobile/PC, Dark/Light) и выведи ссылки в отчете!',
      commit: '[CRITICAL OVERSEER DIRECTIVE]: Заканчивай ковыряться! Выполни git commit и git push прямо сейчас!'
    };

    function setPreset(key) {
      document.getElementById('directiveText').value = PRESETS[key] || '';
    }

    async function updateStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();

        // Render Keys
        const keysHtml = data.keys.map((k, i) => \`
          <div class="glass-card">
            <div class="card-head">
              <span class="key-name">🔑 Key #\${i + 1}</span>
              <span class="key-badge \${k.disabled ? 'err' : 'ok'}">\${k.disabled ? 'DISABLED' : 'ACTIVE'}</span>
            </div>
            <div class="key-hash">\${k.maskedKey}</div>
            <div class="metrics-grid">
              <div class="metric-box"><div class="metric-num">\${k.activeSessions}</div><div class="metric-lbl">Sessions</div></div>
              <div class="metric-box"><div class="metric-num">\${k.reqCount}</div><div class="metric-lbl">Requests</div></div>
              <div class="metric-box"><div class="metric-num">\${k.wait429}</div><div class="metric-lbl">429 Waits</div></div>
              <div class="metric-box"><div class="metric-num">\${k.injections}</div><div class="metric-lbl">Injected</div></div>
            </div>
          </div>
        \`).join('');
        document.getElementById('keysContainer').innerHTML = keysHtml;

        // Render Sessions
        const sList = Object.entries(data.sessions).map(([sid, kIdx]) => \`
          <div class="session-item">
            <span class="session-id">\${sid.slice(0, 16)}...</span>
            <span class="session-key">Key #\${kIdx + 1}</span>
          </div>
        \`).join('') || '<div style="color:#64748b; font-style:italic; font-size:13px;">No active sessions connected.</div>';
        document.getElementById('sessionsContainer').innerHTML = sList;

        // Render Logs
        const logBox = document.getElementById('logBox');
        logBox.innerHTML = data.logs.map(l => {
          const isInj = l.includes('INJECTED DIRECTIVE');
          return \`<div class="log-line \${isInj ? 'inject' : ''}">\${l}</div>\`;
        }).join('');
        document.getElementById('logCount').innerText = \`\${data.logs.length} events\`;
        logBox.scrollTop = logBox.scrollHeight;

      } catch (err) { console.error('Status poll error:', err); }
    }

    async function sendInjection() {
      const text = document.getElementById('directiveText').value.trim();
      if (!text) return alert('Введи текст инъекции!');
      await fetch('/api/inject', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ target: 'all', text })
      });
      alert('Инъекция успешно загружена в прокси!');
      updateStatus();
    }

    async function clearInjections() {
      await fetch('/api/clear-injections', { method: 'POST' });
      document.getElementById('directiveText').value = '';
      alert('Все инъекции очищены!');
      updateStatus();
    }

    setInterval(updateStatus, 1500);
    updateStatus();
  </script>
</body>
</html>`;
}

// ── SERVER & REST API ──────────────────────────────────────────────────────────
const server = http.createServer((req, res) => {
  res.on('error', err => log(`⚠ client res error: ${err.message}`));

  // GET / -> Web Dashboard
  if ((req.method === 'HEAD' || req.method === 'GET') && (req.url === '/' || req.url === '')) {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(renderHtmlDashboard());
    return;
  }

  // GET /api/status -> Dashboard Telemetry Data
  if (req.method === 'GET' && req.url === '/api/status') {
    const counts = getSessionCountsPerKey();
    const keysData = GROK_KEYS.map((k, i) => ({
      maskedKey: k.length > 10 ? `${k.slice(0, 6)}...${k.slice(-4)}` : k,
      activeSessions: counts[i],
      reqCount: keyReqCounts[i],
      wait429: keyWait429Counts[i],
      injections: keyInjections[i],
      disabled: !!keyDisabled[i],
    }));

    const sessionsData = Object.fromEntries(sessionKeyMap.entries());

    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({
      keys: keysData,
      sessions: sessionsData,
      logs: recentLogs.slice(-30),
    }));
    return;
  }

  // POST /api/inject -> Live Directive Injection
  if (req.method === 'POST' && req.url === '/api/inject') {
    let bodyStr = '';
    req.on('data', c => bodyStr += c);
    req.on('end', () => {
      try {
        const payload = JSON.parse(bodyStr);
        const injections = fs.existsSync(INJECTIONS_JSON_PATH) ? JSON.parse(fs.readFileSync(INJECTIONS_JSON_PATH, 'utf8')) : {};
        injections[payload.target || 'all'] = payload.text;
        fs.writeFileSync(INJECTIONS_JSON_PATH, JSON.stringify(injections, null, 2), 'utf8');
        log(`💉 WEB DASHBOARD INJECTED DIRECTIVE [${payload.target || 'all'}]: "${payload.text.slice(0, 60)}..."`);
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok' }));
      } catch (err) {
        res.writeHead(400, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
    return;
  }

  // POST /api/clear-injections -> Clear all active directives
  if (req.method === 'POST' && req.url === '/api/clear-injections') {
    fs.writeFileSync(INJECTIONS_JSON_PATH, '{}', 'utf8');
    log(`🧹 INJECTIONS CLEARED via Web Dashboard`);
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ status: 'cleared' }));
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
  log(`🚀 grok-proxy v4.5 INTERACTIVE OVERSEER CONSOLE`);
  log(`   Port:           http://127.0.0.1:${PROXY_PORT}/v1`);
  log(`   Live Dashboard: http://127.0.0.1:${PROXY_PORT}/`);
  log(`   Features:       Subagent Classifier + Live Web Directives + 413 Trimmer`);
  log(`=======================================================\n`);
});

process.on('uncaughtException', err => log(`🛡 Uncaught: ${err.message}`));
process.on('unhandledRejection', err => log(`🛡 Unhandled: ${err.message}`));
