"""
Social Channels/YouTube/compositors/video_assembler.py
Broadcast-grade FFmpeg video assembly engine:
1. Cinematic Ken Burns camera motion (push-in, pan, pull-out) per scene card
2. AI Anchor hook clip integration (auto-detected or configured)
3. Bloomberg/CNBC live scrolling market ticker tape (NIFTY, BANKNIFTY, VIX, FII/DII, PCR, Max Pain)
4. Dynamic audio mixing with ducked background music
5. High-contrast subtitle burning with safe vertical margin
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class VideoAssembler:
    def __init__(self, bg_music_path: Path | None = None, ffmpeg_bin: str | None = None):
        self.bg_music_path = bg_music_path
        self.ffmpeg_bin = ffmpeg_bin or self._find_ffmpeg()

    def _find_ffmpeg(self) -> str:
        import os
        import shutil

        # 1. Custom ENV
        env_path = os.environ.get("FFMPEG_BINARY")
        if env_path and os.path.exists(env_path):
            return env_path

        # 2. System PATH
        which_path = shutil.which("ffmpeg")
        if which_path:
            return which_path

        # 3. imageio_ffmpeg fallback
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass

        return "ffmpeg"

    def _get_media_duration(self, work_dir: Path, media_name: str) -> float:
        """Extracts media duration in seconds via ffmpeg inspection."""
        media_path = work_dir / media_name
        if not media_path.exists():
            return 180.0

        try:
            cmd = [self.ffmpeg_bin, "-i", media_name]
            res = subprocess.run(
                cmd,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr)
            if match:
                hours = int(match.group(1))
                mins = int(match.group(2))
                secs = float(match.group(3))
                return hours * 3600 + mins * 60 + secs
        except Exception as exc:
            log.warning(f"Could not parse duration for {media_name}: {exc}")
        return 180.0

    def _build_ticker_string(self, metrics: dict | None) -> str:
        """Formats real telemetry into a Bloomberg/CNBC style horizontal ticker string."""
        if not metrics:
            return "NSEBOT MARKET INTELLIGENCE | ALGORITHMIC DERIVATIVES ANALYSIS | FOR EDUCATIONAL PURPOSES ONLY"

        parts: list[str] = []

        # 1. NIFTY 50
        n_close = metrics.get("nifty_close")
        if n_close:
            n_chg = metrics.get("nifty_change", 0.0)
            n_pchg = metrics.get("nifty_pchange", 0.0)
            sign = "+" if n_chg >= 0 else ""
            parts.append(f"NIFTY 50: {n_close:,.2f} ({sign}{n_chg:.2f}, {sign}{n_pchg:.2f}%)")

        # 2. BANK NIFTY
        bn_close = metrics.get("banknifty_close")
        if bn_close:
            bn_chg = metrics.get("banknifty_change", 0.0)
            bn_pchg = metrics.get("banknifty_pchange", 0.0)
            bn_sign = "+" if bn_chg >= 0 else ""
            parts.append(f"BANKNIFTY: {bn_close:,.2f} ({bn_sign}{bn_chg:.2f}, {bn_sign}{bn_pchg:.2f}%)")

        # 3. INDIA VIX
        vix = metrics.get("vix")
        if vix:
            parts.append(f"INDIA VIX: {vix:.2f}")

        # 4. Institutional Cash Flows
        fii = metrics.get("fii_net_cash")
        if fii is not None:
            parts.append(f"FII NET CASH: {fii:+,.2f} Cr")
        dii = metrics.get("dii_net_cash")
        if dii is not None:
            parts.append(f"DII NET CASH: {dii:+,.2f} Cr")

        # 5. Derivatives Signals
        pcr = metrics.get("nifty_pcr")
        if pcr is not None:
            sentiment = "Bullish" if pcr > 1.05 else ("Bearish" if pcr < 0.90 else "Neutral")
            parts.append(f"PCR: {pcr:.2f} ({sentiment})")

        mp = metrics.get("nifty_max_pain")
        if mp:
            parts.append(f"MAX PAIN: {mp:,.0f}")

        # 6. Key Strikes
        ce_strikes = metrics.get("top_ce_oi_strikes", [])
        if ce_strikes:
            ce_str = ", ".join([f"{int(s)}" for s in ce_strikes[:2]])
            parts.append(f"CALL RESISTANCE: {ce_str}")

        pe_strikes = metrics.get("top_pe_oi_strikes", [])
        if pe_strikes:
            pe_str = ", ".join([f"{int(s)}" for s in pe_strikes[:2]])
            parts.append(f"PUT SUPPORT: {pe_str}")

        # 7. Brand / Legal
        parts.append("NSEBOT ALGORITHMIC TRADING INTELLIGENCE")
        parts.append("FOR EDUCATIONAL PURPOSES ONLY - NOT SEBI ADVISORY")

        return "   |   ".join(parts)

    def _render_ken_burns_clip(
        self,
        work_dir: Path,
        card_image: str,
        duration: float,
        clip_name: str,
        motion_index: int = 0,
    ) -> Path:
        """
        Renders a static card image into a 1080p24 video clip with cinematic Ken Burns camera motion.
        """
        out_path = work_dir / clip_name
        frames = max(24, int(round(duration * 24)))

        # Gentle boundary-safe Ken Burns zoom: subtle 1.00 -> 1.025 or 1.025 -> 1.00 centered.
        # Absolutely NO horizontal/vertical panning offsets that can push borders out of frame.
        if motion_index % 2 == 0:
            # Subtle push-in centered
            dz = 0.025 / frames
            vf = (
                f"scale=3840:2160:flags=lanczos,"
                f"zoompan=z='min(zoom+{dz:.6f},1.025)':d={frames}:"
                f"x='trunc((iw-iw/zoom)/2)':y='trunc((ih-ih/zoom)/2)':s=1920x1080:fps=24"
            )
        else:
            # Subtle pull-out centered
            dz = 0.025 / frames
            vf = (
                f"scale=3840:2160:flags=lanczos,"
                f"zoompan=z='if(lte(on,1),1.025,max(1.0,zoom-{dz:.6f}))':d={frames}:"
                f"x='trunc((iw-iw/zoom)/2)':y='trunc((ih-ih/zoom)/2)':s=1920x1080:fps=24"
            )

        cmd = [
            self.ffmpeg_bin, "-y",
            "-loop", "1", "-i", card_image,
            "-vf", vf,
            "-t", f"{duration:.2f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            clip_name,
        ]

        subprocess.run(
            cmd,
            cwd=str(work_dir),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return out_path

    def _create_docked_ass(self, work_dir: Path, srt_name: str, ass_name: str = "dock_subtitles.ass") -> Path:
        """Converts SRT subtitles into an exact 1080p ASS file docked inside Zone D."""
        import re
        srt_path = work_dir / srt_name
        ass_path = work_dir / ass_name

        ass_header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 1920\n"
            "PlayResY: 1080\n"
            "ScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: BroadcastDock, Segoe UI, 22, &H00FFFFFF, &H000000FF, &H00000000, &H00000000, 1, 0, 0, 0, 100, 100, 0, 0, 1, 2, 0, 1, 210, 60, 105, 1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        events = []
        if srt_path.exists():
            srt_text = srt_path.read_text(encoding="utf-8")

            def srt_time_to_ass(t_str: str) -> str:
                parts = t_str.strip().replace(",", ".").split(":")
                h = int(parts[0])
                m = parts[1]
                s = parts[2][:5]
                return f"{h}:{m}:{s}"

            blocks = re.split(r"\n\s*\n", srt_text.strip())
            for b in blocks:
                lines = [l.strip() for l in b.splitlines() if l.strip()]
                if len(lines) >= 3:
                    times = lines[1].split("-->")
                    if len(times) == 2:
                        start = srt_time_to_ass(times[0])
                        end = srt_time_to_ass(times[1])
                        text = " ".join(lines[2:]).replace("\n", " ")
                        events.append(f"Dialogue: 0,{start},{end},BroadcastDock,,0,0,0,,{text}")

        ass_path.write_text(ass_header + "\n".join(events), encoding="utf-8")
        log.info(f"Generated 1080p docked ASS subtitles: {ass_path} ({len(events)} events)")
        return ass_path

    def _normalize_anchor_clip(
        self,
        work_dir: Path,
        anchor_video_name: str,
        output_name: str = "clip_anchor_norm.mp4",
    ) -> tuple[Path, float]:
        """Scales and normalizes anchor video to 1920x1080 24fps with broadcast studio branding."""
        out_path = work_dir / output_name
        dur = self._get_media_duration(work_dir, anchor_video_name)

        vf = (
            "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=24,"
            "drawbox=y=80:x=60:width=520:height=80:color=0x0b1120@0.88:t=fill,"
            "drawbox=y=80:x=60:width=8:height=80:color=0x00E5FF:t=fill,"
            "drawtext=fontfile='C\\:/Windows/Fonts/arialbd.ttf':text='NSEBOT QUANTITATIVE DESK':fontcolor=0x00E5FF:fontsize=15:x=85:y=98,"
            "drawtext=fontfile='C\\:/Windows/Fonts/arialbd.ttf':text='POST-MARKET DERIVATIVES INTELLIGENCE':fontcolor=white:fontsize=18:x=85:y=128"
        )

        cmd = [
            self.ffmpeg_bin, "-y",
            "-i", anchor_video_name,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-an",
            output_name,
        ]
        subprocess.run(
            cmd,
            cwd=str(work_dir),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return out_path, dur

    def _render_anchor_image_clip(
        self,
        work_dir: Path,
        anchor_image_path: Path,
        duration: float = 8.0,
        output_name: str = "clip_anchor_norm.mp4",
        audio_path: Path | None = None,
    ) -> tuple[Path, float]:
        """Renders an anchor video clip with GPU-accelerated DirectML lip-sync or Ken Burns fallback."""
        out_path = work_dir / output_name

        # 1. Attempt DirectML Talking-Head Lip-Sync if audio is provided
        if audio_path and audio_path.exists():
            try:
                from generators.directml_talking_head import DirectMLTalkingHead
                log.info("Generating realistic DirectML talking-head anchor video...")
                talking_engine = DirectMLTalkingHead(use_gpu=True)
                raw_talk_clip = work_dir / "raw_talking_anchor.mp4"
                talking_engine.generate(
                    image_path=str(anchor_image_path),
                    audio_path=str(audio_path),
                    output_path=str(raw_talk_clip),
                    max_duration=duration,
                    batch_size=8,
                )
                if raw_talk_clip.exists():
                    log.info("Normalizing DirectML talking-head clip with broadcast overlay...")
                    norm_path, actual_dur = self._normalize_anchor_clip(
                        work_dir, raw_talk_clip.name, output_name=output_name
                    )
                    try:
                        raw_talk_clip.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return norm_path, actual_dur
            except Exception as exc:
                log.warning(f"DirectML talking head failed ({exc}). Falling back to Ken Burns motion.")

        # 2. Fallback: Ken Burns motion zoompan
        d_frames = int(duration * 24)
        vf = (
            f"scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=24,"
            f"zoompan=z='min(zoom+0.0003,1.04)':d={d_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1920x1080:fps=24,"
            "drawbox=y=80:x=60:width=520:height=80:color=0x0b1120@0.88:t=fill,"
            "drawbox=y=80:x=60:width=8:height=80:color=0x00E5FF:t=fill,"
            "drawtext=fontfile='C\\:/Windows/Fonts/arialbd.ttf':text='NSEBOT QUANTITATIVE DESK':fontcolor=0x00E5FF:fontsize=15:x=85:y=98,"
            "drawtext=fontfile='C\\:/Windows/Fonts/arialbd.ttf':text='POST-MARKET DERIVATIVES INTELLIGENCE':fontcolor=white:fontsize=18:x=85:y=128"
        )
        cmd = [
            self.ffmpeg_bin, "-y",
            "-loop", "1",
            "-i", str(anchor_image_path),
            "-t", f"{duration:.2f}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-an",
            output_name,
        ]
        subprocess.run(
            cmd,
            cwd=str(work_dir),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return out_path, duration

    def assemble(
        self,
        work_dir: Path,
        card_image_names: list[str],
        audio_name: str,
        srt_name: str,
        output_name: str = "final_output.mp4",
        metrics: dict | None = None,
        anchor_video_name: str | None = None,
        card_durations: list[float] | None = None,
    ) -> Path:
        """
        Full broadcast assembly pipeline:
        1. Normalizes anchor hook video (if present) or renders user anchor clip
        2. Renders each scene card with kinetic Ken Burns camera motion
        3. Stitches clips sequentially
        4. Overlays Bloomberg-style live scrolling ticker tape
        5. Burns subtitles docked cleanly inside Zone D lower-third
        6. Mixes voiceover with ducked background music
        """
        final_mp4 = work_dir / output_name
        log.info(f"Starting broadcast assembly in: {work_dir}")

        # 0. Fallback metrics from state.json if not provided
        if not metrics:
            state_json = work_dir / "state.json"
            if state_json.exists():
                try:
                    state_data = json.loads(state_json.read_text(encoding="utf-8"))
                    metrics = state_data.get("metrics")
                except Exception:
                    pass

        # 1. Measure master audio duration
        total_audio_dur = self._get_media_duration(work_dir, audio_name)
        log.info(f"Master voiceover duration: {total_audio_dur:.2f}s")

        # 2. Render sequence of cards (Anchor completely removed)
        anchor_duration = 0.0
        clip_entries: list[str] = []

        # 3. Calculate time allocation across cards
        remaining_time = max(10.0, total_audio_dur)
        card_count = max(1, len(card_image_names))

        if card_durations and len(card_durations) == card_count:
            log.info(f"Using exact per-card durations for {card_count} cards (Total: {sum(card_durations):.2f}s)...")
            durations = card_durations
        else:
            dur_per_card = remaining_time / card_count
            log.info(f"Rendering {card_count} broadcast cards ({dur_per_card:.2f}s each)...")
            durations = [dur_per_card] * card_count

        # 4. Render Ken Burns clips for each card
        for idx, (card, card_dur) in enumerate(zip(card_image_names, durations)):
            clip_name = f"clip_motion_scene_{idx + 1}.mp4"
            self._render_ken_burns_clip(
                work_dir=work_dir,
                card_image=card,
                duration=card_dur,
                clip_name=clip_name,
                motion_index=idx,
            )
            clip_entries.append(clip_name)

        # 5. Build concat demuxer manifest
        concat_txt = work_dir / "clips_concat.txt"
        with open(concat_txt, "w", encoding="utf-8") as f:
            for c in clip_entries:
                f.write(f"file '{c}'\n")

        # 6. Build scrolling ticker and subtitle overlay filtergraph
        ticker_text = self._build_ticker_string(metrics)
        # Escape FFmpeg special characters (: -> \:, , -> \,, % -> \\\% for drawtext, ' -> \')
        esc_ticker = (
            ticker_text.replace(":", r"\:")
            .replace(",", r"\,")
            .replace("%", r"\\\%")
            .replace("'", r"\'")
        )

        # Scrolling ticker: 60px dark ribbon at bottom, text scrolling, then cyan badge layered on top
        ticker_filter = (
            "drawbox=y=ih-60:color=0x0a0e17@0.96:width=iw:height=60:t=fill,"
            f"drawtext=text='{esc_ticker}':fontcolor=white:fontsize=24:y=h-42:x=w-mod(t*120\\,w+tw),"
            "drawbox=y=ih-60:color=0x00E5FF:width=200:height=60:t=fill,"
            "drawtext=text='LIVE WRAP':fontcolor=black:fontsize=24:x=30:y=h-42"
        )

        # Rescaled broadcast closed-captioning: 1080p ASS docked inside Zone D lower-third
        ass_file = self._create_docked_ass(work_dir, srt_name)
        subtitle_filter = f"ass='{ass_file.name}'"

        has_bg_music = self.bg_music_path and self.bg_music_path.exists()

        if has_bg_music:
            filter_complex = (
                f"[0:v]{ticker_filter},{subtitle_filter}[vout];"
                "[1:a]volume=1.0[voice];"
                "[2:a]volume=0.07[music];"
                "[voice][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
            cmd = [
                self.ffmpeg_bin, "-y",
                "-f", "concat", "-safe", "0", "-i", "clips_concat.txt",
                "-i", audio_name,
                "-stream_loop", "-1", "-i", str(self.bg_music_path),
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "[aout]",
                "-r", "24",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_name,
            ]
        else:
            filter_complex = f"[0:v]{ticker_filter},{subtitle_filter}[vout]"
            cmd = [
                self.ffmpeg_bin, "-y",
                "-f", "concat", "-safe", "0", "-i", "clips_concat.txt",
                "-i", audio_name,
                "-filter_complex", filter_complex,
                "-map", "[vout]",
                "-map", "1:a",
                "-r", "24",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_name,
            ]

        try:
            log.info(f"Assembling final composite video ({output_name})...")
            res = subprocess.run(
                cmd,
                cwd=str(work_dir),
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            log.info(f"Broadcast video assembly completed successfully: {final_mp4}")

            # 7. Clean up temporary intermediate clips to save disk space
            for c in clip_entries:
                p = work_dir / c
                if p.exists() and p.name.startswith("clip_"):
                    try:
                        p.unlink()
                    except Exception:
                        pass
            if concat_txt.exists():
                concat_txt.unlink(missing_ok=True)

            return final_mp4
        except subprocess.CalledProcessError as exc:
            log.error(f"FFmpeg render failure! ReturnCode: {exc.returncode}\nSTDERR:\n{exc.stderr}")
            raise RuntimeError(f"FFmpeg failed: {exc.stderr}") from exc
