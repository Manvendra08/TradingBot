"""
Social Channels/YouTube/generators/visual_card_engine.py
Generates deterministic 1080p broadcast-quality scene cards and high-CTR thumbnails using PIL.
Layout Standard: Bloomberg / CNBC Pro 30/70 Asymmetrical Television Grid.
"""
from __future__ import annotations

import logging
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)


class VisualCardEngine:
    def __init__(self, fonts_dir: Path | None = None):
        self.fonts_dir = fonts_dir or Path(__file__).resolve().parents[1] / "assets" / "fonts"
        self.width = 1920
        self.height = 1080

    def _get_font(self, size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        font_names = (
            ["Montserrat-Bold.ttf", "arialbd.ttf", "segoeuib.ttf", "Roboto-Bold.ttf"]
            if bold
            else ["Montserrat-Regular.ttf", "arial.ttf", "segoeui.ttf", "Roboto-Regular.ttf"]
        )

        # 1. Try custom fonts directory
        if self.fonts_dir and self.fonts_dir.exists():
            for name in font_names:
                fp = self.fonts_dir / name
                if fp.exists():
                    try:
                        return ImageFont.truetype(str(fp), size)
                    except Exception:
                        pass

        # 2. Windows system fonts
        for name in font_names:
            sys_path = Path("C:/Windows/Fonts") / name
            if sys_path.exists():
                try:
                    return ImageFont.truetype(str(sys_path), size)
                except Exception:
                    pass

        # 3. Default fallback
        try:
            return ImageFont.truetype("arial.ttf", size)
        except Exception:
            return ImageFont.load_default()

    def generate_broadcast_card(
        self,
        output_path: Path,
        segment_tag: str,
        headline: str,
        statement_text: str,
        metric_label: str,
        metric_val: str,
        chart_image_path: Path | None = None,
        is_bullish: bool = True,
        telemetry_items: list[tuple[str, str]] | None = None,
        burn_statement_text: bool = False,
    ) -> Path:
        """
        Renders a Bloomberg/CNBC style 30/70 asymmetrical television broadcast composition:
        - Zone A (Top Header): Network brand & session telemetry
        - Zone B (Left Pillar - 30%): Telemetry pillar with segment pill, headline, & key metrics
        - Zone C (Right Stage - 70%): Floating financial data canvas
        - Zone D (Lower Third): Analysis quote deck / live caption dock
        - Zone E (Bottom): 60px reserved for live scrolling ticker tape
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (self.width, self.height), color=(7, 11, 20))
        draw = ImageDraw.Draw(img)

        # 1. Subtle Background Grid Texture
        for x in range(0, self.width, 80):
            draw.line([(x, 0), (x, self.height)], fill=(15, 23, 42), width=1)
        for y in range(0, self.height, 80):
            draw.line([(0, y), (self.width, y)], fill=(15, 23, 42), width=1)

        # 2. Zone A: Top Header Slug (Y: 0 to 52)
        draw.rectangle([0, 0, self.width, 52], fill=(11, 17, 32))
        draw.line([(0, 52), (self.width, 52)], fill=(30, 41, 59), width=1)
        
        font_slug_b = self._get_font(16, bold=True)
        font_slug_r = self._get_font(15, bold=False)
        draw.text((40, 16), "NSEBOT ALGORITHMIC WRAP", font=font_slug_b, fill=(0, 229, 255))
        draw.text((320, 17), "•   POST-MARKET DERIVATIVES INTELLIGENCE", font=font_slug_r, fill=(148, 163, 184))

        # 3. Zone B: Left Telemetry Pillar (X: 40, Y: 70, W: 500, H: 830)
        draw.rounded_rectangle([40, 70, 540, 900], radius=12, fill=(15, 23, 42), outline=(30, 41, 59), width=2)

        # Segment Pill
        accent_color = (16, 185, 129) if is_bullish else (244, 63, 94)  # Emerald or Crimson
        draw.rounded_rectangle([65, 95, 290, 135], radius=6, fill=accent_color)
        draw.text((80, 103), segment_tag.upper(), font=self._get_font(16, bold=True), fill=(255, 255, 255))

        # Primary Segment Headline
        draw.text((65, 155), headline[:32], font=self._get_font(26, bold=True), fill=(248, 250, 252))

        # Metric Card Box
        draw.rounded_rectangle([65, 225, 515, 335], radius=8, fill=(11, 17, 32), outline=(51, 65, 85), width=1)
        draw.text((85, 240), metric_label.upper(), font=self._get_font(13, bold=False), fill=(148, 163, 184))
        draw.text((85, 268), metric_val[:22], font=self._get_font(32, bold=True), fill=accent_color)

        # Structured Telemetry Indicators
        default_telemetry = [
            ("INDEX REGIME", "TREND BREAKDOWN" if not is_bullish else "BULLISH EXPANSION"),
            ("SESSION VOLATILITY", "COMPLACENT (INDIA VIX 13.9)"),
            ("SMART MONEY FLOW", "NET BUYING (+₹847 CR)"),
            ("OPTIONS EXPIRY PIVOT", "MAX PAIN 24,800"),
        ]
        items = telemetry_items or default_telemetry
        curr_y = 350
        for label, val in items[:4]:
            draw.text((65, curr_y), label, font=self._get_font(12, bold=False), fill=(100, 116, 139))
            draw.text((65, curr_y + 18), val[:32], font=self._get_font(15, bold=True), fill=(226, 232, 240))
            curr_y += 56

        # Session Range & Intraday Structure Box (Y: 578 to 755) - eliminates 260px void
        draw.rounded_rectangle([65, 578, 515, 755], radius=8, fill=(11, 17, 32), outline=(30, 41, 59), width=1)
        draw.text((80, 592), "INTRADAY STRUCTURE & RANGE", font=self._get_font(11, bold=True), fill=(0, 229, 255))
        
        draw.text((80, 618), "DAY HIGH / LOW", font=self._get_font(11, bold=False), fill=(100, 116, 139))
        draw.text((80, 634), "24,957.50 / 24,788.10", font=self._get_font(13, bold=True), fill=(226, 232, 240))
        
        draw.text((80, 660), "SESSION VWAP", font=self._get_font(11, bold=False), fill=(100, 116, 139))
        draw.text((80, 676), "24,845.20 (BELOW VWAP)" if not is_bullish else "24,845.20 (ABOVE VWAP)", font=self._get_font(13, bold=True), fill=(244, 63, 94) if not is_bullish else (16, 185, 129))

        draw.text((80, 702), "ALGO STRATEGY BIAS", font=self._get_font(11, bold=False), fill=(100, 116, 139))
        draw.text((80, 718), "TFSS STRANGLE / HEDGED CREDIT", font=self._get_font(13, bold=True), fill=(245, 158, 11))

        # Execution Desk Badge (Y: 775 to 835)
        draw.rounded_rectangle([65, 775, 515, 835], radius=6, fill=(15, 23, 42), outline=(51, 65, 85), width=1)
        draw.text((80, 788), "DESK STATUS: LIVE TFSS v4.0", font=self._get_font(12, bold=True), fill=(16, 185, 129))
        draw.text((80, 808), "100% SIZING • STRICT 0.60 DELTA CAP", font=self._get_font(10, bold=False), fill=(148, 163, 184))

        # Micro Brand Watermark in Pillar Footer (Y: 865)
        draw.text((65, 865), "QUANTITATIVE EXECUTION DESK • PRO TV", font=self._get_font(11, bold=True), fill=(71, 85, 105))

        # 4. Zone C: Main Stage Canvas (X: 560, Y: 70, W: 1320, H: 830)
        draw.rounded_rectangle([560, 70, 1880, 900], radius=12, fill=(11, 17, 32), outline=(30, 41, 59), width=2)

        if chart_image_path and chart_image_path.exists():
            try:
                chart_img = Image.open(chart_image_path).convert("RGB")
                chart_resized = chart_img.resize((1280, 790), Image.Resampling.LANCZOS)
                img.paste(chart_resized, (580, 90))
            except Exception as exc:
                log.warning(f"Could not load chart {chart_image_path}: {exc}")

        # 5. Zone D: Lower-Third Analysis Statement Deck / Subtitle Dock (Y: 915 to 1005)
        draw.rounded_rectangle([40, 915, 1880, 1005], radius=8, fill=(15, 23, 42), outline=(51, 65, 85), width=1)
        draw.rounded_rectangle([40, 915, 190, 1005], radius=8, fill=(0, 229, 255))
        draw.text((56, 948), "ANALYSIS", font=self._get_font(18, bold=True), fill=(11, 17, 32))
        
        # Audio feed tracker slug inside dock
        draw.text((215, 926), "AUDIO FEED // LIVE MARKET DISPATCH", font=self._get_font(12, bold=True), fill=(100, 116, 139))

        if burn_statement_text and statement_text:
            display_statement = statement_text[:115]
            draw.text((215, 952), f'"{display_statement}"', font=self._get_font(18, bold=False), fill=(241, 245, 249))

        img.save(output_path, quality=95)
        log.info(f"Generated broadcast-grade 30/70 scene card: {output_path}")
        return output_path

    def generate_scene_card(
        self,
        segment_title: str,
        headline: str,
        metric: str,
        is_bullish: bool,
        output_path: Path,
        chart_image_path: Path | None = None,
        statement_text: str | None = None,
    ) -> Path:
        """Backward-compatible adapter forwarding to generate_broadcast_card."""
        stat_text = statement_text or headline
        return self.generate_broadcast_card(
            output_path=output_path,
            segment_tag=segment_title,
            headline=headline,
            statement_text=stat_text,
            metric_label="KEY METRIC",
            metric_val=metric,
            chart_image_path=chart_image_path,
            is_bullish=is_bullish,
        )

    def generate_thumbnail(
        self,
        hook_text: str,
        nifty_change: float,
        output_path: Path,
        background_chart_path: Path | None = None,
        anchor_image_path: Path | None = None,
    ) -> Path:
        """
        Generates a broadcast-grade 1280x720 YouTube thumbnail:
        - Features the Anchor in high-tech studio on the right
        - High-impact 3D typography & delta pill on the left
        - Clear support levels and action CTA banner
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (1280, 720), color=(7, 11, 20))

        # 1. Background: Candlestick chart with dark navy vignette
        if background_chart_path and background_chart_path.exists():
            try:
                bg = Image.open(background_chart_path).convert("RGBA")
                bg_resized = bg.resize((1280, 720), Image.Resampling.LANCZOS)
                overlay = Image.new("RGBA", (1280, 720), (7, 11, 20, 230))
                composited = Image.alpha_composite(bg_resized, overlay)
                img = composited.convert("RGB")
            except Exception as exc:
                log.warning(f"Could not load thumbnail background chart: {exc}")

        # 2. Check for anchor image (passed or default from assets/anchor/)
        actual_anchor = anchor_image_path
        if not actual_anchor or not actual_anchor.exists():
            default_anchor = Path(__file__).resolve().parents[1] / "assets" / "anchor" / "anchor_lead_primary.jpg"
            if default_anchor.exists():
                actual_anchor = default_anchor

        # 3. Composite Anchor on Right Stage (X: 580 to 1280) with smooth left alpha gradient
        if actual_anchor and actual_anchor.exists():
            try:
                anc_raw = Image.open(actual_anchor).convert("RGBA")
                # Crop anchor torso & desk centered
                anc_w, anc_h = anc_raw.size
                # Anchor is in center, take width 0.35 to 0.85
                crop_box = (int(anc_w * 0.28), 0, int(anc_w * 0.85), anc_h)
                anc_cropped = anc_raw.crop(crop_box)
                anc_resized = anc_cropped.resize((680, 720), Image.Resampling.LANCZOS)

                # Create smooth linear horizontal gradient mask on left edge (fade in over 140px)
                mask = Image.new("L", (680, 720), 255)
                for x in range(140):
                    alpha = int((x / 140.0) * 255)
                    for y in range(720):
                        mask.putpixel((x, y), alpha)

                img.paste(anc_resized, (600, 0), mask)
            except Exception as exc:
                log.warning(f"Could not composite anchor into thumbnail: {exc}")

        draw = ImageDraw.Draw(img)
        is_bullish = nifty_change >= 0
        accent = (16, 185, 129) if is_bullish else (244, 63, 94)

        # 4. Broadcast Border Frame
        draw.rectangle([0, 0, 1279, 719], outline=(30, 41, 59), width=3)
        draw.line([(0, 4), (1280, 4)], fill=(0, 229, 255), width=4)

        # 5. Top Left Delta Pill
        draw.rounded_rectangle([45, 40, 440, 115], radius=14, fill=accent)
        draw.text((65, 54), f"NIFTY {nifty_change:+.0f} PTS", font=self._get_font(42, bold=True), fill=(255, 255, 255))

        # 6. Top Right Desk Brand Badge
        draw.rounded_rectangle([940, 40, 1235, 95], radius=8, fill=(11, 17, 32), outline=(0, 229, 255), width=2)
        draw.text((960, 56), "NSEBOT QUANT DESK", font=self._get_font(18, bold=True), fill=(0, 229, 255))

        # 7. High-Impact Hook Headline Box (Dark Translucent Card on Left)
        draw.rounded_rectangle([45, 145, 680, 360], radius=14, fill=(11, 17, 32), outline=(51, 65, 85), width=2)
        draw.text((68, 165), "SPECIAL EXPIRY BRIEFING // EXCLUSIVE", font=self._get_font(16, bold=True), fill=(245, 158, 11))

        hook_display = hook_text.upper()
        # 3D Drop Shadow
        draw.text((71, 203), hook_display, font=self._get_font(68, bold=True), fill=(0, 0, 0))
        draw.text((69, 201), hook_display, font=self._get_font(68, bold=True), fill=(0, 0, 0))
        draw.text((68, 199), hook_display, font=self._get_font(68, bold=True), fill=(255, 255, 255))

        draw.text((68, 298), "FII/DII FOOTPRINT • OPTIONS OI • BATTLEGROUND", font=self._get_font(16, bold=True), fill=(148, 163, 184))

        # 8. Support Level Callout Pill
        draw.rounded_rectangle([45, 390, 680, 485], radius=12, fill=(15, 23, 42), outline=(30, 41, 59), width=2)
        draw.text((68, 405), "CRITICAL WATCHPOINT FOR TOMORROW", font=self._get_font(13, bold=False), fill=(148, 163, 184))
        draw.text((68, 430), "KEY SUPPORT: 24,700 PIVOT", font=self._get_font(26, bold=True), fill=(16, 185, 129))

        # 9. Action CTA Banner at Bottom
        draw.rounded_rectangle([45, 520, 1235, 645], radius=14, fill=(15, 23, 42), outline=(0, 229, 255), width=2)
        draw.text((70, 545), "WATCH FULL QUANT DERIVATIVES ROADMAP  >>", font=self._get_font(32, bold=True), fill=(255, 255, 255))
        draw.text((70, 595), "INSTITUTIONAL OI DECODED • TARGETS • STOP LOSSES", font=self._get_font(16, bold=True), fill=(0, 229, 255))

        img.save(output_path, quality=95)
        log.info(f"Generated broadcast thumbnail with anchor: {output_path}")
        return output_path
