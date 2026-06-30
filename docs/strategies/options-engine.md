# NSEBOT Options Engine Strategy

## 1. Core Philosophy: The OI Matrix
The options engine does not rely on simple price direction. Instead, it interprets the **intent** of market participants through Open Interest (OI) changes:

*   **Long Buildup:** Price ↑ + OI ↑ → Fresh money entering bullish positions.
*   **Short Buildup:** Price ↓ + OI ↑ → Fresh money entering bearish positions.
*   **Short Covering:** Price ↑ + OI ↓ → Bears exiting; usually a weaker, temporary rally.
*   **Long Unwinding:** Price ↓ + OI ↓ → Bulls exiting; usually a temporary dip.

---

## 2. Strike Selection Logic

### For Buying (CE/PE)
*   **Strategy:** ATM (At-The-Money).
*   **Reasoning:** ATM options have the highest Delta (~0.5), providing the best balance between directional movement and premium cost. They are less susceptible to Theta decay than OTM options.

### For Selling (Writing CE/PE)
*   **Strategy:** OTM (Out-of-The-Money) near Support/Resistance.
*   **Reasoning:** Selling at key levels (e.g., Resistance for CE writing) provides a "margin of safety." Even if the price moves slightly against the position, time decay (Theta) works in the seller's favor.

### MCX Commodities Exception
*   **Liquidity Check:** The engine first checks if ATM options have sufficient volume (>500) and OI (>2000).
*   **Fallback:** If liquidity is low, the engine switches to **FUT (Futures)** trading to avoid wide bid-ask spreads that would erode profits.

---

## 3. Premium Management & Exit Logic

### Stop-Loss (SL) Calculation
*   **Underlying-Based:** SL is primarily defined in terms of the underlying index/commodity price (e.g., "Exit if NIFTY falls below 24,450").
*   **Premium Conversion:** This underlying level is converted to a premium value for broker orders. This prevents premature exits due to temporary volatility spikes in the option premium itself.

### Target Calculation
*   **ATR-Based:** Uses 1.5x ATR for SL and 2.0x ATR for Targets to adapt to current market volatility.
*   **Key Levels:** If a clear Resistance/Support level exists within 3 strike-steps, it is used as the target instead of ATR.

### Timeframe Strategy (3H Entry / 1H Exit)
*   **Entry:** Requires a completed 3-hour candle to close above/below the previous 3-hour high/low with OI confirmation.
*   **Exit:** Monitors 1-hour candles for trend reversals or "crossover" events where the price breaks the previous 1-hour low/high.

---

## 4. Risk Adjustments

### IV Percentile Penalty
If the current Implied Volatility (IV) is in the top 40% of the last 30 days, the engine applies a **-10 point penalty** to the trade score. Buying options when IV is high is statistically unfavorable due to "IV Crush" after events.

### Days-to-Expiry (DTE) Decay
As expiry approaches, the engine discounts the momentum score:
*   **DTE > 3:** No decay (1.0x multiplier).
*   **DTE = 1:** 0.55x multiplier (high gamma risk).
*   **DTE = 0:** 0.40x multiplier (expiry day chaos).

---

## 5. AI Integration in Options Trading

The LLM is provided with:
1.  **OI Semantics:** Explicit rules on how to interpret CE vs. PE buildup.
2.  **Chart Context:** 1H and 3H candle sentiments to judge entry timing.
3.  **Macro Drivers:** Specific catalysts (e.g., EIA reports for Natural Gas, RBI policy for BankNifty).

The AI's role is to provide the **"Signal Chain"**—a three-line summary of OI, Price, and Chart alignment—and to suggest specific invalidation points that the rule engine might miss.

This document serves as the technical reference for the bot's options-specific logic, ensuring that future enhancements remain aligned with the core OI-driven philosophy.
