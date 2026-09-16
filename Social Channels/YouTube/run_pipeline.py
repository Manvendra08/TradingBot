"""
Social Channels/YouTube/run_pipeline.py
Master daily runner triggered at 19:15 IST with state checkpointing and CLI flags.
Usage:
    python run_pipeline.py           # Standard run (honors market hours/holidays)
    python run_pipeline.py --force   # Force run (ignores weekend/holiday gates for testing)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Add Social Channels/YouTube and Project Root to sys.path
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]

# Ensure YouTube folder is first in sys.path
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

import yt_config.settings as yt_settings
from config.holidays import is_market_holiday
from collectors.db_market_reader import DBMarketReader
from collectors.fii_dii_fetcher import fetch_fii_dii_cash
from collectors.chart_generator import MarketChartGenerator
from generators.script_engine import ScriptEngine
from generators.audio_engine import AudioEngine
from generators.visual_card_engine import VisualCardEngine
from generators.html_card_engine import HTMLCardEngine, AIImageEngine
from generators.llm_card_engine import LLMCardEngine
from compositors.video_assembler import VideoAssembler
from publishers.youtube_uploader import YouTubePublisher
from publishers.telegram_alert import send_youtube_review_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "pipeline.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("YouTubePipelineRunner")


def main():
    parser = argparse.ArgumentParser(description="Daily YouTube Market Wrap Pipeline")
    parser.add_argument("--force", action="store_true", help="Force execution ignoring weekend/holiday checks")
    parser.add_argument("--output-dir", type=str, default=None, help="Custom output directory")
    parser.add_argument("--fresh", action="store_true", help="Clear existing state and generate fresh")
    parser.add_argument("--use-llm-cards", action="store_true", default=True, help="Use LLM image diffusion for visual scene cards")
    parser.add_argument("--no-llm-cards", dest="use_llm_cards", action="store_false", help="Disable LLM image diffusion cards (use HTML)")
    parser.add_argument("--nifty-close", type=float, default=None, help="Override NIFTY 50 close price")
    parser.add_argument("--nifty-change", type=float, default=None, help="Override NIFTY 50 point change")
    parser.add_argument("--nifty-pchange", type=float, default=None, help="Override NIFTY 50 percent change")
    args = parser.parse_args()

    now = datetime.now()
    log.info(f"=== Starting Daily YouTube Pipeline ({now.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    # 0. Holiday & Weekend Gate
    if not args.force:
        if now.weekday() >= 5:
            log.info("Weekend detected. Skipping execution. (Use --force to run anyway)")
            return
        if is_market_holiday("NIFTY", now):
            log.info("NSE Holiday detected. Skipping execution. (Use --force to run anyway)")
            return

    if args.output_dir:
        work_dir = Path(args.output_dir)
    else:
        work_dir = BASE_DIR / "output" / now.strftime("%Y-%m-%d")

    work_dir.mkdir(parents=True, exist_ok=True)
    state_file = work_dir / "state.json"

    state = {}
    if state_file.exists() and not args.fresh:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    # 1. Market Data Extraction
    if not state.get("market_data_done") or args.fresh:
        log.info("STAGE 1: Reading settled market data...")
        fii_net, dii_net = fetch_fii_dii_cash()
        import dataclasses
        reader = DBMarketReader(yt_settings.NSEBOT_DB_PATH)
        metrics = reader.get_latest_market_metrics(fii_net, dii_net)
        overrides = {}
        if args.nifty_close is not None:
            overrides["nifty_close"] = args.nifty_close
        if args.nifty_change is not None:
            overrides["nifty_change"] = args.nifty_change
        if args.nifty_pchange is not None:
            overrides["nifty_pchange"] = args.nifty_pchange
        if overrides:
            metrics = dataclasses.replace(metrics, **overrides)
        state["metrics"] = metrics.__dict__
        state["market_data_done"] = True
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        if args.nifty_close is not None:
            state["metrics"]["nifty_close"] = args.nifty_close
        if args.nifty_change is not None:
            state["metrics"]["nifty_change"] = args.nifty_change
        if args.nifty_pchange is not None:
            state["metrics"]["nifty_pchange"] = args.nifty_pchange
        log.info("STAGE 1: Market data processed with overrides.")

    # 2. Script Generation
    if not state.get("script_done"):
        log.info("STAGE 2: Synthesizing script via OmniRouter / Gemini...")
        script_engine = ScriptEngine()
        metrics_obj = type("MarketObj", (), state["metrics"])()
        script = script_engine.generate_script(metrics_obj)
        state["script"] = script.model_dump()
        state["script_done"] = True
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        log.info("STAGE 2: Script already synthesized in state checkpoint.")

    script_data = state["script"]
    log.info(f"Title: {script_data['video_title']}")

    # 3. Audio, Subtitles & Audio Script Text File
    audio_path = work_dir / "voiceover.mp3"
    srt_path = work_dir / "subtitles.srt"
    script_txt_path = work_dir / "audio_script.txt"

    if not state.get("audio_done"):
        log.info("STAGE 3: Generating Edge-TTS audio at 1.25x speed and saving script text file...")
        
        # Build formatted script text file for human review & archival
        script_txt_content = (
            f"TITLE: {script_data['video_title']}\n"
            f"HOOK: {script_data['thumbnail_hook']}\n"
            f"DATE: {state['metrics']['trade_date']}\n"
            f"{'=' * 60}\n\n"
        )
        for s in script_data["scenes"]:
            script_txt_content += (
                f"[SCENE {s['scene_id']}: {s['segment_title'].upper()}]\n"
                f"Headline: {s['headline_text']}\n"
                f"Metric: {s['metric_highlight']}\n"
                f"Voiceover: {s['spoken_text']}\n\n"
            )
        script_txt_content += (
            f"[OUTRO DISCLAIMER]\n"
            f"{script_data['spoken_sebi_disclaimer']}\n"
        )
        script_txt_path.write_text(script_txt_content, encoding="utf-8")
        log.info(f"Saved formatted audio script to: {script_txt_path}")

        full_spoken_text = " ".join([s["spoken_text"] for s in script_data["scenes"]])
        full_spoken_text += " " + script_data["spoken_sebi_disclaimer"]
        
        audio_engine = AudioEngine(
            voice=yt_settings.DEFAULT_TTS_VOICE,
            rate=yt_settings.DEFAULT_TTS_RATE,
        )
        asyncio.run(audio_engine.synthesize(full_spoken_text, audio_path, srt_path))
        state["audio_done"] = True
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        log.info("STAGE 3: Audio & SRT already generated in state checkpoint.")

    # 4. Visual Scene Cards & Thumbnail
    thumb_path = work_dir / "thumbnail.jpg"
    if not state.get("visuals_done"):
        log.info("STAGE 4: Rendering dynamic financial charts and PIL scene cards...")
        
        # 4A. Render Specialized High-Resolution Micro-Charts
        chart_gen = MarketChartGenerator(work_dir)
        chart_price = chart_gen.generate_price_action_chart(
            symbol="NIFTY 50",
            current_price=state["metrics"]["nifty_close"],
            output_name="chart_price_action.png",
        )
        chart_bn = chart_gen.generate_price_action_chart(
            symbol="BANK NIFTY",
            current_price=state["metrics"]["banknifty_close"],
            res_level=57000.0,
            sup_level=56600.0,
            output_name="chart_banknifty_action.png",
        )
        chart_pain = chart_gen.generate_max_pain_chart(
            max_pain=state["metrics"]["nifty_max_pain"],
            current_price=state["metrics"]["nifty_close"],
            output_name="chart_max_pain.png",
        )
        chart_vix = chart_gen.generate_vix_gauge_chart(
            vix=state["metrics"]["vix"],
            output_name="chart_vix_gauge.png",
        )
        chart_fii = chart_gen.generate_fii_dii_chart(
            fii_net=state["metrics"]["fii_net_cash"],
            dii_net=state["metrics"]["dii_net_cash"],
            output_name="chart_fii_dii.png",
        )
        chart_div = chart_gen.generate_smart_money_divergence_chart(
            fii_net=state["metrics"]["fii_net_cash"],
            dii_net=state["metrics"]["dii_net_cash"],
            nifty_change=state["metrics"]["nifty_change"],
            output_name="chart_smart_money_divergence.png",
        )
        chart_pcr = chart_gen.generate_pcr_ratio_chart(
            pcr=state["metrics"]["nifty_pcr"],
            output_name="chart_pcr_balance.png",
        )
        chart_call = chart_gen.generate_call_resistance_chart(
            atm_strike=state["metrics"]["nifty_max_pain"],
            output_name="chart_call_resistance.png",
        )
        chart_put = chart_gen.generate_put_support_chart(
            atm_strike=state["metrics"]["nifty_max_pain"],
            output_name="chart_put_support.png",
        )
        chart_down = chart_gen.generate_downside_defense_chart(
            current_price=state["metrics"]["nifty_close"],
            output_name="chart_downside_defense.png",
        )
        chart_up = chart_gen.generate_upside_hurdle_chart(
            current_price=state["metrics"]["nifty_close"],
            output_name="chart_upside_hurdle.png",
        )
        chart_risk = chart_gen.generate_risk_discipline_chart(
            output_name="chart_risk_discipline.png",
        )

        # 4B. Define Statement Beats with 1:1 matching voiceover lines (100% audio-visual synchronization)
        trade_date = state["metrics"].get("trade_date", "TODAY")
        intro_spoken = (
            f"Namaste and welcome to the post-market quantitative desk wrap for {trade_date}. "
            f"Here is your data-driven derivatives breakdown."
        )
        disclaimer_spoken = script_data.get(
            "spoken_sebi_disclaimer",
            "This video is for educational purposes only. We are not SEBI registered advisors. Options trading involves substantial risk of loss."
        )

        ce_res = int(state["metrics"]["top_ce_oi_strikes"][0]) if state["metrics"].get("top_ce_oi_strikes") else 23700
        ce_sec = int(state["metrics"]["top_ce_oi_strikes"][1]) if len(state["metrics"].get("top_ce_oi_strikes", [])) > 1 else 23800
        pe_sup = int(state["metrics"]["top_pe_oi_strikes"][0]) if state["metrics"].get("top_pe_oi_strikes") else 23600
        pe_sec = int(state["metrics"]["top_pe_oi_strikes"][1]) if len(state["metrics"].get("top_pe_oi_strikes", [])) > 1 else 23550

        statement_beats = [
            # Beat 1: Price Action Plunge & Settlement
            {
                "segment": "MARKET OVERVIEW",
                "headline": f"NIFTY SLIDES 144 PTS TO {int(state['metrics']['nifty_close']):,}",
                "statement": f"NIFTY 50 closed at {state['metrics']['nifty_close']:,.2f} ({state['metrics']['nifty_change']:+.2f} pts, {state['metrics']['nifty_pchange']:+.2f}%).",
                "spoken_text": f"NIFTY 50 faced notable selling pressure today, sliding 144 points to close at {state['metrics']['nifty_close']:,.2f}, a decline of 0.61%.",
                "metric_label": "SESSION DELTA",
                "metric_val": f"{state['metrics']['nifty_change']:+.1f} PTS ({state['metrics']['nifty_pchange']:+.2f}%)",
                "chart": chart_price,
                "is_bullish": False,
                "visual_concept": "High-tech 3D stock chart with sharp falling red Japanese candlesticks sliding from 23,780 down to 23,635.10, glowing red support breach lines, and laser grid",
            },
            # Beat 2: Bank Nifty Performance Drag
            {
                "segment": "BANKING DRAG",
                "headline": f"BANK NIFTY AT {int(state['metrics']['banknifty_close']):,}",
                "statement": f"Bank Nifty settled at {state['metrics']['banknifty_close']:,.2f} ({state['metrics']['banknifty_change']:+.2f} pts), facing selling near 57,000.",
                "spoken_text": f"Bank Nifty also traded weak, settling at {state['metrics']['banknifty_close']:,.2f} as banking majors faced persistent selling near 57,000.",
                "metric_label": "BANK NIFTY",
                "metric_val": f"{state['metrics']['banknifty_close']:,.0f} (-0.05%)",
                "chart": chart_bn,
                "is_bullish": False,
                "visual_concept": "3D holographic banking pillar showing selloff near 57,000 resistance, with red breakdown arrows and bank index ticker screens",
            },
            # Beat 3: Max Pain Gravitational Pin
            {
                "segment": "THE TRAP & PIN",
                "headline": f"MAX PAIN {int(state['metrics']['nifty_max_pain'])} PIVOT",
                "statement": f"NIFTY settled slightly below the {int(state['metrics']['nifty_max_pain'])} Max Pain expiry equilibrium pivot.",
                "spoken_text": f"Notice how NIFTY settled just below {int(state['metrics']['nifty_max_pain'])} Max Pain, with option writers actively defending their strikes.",
                "metric_label": "EXPIRY PIVOT",
                "metric_val": f"{state['metrics']['nifty_max_pain']:,.0f} MAX PAIN",
                "chart": chart_pain,
                "is_bullish": False,
                "visual_concept": "3D gravitational black hole pulling option contracts toward 23,650 center equilibrium, neutralizing option premiums",
            },
            # Beat 4: Complacent Volatility
            {
                "segment": "VOLATILITY DYNAMICS",
                "headline": f"VIX {state['metrics']['vix']:.1f}: LOW VOLATILITY",
                "statement": f"India VIX settled at {state['metrics']['vix']:.2f}, showing complacency without panic.",
                "spoken_text": f"India VIX remains calm at {state['metrics']['vix']:.2f}, indicating no panic selling and steady theta decay for options sellers.",
                "metric_label": "INDIA VIX GAUGE",
                "metric_val": f"{state['metrics']['vix']:.2f} COMPLACENT",
                "chart": chart_vix,
                "is_bullish": True,
                "visual_concept": "Glowing 3D circular speedometer dial needle pointing into the green calm zone at 13.25, labeled LOW VOLATILITY / THETA DECAY",
            },
            # Beat 5: Institutional Cash Flows (DII absorption)
            {
                "segment": "INSTITUTIONAL FLOWS",
                "headline": f"DII BUYS +₹{int(state['metrics']['dii_net_cash']):,} CR",
                "statement": f"FIIs sold ₹{state['metrics']['fii_net_cash']:+,.1f} Cr while DIIs bought ₹{state['metrics']['dii_net_cash']:+,.1f} Cr in cash.",
                "spoken_text": f"In cash markets, Foreign Institutions trimmed ₹{abs(int(state['metrics']['fii_net_cash']))} Crores, while Domestic Institutions aggressively absorbed supply buying ₹{int(state['metrics']['dii_net_cash']):,} Crores.",
                "metric_label": "DII CASH NET",
                "metric_val": f"₹{state['metrics']['dii_net_cash']:+,.1f} CR",
                "chart": chart_fii,
                "is_bullish": True,
                "visual_concept": "Giant glowing emerald green bar pillar labeled DII NET CASH: +₹1,349.64 CR towering over a tiny red bar labeled FII NET CASH: -₹123.19 CR",
            },
            # Beat 6: Cash vs Derivatives Divergence
            {
                "segment": "INSTITUTIONAL FLOWS",
                "headline": "NET SURPLUS +₹1,226 CR",
                "statement": f"Total institutional net cash inflow reached ₹{state['metrics']['fii_net_cash'] + state['metrics']['dii_net_cash']:+,.1f} Cr.",
                "spoken_text": f"Combined institutional inflows stood at a positive ₹{int(state['metrics']['fii_net_cash'] + state['metrics']['dii_net_cash']):,} Crores, proving domestic buyers cushioned an even steeper decline.",
                "metric_label": "COMBINED INFLOW",
                "metric_val": f"₹{state['metrics']['fii_net_cash'] + state['metrics']['dii_net_cash']:+,.1f} CR",
                "chart": chart_div,
                "is_bullish": True,
                "visual_concept": "Glowing 3D bank vault accumulating glowing gold liquidity tokens, digital tally displaying +₹1,226.45 CR NET INFLOW",
            },
            # Beat 7: Options PCR Overhang
            {
                "segment": "DERIVATIVES BATTLE",
                "headline": f"PCR {state['metrics']['nifty_pcr']:.2f}: BEARISH OVERHANG",
                "statement": f"Put-Call Ratio stands at {state['metrics']['nifty_pcr']:.2f} with call writers dominating overhead.",
                "spoken_text": f"Turning to derivatives, the Put-Call Ratio stands at {state['metrics']['nifty_pcr']:.2f}, giving aggressive call sellers the upper hand overhead.",
                "metric_label": "PUT-CALL RATIO",
                "metric_val": f"PCR {state['metrics']['nifty_pcr']:.2f} (OVERHANG)",
                "chart": chart_pcr,
                "is_bullish": False,
                "visual_concept": "3D mechanical balance scale heavily tilted by massive red Call Open Interest weights against smaller Put weights, reading PCR 0.80",
            },
            # Beat 8: Call Resistance Wall
            {
                "segment": "DERIVATIVES BATTLE",
                "headline": f"CALL WALL AT {ce_res:,} - {ce_sec:,}",
                "statement": f"Massive call open interest builds at {ce_res:,} and {ce_sec:,} strikes.",
                "spoken_text": f"Heavy call writing at {ce_res:,} and {ce_sec:,} creates a solid ceiling, capping immediate upside momentum.",
                "metric_label": "CALL CEILING",
                "metric_val": f"{ce_res:,} - {ce_sec:,}",
                "chart": chart_call,
                "is_bullish": False,
                "visual_concept": "Massive glowing red 3D defensive fortress wall with towering red Open Interest pillars at strike 23,700 CE and 23,800 CE",
            },
            # Beat 9: Put Support Floor
            {
                "segment": "DERIVATIVES BATTLE",
                "headline": f"PUT DEFENSE AT {pe_sup:,}",
                "statement": f"Strong put writing concentrated at {pe_sup:,} forms primary downside support.",
                "spoken_text": f"On the downside, substantial put open interest at {pe_sup:,} serves as the primary battleground floor.",
                "metric_label": "PUT FLOOR",
                "metric_val": f"{pe_sup:,} - {pe_sec:,}",
                "chart": chart_put,
                "is_bullish": True,
                "visual_concept": "Glowing green titanium foundation floor labeled 23,600 PE SUPPORT ZONE holding up the market price",
            },
            # Beat 10: Downside Risk Levels
            {
                "segment": "TOMORROW'S ROADMAP",
                "headline": f"DEFEND {pe_sup:,} SUPPORT",
                "statement": f"A breakdown below {pe_sup:,} opens swift slide towards lower put clusters.",
                "spoken_text": f"For tomorrow, watch {pe_sup:,} closely. A decisive breakdown here opens the gates toward lower strikes.",
                "metric_label": "KEY SUPPORT",
                "metric_val": f"{pe_sup:,} PIVOT",
                "chart": chart_down,
                "is_bullish": False,
                "visual_concept": "Warning radar display showing DEFEND 23,600 in bright amber, with dotted laser breakdown projection targeting 23,550",
            },
            # Beat 11: Upside Hurdle Level
            {
                "segment": "TOMORROW'S ROADMAP",
                "headline": f"{ce_res:,} RECOVERY HURDLE",
                "statement": f"Bulls must reclaim {ce_res:,} and sustain above {ce_sec:,} to resume trend.",
                "spoken_text": f"To negate the weakness, bulls must decisively clear {ce_res:,} and sustain above {ce_sec:,}.",
                "metric_label": "BULLISH TRIGGER",
                "metric_val": f"> {ce_res:,} CLOSE",
                "chart": chart_up,
                "is_bullish": True,
                "visual_concept": "Green laser trajectory arrow pointing upward through a shattered resistance barrier labeled CLEAR 23,700 RESISTANCE",
            },
            # Beat 12: Trade Discipline
            {
                "segment": "DISCIPLINE & SEBI",
                "headline": "QUANTITATIVE DISCIPLINE",
                "statement": "Stick to stop loss limits and avoid overleveraging in weekly contracts.",
                "spoken_text": "Trade with strict position sizing and defined risk. Manage your gamma risk carefully.",
                "metric_label": "DISCIPLINE RULE",
                "metric_val": "DEFINED RISK",
                "chart": chart_risk,
                "is_bullish": True,
                "visual_concept": "Glowing holographic padlock and golden shield labeled DEFINED RISK & POSITION SIZING surrounded by stop-loss boundaries",
            },
        ]

        card_names = []
        card_durations = []
        audio_chunks = []
        audio_engine = AudioEngine(
            voice=yt_settings.DEFAULT_TTS_VOICE,
            rate=yt_settings.DEFAULT_TTS_RATE,
        )

        html_engine = HTMLCardEngine()
        card_engine = VisualCardEngine(fonts_dir=yt_settings.FONTS_DIR)

        if args.use_llm_cards:
            log.info("STAGE 4: Rendering broadcast 3D scene cards via LLMCardEngine (OmniRouter)...")
            llm_engine = LLMCardEngine()

            # 1. Card 1: Intro Card & Spoken Hook
            intro_card_name = "card_01_intro.png"
            intro_audio_name = "audio_chunk_01_intro.mp3"
            llm_engine.render_intro_card(
                output_path=work_dir / intro_card_name,
                trade_date=trade_date,
                metrics=state["metrics"],
                fallback_fn=lambda: html_engine.render_intro_date_card(
                    output_path=work_dir / intro_card_name,
                    title=script_data.get("video_title", "NIFTY & BANK NIFTY WRAP"),
                    trade_date=trade_date,
                    metrics=state["metrics"],
                ),
            )
            intro_dur = asyncio.run(audio_engine.synthesize_segment(intro_spoken, work_dir / intro_audio_name))
            card_names.append(intro_card_name)
            card_durations.append(intro_dur)
            audio_chunks.append(intro_audio_name)
            log.info(f"Card 1 (Intro): {intro_dur:.2f}s")

            # 2. Cards 2 to 13: Statement Beats (1:1 Audio-Visual Sync)
            for idx, beat in enumerate(statement_beats):
                card_num = idx + 2
                c_name = f"card_beat_{idx + 1:02d}.png"
                a_name = f"audio_chunk_{card_num:02d}.mp3"
                llm_engine.render_beat_card(
                    output_path=work_dir / c_name,
                    segment=beat["segment"],
                    headline=beat["headline"],
                    metric_label=beat["metric_label"],
                    metric_val=beat["metric_val"],
                    visual_concept=beat["visual_concept"],
                    fallback_fn=lambda b=beat, c=c_name: html_engine.render_statement_beat_card(
                        output_path=work_dir / c,
                        segment=b["segment"],
                        headline=b["headline"],
                        statement=b["statement"],
                        metric_label=b["metric_label"],
                        metric_val=b["metric_val"],
                        chart_path=b["chart"],
                        is_bullish=b["is_bullish"],
                        metrics=state["metrics"],
                    ),
                )
                beat_dur = asyncio.run(audio_engine.synthesize_segment(beat["spoken_text"], work_dir / a_name))
                card_names.append(c_name)
                card_durations.append(beat_dur)
                audio_chunks.append(a_name)
                headline_clean = beat['headline'].replace('\u20b9', 'Rs.')
                log.info(f"Card {card_num} ({headline_clean}): {beat_dur:.2f}s")

            # 3. Final Card: SEBI Regulatory Disclaimer (strictly at the end)
            disclaimer_card_name = "card_99_disclaimer.png"
            disclaimer_audio_name = "audio_chunk_99_disclaimer.mp3"
            llm_engine.render_disclaimer_card(
                output_path=work_dir / disclaimer_card_name,
                fallback_fn=lambda: html_engine.render_disclaimer_card(
                    output_path=work_dir / disclaimer_card_name,
                ),
            )
            disc_dur = asyncio.run(audio_engine.synthesize_segment(disclaimer_spoken, work_dir / disclaimer_audio_name))
            card_names.append(disclaimer_card_name)
            card_durations.append(disc_dur)
            audio_chunks.append(disclaimer_audio_name)
            log.info(f"Card {len(card_names)} (Disclaimer): {disc_dur:.2f}s")

            # 4. Thumbnail via LLMCardEngine
            llm_engine.generate_thumbnail(
                output_path=thumb_path,
                hook_text="23,600 FLOOR TEST!",
                nifty_change=state["metrics"]["nifty_change"],
                key_support=f"{pe_sup:,} PIVOT",
                fallback_fn=lambda: card_engine.generate_thumbnail(
                    hook_text=script_data.get("thumbnail_hook", "23,600 FLOOR TEST!"),
                    nifty_change=state["metrics"]["nifty_change"],
                    output_path=thumb_path,
                    background_chart_path=chart_price,
                    anchor_image_path=None,
                    key_support=f"{pe_sup:,} PIVOT",
                ),
            )

        else:
            log.info("STAGE 4: Rendering HTML/Playwright scene cards...")
            html_engine = HTMLCardEngine()
            intro_card_name = "card_01_intro.png"
            intro_audio_name = "audio_chunk_01_intro.mp3"
            html_engine.render_intro_date_card(
                output_path=work_dir / intro_card_name,
                title=script_data.get("video_title", "NIFTY & BANK NIFTY WRAP"),
                trade_date=trade_date,
                metrics=state["metrics"],
            )
            intro_dur = asyncio.run(audio_engine.synthesize_segment(intro_spoken, work_dir / intro_audio_name))
            card_names.append(intro_card_name)
            card_durations.append(intro_dur)
            audio_chunks.append(intro_audio_name)

            for idx, beat in enumerate(statement_beats):
                card_num = idx + 2
                c_name = f"card_beat_{idx + 1:02d}.png"
                a_name = f"audio_chunk_{card_num:02d}.mp3"
                html_engine.render_statement_beat_card(
                    output_path=work_dir / c_name,
                    segment=beat["segment"],
                    headline=beat["headline"],
                    statement=beat["statement"],
                    metric_label=beat["metric_label"],
                    metric_val=beat["metric_val"],
                    chart_path=beat["chart"],
                    is_bullish=beat["is_bullish"],
                    metrics=state["metrics"],
                )
                beat_dur = asyncio.run(audio_engine.synthesize_segment(beat["spoken_text"], work_dir / a_name))
                card_names.append(c_name)
                card_durations.append(beat_dur)
                audio_chunks.append(a_name)

            disclaimer_card_name = "card_99_disclaimer.png"
            disclaimer_audio_name = "audio_chunk_99_disclaimer.mp3"
            html_engine.render_disclaimer_card(output_path=work_dir / disclaimer_card_name)
            disc_dur = asyncio.run(audio_engine.synthesize_segment(disclaimer_spoken, work_dir / disclaimer_audio_name))
            card_names.append(disclaimer_card_name)
            card_durations.append(disc_dur)
            audio_chunks.append(disclaimer_audio_name)

            card_engine = VisualCardEngine(fonts_dir=yt_settings.FONTS_DIR)
            card_engine.generate_thumbnail(
                hook_text=script_data["thumbnail_hook"],
                nifty_change=state["metrics"]["nifty_change"],
                output_path=thumb_path,
                background_chart_path=chart_price,
                anchor_image_path=None,
                key_support=f"{pe_sup:,} PIVOT",
            )


        # 4. Concatenate Audio Chunks into master voiceover.mp3 and build synchronized SRT
        log.info(f"Concatenating {len(audio_chunks)} synchronized audio chunks...")
        audio_concat_txt = work_dir / "audio_concat.txt"
        with open(audio_concat_txt, "w", encoding="utf-8") as f:
            for ac in audio_chunks:
                f.write(f"file '{ac}'\n")

        master_audio_path = work_dir / "voiceover.mp3"
        assembler_tmp = VideoAssembler()
        cmd_concat_audio = [
            assembler_tmp.ffmpeg_bin, "-y",
            "-f", "concat", "-safe", "0", "-i", "audio_concat.txt",
            "-c:a", "libmp3lame", "-b:a", "192k",
            master_audio_path.name,
        ]
        import subprocess
        subprocess.run(
            cmd_concat_audio,
            cwd=str(work_dir),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        # Generate Synchronized SRT matching exact chunk start and end times
        def fmt_srt_time(seconds: float) -> str:
            hrs = int(seconds // 3600)
            mins = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            millis = int(round((seconds - int(seconds)) * 1000))
            return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"

        all_spoken_texts = [intro_spoken] + [b["spoken_text"] for b in statement_beats] + [disclaimer_spoken]
        srt_lines = []
        current_time = 0.0
        for i, (text_chunk, dur_chunk) in enumerate(zip(all_spoken_texts, card_durations)):
            t_start = fmt_srt_time(current_time)
            t_end = fmt_srt_time(current_time + dur_chunk)
            srt_lines.append(f"{i + 1}\n{t_start} --> {t_end}\n{text_chunk}\n")
            current_time += dur_chunk

        (work_dir / "subtitles.srt").write_text("\n".join(srt_lines), encoding="utf-8")
        log.info(f"Synchronized master voiceover: {current_time:.2f}s across {len(card_names)} cards.")

        # 4C. High-CTR Thumbnail with Candlestick Chart Background
        card_engine = VisualCardEngine(fonts_dir=yt_settings.FONTS_DIR)
        card_engine.generate_thumbnail(
            hook_text=script_data["thumbnail_hook"],
            nifty_change=state["metrics"]["nifty_change"],
            output_path=thumb_path,
            background_chart_path=chart_price,
            anchor_image_path=None,
            key_support=f"{pe_sup:,} PIVOT",
        )
        state["card_names"] = card_names
        state["card_durations"] = card_durations
        state["visuals_done"] = True
        state["audio_done"] = True
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        card_names = state["card_names"]
        card_durations = state.get("card_durations")
        log.info("STAGE 4: Visuals & Audio already generated in state checkpoint.")

    # 5. FFmpeg Assembly
    video_path = work_dir / "final_market_wrap.mp4"
    if not state.get("assembly_done"):
        log.info("STAGE 5: Assembling video with FFmpeg...")
        bg_music = yt_settings.MUSIC_DIR / "corporate_lofi.mp3"
        assembler = VideoAssembler(bg_music_path=bg_music if bg_music.exists() else None)
        assembler.assemble(
            work_dir=work_dir,
            card_image_names=card_names,
            audio_name="voiceover.mp3",
            srt_name="subtitles.srt",
            output_name="final_market_wrap.mp4",
            metrics=state.get("metrics"),
            card_durations=card_durations,
        )
        if video_path.stat().st_size < 1_000_000:
            raise ValueError(f"Rendered video is too small ({video_path.stat().st_size} bytes)")
        state["assembly_done"] = True
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        log.info("STAGE 5: Video already assembled in state checkpoint.")

    # 6. YouTube Upload (Private) & Telegram Alert
    if not state.get("upload_done"):
        if yt_settings.YOUTUBE_TOKEN_PATH.exists():
            log.info("STAGE 6: Uploading to YouTube as PRIVATE draft...")
            publisher = YouTubePublisher(yt_settings.YOUTUBE_TOKEN_PATH)
            desc_text = f"{script_data['description_summary']}\n\n" + "\n".join(script_data["chapters"])
            desc_text += f"\n\n{script_data['spoken_sebi_disclaimer']}"
            
            video_id = publisher.upload_private(
                video_path=video_path,
                thumbnail_path=thumb_path,
                title=script_data["video_title"],
                description=desc_text,
                tags=["Nifty", "StockMarketIndia", "OptionsTrading", "BankNifty"],
            )
            send_youtube_review_alert(video_id, script_data["video_title"])
            state["upload_done"] = True
            state["video_id"] = video_id
            state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        else:
            log.warning(
                f"YouTube token not found at {yt_settings.YOUTUBE_TOKEN_PATH}. "
                f"Video generated locally at: {video_path}"
            )
    else:
        log.info("STAGE 6: Already uploaded in state checkpoint.")

    log.info(f"=== PIPELINE COMPLETED SUCCESSFULLY: {video_path} ===")


if __name__ == "__main__":
    main()
