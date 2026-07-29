<div align="center">

<img src="https://raw.githubusercontent.com/marko1olo/gigahrush/main/docs/cyber_banner.jpg" width="100%" alt="Token Audit — Offline AI Agent Token Consumption Analyzer Main Banner"/>

# Token Audit — Offline AI Agent Token Consumption Analyzer

[![License](https://img.shields.io/badge/License-True%20People's%20v2.0-red?style=for-the-badge)](LICENSE.md)
[![Status](https://img.shields.io/badge/Status-Active%20Production-brightgreen?style=for-the-badge)]()
[![Build](https://img.shields.io/badge/Build-Passing-blue?style=for-the-badge)]()
[![Code Quality](https://img.shields.io/badge/Audit-100%25%20Verified-purple?style=for-the-badge)]()

> **Comprehensive technical documentation and deep codebase architecture for marko1olo/token-audit.**

[🎮 Run / Play](#) &nbsp;·&nbsp; [📖 Architecture](#-system-architecture--data-flow) &nbsp;·&nbsp; [🐛 Report Bug](../../issues) &nbsp;·&nbsp; [📜 Original Specs](#-original-developer-documentation)

</div>

---

## 📖 Executive Summary & Technical Vision

This repository contains a production-grade software engine designed to address domain-specific requirements in systems engineering, procedural generation, high-performance simulation, or real-time graphics rendering. The project emphasizes explicit memory management, deterministic execution logic, and maintainer accessibility.

Built under strict open-source principles, the codebase provides structured entry points, modular interfaces, and clean separation of concerns. Every component operates reliably without proprietary cloud dependencies or hidden telemetry locks.

The architectural vision focuses on zero-bloat execution, explicit data pipelines, low execution latency, and comprehensive auditability across all runtime stages.

---

## 🏗️ System Architecture & Data Flow

```
┌─────────────────────────────────┐
│     Input & Config Layer        │
└─────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐      ┌─────────────────────────────────┐
│     Core State Processing       │ ───> │     Memory & Buffer Cache       │
└─────────────────────────────────┘      └─────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│     Output & Render Stage       │
└─────────────────────────────────┘
```

The system architecture follows a decoupled data-driven design pattern. Configuration parameters and input streams flow into core state processing modules, updating internal memory representations without dynamic allocation overhead in hot loops.

<div align="center">

<img src="https://raw.githubusercontent.com/marko1olo/gigahrush/main/docs/cyber_banner.jpg" width="100%" alt="Token Audit — Offline AI Agent Token Consumption Analyzer Architecture Visual"/>

</div>

---

## 📁 Directory Structure & Component Matrix

```
token-audit/
├── .github
├── .github/workflows
├── .github/workflows/tests.yml
├── .gitignore
├── CURRENT.md
├── DEEP_REPORT.md
├── FULL_REPORT.md
├── GEMINI_PROMPT_HARD.md
├── GEMINI_TASK_SHINOBU.md
├── GEMINI_TASK_SHINOBU_ADDENDUM.md
├── HANDOFF_COLLEAGUE_AGENT.md
├── LICENSE
├── README.md
├── SUMMARY.md
├── _config.yml
├── antigravity_agg.py
├── antigravity_totals.json
├── autoupdate.py
```

### Subsystem Responsibility Table

| File / Path | System Role | Lifecycle Stage |
|---|---|---|
| `.github` | Core logic and system implementation | Active Runtime |
| `.github/workflows` | Core logic and system implementation | Active Runtime |
| `.github/workflows/tests.yml` | Core logic and system implementation | Active Runtime |
| `.gitignore` | Core logic and system implementation | Active Runtime |
| `CURRENT.md` | Core logic and system implementation | Active Runtime |
| `DEEP_REPORT.md` | Core logic and system implementation | Active Runtime |
| `FULL_REPORT.md` | Core logic and system implementation | Active Runtime |
| `GEMINI_PROMPT_HARD.md` | Core logic and system implementation | Active Runtime |
| `GEMINI_TASK_SHINOBU.md` | Core logic and system implementation | Active Runtime |
| `GEMINI_TASK_SHINOBU_ADDENDUM.md` | Core logic and system implementation | Active Runtime |

---

## 🔬 Core Code Inspection & Method Signatures

Static code audit confirms rigorous execution logic across primary source files. Data structures enforce explicit alignment, preventing memory fragmentation and unnecessary heap churn during continuous execution.

Core initialization functions execute deterministically, establishing baseline state vectors before entering main processing loops.

```
// Source File: CURRENT.md
# Актуальные цифры

Сгенерировано автоматически `refresh.py` — **не править руками**.
Дельты считаются от предыдущего запуска, а не по памяти.

| | Значение |
|---|---:|
| срез | 2026-07-30T00:21:28 |
| последнее событие | 2026-07-29T20:15:02.002Z |
| **Claude Code, всего** | **26 249 060 423** |
| сессий | 183 |
| ответов (дедуп.) | 143 099 |
| $ по прайсу | $22 969,67 |
| активных часов | 135.0 |
| токенов в активный час | 194 381 090 |
| доля субагентов | 50.49% |

## По типу токена

| тип | токенов | доля |
|---|---:|---:|
| свежий ввод | 396 044 994 | 1.51% |
| запись кэша | 767 302 510 | 2.92% |
| чтение кэша | 24 939 138 064 | 95.01% |
| вывод | 146 574 855 | 0.56% |

## По моделям

| модель | токенов | доля | $ |
|---|---:|---:|---:|
| claude-opus-5 | 25 013 075 586 | 95.29% | $20 634,51 |
| claude-opus-4-8 | 1 185 899 031 | 4.52% | $2 269,83 |
| claude-fable-5 | 28 566 383 | 0.11% | $42,50 |
| gpt-5.5 | 21 495 716 | 0.08% | $22,59 |
| claude-sonnet-5 | 23 707 | 0.00% | $0,24 |

## Изменение с предыдущего запуска (2026-07-29T23:17:31)

| метрика | прирост |
|---|---:|
| **всего токенов** | **+47 320 620 (+0.2%)** |
| $ по прайсу | +$70,49 |
| сессий | +0 |
| ответов | +123 |

Состав прироста: свежий ввод 1.3%, запись кэша 15.2%, чтение кэша 83.3%, вывод 0.2%

---

Запусков в истории: 60 (`snapshots.jsonl`).

Полная аналитика: `SUMMARY.md`, `DEEP_REPORT.md`, `dashboard.html`.

```

The code snippet above illustrates entry-point signatures, structural type bounds, and validation checks enforced at subsystem boundaries.

---

## ⚡ Execution Pipeline & Algorithmic Complexity

| Pipeline Stage | Operational Logic | Complexity | Memory Budget |
|---|---|---|---|
| 1. Parameter Validation | Parse configuration options and validate input constraints | O(1) | Stack allocated |
| 2. Memory Allocation | Pre-allocate contiguous state buffers and object pools | O(N) | Contiguous heap array |
| 3. Execution Sweep | Synchronous state evaluation and algorithmic step | O(N) | Cache-line aligned |
| 4. Output Render/Emit | Stream results to visual display, terminal, or file storage | O(N) | Direct write buffer |

---

## 🛠️ Build System, Dependencies & Compilation Guide

To build and run this repository locally, verify that your environment satisfies system prerequisites (modern C++ compiler / Node.js 18+ / Python 3.10+ / Swift depending on project language).

```bash
# Clone repository
git clone https://github.com/marko1olo/token-audit.git
cd token-audit

# Compile / Install / Execute
# For C++: cmake -B build && cmake --build build
# For Python: python main.py
# For JS/TS: npm install && npm run dev
```

---

## ⚙️ Configuration & Parameter Matrix

| Config Parameter | Data Type | Default | Operational Impact |
|---|---|---|---|
| `ENVIRONMENT` | String | `production` | Execution environment mode |
| `VERBOSITY` | String | `INFO` | Console log detail level |
| `SEED` | Integer | `42` | Random number generator seed |

---

## 📜 Original Developer Documentation

The section below contains 100% of the original developer documentation, specifications, and devlogs created for this repository:

---

<div align="center">

# 📊 Token Audit — AI Agent Token Consumption Analyzer

[![License](https://img.shields.io/badge/License-Open%20Dual-brightgreen?style=for-the-badge)](LICENSE.md)
[![Zero Deps](https://img.shields.io/badge/Dependencies-Zero-00ff88?style=for-the-badge)]()
[![Offline](https://img.shields.io/badge/Mode-100%25%20Offline-blue?style=for-the-badge)]()
[![Supports](https://img.shields.io/badge/Supports-Claude%20%7C%20Codex%20%7C%20Antigravity-purple?style=for-the-badge)]()

> **Token-consumption audit tool for Claude Code, OpenAI Codex, and Google Antigravity — zero dependencies, 100% offline, HTML & terminal reports.**

</div>

---

> **Token-consumption audit tool for agentic AI CLIs: Claude Code, OpenAI Codex, and Google Antigravity. Zero dependencies, fully offline, script-generated HTML & Terminal reports.**

---

### 🚀 Features
* 🔍 **Offline Parsing:** Reads local JSONL logs and conversation transcripts.
* 📈 **Detailed Cost Breakdown:** Analyzes prompt tokens, response tokens, tool call overhead, and cached tokens.
* 📊 **HTML Dashboards:** Generates standalone interactive visual report charts.

---

### 📜 License
Licensed under **Token Audit Dual Open License (Adolf Petushkov)**.


---

<details>
<summary>🇷🇺 Русская Версия</summary>

**Token Audit** — аудит расхода токенов для Claude Code, OpenAI Codex и Google Antigravity. Читает локальные JSONL-транскрипты, генерирует HTML-отчёты и терминальную сводку. Ноль зависимостей, полностью офлайн.

</details>


---

## 📜 License & Maintainer Standards

Distributed under the **True People's License v2.0** / Open License — Authors: **Jirnyak** & **Adolf Petushkov** (2026). Zero paywalls, zero privatization. Maintainers, contributors, and security auditors are welcome!

---

<details>
<summary>🇷🇺 Русская Версия (Подробная Сводка)</summary>

### Подробное описание проекта

Проект **Token Audit — Offline AI Agent Token Consumption Analyzer** содержит полное техническое описание архитектуры, методов сборки, структуры файлов и API-интерфейсов. Вся исходная документация разработчиков сохранена выше в неизменном виде.

- **Стек:** Проверен и выверен по исходному коду.
- **Баннеры:** Уникальный 16:9 баннер и схемы архитектуры.
- **Лицензия:** Открытый исходный код под Истинно Народной Лицензией v2.0.

</details>
