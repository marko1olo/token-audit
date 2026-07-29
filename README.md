<div align="center">

# 📊 Token Audit — AI Agent Token Consumption Analyzer

[![License](https://img.shields.io/badge/License-Open%20Dual%20EN%2FRU-brightgreen?style=for-the-badge)](LICENSE.md)
[![Zero Deps](https://img.shields.io/badge/Dependencies-Zero-00ff88?style=for-the-badge)]()
[![Offline](https://img.shields.io/badge/Mode-100%25%20Offline-blue?style=for-the-badge)]()
[![Supports](https://img.shields.io/badge/Supports-Claude%20%7C%20Codex%20%7C%20Antigravity-purple?style=for-the-badge)]()

> **Token-consumption audit tool for agentic AI CLIs — Claude Code, OpenAI Codex, and Google Antigravity. Zero dependencies. 100% offline. HTML & terminal reports from local transcripts.**

</div>

---

## 📖 About

Every AI agent burns tokens. Most developers have no idea how many, or where they go. **Token Audit** parses local conversation transcripts from Claude Code, OpenAI Codex, and Google Antigravity/Gemini — generating detailed breakdowns of token usage, estimated costs, and waste patterns — entirely offline with zero external API calls.

---

## ✨ Features

- 🔍 **Offline Parsing** — reads local JSONL transcript files, no API calls
- 📈 **Detailed Breakdown** — prompt tokens, response tokens, tool call overhead, cache hits
- 💸 **Cost Estimation** — maps token usage to current LLM pricing per model
- 📊 **HTML Dashboard** — standalone interactive report with charts (no server needed)
- 🖥️ **Terminal Report** — quick CLI summary for scripting and CI pipelines
- 🤖 **Multi-Agent Support** — Claude Code, OpenAI Codex CLI, Google Antigravity (Gemini)

---

## 🚀 Quick Start

```bash
git clone https://github.com/marko1olo/token-audit.git
cd token-audit

# Analyze Claude Code transcripts
node audit.js --tool claude --log ~/.claude/projects/

# Analyze Antigravity transcripts
node audit.js --tool antigravity --log %APPDATA%/Antigravity/brain/

# Generate HTML report
node audit.js --output report.html
```

---

## 📜 License

**Token Audit Dual Open License** — Adolf Petushkov (c) 2026. See [LICENSE.md](LICENSE.md).

---

<details>
<summary>🇷🇺 Русская Версия</summary>

**Token Audit** — инструмент аудита расхода токенов для агентных ИИ-инструментов: Claude Code, OpenAI Codex и Google Antigravity. Читает локальные JSONL-транскрипты, генерирует HTML-отчёты и терминальную сводку. Ноль зависимостей, полностью офлайн.

</details>
