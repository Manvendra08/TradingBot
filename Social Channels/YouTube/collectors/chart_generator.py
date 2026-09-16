"""
Social Channels/YouTube/collectors/chart_generator.py
Generates professional dark-themed financial charts for YouTube scenes using Matplotlib.
Theme: TradingView Dark / Bloomberg Terminal style (#0b1120 background, neon accents).
"""
from __future__ import annotations

import logging
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches

log = logging.getLogger(__name__)

DARK_BG = "#0b1120"
PANEL_BG = "#131c31"
TEXT_COLOR = "#f8fafc"
TEXT_MUTED = "#94a3b8"
GRID_COLOR = "#1e293b"
GREEN_COLOR = "#10b981"
RED_COLOR = "#ef4444"
CYAN_COLOR = "#06b6d4"
GOLD_COLOR = "#f59e0b"


class MarketChartGenerator:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        plt.rcParams.update({
            "figure.facecolor": DARK_BG,
            "axes.facecolor": PANEL_BG,
            "text.color": TEXT_COLOR,
            "axes.labelcolor": TEXT_COLOR,
            "xtick.color": TEXT_MUTED,
            "ytick.color": TEXT_MUTED,
            "grid.color": GRID_COLOR,
            "font.family": "sans-serif",
            "font.size": 12,
        })

    def generate_price_action_chart(
        self,
        symbol: str = "NIFTY 50",
        current_price: float = 23643.90,
        res_level: float | None = None,
        sup_level: float | None = None,
        output_name: str = "chart_price_action.png",
    ) -> Path:
        """Chart 1: High-definition candlestick / intraday breakdown chart."""
        output_path = self.output_dir / output_name
        fig, (ax_main, ax_vol) = plt.subplots(
            2, 1, figsize=(14, 7), gridspec_kw={"height_ratios": [4, 1]}, facecolor=DARK_BG
        )

        if res_level is None:
            res_level = float(round((current_price + 60.0) / 50.0) * 50.0)
        if sup_level is None:
            sup_level = float(round((current_price - 50.0) / 50.0) * 50.0)

        # Generate realistic 5-min intraday price action showing consolidation
        np.random.seed(42)
        n_bars = 40
        times = [f"{9 + i // 12:02d}:{(i % 12) * 5:02d}" for i in range(n_bars)]
        base = current_price + 35.0
        walk = np.cumsum(np.random.normal(-0.8, 3.5, n_bars))
        prices = base + walk
        prices[-1] = current_price

        # Draw candlesticks
        for i in range(n_bars):
            p_open = prices[i] + np.random.uniform(-4, 4)
            p_close = prices[i]
            p_high = max(p_open, p_close) + np.random.uniform(1, 6)
            p_low = min(p_open, p_close) - np.random.uniform(1, 6)
            is_up = p_close >= p_open
            c = GREEN_COLOR if is_up else RED_COLOR

            ax_main.vlines(i, p_low, p_high, color=c, linewidth=1.5)
            ax_main.bar(i, max(0.5, abs(p_close - p_open)), bottom=min(p_open, p_close), color=c, width=0.6, align="center")

            # Volume bar
            vol = np.random.uniform(50000, 250000) * (1.5 if i > 25 else 1.0)
            ax_vol.bar(i, vol, color=c, width=0.6, alpha=0.8)

        # Technical levels
        ax_main.axhline(res_level, color=RED_COLOR, linestyle="--", linewidth=2.0, alpha=0.9)
        ax_main.text(1, res_level + 3, f"CALL WALL / RESISTANCE: {res_level:,.0f}", color=RED_COLOR, fontweight="bold", fontsize=12, va="bottom")

        ax_main.axhline(sup_level, color=GREEN_COLOR, linestyle="--", linewidth=2.0, alpha=0.9)
        ax_main.text(1, sup_level + 3, f"KEY SUPPORT ZONE: {sup_level:,.0f}", color=GREEN_COLOR, fontweight="bold", fontsize=12, va="bottom")

        # Current Price Line
        ax_main.axhline(current_price, color=CYAN_COLOR, linestyle=":", linewidth=1.5)
        ax_main.text(n_bars - 1, current_price + 3, f"LTP: {current_price:,.2f}", color=CYAN_COLOR, fontweight="bold", fontsize=13, ha="right", va="bottom")

        # Titles & Format
        ax_main.set_title(f"{symbol} INTRADAY PRICE ACTION & CONSOLIDATION", color=TEXT_COLOR, fontsize=18, fontweight="bold", pad=15)
        ax_main.grid(True, linestyle="--", alpha=0.3)
        ax_main.set_xticks(range(0, n_bars, 5))
        ax_main.set_xticklabels([times[i] for i in range(0, n_bars, 5)])
        ax_main.set_ylabel("Index Points", fontweight="bold")
        ax_main.set_ylim(sup_level - 25, res_level + 25)

        ax_vol.set_ylabel("Volume", fontsize=10)
        ax_vol.grid(True, linestyle=":", alpha=0.2)
        ax_vol.set_xticks([])

        plt.tight_layout(rect=[0, 0.02, 1, 0.96])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated price action chart: {output_path}")
        return output_path

    def generate_fii_dii_chart(
        self,
        fii_net: float = -1850.50,
        dii_net: float = 1420.20,
        output_name: str = "chart_fii_dii.png",
    ) -> Path:
        """Chart 2: Institutional Inflows/Outflows Comparison."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        categories = ["FII / FPI NET", "DII NET", "NET COMBINED"]
        combined = fii_net + dii_net
        values = [fii_net, dii_net, combined]
        colors = [RED_COLOR if v < 0 else GREEN_COLOR for v in values]

        bars = ax.bar(categories, values, color=colors, width=0.45, edgecolor=DARK_BG, linewidth=2)

        ax.axhline(0, color=TEXT_MUTED, linewidth=1.2)
        ax.set_title("INSTITUTIONAL CASH FLOWS (FII vs DII NET ₹ CRORES)", color=TEXT_COLOR, fontsize=18, fontweight="bold", pad=18)
        ax.set_ylabel("Net Cash Flow (₹ Crores)", fontweight="bold", fontsize=13)
        ax.grid(True, linestyle="--", alpha=0.3, axis="y")

        # Data value callouts on bars
        for bar in bars:
            height = bar.get_height()
            offset = 14 if height >= 0 else -14
            va_pos = "bottom" if height >= 0 else "top"
            ax.annotate(
                f"₹{height:+,.1f} Cr",
                xy=(bar.get_x() + bar.get_width() / 2, height),
                xytext=(0, offset),
                textcoords="offset points",
                ha="center",
                va=va_pos,
                fontsize=15,
                fontweight="bold",
                color=TEXT_COLOR,
                bbox=dict(boxstyle="round,pad=0.3", facecolor=PANEL_BG, edgecolor=bar.get_facecolor(), linewidth=1.5),
            )

        y_max = max([abs(v) for v in values] + [100.0]) * 1.55
        ax.set_ylim(-y_max, y_max)

        plt.tight_layout(rect=[0, 0.02, 1, 0.96])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated FII/DII chart: {output_path}")
        return output_path

    def generate_open_interest_chart(
        self,
        atm_strike: float = 24800.0,
        output_name: str = "chart_open_interest.png",
    ) -> Path:
        """Chart 3: Open Interest Strike Heatmap (Call OI vs Put OI)."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        strikes = [atm_strike + i * 50 for i in range(-5, 6)]
        n_strikes = len(strikes)

        # Realistic OI buildup
        call_oi = [45, 60, 75, 110, 140, 195, 310, 420, 390, 260, 180]  # Heavy CE at 24900, 25000
        put_oi = [220, 340, 410, 380, 290, 210, 140, 95, 60, 40, 25]    # Heavy PE at 24600, 24700

        x = np.arange(n_strikes)
        width = 0.38

        rects1 = ax.bar(x - width/2, put_oi, width, label="Put OI (Support)", color=GREEN_COLOR, alpha=0.9)
        rects2 = ax.bar(x + width/2, call_oi, width, label="Call OI (Resistance)", color=RED_COLOR, alpha=0.9)

        ax.set_title("NIFTY DERIVATIVES OPEN INTEREST DISTRIBUTION (STRIKE-BY-STRIKE)", fontsize=18, fontweight="bold", pad=18)
        ax.set_xticks(x)
        ax.set_xticklabels([f"{int(s)}" for s in strikes], rotation=30, fontweight="bold")
        ax.set_ylabel("Open Interest (Contracts in '000s)", fontweight="bold")
        ax.legend(facecolor=PANEL_BG, edgecolor=GRID_COLOR, fontsize=13, loc="upper right")
        ax.grid(True, linestyle="--", alpha=0.3, axis="y")

        # Mark Max Pain strike
        max_pain_idx = 5  # atm
        ax.annotate(
            f"MAX PAIN: {int(atm_strike)}",
            xy=(max_pain_idx, 210),
            xytext=(max_pain_idx, 350),
            arrowprops=dict(facecolor=GOLD_COLOR, shrink=0.08, width=2, headwidth=8),
            bbox=dict(boxstyle="round,pad=0.3", facecolor=GOLD_COLOR, alpha=0.8),
            color="#000000",
            fontweight="bold",
            fontsize=12,
            ha="center",
        )

        plt.tight_layout()
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated OI chart: {output_path}")
        return output_path

    def generate_levels_roadmap_chart(
        self,
        current_price: float = 24810.50,
        output_name: str = "chart_levels_roadmap.png",
    ) -> Path:
        """Chart 4: Tomorrow's Battleground Support & Resistance Roadmap."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        levels = [
            ("R2 (Major Resistance)", 25050.0, RED_COLOR),
            ("R1 (Call Writers Wall)", 24920.0, RED_COLOR),
            ("LTP (Current Close)", current_price, CYAN_COLOR),
            ("S1 (Immediate Support)", 24700.0, GREEN_COLOR),
            ("S2 (Demand Fortress)", 24550.0, GREEN_COLOR),
        ]

        y_pos = np.arange(len(levels))[::-1]
        prices = [l[1] for l in levels]
        labels = [l[0] for l in levels]
        colors = [l[2] for l in levels]

        bars = ax.barh(y_pos, prices, color=colors, height=0.45, alpha=0.85)

        # Baseline offset for visual clarity
        min_p = 24400.0
        ax.set_xlim(min_p, 25200.0)

        for i, (bar, label, price) in enumerate(zip(bars, labels, prices)):
            ax.text(min_p + 20, bar.get_y() + bar.get_height()/2, f"{label}", color=TEXT_COLOR, fontweight="bold", fontsize=14, va="center")
            ax.text(price + 15, bar.get_y() + bar.get_height()/2, f"{price:,.1f}", color=bar.get_facecolor(), fontweight="bold", fontsize=15, va="center")

        ax.set_yticks([])
        ax.set_title("TOMORROW'S WATCH OUT: CRITICAL BATTLEGROUND LEVELS", fontsize=18, fontweight="bold", pad=18)
        ax.set_xlabel("Nifty Index Strike Price", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.3, axis="x")

        plt.tight_layout()
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated levels roadmap chart: {output_path}")
        return output_path

    def generate_vix_gauge_chart(
        self,
        vix: float = 13.90,
        output_name: str = "chart_vix_gauge.png",
    ) -> Path:
        """Chart 5: India VIX Volatility Speedometer / Dial Gauge."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG, subplot_kw={"projection": "polar"})
        ax.set_facecolor(DARK_BG)

        # Gauge arc from pi (left) to 0 (right)
        theta_min = np.pi
        theta_max = 0.0

        # Zones: Low (10-15: Green), Mid (15-20: Amber), High (20-30: Red)
        # Mapping VIX 10..30 to pi..0
        def vix_to_theta(val: float) -> float:
            clamped = max(10.0, min(30.0, val))
            return np.pi - ((clamped - 10.0) / 20.0) * np.pi

        # Draw colored background arcs
        t_low = np.linspace(vix_to_theta(10), vix_to_theta(15), 50)
        t_mid = np.linspace(vix_to_theta(15), vix_to_theta(20), 50)
        t_high = np.linspace(vix_to_theta(20), vix_to_theta(30), 50)

        ax.plot(t_low, [1.0] * len(t_low), color=GREEN_COLOR, linewidth=18, solid_capstyle="round")
        ax.plot(t_mid, [1.0] * len(t_mid), color=GOLD_COLOR, linewidth=18)
        ax.plot(t_high, [1.0] * len(t_high), color=RED_COLOR, linewidth=18, solid_capstyle="round")

        # Needle pointing to current VIX
        needle_theta = vix_to_theta(vix)
        ax.annotate(
            "",
            xy=(needle_theta, 0.92),
            xytext=(0, 0),
            arrowprops=dict(arrowstyle="->", color=CYAN_COLOR, lw=4),
        )
        ax.plot([0], [0], "o", color=CYAN_COLOR, markersize=14)

        # Hide full circular polar spine
        ax.spines["polar"].set_visible(False)
        ax.set_thetamin(0)
        ax.set_thetamax(180)

        # Labels
        ax.set_ylim(0, 1.25)
        ax.set_yticks([])
        ax.set_xticks([np.pi, vix_to_theta(15), vix_to_theta(20), 0])
        ax.set_xticklabels(["10 (Calm)", "15 (Normal)", "20 (Elevated)", "30 (Panic)"], color=TEXT_MUTED, fontweight="bold")
        ax.tick_params(axis="x", pad=10)
        ax.grid(False)

        # Center readout
        regime = "COMPLACENT / LOW RISK" if vix < 15 else ("ELEVATED RISK" if vix < 20 else "HIGH VOLATILITY")
        regime_color = GREEN_COLOR if vix < 15 else (GOLD_COLOR if vix < 20 else RED_COLOR)
        fig.text(0.5, 0.22, f"INDIA VIX: {vix:.2f}", color=TEXT_COLOR, fontsize=24, fontweight="bold", ha="center")
        fig.text(0.5, 0.14, f"VOLATILITY REGIME: {regime}", color=regime_color, fontsize=16, fontweight="bold", ha="center")
        fig.text(0.5, 0.93, "INDIA VIX VOLATILITY & FEAR SPEEDOMETER", color=TEXT_COLOR, fontsize=18, fontweight="bold", ha="center")

        plt.tight_layout(rect=[0, 0.08, 1, 0.92])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated VIX gauge chart: {output_path}")
        return output_path

    def generate_max_pain_chart(
        self,
        max_pain: float = 24800.0,
        current_price: float = 24810.50,
        output_name: str = "chart_max_pain.png",
    ) -> Path:
        """Chart 6: Expiry Max Pain Magnetic Bullseye / Target Dial."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        # Target circles
        diff = abs(current_price - max_pain)
        circle_radii = [150, 100, 50, 20]
        colors = ["#1e293b", "#334155", "#475569", GOLD_COLOR]

        for r, c in zip(circle_radii, colors):
            circle = patches.Circle((0, 0), r, fill=True, facecolor=c, alpha=0.35 if c != GOLD_COLOR else 0.8, edgecolor=GOLD_COLOR, linewidth=1.5)
            ax.add_patch(circle)

        # Crosshairs
        ax.axhline(0, color=GRID_COLOR, linestyle="--", alpha=0.6)
        ax.axvline(0, color=GRID_COLOR, linestyle="--", alpha=0.6)

        # Spot position relative to Max Pain
        dx = current_price - max_pain
        ax.plot([dx], [0], "o", color=CYAN_COLOR, markersize=16, label=f"NIFTY Close ({current_price:,.2f})")

        ax.set_xlim(-180, 180)
        ax.set_ylim(-180, 180)
        ax.set_aspect("equal")
        ax.axis("off")

        # Titles and Readouts
        ax.text(0, 0, f"MAX PAIN\n{int(max_pain)}", color="#000000", fontweight="bold", fontsize=13, ha="center", va="center")
        ax.text(dx, 22, f"LTP: {current_price:,.2f}\n({dx:+.1f} pts)", color=CYAN_COLOR, fontweight="bold", fontsize=13, ha="center")
        ax.set_title("OPTIONS EXPIRY MAX PAIN GRAVITATIONAL TARGET", color=TEXT_COLOR, fontsize=18, fontweight="bold", pad=20)
        
        fig.text(0.5, 0.12, f"Pin Risk Delta: {abs(dx):.1f} Points from Expiry Equilibrium Pivot", color=TEXT_COLOR, fontsize=15, fontweight="bold", ha="center")
        fig.text(0.5, 0.06, "Option writers maximize profits when underlying closes precisely at Max Pain", color=TEXT_MUTED, fontsize=12, ha="center")

        plt.tight_layout()
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated Max Pain chart: {output_path}")
        return output_path

    def generate_pcr_ratio_chart(
        self,
        pcr: float = 0.82,
        output_name: str = "chart_pcr_balance.png",
    ) -> Path:
        """Chart 7: Put-Call Ratio (PCR) Battleground Balance Scale."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        # Dual balance bars
        put_weight = (pcr / (1.0 + pcr)) * 100.0
        call_weight = 100.0 - put_weight

        categories = ["PUT WRITERS (BULL DEFENSE)", "CALL WRITERS (BEAR DOMINANCE)"]
        weights = [put_weight, call_weight]
        colors = [GREEN_COLOR, RED_COLOR]

        bars = ax.barh(categories, weights, color=colors, height=0.45, alpha=0.9, edgecolor=DARK_BG, linewidth=2)

        sentiment = "BEARISH OVERHANG" if pcr < 0.90 else ("BULLISH EXPANSION" if pcr > 1.10 else "BALANCED / NEUTRAL")
        sent_color = RED_COLOR if pcr < 0.90 else (GREEN_COLOR if pcr > 1.10 else GOLD_COLOR)

        ax.set_xlim(0, 100)
        ax.set_xlabel("Relative Weight (% of Volume/OI)", fontweight="bold")
        ax.set_title(f"PUT-CALL RATIO (PCR) EQUILIBRIUM: {pcr:.2f}  •  {sentiment}", fontsize=17, fontweight="bold", pad=20, color=TEXT_COLOR)
        ax.grid(True, linestyle="--", alpha=0.3, axis="x")

        for bar, w in zip(bars, weights):
            ax.text(w + 2, bar.get_y() + bar.get_height()/2, f"{w:.1f}%", color=TEXT_COLOR, fontweight="bold", fontsize=16, va="center")

        fig.text(0.5, 0.03, "PCR < 0.90 indicates Call writers dominate overhead resistance  •  PCR > 1.10 indicates Bull dominance", color=TEXT_MUTED, fontsize=13, ha="center")

        plt.tight_layout(rect=[0, 0.08, 1, 0.95])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated PCR balance chart: {output_path}")
        return output_path

    def generate_smart_money_divergence_chart(
        self,
        fii_net: float = 280.13,
        dii_net: float = 566.76,
        nifty_change: float = -145.20,
        output_name: str = "chart_smart_money_divergence.png",
    ) -> Path:
        """Chart 8: Smart Money Divergence (Institutions Buying into Selloff)."""
        output_path = self.output_dir / output_name
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 7), facecolor=DARK_BG, gridspec_kw={"width_ratios": [1.1, 1]})

        combined_inflow = fii_net + dii_net
        categories = ["COMBINED\nINSTITUTIONAL\nINFLOW", "NIFTY 50\nSESSION\nDELTA"]
        values = [combined_inflow, nifty_change]
        colors = [GREEN_COLOR, RED_COLOR]

        bars = ax1.bar(categories, values, color=colors, width=0.45, edgecolor=DARK_BG, linewidth=2)
        ax1.axhline(0, color=TEXT_MUTED, linewidth=1.2)
        ax1.set_title("SMART MONEY FLOW VS PRICE ACTION", fontsize=16, fontweight="bold", pad=20)
        ax1.set_ylabel("Metric Magnitude", fontweight="bold")
        ax1.grid(True, linestyle="--", alpha=0.3, axis="y")

        max_mag = max(abs(combined_inflow), abs(nifty_change), 200.0)
        ax1.set_ylim(-max_mag * 1.55, max_mag * 1.55)

        ax1.annotate(
            f"+₹{combined_inflow:,.1f} Cr\n(NET ACCUMULATION)",
            xy=(0, combined_inflow),
            xytext=(0, 12),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=13,
            fontweight="bold",
            color=GREEN_COLOR,
            bbox=dict(boxstyle="round,pad=0.3", facecolor=PANEL_BG, edgecolor=GREEN_COLOR, linewidth=1.5),
        )
        ax1.annotate(
            f"{nifty_change:+.1f} Pts\n(PRICE SLIDE)",
            xy=(1, nifty_change),
            xytext=(0, -12),
            textcoords="offset points",
            ha="center",
            va="top",
            fontsize=13,
            fontweight="bold",
            color=RED_COLOR,
            bbox=dict(boxstyle="round,pad=0.3", facecolor=PANEL_BG, edgecolor=RED_COLOR, linewidth=1.5),
        )

        # Right Panel: Flow Dynamics Breakdown Box
        ax2.set_facecolor(PANEL_BG)
        ax2.axis("off")
        ax2.text(0.5, 0.88, "DIVERGENCE DIAGNOSTICS", fontsize=18, fontweight="bold", color=CYAN_COLOR, ha="center")
        
        diagnostics = [
            ("• INSTITUTIONAL STANCE", f"NET BUYERS (+₹{combined_inflow:,.1f} CR COMBINED)", GREEN_COLOR),
            ("• MARKET PHENOMENON", "SECTOR ROTATION & PROFIT BOOKING", GOLD_COLOR),
            ("• RETAIL ABSORPTION", "HIGH SUPPLY ABSORBED BY DOMESTIC DESKS", TEXT_COLOR),
            ("• QUANT REGIME", "CONSOLIDATION BEFORE NEXT EXPIRY EXPANSION", CYAN_COLOR),
        ]
        
        y_pos = 0.70
        for title, val, col in diagnostics:
            ax2.text(0.08, y_pos, title, fontsize=12, fontweight="bold", color=TEXT_MUTED)
            ax2.text(0.08, y_pos - 0.08, val, fontsize=13, fontweight="bold", color=col)
            y_pos -= 0.18

        fig.suptitle("INSTITUTIONAL FOOTPRINT: BULLISH SMART MONEY DIVERGENCE", fontsize=18, fontweight="bold", color=TEXT_COLOR, y=0.97)
        plt.tight_layout(rect=[0, 0.04, 1, 0.93])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated smart money divergence chart: {output_path}")
        return output_path

    def generate_call_resistance_chart(
        self,
        atm_strike: float = 23650.0,
        output_name: str = "chart_call_resistance.png",
    ) -> Path:
        """Chart 9: Call Writers Resistance Fortress (Overhead Ceiling)."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        atm = int(round(atm_strike / 50.0) * 50)
        s1 = atm + 50
        s2 = atm + 150
        strikes = [f"{atm - 50:,}", f"{atm:,} (ATM)", f"{s1:,} ★", f"{atm + 100:,}", f"{s2:,} ★", f"{atm + 200:,}", f"{atm + 250:,}"]
        call_oi = [65, 140, 520, 260, 430, 180, 110]
        colors = [RED_COLOR if "★" in s else "#7f1d1d" for s in strikes]

        bars = ax.bar(strikes, call_oi, color=colors, width=0.55, edgecolor=DARK_BG, linewidth=2)
        ax.set_title("CALL WRITERS FORTRESS: OVERHEAD RESISTANCE CLUSTERS", fontsize=18, fontweight="bold", pad=20)
        ax.set_ylabel("Call Open Interest (Relative Units)", fontweight="bold")
        ax.set_xlabel("Nifty Option Strike Price", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.3, axis="y")

        # Annotate major resistance strikes
        ax.annotate(
            f"PRIMARY CEILING: {s1:,}\n(34.4M Contracts)",
            xy=(2, 520),
            xytext=(2, 600),
            arrowprops=dict(facecolor=RED_COLOR, shrink=0.08, width=2, headwidth=8),
            bbox=dict(boxstyle="round,pad=0.4", facecolor=PANEL_BG, edgecolor=RED_COLOR, linewidth=1.5),
            color=TEXT_COLOR,
            fontweight="bold",
            fontsize=12,
            ha="center",
        )
        ax.annotate(
            f"SECONDARY CEILING: {s2:,}\n(25.9M Contracts)",
            xy=(4, 430),
            xytext=(4, 520),
            arrowprops=dict(facecolor=GOLD_COLOR, shrink=0.08, width=2, headwidth=8),
            bbox=dict(boxstyle="round,pad=0.4", facecolor=PANEL_BG, edgecolor=GOLD_COLOR, linewidth=1.5),
            color=TEXT_COLOR,
            fontweight="bold",
            fontsize=12,
            ha="center",
        )

        ax.set_ylim(0, 680)
        ax.tick_params(axis="x", pad=8)
        fig.text(0.5, 0.03, f"Aggressive Call writing at {s1:,} and {s2:,} caps immediate upside momentum", color=TEXT_MUTED, fontsize=13, ha="center")
        plt.tight_layout(rect=[0, 0.08, 1, 0.95])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated Call resistance chart: {output_path}")
        return output_path

    def generate_put_support_chart(
        self,
        atm_strike: float = 23650.0,
        output_name: str = "chart_put_support.png",
    ) -> Path:
        """Chart 10: Put Writers Support Base (Downside Cushions)."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        atm = int(round(atm_strike / 50.0) * 50)
        p1 = atm - 50
        p2 = atm - 100
        strikes = [f"{atm - 150:,}", f"{p2:,} ★", f"{p1:,} ★", f"{atm:,} (ATM) ★", f"{atm + 50:,}", f"{atm + 100:,}"]
        put_oi = [140, 280, 560, 410, 190, 80]
        colors = [GREEN_COLOR if "★" in s else "#065f46" for s in strikes]

        bars = ax.bar(strikes, put_oi, color=colors, width=0.55, edgecolor=DARK_BG, linewidth=2)
        ax.set_title("PUT WRITERS CITADEL: CRITICAL DOWNSIDE DEMAND CUSHIONS", fontsize=18, fontweight="bold", pad=20)
        ax.set_ylabel("Put Open Interest (Relative Units)", fontweight="bold")
        ax.set_xlabel("Nifty Option Strike Price", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.3, axis="y")

        ax.annotate(
            f"PRIMARY DEFENSE: {p1:,}\n(30.8M Contracts)",
            xy=(2, 560),
            xytext=(2, 630),
            arrowprops=dict(facecolor=GREEN_COLOR, shrink=0.08, width=2, headwidth=8),
            bbox=dict(boxstyle="round,pad=0.4", facecolor=PANEL_BG, edgecolor=GREEN_COLOR, linewidth=1.5),
            color=TEXT_COLOR,
            fontweight="bold",
            fontsize=12,
            ha="center",
        )
        ax.annotate(
            f"BEDROCK FLOOR: {p2:,}\n(16.2M Contracts)",
            xy=(1, 280),
            xytext=(0.8, 380),
            arrowprops=dict(facecolor=CYAN_COLOR, shrink=0.08, width=2, headwidth=8),
            bbox=dict(boxstyle="round,pad=0.4", facecolor=PANEL_BG, edgecolor=CYAN_COLOR, linewidth=1.5),
            color=TEXT_COLOR,
            fontweight="bold",
            fontsize=12,
            ha="center",
        )

        ax.set_ylim(0, 700)
        ax.tick_params(axis="x", pad=8)
        fig.text(0.5, 0.03, f"Put buildup concentrated at {p1:,} and {p2:,} provides critical downside cushion", color=TEXT_MUTED, fontsize=13, ha="center")
        plt.tight_layout(rect=[0, 0.08, 1, 0.95])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated Put support chart: {output_path}")
        return output_path

    def generate_downside_defense_chart(
        self,
        current_price: float = 23643.90,
        output_name: str = "chart_downside_defense.png",
    ) -> Path:
        """Chart 11: Downside Defense & Support Risk Ladder."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        s1 = float(round((current_price - 40.0) / 50.0) * 50.0)
        s2 = s1 - 50.0
        s3 = s1 - 100.0

        defenses = [
            ("LTP (Current Close)", current_price, CYAN_COLOR, "Current Anchor"),
            ("S1: Primary Support", s1, GREEN_COLOR, f"-{current_price - s1:.1f} Pts | Primary Put Bastion"),
            ("S2: Intermediate Floor", s2, GREEN_COLOR, f"-{current_price - s2:.1f} Pts | Secondary Cushion"),
            ("S3: Bedrock Support", s3, GOLD_COLOR, f"-{current_price - s3:.1f} Pts | Major Value Zone"),
        ]

        y_pos = np.arange(len(defenses))[::-1]
        prices = [d[1] for d in defenses]
        labels = [d[0] for d in defenses]
        colors = [d[2] for d in defenses]
        notes = [d[3] for d in defenses]

        bars = ax.barh(y_pos, prices, color=colors, height=0.45, alpha=0.9, edgecolor=DARK_BG, linewidth=2)
        ax.set_xlim(s3 - 100.0, current_price + 80.0)

        for bar, label, price, note in zip(bars, labels, prices, notes):
            ax.text(s3 - 80.0, bar.get_y() + bar.get_height()/2, f"{label}", color=TEXT_COLOR, fontweight="bold", fontsize=14, va="center")
            ax.text(price + 8, bar.get_y() + bar.get_height()/2, f"{price:,.1f}  ({note})", color=bar.get_facecolor(), fontweight="bold", fontsize=13, va="center")

        ax.set_yticks([])
        ax.set_title("DOWNSIDE RISK LADDER: CRITICAL SUPPORT ROADMAP", fontsize=18, fontweight="bold", pad=20)
        ax.set_xlabel("NIFTY Index Levels", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.3, axis="x")

        fig.text(0.5, 0.03, f"Break below {s1:,.0f} exposes test of {s2:,.0f} and {s3:,.0f} demand fortress", color=TEXT_MUTED, fontsize=13, ha="center")
        plt.tight_layout(rect=[0, 0.08, 1, 0.95])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated downside defense chart: {output_path}")
        return output_path

    def generate_upside_hurdle_chart(
        self,
        current_price: float = 23643.90,
        output_name: str = "chart_upside_hurdle.png",
    ) -> Path:
        """Chart 12: Upside Recovery & Bullish Hurdle Gate."""
        output_path = self.output_dir / output_name
        fig, ax = plt.subplots(figsize=(14, 7), facecolor=DARK_BG)

        r1 = float(round((current_price + 55.0) / 50.0) * 50.0)
        r2 = r1 + 50.0
        r3 = r1 + 100.0

        hurdles = [
            ("R3: Short Covering Expansion", r3, GOLD_COLOR, f"+{r3 - current_price:.1f} Pts | Gamma Squeeze Zone"),
            ("R2: Secondary Hurdle", r2, RED_COLOR, f"+{r2 - current_price:.1f} Pts | Call Wall Barrier"),
            ("R1: Immediate Hurdle", r1, RED_COLOR, f"+{r1 - current_price:.1f} Pts | Immediate Resistance"),
            ("LTP: Baseline Spot", current_price, CYAN_COLOR, "Current Reference"),
        ]

        y_pos = np.arange(len(hurdles))[::-1]
        prices = [h[1] for h in hurdles]
        labels = [h[0] for h in hurdles]
        colors = [h[2] for h in hurdles]
        notes = [h[3] for h in hurdles]

        bars = ax.barh(y_pos, prices, color=colors, height=0.45, alpha=0.9, edgecolor=DARK_BG, linewidth=2)
        ax.set_xlim(current_price - 60.0, r3 + 80.0)

        for bar, label, price, note in zip(bars, labels, prices, notes):
            ax.text(current_price - 50.0, bar.get_y() + bar.get_height()/2, f"{label}", color=TEXT_COLOR, fontweight="bold", fontsize=14, va="center")
            ax.text(price + 8, bar.get_y() + bar.get_height()/2, f"{price:,.1f}  ({note})", color=bar.get_facecolor(), fontweight="bold", fontsize=13, va="center")

        ax.set_yticks([])
        ax.set_title("BULLISH RECOVERY HURDLES: RESISTANCE CONVERGENCE", fontsize=18, fontweight="bold", pad=20)
        ax.set_xlabel("NIFTY Index Levels", fontweight="bold")
        ax.grid(True, linestyle="--", alpha=0.3, axis="x")

        fig.text(0.5, 0.03, f"Sustained close above {r1:,.0f} required for bulls to regain directional control", color=TEXT_MUTED, fontsize=13, ha="center")
        plt.tight_layout(rect=[0, 0.08, 1, 0.95])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated upside hurdle chart: {output_path}")
        return output_path

    def generate_risk_discipline_chart(
        self,
        output_name: str = "chart_risk_discipline.png",
    ) -> Path:
        """Chart 13: Quantitative Risk Desk & Regulatory Compliance Framework."""
        output_path = self.output_dir / output_name
        fig, axes = plt.subplots(2, 2, figsize=(14, 7), facecolor=DARK_BG)

        quadrants = [
            (axes[0, 0], "1. CAPITAL ALLOCATION", "MAX 2% RISK PER TRADE", "Never risk >2% of portfolio margin on single trade structure", GREEN_COLOR),
            (axes[0, 1], "2. STOP LOSS DISCIPLINE", "FAIL-CLOSED TRAILING SL", "Respect predefined invalidation levels; zero emotional holding", RED_COLOR),
            (axes[1, 0], "3. RISK REWARD REGIME", "MIN 1:2 RISK/REWARD RATIO", "Prioritize defined-risk multi-leg credit spreads & hedges", CYAN_COLOR),
            (axes[1, 1], "4. REGULATORY NOTICE", "EDUCATIONAL PURPOSE ONLY", "Not SEBI registered financial advisors; consult certified professionals", GOLD_COLOR),
        ]

        for ax, title, badge, desc, col in quadrants:
            ax.set_facecolor(PANEL_BG)
            ax.axis("off")
            # Card bounding outline
            rect = patches.FancyBboxPatch((0.03, 0.05), 0.94, 0.90, boxstyle="round,pad=0.03", facecolor=PANEL_BG, edgecolor=col, linewidth=1.5)
            ax.add_patch(rect)
            ax.text(0.08, 0.78, title, color=TEXT_MUTED, fontsize=12, fontweight="bold")
            ax.text(0.08, 0.52, badge, color=col, fontsize=16, fontweight="bold")
            ax.text(0.08, 0.22, desc, color=TEXT_COLOR, fontsize=11, wrap=True)

        fig.suptitle("NSEBOT RISK DESK: EXECUTION PRINCIPLES & COMPLIANCE", fontsize=18, fontweight="bold", color=TEXT_COLOR, y=0.97)
        plt.tight_layout(rect=[0, 0.04, 1, 0.93])
        plt.savefig(output_path, dpi=120, facecolor=DARK_BG)
        plt.close()
        log.info(f"Generated risk discipline chart: {output_path}")
        return output_path


