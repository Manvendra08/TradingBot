"""
directml_talking_head.py — High-Performance Windows DirectML Lip-Sync Engine.
Uses OpenCV YuNet face detection and ONNX Runtime DirectML (DirectX 12 on Intel GPU)
to generate photorealistic talking anchor video from static portrait + audio track.
"""
import os
import sys
from pathlib import Path
import cv2
import numpy as np
import soundfile as sf
import librosa
import onnxruntime as ort

# Add parent and local generators to path
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.append(str(CURRENT_DIR))

import wav2lip_audio

MODELS_DIR = PROJECT_ROOT / "assets" / "models"
YUNET_MODEL = MODELS_DIR / "face_detection_yunet.onnx"
WAV2LIP_MODEL = MODELS_DIR / "wav2lip_gan.onnx"

class DirectMLTalkingHead:
    def __init__(self, use_gpu: bool = True):
        self.use_gpu = use_gpu
        self._init_models()

    def _init_models(self):
        if not WAV2LIP_MODEL.exists():
            raise FileNotFoundError(f"Wav2Lip ONNX model not found: {WAV2LIP_MODEL}")
        if not YUNET_MODEL.exists():
            raise FileNotFoundError(f"YuNet ONNX model not found: {YUNET_MODEL}")

        providers = ["DmlExecutionProvider", "CPUExecutionProvider"] if self.use_gpu else ["CPUExecutionProvider"]
        print(f"[*] Initializing Wav2Lip with providers: {providers}")
        
        # Session options for performance
        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        
        self.wav2lip_session = ort.InferenceSession(
            str(WAV2LIP_MODEL),
            sess_options=sess_opts,
            providers=providers
        )
        print(f"[+] Loaded Wav2Lip on: {self.wav2lip_session.get_providers()}")

    def detect_face(self, image: np.ndarray):
        """Detect face using OpenCV YuNet."""
        h, w, _ = image.shape
        detector = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (w, h), score_threshold=0.6)
        faces = detector.detect(image)
        if faces[1] is None or len(faces[1]) == 0:
            return None
        # Best face
        bbox = faces[1][0][:4].astype(int)
        x, y, bw, bh = bbox
        
        # Expand slightly to cover chin and forehead comfortably
        pad_x = int(bw * 0.15)
        pad_y = int(bh * 0.15)
        
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(w, x + bw + pad_x)
        y2 = min(h, y + bh + pad_y)
        
        return (x1, y1, x2, y2)

    def generate(
        self,
        image_path: str,
        audio_path: str,
        output_path: str,
        fps: int = 25,
        max_duration: float = None,
        batch_size: int = 8
    ) -> str:
        """
        Generate talking-head video synced to audio.
        """
        image_path = str(image_path)
        audio_path = str(audio_path)
        output_path = str(output_path)

        # 1. Load image
        img = cv2.imread(image_path)
        if img is None:
            raise ValueError(f"Could not load image: {image_path}")
        h, w, _ = img.shape

        # 2. Detect face
        face_box = self.detect_face(img)
        if face_box is None:
            raise RuntimeError(f"No face detected in anchor portrait: {image_path}")
        x1, y1, x2, y2 = face_box
        print(f"[*] Face detected: [{x1}, {y1}] to [{x2}, {y2}] ({x2-x1}x{y2-y1})")

        # 3. Load audio & compute Mel Spectrogram
        print(f"[*] Loading audio: {audio_path}")
        wav, sr = librosa.load(audio_path, sr=16000)
        if max_duration:
            max_samples = int(max_duration * 16000)
            wav = wav[:max_samples]

        duration = len(wav) / 16000.0
        total_frames = int(duration * fps)
        print(f"[*] Audio length: {duration:.2f}s -> {total_frames} video frames at {fps} fps")

        # Compute Mel
        mel = wav2lip_audio.melspectrogram(wav)
        if np.isnan(mel).any():
            raise ValueError("Mel spectrogram contains NaN")

        # 4. Prepare Mel chunks
        # 16 mel frames per audio window (~200ms)
        mel_step_size = 16
        mel_chunks = []
        mel_idx_multiplier = 80.0 / fps

        for i in range(total_frames):
            start_idx = int(i * mel_idx_multiplier)
            if start_idx + mel_step_size > mel.shape[1]:
                # Pad with zeros if at very end
                chunk = mel[:, start_idx:]
                pad_width = mel_step_size - chunk.shape[1]
                chunk = np.pad(chunk, ((0, 0), (0, pad_width)), mode="edge")
            else:
                chunk = mel[:, start_idx : start_idx + mel_step_size]
            mel_chunks.append(chunk)

        # 5. Crop face and resize to 96x96
        face_crop = img[y1:y2, x1:x2]
        crop_h, crop_w, _ = face_crop.shape
        face_96 = cv2.resize(face_crop, (96, 96))
        face_96_rgb = cv2.cvtColor(face_96, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0

        # Mask lower half for Wav2Lip (rows 48:96)
        masked_face = face_96_rgb.copy()
        masked_face[48:, :, :] = 0.0

        # 6-channel input: [masked, original]
        vid_input = np.concatenate([masked_face, face_96_rgb], axis=2) # (96, 96, 6)
        vid_input = np.transpose(vid_input, (2, 0, 1)) # (6, 96, 96)

        # 6. Run Inference in batches
        print(f"[*] Generating {total_frames} frames via DirectML GPU...")
        gen_faces_96 = []
        
        for i in range(0, total_frames, batch_size):
            batch_mels = mel_chunks[i : i + batch_size]
            cur_bs = len(batch_mels)
            
            # Mel input shape: (batch, 1, 80, 16)
            mel_batch = np.array(batch_mels, dtype=np.float32)
            mel_batch = np.expand_dims(mel_batch, axis=1)
            
            # Video input shape: (batch, 6, 96, 96)
            vid_batch = np.repeat(np.expand_dims(vid_input, axis=0), cur_bs, axis=0).astype(np.float32)
            
            # Execute on DirectML
            gen = self.wav2lip_session.run(None, {"mel": mel_batch, "vid": vid_batch})[0]
            
            for b in range(cur_bs):
                g = gen[b] # (3, 96, 96)
                g = np.transpose(g, (1, 2, 0)) * 255.0
                g = np.clip(g, 0, 255).astype(np.uint8)
                g_bgr = cv2.cvtColor(g, cv2.COLOR_RGB2BGR)
                gen_faces_96.append(g_bgr)

        # 7. Composite frames with smooth blending & subtle natural micro-movement
        temp_video_path = output_path.replace(".mp4", "_temp_silent.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(temp_video_path, fourcc, fps, (w, h))

        # Soft elliptical blend mask for natural skin seamless seam
        mask = np.zeros((crop_h, crop_w), dtype=np.float32)
        center_x = crop_w // 2
        center_y = int(crop_h * 0.65) # center on mouth/chin region
        axis_x = int(crop_w * 0.38)
        axis_y = int(crop_h * 0.32)
        cv2.ellipse(mask, (center_x, center_y), (axis_x, axis_y), 0, 0, 360, 1.0, -1)
        mask = cv2.GaussianBlur(mask, (21, 21), 11)
        mask_3ch = np.repeat(np.expand_dims(mask, axis=2), 3, axis=2)

        for i, face_g in enumerate(gen_faces_96):
            # Scale generated mouth back to crop resolution
            face_high = cv2.resize(face_g, (crop_w, crop_h), interpolation=cv2.INTER_LANCZOS4)
            
            # Blend generated mouth with original face
            blended_face = (face_high * mask_3ch + face_crop * (1.0 - mask_3ch)).astype(np.uint8)
            
            # Insert into frame
            frame = img.copy()
            frame[y1:y2, x1:x2] = blended_face
            
            # Optional subtle breathing / micro-nodding motion (1-2px sinusoidal)
            # Makes anchor look alive rather than a static frozen torso
            nod_offset = int(1.5 * np.sin(i * 0.15))
            if nod_offset != 0:
                M = np.float32([[1, 0, 0], [0, 1, nod_offset]])
                frame = cv2.warpAffine(frame, M, (w, h), borderMode=cv2.BORDER_REPLICATE)

            writer.write(frame)

        writer.release()
        print(f"[+] Video frames written: {temp_video_path}")

        # 8. Mux audio using FFmpeg
        import subprocess
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        
        args = [
            ffmpeg_exe, "-y",
            "-i", str(temp_video_path),
            "-i", str(audio_path),
            "-t", f"{duration:.2f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-shortest",
            str(output_path)
        ]
        print(f"[*] Muxing final video with audio via subprocess...")
        res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if os.path.exists(temp_video_path):
            try:
                os.remove(temp_video_path)
            except Exception:
                pass

        if res.returncode == 0 and os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            print(f"[+] SUCCESS! Talking head generated: {output_path} ({size_mb:.2f} MB)")
            return output_path
        else:
            stderr_preview = res.stderr[-400:] if res.stderr else "Unknown error"
            raise RuntimeError(f"FFmpeg muxing failed with code {res.returncode}: {stderr_preview}")

if __name__ == "__main__":
    anchor_img = PROJECT_ROOT / "assets" / "anchor" / "anchor_lead_primary.jpg"
    test_audio = PROJECT_ROOT / "output" / "2026-09-07" / "voiceover.mp3"
    out_file = PROJECT_ROOT / "output" / "2026-09-07" / "test_talking_anchor.mp4"

    engine = DirectMLTalkingHead(use_gpu=True)
    # Generate first 6 seconds for test verification
    engine.generate(
        image_path=str(anchor_img),
        audio_path=str(test_audio),
        output_path=str(out_file),
        max_duration=6.0,
        batch_size=8
    )
