# UI Review — Phase 1: Testing and Fixes

## 📊 Score Summary: 21/24

| Pillar | Score | Assessment |
|--------|-------|------------|
| **Copywriting** | 4/4 | Clear, professional labels. Terminology is consistent across Dashboard and Extension. |
| **Visuals** | 3/4 | Good use of interactive Plotly charts. Extension uses icons and badges effectively. Tables could be cleaner. |
| **Color** | 3/4 | Semantic CE/PE colors are intuitive. Good contrast. Plotly defaults could be themed for a more premium feel. |
| **Typography** | 4/4 | Clean system fonts. Excellent use of tabular numerals for financial data. Bold headers provide good hierarchy. |
| **Spacing** | 3/4 | Logical grouping via dividers and grids. Some data-heavy tables feel a bit cramped in narrow viewports. |
| **Experience Design** | 4/4 | High transparency with logs and status badges. Logical tab system. Functional controls (Refresh, Force Scan). |

---

## 🔍 Top Findings & Fixes

### 1. Dashboard Table Density (Spacing/Visuals)
**Issue:** The `alerts_df` table in Streamlit displays a raw JSON-like string for "detail" which makes the rows very tall and hard to scan.
**Fix:** Implement a cleaner detail formatter that shows only the most critical delta values, or use a "Expandable" row if Streamlit supports it.

### 2. Plotly Theming (Color)
**Issue:** Using default Plotly colors (Muted blue/orange) alongside high-contrast semantic colors (Red/Green) creates some visual inconsistency.
**Fix:** Force a custom `color_discrete_map` for all Plotly charts to match the NSEBOT brand (e.g., #ef5350 for CE, #26a69a for PE).

### 3. Extension Layout Stability (Visuals)
**Issue:** The extension popup has a fixed width (480px), but the content inside (like the OI Table) can sometimes overflow or require horizontal scrolling.
**Fix:** Ensure the OI table uses `table-layout: fixed` and truncates long symbol names if necessary to prevent layout shifts.

---

## ▶ Next Steps

- **[ ] Verify fixes** in next development cycle.
- **[ ] UAT** — Validate that the data presented in the UI matches the database exactly.
