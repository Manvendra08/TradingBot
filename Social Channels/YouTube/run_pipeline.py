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

    work_dir = BASE_DIR / "output" / now.strftime("%Y-%m-%d")
    work_dir.mkdir(parents=True, exist_ok=True)
    state_file = work_dir / "state.json"

    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    # 1. Market Data Extraction
    if not state.get("market_data_done"):
        log.info("STAGE 1: Reading settled market data...")
        fii_net, dii_net = fetch_fii_dii_cash()
        reader = DBMarketReader(yt_settings.NSEBOT_DB_PATH)
        metrics = reader.get_latest_market_metrics(fii_net, dii_net)
        state["metrics"] = metrics.__dict__
        state["market_data_done"] = True
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        log.info("STAGE 1: Market data already processed in state checkpoint.")

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
        
        # 4A. Render 7 Specialized High-Resolution Micro-Charts
        chart_gen = MarketChartGenerator(work_dir)
        chart_price = chart_gen.generate_price_action_chart(
            current_price=state["metrics"]["nifty_close"],
            output_name="chart_price_action.png",
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

        # 4B. Define Statement Beats for 4-15s granular TV pacing (11 unique visual stages)
        statement_beats = [
            # Scene 1: The Trap
            {
                "segment": "THE TRAP",
                "headline": "NIFTY PLUNGES 145 PTS",
                "statement": "NIFTY 50 after early strength plunged a massive 145 points closing at 24810.50.",
                "metric_label": "SESSION DELTA",
                "metric_val": f"{state['metrics']['nifty_change']:+.1f} PTS ({state['metrics']['nifty_pchange']:+.2f}%)",
                "chart": chart_price,
                "is_bullish": False,
            },
            {
                "segment": "THE TRAP",
                "headline": "MAX PAIN 24,800 TRAP",
                "statement": "NIFTY closed precisely at the Max Pain level of 24800! Was this calculated?",
                "metric_label": "EXPIRY PIVOT",
                "metric_val": f"{state['metrics']['nifty_max_pain']:,.0f} PIN PIVOT",
                "chart": chart_pain,
                "is_bullish": False,
            },
            {
                "segment": "THE TRAP",
                "headline": "VIX 13.9: CALM OVERHANG",
                "statement": "The India VIX, calm at 13.90, didn't signal panic, yet the market bled.",
                "metric_label": "INDIA VIX GAUGE",
                "metric_val": f"{state['metrics']['vix']:.2f} COMPLACENT",
                "chart": chart_vix,
                "is_bullish": True,
            },
            # Scene 2: Institutional Flows
            {
                "segment": "INSTITUTIONAL FLOWS",
                "headline": "FIIS NET BUY ₹280 CR",
                "statement": "FIIs were net buyers pumping in ₹280 Cr while DIIs bought ₹566 Cr.",
                "metric_label": "FII NET CASH",
                "metric_val": f"₹{state['metrics']['fii_net_cash']:+,.1f} CR",
                "chart": chart_fii,
                "is_bullish": True,
            },
            {
                "segment": "INSTITUTIONAL FLOWS",
                "headline": "SMART MONEY DIVERGENCE",
                "statement": "Both FIIs and DIIs were net buyers, signaling sector rotation and profit booking.",
                "metric_label": "COMBINED INFLOW",
                "metric_val": f"₹{state['metrics']['fii_net_cash'] + state['metrics']['dii_net_cash']:+,.1f} CR",
                "chart": chart_div,
                "is_bullish": True,
            },
            # Scene 3: Derivatives Battleground
            {
                "segment": "DERIVATIVES BATTLE",
                "headline": "PCR 0.82: BEARISH OVERHANG",
                "statement": "Put-Call Ratio stands at 0.82. Call writers dominate overhead resistance.",
                "metric_label": "PUT-CALL RATIO",
                "metric_val": f"PCR {state['metrics']['nifty_pcr']:.2f} (BEARISH)",
                "chart": chart_pcr,
                "is_bullish": False,
            },
            {
                "segment": "DERIVATIVES BATTLE",
                "headline": "CALL WRITERS AT 24,900",
                "statement": "Heavy Call writing at 24900 and 25000 forms formidable resistance zones.",
                "metric_label": "CALL CEILING",
                "metric_val": "24,900 - 25,000",
                "chart": chart_call,
                "is_bullish": False,
            },
            {
                "segment": "DERIVATIVES BATTLE",
                "headline": "PUT WRITERS AT 24,700",
                "statement": "Strong Put writing at 24700 and 24600 provides essential downside cushions.",
                "metric_label": "PUT FLOOR",
                "metric_val": "24,700 - 24,500",
                "chart": chart_put,
                "is_bullish": True,
            },
            # Scene 4: Tomorrow's Roadmap
            {
                "segment": "TOMORROW'S ROADMAP",
                "headline": "DEFEND 24,700 SUPPORT",
                "statement": "Immediate support lies at 24700. Break below triggers tests of 24600 and 24500.",
                "metric_label": "KEY SUPPORT",
                "metric_val": "24,700 PIVOT",
                "chart": chart_down,
                "is_bullish": False,
            },
            {
                "segment": "TOMORROW'S ROADMAP",
                "headline": "25,000 RECOVERY HURDLE",
                "statement": "Sustained trade above 25000 is required for bulls to regain momentum.",
                "metric_label": "BULLISH TRIGGER",
                "metric_val": "> 25,000 CLOSE",
                "chart": chart_up,
                "is_bullish": True,
            },
            {
                "segment": "DISCIPLINE & SEBI",
                "headline": "QUANTITATIVE DISCIPLINE",
                "statement": "Educational analysis only. We are not SEBI registered advisors. Trade with discipline.",
                "metric_label": "REGULATORY NOTICE",
                "metric_val": "NOT SEBI ADVICE",
                "chart": chart_risk,
                "is_bullish": False,
            },
        ]

        card_engine = VisualCardEngine(fonts_dir=yt_settings.FONTS_DIR)
        card_names = []

        for idx, beat in enumerate(statement_beats):
            c_name = f"card_beat_{idx + 1:02d}.png"
            card_engine.generate_broadcast_card(
                output_path=work_dir / c_name,
                segment_tag=beat["segment"],
                headline=beat["headline"],
                statement_text=beat["statement"],
                metric_label=beat["metric_label"],
                metric_val=beat["metric_val"],
                chart_image_path=beat["chart"],
                is_bullish=beat["is_bullish"],
                burn_statement_text=False,
            )
            card_names.append(c_name)

        # 4C. High-CTR Thumbnail with Candlestick Chart Background & User Anchor
        anchor_img = work_dir / "anchor_studio.jpg"
        if not anchor_img.exists():
            default_anchor = BASE_DIR / "assets" / "anchor" / "anchor_lead_primary.jpg"
            if default_anchor.exists():
                anchor_img = default_anchor
        card_engine.generate_thumbnail(
            hook_text=script_data["thumbnail_hook"],
            nifty_change=state["metrics"]["nifty_change"],
            output_path=thumb_path,
            background_chart_path=chart_price,
            anchor_image_path=anchor_img if anchor_img.exists() else None,
        )
        state["card_names"] = card_names
        state["visuals_done"] = True
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    else:
        card_names = state["card_names"]
        log.info("STAGE 4: Visuals already generated in state checkpoint.")

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
