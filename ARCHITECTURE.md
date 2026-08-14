# Token Audit — Architecture Specification

## 1. Byte-Pair Encoding (BPE) Formalism
Tokenization splits raw UTF-8 byte streams into integer vocabulary indices based on merge tables ($V$).

$$L(T) = \sum_{i=1}^N \text{byte\_length}(t_i)$$

## 2. Multi-Model Cost Matrix
- **Claude 3.7 Sonnet:** $3.00/MTok input, $0.30/MTok cached, $15.00/MTok output.
- **Claude Opus 4.8:** $15.00/MTok input, $1.50/MTok cached, $75.00/MTok output.
- **DeepSeek-V3:** $0.27/MTok input, $0.07/MTok cached, $1.10/MTok output.
