# NSEBOT Documentation Index

Welcome to the official documentation for the **NSEBOT** algorithmic trading system. This directory contains all necessary context for AI agents and developers to understand, maintain, and optimize the bot.

---

## 🚀 Quick Start Guide

1.  **For New AI Sessions:** Start by reading `AGoT-playbook.md` to understand the reasoning framework and session startup checklist.
2.  **For System Architecture:** Read `ARCHITECTURE.md` to understand module responsibilities and data flow.
3.  **For Trading Logic:** Read `TRADING_STRATEGY.md` to understand entry/exit criteria and risk management.
4.  **For Order Execution:** Read `order-flow.md` to understand the state machine and error handling.
5.  **For Options Specifics:** Read `strategies/options-engine.md` for strike selection and premium management.

---

## 📂 Document Map

| File | Description |
| :--- | :--- |
| **[AGoT-playbook.md](./AGoT-playbook.md)** | The "Brain" of the project. Contains decision trees, debugging guides, and key thresholds. |
| **[ARCHITECTURE.md](./ARCHITECTURE.md)** | High-level system design, broker integrations, and technical debt status. |
| **[TRADING_STRATEGY.md](./TRADING_STRATEGY.md)** | Detailed explanation of the Price × OI matrix and risk engine logic. |
| **[order-flow.md](./order-flow.md)** | Step-by-step lifecycle of a trade from signal to execution. |
| **[strategies/options-engine.md](./strategies/options-engine.md)** | Specialized logic for options strike selection and timeframe strategies. |
| **[CLEANUP_REPORT.md](./CLEANUP_REPORT.md)** | History of recent high-priority maintenance and file consolidation. |

---

## ⚙️ Key Configuration Files

*   `config/settings.py`: Global thresholds (Confidence floors, Risk limits, Market hours).
*   `config/runtime_config.json`: User-adjustable settings via the Dashboard UI.
*   `.env`: Sensitive credentials (Broker API keys, Telegram tokens, LLM API keys).

---

## 🛠️ Operational Commands

*   **Start Bot:** `python src/engine/main.py`
*   **Start Dashboard:** `streamlit run src/dashboard/app.py`
*   **Run Tests:** `pytest tests/ -v`
*   **Cleanup Tools:** `python tools/cleanup_high_priority.py`

---

## 🤝 Contributing & Maintenance

When making changes to the codebase:
1.  Always update the relevant documentation in this `docs/` folder.
2.  Ensure new features pass the existing test suite in `tests/`.
3.  Use the AGoT framework to evaluate trade-offs before implementing complex logic.

*Last Updated: June 29, 2026*
