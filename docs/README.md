# NSEBOT Documentation

> **Last Updated:** June 22, 2026 | **Method:** Adaptive Graph of Thoughts (AGoT)

---

## 🚀 Quick Start for AI Sessions

**Read in this order:**

1. **[AGoT Playbook](AGoT-playbook.md)** ⭐ — Read FIRST. Reasoning framework, decision trees, debugging guide.
2. **[Architecture](architecture.md)** — System structure, module responsibilities, data flow, configuration.
3. **[Order Flow](order-flow.md)** — Signal generation → execution path, state machine, error handling.
4. **[Options Engine](strategies/options-engine.md)** — Options-specific logic, strike selection, timeframe strategy.

---

## 📚 Documentation Index

| Document | Lines | Purpose | When to Read |
|----------|-------|---------|--------------|
| [AGoT Playbook](AGoT-playbook.md) | 658 | Reasoning framework, decision trees, debugging | **Every session start** |
| [Architecture](architecture.md) | 389 | System design, modules, data flow, config | Understanding structure |
| [Order Flow](order-flow.md) | 587 | Signal → execution path, state machine | Understanding runtime |
| [Options Engine](strategies/options-engine.md) | 562 | Options logic, strike selection, timeframe | Options-specific work |

---

## 🗂️ File Structure

```
docs/
├── README.md                          ← You are here
├── AGoT-playbook.md                   ← ⭐ Read FIRST
├── architecture.md                    ← System architecture
├── order-flow.md                      ← Signal to execution
└── strategies/
    └── options-engine.md              ← Options-specific logic
```

---

## 🔑 Key Concepts

### Pipeline Flow
```
Fetch → Detect Anomalies → Dedup → Intelligence → LLM Enrich
→ Trade Decision → Risk Check → Plan → Execute → Monitor → Alert
```

### Trade Decision Modes
| Mode | Description |
|------|-------------|
| `conservative` | Trend persistence only |
| `balanced` | Momentum scoring |
| `aggressive` | Reversal detection |
| `hybrid` (default) | All strategies in priority order |

### AI Integration Modes
| Mode | Description |
|------|-------------|
| `advisory` (default) | Log + display only |
| `boost_only` | Promotes BLOCKED → EXPERIMENTAL |
| `full` | Can boost AND veto trades |

### Execution Modes
| Mode | Description |
|------|-------------|
| Paper Trading | Simulated orders, safe testing |
| Live Trading | Real broker orders, requires setup |

---

## ⚙️ Configuration Quick Reference

### Environment Variables (`.env`)
```bash
ACTIVE_BROKER=zerodha
AI_DECISION_MODE=advisory
PAPER_RESEARCH_MODE=true
DISABLE_LLM_ENRICHMENT=false
```

### Runtime Config (`data/runtime_config.json`)
```json
{
    "live_trading_enabled": false,
    "live_ai_decision_mode": "advisory",
    "scan_frequency_nse": 5,
    "scan_frequency_mcx": 5
}
```

---

## 🧪 Testing

```bash
# All tests
pytest tests/

# Critical tests (must pass before live trading)
pytest tests/test_live_trading_p0.py -v

# Risk engine validation
pytest tests/test_risk_metrics.py -v
```

---

## 📞 Support

For issues not covered in documentation:
1. Check `logs/main.log` for recent errors
2. Use decision trees in [AGoT Playbook](AGoT-playbook.md)
3. Review known failure modes in [AGoT Playbook](AGoT-playbook.md#5-known-failure-modes--mitigations)

---

**Total Documentation:** 2,196 lines across 4 files
**Analysis Method:** Adaptive Graph of Thoughts (AGoT)
**Generated:** June 22, 2026
