# 🛠️ Contributing to marko1olo/token-audit

> **Engineering Mandate, Architectural Invariants & Contribution Standard**  
> Maintained by the **Жирняк & Адольф Петушков** Engineering Syndicate  
> Technology Foundation: `TypeScript 5.8 / Vite / WebAssembly tiktoken / Vanilla CSS`

---

## 📑 Table of Contents
1. [🏛️ Architectural Overview & Data Flow](#️-1-architectural-overview--data-flow)
2. [📐 Strict Domain Invariants](#-2-strict-domain-invariants)
3. [💻 Development Toolchain & Local Environment](#-3-development-toolchain--local-environment)
4. [🧪 Testing Strategy & Verification Pipeline](#-4-testing-strategy--verification-pipeline)
5. [💎 Code Standards & Anti-Patterns](#-5-code-standards--anti-patterns)
6. [🚀 Pull Request Protocol & Review Workflow](#-6-pull-request-protocol--review-workflow)
7. [👥 Syndicate Governance & Attribution](#-7-syndicate-governance--attribution)

---

## 🏛️ 1. Architectural Overview & Data Flow

Token Audit LLM Token Cost Waterfall & BPE Tokenizer Visualizer is engineered for maximum performance, deterministic state transitions, and zero computational slop. All contributions must respect existing subsystem boundaries and data flows:

```mermaid
graph LR
    A[User Raw Prompt Text] -->|WASM Linear Memory| B[tiktoken BPE Parser]
    B -->|Token IDs & Byte Offsets| C[Token Highlighter DOM Engine]
    B -->|Token Count Metrics| D[Pricing Matrix Waterfall Solver]
    D -->|Micro-Cent Cost Breakdown| E[Cost Analytics Dashboard]
```

### 1.1 Core Subsystems
* **Primary Compute / Domain Engine**: Handles low-latency calculations, domain solvers, and state mutations.
* **Validation & Boundary Layer**: Enforces strict typing, schema assertions, and input sanitization before payloads enter the internal core.
* **Presentation & Stream Sinks**: Zero-allocation rendering, audio synthesis, or serialization buffers feeding client viewports.

---

## 📐 2. Strict Domain Invariants

Every pull request is automatically audited against these immutable project invariants. If any invariant is violated, the PR will be rejected:

### 1. Byte-Pair Boundary Fidelity
* **Formal Requirement**: Token decomposition must match official OpenAI/Anthropic tokenizer vocabularies with zero offset shift.
* **Verification Protocol**: Automated unit test assertion + mathematical boundary check.
* **Failure Mode**: Immediate build rejection; PR cannot be approved without meeting this invariant.
### 2. Pricing Matrix Micro-Precision
* **Formal Requirement**: Cost calculation formulas must evaluate cache read, cache write, and context tiers down to $0.000001.
* **Verification Protocol**: Automated unit test assertion + mathematical boundary check.
* **Failure Mode**: Immediate build rejection; PR cannot be approved without meeting this invariant.
### 3. Zero-Telemetry Guarantee
* **Formal Requirement**: Prompt text entered into the visualizer must NEVER leave the user's browser runtime.
* **Verification Protocol**: Automated unit test assertion + mathematical boundary check.
* **Failure Mode**: Immediate build rejection; PR cannot be approved without meeting this invariant.
### 4. WASM Memory Bounding
* **Formal Requirement**: Tiktoken WebAssembly instance must be bounded and properly recycled across large text inputs.
* **Verification Protocol**: Automated unit test assertion + mathematical boundary check.
* **Failure Mode**: Immediate build rejection; PR cannot be approved without meeting this invariant.

---

## 💻 3. Development Toolchain & Local Environment

### 3.1 Environment Prerequisites
* Primary Runtime: `TypeScript 5.8 / Vite / WebAssembly tiktoken / Vanilla CSS`
* Git with configured GPG signing keys
* Static Analysis & Linters matching project versions

### 3.2 Setup Procedure
```bash
# 1. Clone the repository
git clone https://github.com/marko1olo/token-audit.git
cd token-audit

# 2. Check out target working branch
git checkout main

# 3. Install dependencies & initialize toolchains
npm install || cargo check || dotnet restore || pip install -r requirements.txt || make preflight

# 4. Execute the complete test suite
npm test || pytest || dotnet test || make test
```

---

## 🧪 4. Testing Strategy & Verification Pipeline

Every non-trivial PR must contain empirical verification evidence. We do NOT accept "tested manually and looks fine":

1. **Unit & Invariant Tests**: Must explicitly verify the mathematical or logical properties of the modified subsystem.
2. **Boundary & Edge-Case Sweeps**: Test with zero-length inputs, extreme boundary coordinates, or adversarial configurations.
3. **Zero-Allocation Benchmarking**: For render or audio frame loops, run the memory profiler to verify zero heap allocations per tick.

---

## 💎 5. Code Standards & Anti-Patterns

### 5.1 Exemplary vs. Forbidden Patterns

```typescript
// ✅ CORRECT: Micro-Cent Cost Calculation
export function calculateTokenTierCost(promptTokens: number, cacheHits: number, ratePerMillion: number): number {
    const netTokens = Math.max(0, promptTokens - cacheHits);
    return (netTokens * ratePerMillion) / 1_000_000;
}
```

### 5.2 Anti-Patterns Blacklist
* ❌ **No AI Slop Comments**: Avoid decorative fluff like `// This function handles calculating the result`. Comment *why*, never *what*.
* ❌ **No Type Bypasses**: Never use `any`, `unknown` casts without runtime assertions, or unchecked pointer arithmetic.
* ❌ **No Unbounded Memory Growth**: Always provide explicit upper bounds on caches, array allocations, and event queues.

---

## 🚀 6. Pull Request Protocol & Review Workflow

```mermaid
graph TD
    A[Fork Repository] --> B[Create Descriptive Branch /feat or /fix]
    B --> C[Implement Code & Satisfy Invariants]
    C --> D[Run Full Test Suite & Linters]
    D --> E[Submit PR with Benchmark Proof]
    E --> F[Syndicate Adversarial Code Review]
    F -->|Approved| G[Rebase & Fast-Forward Merge]
    F -->|Corrections Needed| C
```

1. **Branch Naming**: `feat/<subsystem>-<feature>`, `fix/<subsystem>-<bug>`, `perf/<subsystem>-<optimization>`.
2. **Commit Standard**: Conventional Commits format with lowercase scope (`feat(core): implement SIMD acceleration`).
3. **PR Description**: Include root-cause analysis, benchmark numbers (before/after), and test commands executed.

---

## 👥 7. Syndicate Governance & Attribution

This project is authored and curated under the oversight of the **Жирняк & Адольф Петушков** Engineering Syndicate. All contributions merged into this repository will be credited to their authors while maintaining syndicate licensing integrity.
