# 📊 Token Audit — LLM Token Cost Waterfall & BPE Telemetry Suite

[![Live Demo](https://img.shields.io/badge/Live_Showcase-GitHub_Pages-ffb700?style=for-the-badge&logo=github)](https://marko1olo.github.io/token-audit/)
[![PWA Ready](https://img.shields.io/badge/PWA-Installable-22c55e?style=for-the-badge&logo=pwa)](https://marko1olo.github.io/token-audit/manifest.json)
[![AI Index](https://img.shields.io/badge/LLM_Search-llms.txt-38bdf8?style=for-the-badge)](https://marko1olo.github.io/token-audit/llms.txt)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.6-3178C6?style=for-the-badge&logo=typescript)](https://www.typescriptlang.org/)
[![Web Audio](https://img.shields.io/badge/Web_Audio-DSP_HUD-a855f7?style=for-the-badge)](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API)

An interactive developer telemetry suite and BPE tokenizer cost waterfall analyzer for tracking prompt caching economics, Time-to-First-Token (TTFT) distributions, and context window footprint across multi-model AI agent swarms.

---

## 🏛️ Cost Waterfall Architecture

```mermaid
graph TD
    Prompt[Raw Agent Context Prompt] -->|BPE Tokenizer cl100k / o200k| Toks[Token Sequence]
    Toks --> CacheCheck{Prompt Cache Hit?}
    CacheCheck -->|Yes (90% Cost Reduction)| CachePrice[Cached Rate: $0.30 / MTok]
    CacheCheck -->|No| FullPrice[Uncached Input: $3.00 / MTok]
    Toks --> OutputGen[Streaming Generation] --> OutPrice[Output Rate: $15.00 / MTok]
```

---

## 🔬 Key Telemetry Tools

1. **BPE Token Highlighter:** Live token boundary visualizer with byte-length inspection and token ID resolution.
2. **Cost Waterfall Simulator:** Comparative financial modeler across Claude 3.7 Sonnet, Opus 4.8, GPT-5, and DeepSeek-V3.
3. **Real-Time Stream Engine:** Dynamic tok/s throughput monitor with TTFT latency distributions.
4. **Cyberpunk Chronicles:** Comprehensive case studies on token wastage, context compaction, and multi-agent coordination.

---

### 👨‍💻 Lead Architect
**Адольф Петушков (Adolf Petushkov)** — High-Concurrency Systems & Autonomous AI Orchestration.  
GitHub: [@marko1olo](https://github.com/marko1olo)
