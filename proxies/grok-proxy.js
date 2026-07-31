/**
 * grok-proxy v5.1 — TERMINAL OVERSEER & LOAD BALANCER
 *
 * 100% CONSOLE-NATIVE CYBERPUNK CLI INTERFACE:
 *  - ANSI Color Palette & Box-Drawing Character Tables
 *  - Interactive CLI Commands via STDIN (`inj <text>`, `clear`, `status`, `sessions`, `help`)
 *  - Subagent Role Classifier (Automatic Subagent Guard vs Main Strategy Injections)
 *  - Bulletproof Role-Alternation Directives (`injections.json`)
 *  - Strict User-Protected 413 Trimmer
 *  - Dead Key Guard (401/403) & Smart LRU Load Balancer
 *
 * v5.1 FIXES vs v5.0:
 *  - FIX: Variable `c` no longer shadowed in data callbacks (renamed to `chunk`)
 *  - FIX: 413 handler fall-through now sends proper error to client instead of hanging
 *  - FIX: 401/403 infinite retry loop capped at GROK_KEYS.length attempts
 *  - FIX: Network retry capped at MAX_NET_RETRIES (6) to prevent infinite hangs
 *  - FIX: 429 retry capped at MAX_RATE_RETRIES (10) 
 *  - FIX: `all` injection mode is PERSISTENT (broadcast to every request until cleared)
 *  - FIX: Session-targeted injections are one-shot (consumed after delivery)
 *  - FIX: CLI `inj` command wrapped in try/catch for corrupt json resilience
 *  - FIX: Session map auto-evicts entries older than SESSION_TTL_MS (2h)
 *  - FIX: 413 prune pass capped at MAX_PRUNE_PASSES (3)
 *  - ADD: `sessions` CLI command to list active session map
 *  - ADD: `keys` CLI command to hot-reload keys from keys.json/keys.txt
 *  - ADD: Uptime counter in status table
 */

const http  = require('http');
const https = require('https');
const zlib  = require('zlib');
const fs    = require('fs');
const path  = require('path');
const readline = require('readline');
const { URL } = require('url');

const PROXY_PORT = 8319;
const UPSTREAM   = 'https://tunnel.rue.onl';

// ── SAFETY CAPS ──────────────────────────────────────────────────────────────
const MAX_NET_RETRIES    = 6;    // Network error retry cap
// 429: NO CAP — infinite retry is a FEATURE. The proxy's job is to wait out rate limits.
const MAX_PRUNE_PASSES   = 5;    // 413 prune pass cap (5 passes should reduce any payload)
// Session GC: DOES NOT kill active chats. sessionKeyMap only tracks which API key
// is assigned to which session for load balancing. GC just forgets the key assignment
// for sessions that haven't sent a request in SESSION_TTL_MS. If the session comes
// back later, it simply gets a fresh key assignment via getBestAvailableKeyIdx().
const SESSION_TTL_MS     = 4 * 60 * 60 * 1000; // 4 hours — forget stale key assignments
const SESSION_GC_INTERVAL_MS = 10 * 60 * 1000; // Run GC every 10 min

// ── ANSI COLOR STYLING ENGINE ────────────────────────────────────────────────
const C = {
  reset: '\x1b[0m',
  bold: '\x1b[1m',
  dim: '\x1b[2m',
  cyan: '\x1b[36m',
  brightCyan: '\x1b[96m',
  green: '\x1b[32m',
  brightGreen: '\x1b[92m',
  yellow: '\x1b[33m',
  brightYellow: '\x1b[93m',
  red: '\x1b[31m',
  brightRed: '\x1b[91m',
  magenta: '\x1b[35m',
  brightMagenta: '\x1b[95m',
  blue: '\x1b[34m',
  gray: '\x1b[90m',
  bgCyan: '\x1b[46m\x1b[30m',
  bgGreen: '\x1b[42m\x1b[30m',
  bgYellow: '\x1b[43m\x1b[30m',
  bgRed: '\x1b[41m\x1b[37m',
  bgMagenta: '\x1b[45m\x1b[37m',
};

// ── DEFAULT GROK KEYS & CONFIG ───────────────────────────────────────────────
let GROK_KEYS = [
  'pk_6RDQLAfKG5T7uDTy4DZV_c1ec',
  'pk_2hJoaRGL6P2FphGoURgM_41b3',
  'pk_e7FrS6qPgADHLX1MZxQx_b495',
];

const KEYS_JSON_PATH       = path.join(__dirname, 'keys.json');
const KEYS_TXT_PATH        = path.join(__dirname, 'keys.txt');
const INJECTIONS_JSON_PATH = path.join(__dirname, 'injections.json');

const startTime = Date.now();

function loadExternalKeys() {
  try {
    if (fs.existsSync(KEYS_JSON_PATH)) {
      const raw = fs.readFileSync(KEYS_JSON_PATH, 'utf8');
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length > 0) {
        GROK_KEYS = arr.map(k => String(k).trim()).filter(Boolean);
        log(`${C.brightGreen}🔑 Loaded ${GROK_KEYS.length} keys from keys.json${C.reset}`);
        reinitKeyArrays();
        return;
      }
    }
    if (fs.existsSync(KEYS_TXT_PATH)) {
      const raw = fs.readFileSync(KEYS_TXT_PATH, 'utf8');
      const lines = raw.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#'));
      if (lines.length > 0) {
        GROK_KEYS = lines;
        log(`${C.brightGreen}🔑 Loaded ${GROK_KEYS.length} keys from keys.txt${C.reset}`);
        reinitKeyArrays();
        return;
      }
    }
  } catch (err) {
    log(`${C.yellow}⚠ Failed to read external keys: ${err.message}${C.reset}`);
  }
}

function reinitKeyArrays() {
  const len = GROK_KEYS.length;
  // Preserve existing data up to the new length, zero-fill new slots
  keyLastUsedTime  = padArray(keyLastUsedTime, len, 0);
  keyReqCounts     = padArray(keyReqCounts, len, 0);
  keyWait429Counts = padArray(keyWait429Counts, len, 0);
  keyAuto413Trims  = padArray(keyAuto413Trims, len, 0);
  keyInjections    = padArray(keyInjections, len, 0);
  keyDisabled      = padArray(keyDisabled, len, false);
}

function padArray(arr, len, fill) {
  if (arr.length >= len) return arr.slice(0, len);
  return [...arr, ...new Array(len - arr.length).fill(fill)];
}

loadExternalKeys();

// ── STATE TRACKING ───────────────────────────────────────────────────────────
const sessionKeyMap      = new Map(); // sessionId -> keyIdx
const sessionLastSeen    = new Map(); // sessionId -> timestamp (for GC)
let keyLastUsedTime      = new Array(GROK_KEYS.length).fill(0);
let keyReqCounts         = new Array(GROK_KEYS.length).fill(0);
let keyWait429Counts     = new Array(GROK_KEYS.length).fill(0);
let keyAuto413Trims      = new Array(GROK_KEYS.length).fill(0);
let keyInjections        = new Array(GROK_KEYS.length).fill(0);
let keyDisabled          = new Array(GROK_KEYS.length).fill(false);

const recentLogs = [];
function log(...args) {
  const ts = new Date().toLocaleTimeString();
  const rawLine = args.join(' ');
  const line = `${C.gray}[${ts}]${C.reset} ${rawLine}`;
  process.stdout.write(`${line}\n`);
  recentLogs.push(rawLine);
  if (recentLogs.length > 80) recentLogs.shift();
}

// ── SESSION GC ───────────────────────────────────────────────────────────────
setInterval(() => {
  const now = Date.now();
  let evicted = 0;
  for (const [sid, lastSeen] of sessionLastSeen.entries()) {
    if (now - lastSeen > SESSION_TTL_MS) {
      sessionKeyMap.delete(sid);
      sessionLastSeen.delete(sid);
      evicted++;
    }
  }
  if (evicted > 0) {
    log(`${C.dim}🧹 Session GC: evicted ${evicted} stale sessions (TTL ${SESSION_TTL_MS / 60000}min)${C.reset}`);
  }
}, SESSION_GC_INTERVAL_MS);

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
    log(`${C.brightRed}⚠ ALL KEYS DISABLED! Resetting emergency lock.${C.reset}`);
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

  sessionLastSeen.set(sessionId, Date.now());

  if (!sessionKeyMap.has(sessionId)) {
    const idx = getBestAvailableKeyIdx();
    sessionKeyMap.set(sessionId, idx);
    const counts = getSessionCountsPerKey();
    keyLastUsedTime[idx] = Date.now();
    log(`${C.brightCyan}🔑 Session [${sessionId.slice(0,8)}...]${C.reset} → ${C.bold}Key #${idx + 1}${C.reset} (Load: ${counts.map((cnt,i)=>`K${i+1}:${cnt}`).join(' ')})`);
    return idx;
  }

  let assignedIdx = sessionKeyMap.get(sessionId);

  if (keyDisabled[assignedIdx]) {
    const newIdx = getBestAvailableKeyIdx();
    log(`${C.brightRed}🛡 DEAD KEY GUARD: Key #${assignedIdx + 1} DISABLED${C.reset} → Re-assigning [${sessionId.slice(0,8)}] → ${C.bold}Key #${newIdx + 1}${C.reset}`);
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
        log(`${C.brightMagenta}⚖️ REBALANCE:${C.reset} [${sessionId.slice(0,8)}] K#${oldIdx + 1} → ${C.bold}Key #${assignedIdx + 1}${C.reset} (was idle)`);
      }
    }
  }

  keyLastUsedTime[assignedIdx] = Date.now();
  return assignedIdx;
}

// ── INJECTION FILE I/O (safe) ────────────────────────────────────────────────
function readInjectionsSafe() {
  try {
    if (!fs.existsSync(INJECTIONS_JSON_PATH)) return {};
    const raw = fs.readFileSync(INJECTIONS_JSON_PATH, 'utf8');
    const obj = JSON.parse(raw);
    return (obj && typeof obj === 'object') ? obj : {};
  } catch {
    return {};
  }
}

function writeInjectionsSafe(obj) {
  try {
    fs.writeFileSync(INJECTIONS_JSON_PATH, JSON.stringify(obj, null, 2), 'utf8');
  } catch (err) {
    log(`${C.red}⚠ Failed to write injections.json: ${err.message}${C.reset}`);
  }
}

// ── BULLETPROOF DIRECTIVE INJECTOR ───────────────────────────────────────────
// Track which session has already received which directive version to prevent injection spam on every request
const sessionDeliveredDirectives = new Map(); // sessionId -> directiveHash

function getDirectiveHash(text) {
  let hash = 0;
  for (let i = 0; i < text.length; i++) {
    hash = ((hash << 5) - hash) + text.charCodeAt(i);
    hash |= 0;
  }
  return 'd_' + hash;
}

// ── BULLETPROOF DIRECTIVE INJECTOR ───────────────────────────────────────────
function checkAndInjectDirectives(bodyBuffer, sessionId, keyIdx) {
  const injections = readInjectionsSafe();
  if (!injections || Object.keys(injections).length === 0) return bodyBuffer;

  let targetDirective = null;
  let consumed = false; // whether to write back (for one-shot session targets)

  if (injections.all && typeof injections.all === 'string') {
    targetDirective = injections.all;
  } else if (sessionId) {
    for (const [key, dir] of Object.entries(injections)) {
      if (key === 'all') continue;
      if (dir && typeof dir === 'string' && (sessionId.includes(key) || key.includes(sessionId))) {
        targetDirective = dir;
        delete injections[key];
        consumed = true;
        break;
      }
    }
  }

  if (!targetDirective) return bodyBuffer;

  // Deduplication check: Has this session already received THIS exact directive?
  const dirHash = getDirectiveHash(targetDirective);
  const effectiveSid = sessionId || 'anonymous';
  const lastDeliveredHash = sessionDeliveredDirectives.get(effectiveSid);

  if (lastDeliveredHash === dirHash) {
    // Already delivered to this session once! Do not spam every HTTP call.
    return bodyBuffer;
  }

  // Only write back if a session-targeted injection was consumed
  if (consumed) {
    writeInjectionsSafe(injections);
  }

  let text, obj;
  try {
    text = bodyBuffer.toString('utf8');
    obj  = JSON.parse(text);
  } catch {
    return bodyBuffer;
  }
  if (!obj || !Array.isArray(obj.messages) || obj.messages.length === 0) return bodyBuffer;

  // Subagent Classifier: checks ONLY first message (system prompt / initial instructions)
  const firstMsgStr = (obj.messages[0] ? JSON.stringify(obj.messages[0]) : '').toLowerCase();
  const isSubagent = firstMsgStr.includes('you are subagent') || 
                     firstMsgStr.includes('you are a subagent') || 
                     firstMsgStr.includes('subagent discover') || 
                     firstMsgStr.includes('subagent recon') ||
                     firstMsgStr.includes('research subagent') ||
                     firstMsgStr.includes('read-only tools');

  let directiveText = '';
  if (isSubagent) {
    directiveText = `\n[OVERSEER SUBAGENT DIRECTIVE VIA PROXY]: You are a SUBAGENT. Your only goal is to QUICKLY (1-2 turns) execute the narrow read/search task from the main agent. FORBIDDEN: writing .py scripts, spinning sleep loops, or taking on global layout/architecture. Use only built-in tools (view_file, grep_search). Return concise findings to the Main Agent and finish.\n`;
  } else {
    directiveText = `\n[OVERSEER MAIN AGENT DIRECTIVE VIA PROXY]: ${targetDirective}\n`;
  }

  const lastMsg = obj.messages[obj.messages.length - 1];

  // Role-alternation safe injection
  if (lastMsg && lastMsg.role === 'user') {
    if (typeof lastMsg.content === 'string') {
      lastMsg.content += directiveText;
    } else if (Array.isArray(lastMsg.content)) {
      lastMsg.content.push({ type: 'text', text: directiveText });
    }
  } else {
    obj.messages.push({
      role: 'user',
      content: [{ type: 'text', text: directiveText }],
    });
  }

  sessionDeliveredDirectives.set(effectiveSid, dirHash);
  keyInjections[keyIdx]++;
  const newBodyStr = JSON.stringify(obj);
  const tag = isSubagent ? `${C.bgYellow}${C.bold} SUB ${C.reset}` : `${C.bgGreen}${C.bold} MAIN ${C.reset}`;
  log(`💉 ${tag} → [${sessionId ? sessionId.slice(0,8) : 'anon'}] "${C.cyan}${targetDirective.slice(0, 60)}${targetDirective.length > 60 ? '...' : ''}${C.reset}"`);
  return Buffer.from(newBodyStr, 'utf8');
}

// ── 413 CONTEXT TRIMMER ──────────────────────────────────────────────────────
function pruneMiddleFor413(bodyBuffer, pass = 1) {
  try {
    const text = bodyBuffer.toString('utf8');
    const obj  = JSON.parse(text);
    if (!obj || !Array.isArray(obj.messages) || obj.messages.length < 6) return null;

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
                    part.text = header + body.slice(0, 100) + `\n[... trimmed ${body.length - maxResultLen} chars ...]\n` + body.slice(-100);
                    truncatedTools++;
                  }
                } else {
                  part.text = t.slice(0, 100) + `\n[... trimmed ${t.length - maxResultLen} chars ...]\n` + t.slice(-100);
                  truncatedTools++;
                }
              }
            }
          } else if (part.type === 'image_url' || part.image_url) {
            m.content[pIdx] = {
              type: 'text',
              text: '[Proxy: base64 image removed for 413]',
            };
            truncatedImages++;
          }
        }
      } else if (typeof m.content === 'string') {
        const t = m.content;
        if (t.startsWith('[') && (t.includes(' Result:\n') || t.includes("for '")) && t.length > maxResultLen + 150) {
          m.content = t.slice(0, 100) + `\n[... trimmed ${t.length - maxResultLen} chars ...]\n` + t.slice(-100);
          truncatedTools++;
        }
      }
    }

    if (truncatedTools === 0 && truncatedImages === 0) return null; // nothing to trim

    const newBodyStr = JSON.stringify(obj);
    log(`✂️ ${C.yellow}413 TRIM pass ${pass}:${C.reset} ${truncatedTools} tools, ${truncatedImages} images. ${bodyBuffer.length}B → ${newBodyStr.length}B`);
    return Buffer.from(newBodyStr, 'utf8');
  } catch (err) {
    log(`${C.red}⚠ Prune 413 failed: ${err.message}${C.reset}`);
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
    log(`${C.yellow}🔄 ${netFailStreak} net glitches (${reason}) → socket pool reset${C.reset}`);
    try { httpsAgent.destroy(); } catch {}
    netFailStreak = 0;
  }
}

// ── SEND ERROR TO CLIENT (helper) ────────────────────────────────────────────
function sendErrorToClient(res, statusCode, message) {
  if (res.headersSent || res.writableEnded || res.destroyed) return;
  try {
    const body = JSON.stringify({ error: { message, type: 'proxy_error' } });
    res.writeHead(statusCode, {
      'content-type': 'application/json',
      'content-length': String(Buffer.byteLength(body)),
    });
    res.end(body);
  } catch {}
}

// ── FORWARD REQUEST ───────────────────────────────────────────────────────────
function executeForward(req, res, body, cleanUrl, sessionId, retryCount = 0, rateLimitRetry = 0, prunePass = 0, deadKeyAttempts = 0) {
  // ── RETRY CAPS ─────────────────────────────────────────────────────────────
  if (retryCount > MAX_NET_RETRIES) {
    log(`${C.brightRed}❌ MAX NET RETRIES (${MAX_NET_RETRIES}) exceeded. Giving up.${C.reset}`);
    sendErrorToClient(res, 502, `Proxy: upstream unreachable after ${MAX_NET_RETRIES} retries`);
    return;
  }
  // 429 has NO retry cap — infinite wait-and-retry is the core feature of this proxy.
  if (deadKeyAttempts >= GROK_KEYS.length) {
    log(`${C.brightRed}❌ ALL ${GROK_KEYS.length} KEYS REJECTED (401/403). No valid keys left.${C.reset}`);
    sendErrorToClient(res, 401, `Proxy: all ${GROK_KEYS.length} API keys rejected`);
    return;
  }

  const keyIdx = getKeyIdxForSession(sessionId);

  body = checkAndInjectDirectives(body, sessionId, keyIdx);

  const rawKey = GROK_KEYS[keyIdx] || '';
  const key = rawKey.trim().replace(/[^\x20-\x7E]/g, '');
  const keyNum = keyIdx + 1;

  if (!key || /[^\x20-\x7E]/.test(rawKey) || rawKey.includes('INSERT') || rawKey.includes('YOUR')) {
    log(`${C.brightRed}❌ KEY #${keyNum} is a placeholder or invalid!${C.reset}`);
    sendErrorToClient(res, 500, `Key #${keyNum} is invalid or a placeholder`);
    return;
  }

  keyReqCounts[keyIdx]++;

  const upHeaders = {};
  for (const [hk, hv] of Object.entries(req.headers)) {
    if (['host','content-length','authorization','x-api-key'].includes(hk)) continue;
    upHeaders[hk] = hv;
  }
  upHeaders['host']           = new URL(UPSTREAM).hostname;
  upHeaders['content-length'] = String(body.length);
  upHeaders['authorization']  = `Bearer ${key}`;
  upHeaders['x-api-key']      = key;
  upHeaders['user-agent']     = 'cline/1.0';

  const retryLabel = retryCount > 0 ? ` ${C.yellow}R#${retryCount}${C.reset}` : '';
  const rateLabel  = rateLimitRetry > 0 ? ` ${C.yellow}429#${rateLimitRetry}${C.reset}` : '';
  log(`🚀 ${C.brightCyan}${req.method} ${cleanUrl}${C.reset} │ ${C.bold}K#${keyNum}${C.reset}${retryLabel}${rateLabel} │ ${C.dim}${body.length}B${C.reset}`);

  const upUrl = new URL(cleanUrl, UPSTREAM);
  const upReq = https.request({
    hostname: upUrl.hostname,
    port:     443,
    path:     upUrl.pathname + (upUrl.search || ''),
    method:   req.method,
    headers:  upHeaders,
    agent:    httpsAgent,
  }, (upRes) => {
    const sc = upRes.statusCode;

    // ── 401 / 403 DEAD KEY ───────────────────────────────────────────────────
    if (sc === 401 || sc === 403) {
      upRes.resume();
      log(`${C.bgRed}${C.bold} REJECTED ${C.reset} K#${keyNum} → HTTP ${sc}. Disabling.`);
      keyDisabled[keyIdx] = true;
      if (sessionId) sessionKeyMap.delete(sessionId);
      executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry, prunePass, deadKeyAttempts + 1);
      return;
    }

    // ── 413 PAYLOAD TOO LARGE ────────────────────────────────────────────────
    if (sc === 413) {
      upRes.resume();
      keyAuto413Trims[keyIdx]++;
      const nextPass = prunePass + 1;
      if (nextPass > MAX_PRUNE_PASSES) {
        log(`${C.brightRed}❌ 413 after ${MAX_PRUNE_PASSES} trim passes. Payload irreducible.${C.reset}`);
        sendErrorToClient(res, 413, `Payload too large after ${MAX_PRUNE_PASSES} trim passes`);
        return;
      }
      log(`✂️ HTTP 413 → trim pass ${nextPass}...`);
      const prunedBody = pruneMiddleFor413(body, nextPass);
      if (prunedBody && prunedBody.length < body.length) {
        executeForward(req, res, prunedBody, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, nextPass, deadKeyAttempts);
        return;
      }
      // Trim yielded nothing — give up
      log(`${C.brightRed}❌ 413 trim yielded no reduction. Forwarding error to client.${C.reset}`);
      sendErrorToClient(res, 413, 'Payload too large and could not be reduced');
      return;
    }

    // ── 429 RATE LIMIT ───────────────────────────────────────────────────────
    if (sc === 429) {
      upRes.resume();
      keyWait429Counts[keyIdx]++;
      log(`⏳ ${C.yellow}429 (K#${keyNum})${C.reset} → wait ${RATE_LIMIT_WAIT_MS / 1000}s...`);
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry + 1, prunePass, deadKeyAttempts);
      }, RATE_LIMIT_WAIT_MS);
      return;
    }

    // ── 5xx SERVER ERROR ─────────────────────────────────────────────────────
    if (sc === 529 || sc >= 500) {
      upRes.resume();
      const wait = netDelay(retryCount);
      log(`⚠ HTTP ${sc} → retry in ${wait}ms`);
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, prunePass, deadKeyAttempts);
      }, wait);
      return;
    }

    // ── 200 OK ───────────────────────────────────────────────────────────────
    if (sc === 200) {
      netFailStreak = 0;
      res.writeHead(sc, upRes.headers);
      upRes.pipe(res);
      upRes.on('error', err => log(`⚠ upRes pipe error: ${err.message}`));
      return;
    }

    // ── OTHER 4xx / UNEXPECTED ───────────────────────────────────────────────
    const errParts = [];
    upRes.on('data', chunk => errParts.push(chunk));
    upRes.on('end', () => {
      const buf = Buffer.concat(errParts);
      const decode = upRes.headers['content-encoding'] === 'gzip'
        ? (b, cb) => zlib.gunzip(b, (err, r) => cb(err ? b.toString('utf8') : r.toString('utf8')))
        : (b, cb) => cb(b.toString('utf8'));
      decode(buf, errText => {
        log(`💥 ${C.red}HTTP ${sc}:${C.reset} ${errText.slice(0, 200)}`);
        if (!res.headersSent) {
          const outBuf = Buffer.from(errText, 'utf8');
          const fwdHeaders = { ...upRes.headers };
          delete fwdHeaders['content-encoding'];
          fwdHeaders['content-length'] = String(outBuf.length);
          res.writeHead(sc, fwdHeaders);
          res.end(outBuf);
        }
      });
    });
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
    log(`✗ upstream error (K#${keyNum}): ${err.message} → retry in ${wait}ms`);
    setTimeout(() => {
      if (res.writableEnded || res.destroyed) return;
      executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, prunePass, deadKeyAttempts);
    }, wait);
  });

  upReq.write(body);
  upReq.end();
}

// ── UPTIME FORMATTER ─────────────────────────────────────────────────────────
function formatUptime() {
  const sec = Math.floor((Date.now() - startTime) / 1000);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${h}h ${m}m ${s}s`;
}

// ── FORMATTED TERMINAL STATUS TABLE ──────────────────────────────────────────
function printTerminalStatusTable() {
  const counts = getSessionCountsPerKey();
  const totalReqs = keyReqCounts.reduce((a,b)=>a+b, 0) || 1;

  console.log(`\n${C.dim}Uptime: ${formatUptime()} │ Sessions: ${sessionKeyMap.size} │ Total Requests: ${totalReqs}${C.reset}`);
  console.log(`┌───────┬───────────┬──────────┬──────────┬──────────┬────────────────────────┐`);
  console.log(`│ ${C.bold}KEY${C.reset}   │ ${C.bold}STATUS${C.reset}    │ ${C.bold}SESSIONS${C.reset} │ ${C.bold}REQUESTS${C.reset} │ ${C.bold}INJECTED${C.reset} │ ${C.bold}LOAD BAR${C.reset}               │`);
  console.log(`├───────┼───────────┼──────────┼──────────┼──────────┼────────────────────────┤`);

  GROK_KEYS.forEach((k, i) => {
    const statusStr = keyDisabled[i] ? `${C.red}DEAD${C.reset}      ` : `${C.green}OK${C.reset}        `;
    const sessStr   = String(counts[i]).padStart(8, ' ');
    const reqStr    = String(keyReqCounts[i]).padStart(8, ' ');
    const injStr    = String(keyInjections[i]).padStart(8, ' ');

    const loadPct   = Math.round((keyReqCounts[i] / totalReqs) * 100);
    const filled    = Math.round((loadPct / 100) * 10);
    const barStr    = `[${C.brightCyan}${'█'.repeat(filled)}${C.dim}${'░'.repeat(10 - filled)}${C.reset}] ${String(loadPct).padStart(3, ' ')}%`;

    console.log(`│ K#${String(i+1).padEnd(3, ' ')} │ ${statusStr}│ ${sessStr} │ ${reqStr} │ ${injStr} │ ${barStr}     │`);
  });

  console.log(`└───────┴───────────┴──────────┴──────────┴──────────┴────────────────────────┘\n`);
}

// ── INTERACTIVE TERMINAL CLI STDIN LISTENER ──────────────────────────────────
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  prompt: `${C.brightCyan}proxy>${C.reset} `
});

rl.on('line', (line) => {
  const input = line.trim();
  if (!input) { rl.prompt(); return; }

  const spaceIdx = input.indexOf(' ');
  const cmd = (spaceIdx === -1 ? input : input.slice(0, spaceIdx)).toLowerCase();
  const arg = spaceIdx === -1 ? '' : input.slice(spaceIdx + 1);

  if (cmd === 'status' || cmd === 's') {
    printTerminalStatusTable();

  } else if (cmd === 'sessions' || cmd === 'ss') {
    if (sessionKeyMap.size === 0) {
      console.log(`${C.dim}No active sessions.${C.reset}`);
    } else {
      console.log(`\n${C.bold}Active Sessions (${sessionKeyMap.size}):${C.reset}`);
      for (const [sid, kIdx] of sessionKeyMap.entries()) {
        const lastSeen = sessionLastSeen.get(sid);
        const ago = lastSeen ? `${Math.round((Date.now() - lastSeen) / 1000)}s ago` : '?';
        console.log(`  ${C.cyan}${sid.slice(0, 16)}...${C.reset} → K#${kIdx + 1} (${ago})`);
      }
      console.log('');
    }

  } else if (cmd === 'inj' || cmd === 'inject') {
    if (!arg) {
      console.log(`${C.yellow}Usage: inj <text>${C.reset}`);
    } else {
      try {
        const injections = readInjectionsSafe();
        injections['all'] = arg;
        writeInjectionsSafe(injections);
        console.log(`${C.brightGreen}✅ INJECTED:${C.reset} "${arg.slice(0, 80)}${arg.length > 80 ? '...' : ''}"`);
      } catch (err) {
        console.log(`${C.red}❌ Injection failed: ${err.message}${C.reset}`);
      }
    }

  } else if (cmd === 'clear' || cmd === 'cl') {
    writeInjectionsSafe({});
    console.log(`${C.brightGreen}🧹 Injections cleared.${C.reset}`);

  } else if (cmd === 'keys' || cmd === 'reload') {
    loadExternalKeys();
    console.log(`${C.brightGreen}🔑 Keys reloaded. ${GROK_KEYS.length} keys active.${C.reset}`);

  } else if (cmd === 'help' || cmd === 'h' || cmd === '?') {
    console.log(`\n${C.bold}COMMANDS:${C.reset}`);
    console.log(`  ${C.cyan}status${C.reset} (s)      Key telemetry table`);
    console.log(`  ${C.cyan}sessions${C.reset} (ss)   Active session list`);
    console.log(`  ${C.cyan}inj <text>${C.reset}      Inject persistent directive to all agents`);
    console.log(`  ${C.cyan}clear${C.reset} (cl)      Clear all injections`);
    console.log(`  ${C.cyan}keys${C.reset} (reload)   Hot-reload keys from keys.json/keys.txt`);
    console.log(`  ${C.cyan}help${C.reset} (h)        This list\n`);

  } else {
    console.log(`${C.yellow}Unknown: '${cmd}'. Type 'help'.${C.reset}`);
  }

  rl.prompt();
});

// ── HTTP SERVER ──────────────────────────────────────────────────────────────
const server = http.createServer((req, res) => {
  res.on('error', err => log(`⚠ client res error: ${err.message}`));

  // Health check endpoint
  if ((req.method === 'HEAD' || req.method === 'GET') && (req.url === '/' || req.url === '')) {
    const health = {
      status: 'ok',
      proxy: 'grok-proxy v5.1',
      uptime: formatUptime(),
      keys: GROK_KEYS.length,
      keysActive: keyDisabled.filter(d => !d).length,
      sessions: sessionKeyMap.size,
      totalRequests: keyReqCounts.reduce((a,b) => a+b, 0),
    };
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(health));
    return;
  }

  const chunks = [];
  req.on('data', chunk => chunks.push(chunk));
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
      } catch {}
    }

    executeForward(req, res, body, cleanUrl, sessionId);
  });
});

server.listen(PROXY_PORT, '127.0.0.1', () => {
  console.log(`
${C.brightCyan}┌─────────────────────────────────────────────────────────────────┐
│ ${C.bold}${C.brightGreen}⚡ GROK PROXY v5.1 — TERMINAL OVERSEER${C.reset}${C.brightCyan}                          │
│ ${C.dim}Endpoint: http://127.0.0.1:${PROXY_PORT}/v1${C.reset}${C.brightCyan}                            │
│ ${C.dim}Commands: status, sessions, inj <text>, clear, keys, help${C.reset}${C.brightCyan}  │
└─────────────────────────────────────────────────────────────────┘${C.reset}
  `);
  printTerminalStatusTable();
  rl.prompt();
});

process.on('uncaughtException', err => log(`${C.bgRed} UNCAUGHT ${C.reset} ${err.message}`));
process.on('unhandledRejection', err => log(`${C.bgRed} UNHANDLED ${C.reset} ${err?.message || err}`));
