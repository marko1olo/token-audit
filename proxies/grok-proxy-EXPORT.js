/**
 * grok-proxy v5.0 — TERMINAL OVERSEER & LOAD BALANCER
 *
 * 100% CONSOLE-NATIVE CYBERPUNK CLI INTERFACE:
 *  - ANSI Color Palette & Box-Drawing Character Tables
 *  - Interactive CLI Commands via STDIN (`inj <text>`, `clear`, `status`, `help`)
 *  - Subagent Role Classifier (Automatic Subagent Guard vs Main Strategy Injections)
 *  - Bulletproof Role-Alternation Directives (`injections.json`)
 *  - Strict User-Protected 413 Trimmer
 *  - Dead Key Guard (401/403) & Smart LRU Load Balancer
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

// ── ANSI COLOR STYLING ENGINE ────────────────────────────────────────────────
const c = {
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
        log(`${c.brightGreen}🔑 Loaded ${GROK_KEYS.length} keys from keys.json${c.reset}`);
        return;
      }
    }
    if (fs.existsSync(KEYS_TXT_PATH)) {
      const raw = fs.readFileSync(KEYS_TXT_PATH, 'utf8');
      const lines = raw.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#'));
      if (lines.length > 0) {
        GROK_KEYS = lines;
        log(`${c.brightGreen}🔑 Loaded ${GROK_KEYS.length} keys from keys.txt${c.reset}`);
        return;
      }
    }
  } catch (err) {
    log(`${c.yellow}⚠ Failed to read external keys: ${err.message}${c.reset}`);
  }
}

loadExternalKeys();

// ── STATE TRACKING ───────────────────────────────────────────────────────────
const sessionKeyMap    = new Map();
let keyLastUsedTime    = new Array(GROK_KEYS.length).fill(0);
let keyReqCounts       = new Array(GROK_KEYS.length).fill(0);
let keyWait429Counts   = new Array(GROK_KEYS.length).fill(0);
let keyAuto413Trims    = new Array(GROK_KEYS.length).fill(0);
let keyInjections      = new Array(GROK_KEYS.length).fill(0);
let keyDisabled        = new Array(GROK_KEYS.length).fill(false);

const recentLogs = [];
function log(...args) {
  const ts = new Date().toLocaleTimeString();
  const rawLine = args.join(' ');
  const line = `${c.gray}[${ts}]${c.reset} ${rawLine}`;
  process.stdout.write(`${line}\n`);
  recentLogs.push(rawLine);
  if (recentLogs.length > 60) recentLogs.shift();
}

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
    log(`${c.brightRed}⚠ ALL KEYS DISABLED! Resetting emergency lock.${c.reset}`);
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
    log(`${c.brightCyan}🔑 Session [${sessionId.slice(0,8)}...]${c.reset} → ${c.bold}Key #${idx + 1}${c.reset} (Load: ${counts.map((cnt,i)=>`K${i+1}:${cnt}`).join(' ')})`);
    return idx;
  }

  let assignedIdx = sessionKeyMap.get(sessionId);

  if (keyDisabled[assignedIdx]) {
    const newIdx = getBestAvailableKeyIdx();
    log(`${c.brightRed}🛡 DEAD KEY GUARD: Key #${assignedIdx + 1} DISABLED${c.reset} → Re-assigning [${sessionId.slice(0,8)}] → ${c.bold}Key #${newIdx + 1}${c.reset}`);
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
        log(`${c.brightMagenta}⚖️ SMART REBALANCE:${c.reset} [${sessionId.slice(0,8)}] moved K#${oldIdx + 1} → ${c.bold}Key #${assignedIdx + 1}${c.reset} (0 sessions)!`);
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

    // Subagent Classifier
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

    keyInjections[keyIdx]++;
    const newBodyStr = JSON.stringify(obj);
    const tag = isSubagent ? `${c.bgYellow}${c.bold} SUBAGENT GUARD ${c.reset}` : `${c.bgGreen}${c.bold} MAIN STRATEGY ${c.reset}`;
    log(`💉 ${tag} Injected into Session [${sessionId ? sessionId.slice(0,8) : 'general'}]: "${c.cyan}${targetDirective.slice(0, 50)}...${c.reset}"`);
    return Buffer.from(newBodyStr, 'utf8');
  } catch (err) {
    log(`${c.red}⚠ Directive Injection error: ${err.message}${c.reset}`);
    return bodyBuffer;
  }
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
    log(`✂️ ${c.yellow}STRICT 413 TRIM (Pass ${pass}):${c.reset} ${truncatedTools} tools & ${truncatedImages} images trimmed. ${bodyBuffer.length}B → ${newBodyStr.length}B`);
    return Buffer.from(newBodyStr, 'utf8');
  } catch (err) {
    log(`${c.red}⚠ Prune 413 failed: ${err.message}${c.reset}`);
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
    log(`${c.yellow}🔄 ${netFailStreak} network glitches (${reason}) → Resetting socket pool${c.reset}`);
    try { httpsAgent.destroy(); } catch {}
    netFailStreak = 0;
  }
}

// ── FORWARD REQUEST ───────────────────────────────────────────────────────────
function executeForward(req, res, body, cleanUrl, sessionId, retryCount = 0, rateLimitRetry = 0, prunePass = 0) {
  const keyIdx = getKeyIdxForSession(sessionId);

  body = checkAndInjectDirectives(body, sessionId, keyIdx);

  let rawKey = GROK_KEYS[keyIdx] || '';
  const key = rawKey.trim().replace(/[^\x20-\x7E]/g, '');
  const keyNum = keyIdx + 1;

  if (!key || /[^\x20-\x7E]/.test(rawKey) || rawKey.includes('ВСТАВЬ')) {
    log(`${c.brightRed}❌ KEY ERROR: Key #${keyNum} is invalid!${c.reset}`);
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

  const retryLabel = retryCount > 0 ? ` ${c.yellow}RETRY#${retryCount}${c.reset}` : '';
  const rateLabel  = rateLimitRetry > 0 ? ` ${c.yellow}429-WAIT#${rateLimitRetry}${c.reset}` : '';
  log(`🚀 ${c.brightCyan}POST ${cleanUrl}${c.reset} │ ${c.bold}Key #${keyNum}${c.reset}${retryLabel}${rateLabel} │ ${c.dim}${body.length}B${c.reset}`);

  const upUrl = new URL(cleanUrl, UPSTREAM);
  const upReq = https.request({
    hostname: upUrl.hostname,
    port:     443,
    path:     upUrl.pathname + (upUrl.search || ''),
    method:   req.method,
    headers:  upHeaders,
    agent:    httpsAgent,
  }, (upRes) => {
    log(`↑ ${c.brightGreen}HTTP ${upRes.statusCode}${c.reset} (Key #${keyNum})`);

    if (upRes.statusCode === 401 || upRes.statusCode === 403) {
      upRes.resume();
      log(`${c.bgRed}${c.bold} KEY REJECTED ${c.reset} Key #${keyNum} returned HTTP ${upRes.statusCode}! Disabling Key #${keyNum}.`);
      keyDisabled[keyIdx] = true;
      if (sessionId) sessionKeyMap.delete(sessionId);
      executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry, prunePass);
      return;
    }

    if (upRes.statusCode === 413) {
      upRes.resume();
      keyAuto413Trims[keyIdx]++;
      const nextPass = prunePass + 1;
      log(`✂️ HTTP 413 Payload Too Large → Trim pass ${nextPass} & retry...`);
      const prunedBody = pruneMiddleFor413(body, nextPass);
      if (prunedBody && prunedBody.length < body.length) {
        executeForward(req, res, prunedBody, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, nextPass);
        return;
      }
    }

    if (upRes.statusCode === 429) {
      upRes.resume();
      keyWait429Counts[keyIdx]++;
      log(`⏳ ${c.yellow}429 Rate Limit (Key #${keyNum})${c.reset} → Waiting 20s on same key...`);
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, sessionId, retryCount, rateLimitRetry + 1, prunePass);
      }, RATE_LIMIT_WAIT_MS);
      return;
    }

    if (upRes.statusCode === 529 || upRes.statusCode >= 500) {
      const wait = netDelay(retryCount);
      log(`⚠ HTTP ${upRes.statusCode} → Retry in ${wait}ms`);
      upRes.resume();
      setTimeout(() => {
        if (res.writableEnded || res.destroyed) return;
        executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, prunePass);
      }, wait);
      return;
    }

    if (upRes.statusCode !== 200) {
      const parts = [];
      upRes.on('data', c => parts.push(c));
      upRes.on('end', () => {
        const buf = Buffer.concat(parts);
        const decode = upRes.headers['content-encoding'] === 'gzip'
          ? (b, cb) => zlib.gunzip(b, (err, r) => cb(err ? b.toString('utf8') : r.toString('utf8')))
          : (b, cb) => cb(b.toString('utf8'));
        decode(buf, text => {
          log(`💥 ${c.red}ERROR ${upRes.statusCode}:${c.reset} ${text.slice(0, 200)}`);
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
    log(`✗ upstream error (Key #${keyNum}): ${err.message} → retry in ${wait}ms`);
    setTimeout(() => {
      if (res.writableEnded || res.destroyed) return;
      executeForward(req, res, body, cleanUrl, sessionId, retryCount + 1, rateLimitRetry, prunePass);
    }, wait);
  });

  upReq.write(body);
  upReq.end();
}

// ── FORMATTED TERMINAL STATUS TABLE ──────────────────────────────────────────
function printTerminalStatusTable() {
  const counts = getSessionCountsPerKey();
  console.log(`\n┌─────────────────────────────────────────────────────────────────────────────┐`);
  console.log(`│ ${c.bold}${c.brightCyan}⚡ GROK PROXY v5.0 TERMINAL OVERSEER & BALANCER TELEMETRY${c.reset}               │`);
  console.log(`├───────┬───────────┬──────────┬──────────┬──────────┬────────────────────────┤`);
  console.log(`│ ${c.bold}KEY   │ STATUS    │ SESSIONS │ REQUESTS │ INJECTED │ LOAD BAR               │`);
  console.log(`├───────┼───────────┼──────────┼──────────┼──────────┼────────────────────────┤`);

  GROK_KEYS.forEach((k, i) => {
    const statusStr = keyDisabled[i] ? `${c.red}⛔ OFF${c.reset}    ` : `${c.green}✅ OK${c.reset}     `;
    const sessStr   = String(counts[i]).padStart(8, ' ');
    const reqStr    = String(keyReqCounts[i]).padStart(8, ' ');
    const injStr    = String(keyInjections[i]).padStart(8, ' ');

    const totalReqs = keyReqCounts.reduce((a,b)=>a+b, 0) || 1;
    const loadPct   = Math.round((keyReqCounts[i] / totalReqs) * 100);
    const filled    = Math.round((loadPct / 100) * 10);
    const barStr    = '[' + '█'.repeat(filled) + '░'.repeat(10 - filled) + '] ' + String(loadPct).padStart(3, ' ') + '%';

    console.log(`│ Key #${i+1}│ ${statusStr}│ ${sessStr} │ ${reqStr} │ ${injStr} │ ${barStr}     │`);
  });

  console.log(`└───────┴───────────┴──────────┴──────────┴──────────┴────────────────────────┘\n`);
}

// ── INTERACTIVE TERMINAL CLI STDIN LISTENER ──────────────────────────────────
const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  prompt: `${c.brightCyan}grok-proxy> ${c.reset}`
});

rl.on('line', (line) => {
  const input = line.trim();
  if (!input) { rl.prompt(); return; }

  const parts = input.split(' ');
  const cmd = parts[0].toLowerCase();
  const arg = parts.slice(1).join(' ');

  if (cmd === 'status' || cmd === 's') {
    printTerminalStatusTable();
  } else if (cmd === 'inj' || cmd === 'inject') {
    if (!arg) {
      console.log(`${c.yellow}Usage: inj <text>  (Injects text into live agents)${c.reset}`);
    } else {
      const injections = fs.existsSync(INJECTIONS_JSON_PATH) ? JSON.parse(fs.readFileSync(INJECTIONS_JSON_PATH, 'utf8')) : {};
      injections['all'] = arg;
      fs.writeFileSync(INJECTIONS_JSON_PATH, JSON.stringify(injections, null, 2), 'utf8');
      console.log(`${c.brightGreen}✅ DIRECTIVE INJECTED VIA CLI:${c.reset} "${arg}"`);
    }
  } else if (cmd === 'clear' || cmd === 'c') {
    fs.writeFileSync(INJECTIONS_JSON_PATH, '{}', 'utf8');
    console.log(`${c.brightGreen}🧹 ALL INJECTIONS CLEARED VIA CLI.${c.reset}`);
  } else if (cmd === 'help' || cmd === 'h' || cmd === '?') {
    console.log(`\n${c.bold}AVAILABLE CLI COMMANDS:${c.reset}`);
    console.log(`  ${c.cyan}status (s)${c.reset}     - Print key telemetry table & load bar`);
    console.log(`  ${c.cyan}inj <text>${c.reset}     - Inject directive into live agents immediately`);
    console.log(`  ${c.cyan}clear (c)${c.reset}      - Clear all active injections`);
    console.log(`  ${c.cyan}help (h)${c.reset}       - Show this command list\n`);
  } else {
    console.log(`${c.yellow}Unknown command '${cmd}'. Type 'help' for available CLI commands.${c.reset}`);
  }

  rl.prompt();
});

// ── HTTP SERVER ──────────────────────────────────────────────────────────────
const server = http.createServer((req, res) => {
  res.on('error', err => log(`⚠ client res error: ${err.message}`));

  if ((req.method === 'HEAD' || req.method === 'GET') && (req.url === '/' || req.url === '')) {
    res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ status: 'ok', proxy: 'grok-proxy v5.0 Terminal Overseer', keys: GROK_KEYS.length }));
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
      } catch {}
    }

    executeForward(req, res, body, cleanUrl, sessionId);
  });
});

server.listen(PROXY_PORT, '127.0.0.1', () => {
  console.log(`
${c.brightCyan}┌─────────────────────────────────────────────────────────────────────────────┐
│ ${c.bold}${c.brightGreen}⚡ GROK PROXY v5.0 — TERMINAL OVERSEER & SMART BALANCER${c.reset}${c.brightCyan}                     │
│ ${c.dim}Port: http://127.0.0.1:${PROXY_PORT}/v1  │ CLI Commands: inj <text>, clear, status${c.reset}${c.brightCyan} │
└─────────────────────────────────────────────────────────────────────────────┘${c.reset}
  `);
  printTerminalStatusTable();
  rl.prompt();
});

process.on('uncaughtException', err => log(`🛡 Uncaught: ${err.message}`));
process.on('unhandledRejection', err => log(`🛡 Unhandled: ${err.message}`));
