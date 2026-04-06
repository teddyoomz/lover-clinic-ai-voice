"""
Patched version of app.py for Gradio 5.x compatibility.
Removes streaming audio output to avoid covert_to_adts error.
Adds Thai TTS tab via f5-tts-th (F5-TTS-THAI model).
Adds Vocal Enhancer tab via Demucs + resemble-enhance.
Runs from the app/ working directory:
    python ../app_fixed.py --enable-v1 --enable-v2
"""
import sys
import os
sys.path.insert(0, os.getcwd())  # add app/ to path for relative imports

# Fix: model checkpoints saved on Linux use PosixPath; loading on Windows fails
# unless we remap PosixPath → WindowsPath before any torch.load calls.
import pathlib
if sys.platform == "win32":
    pathlib.PosixPath = pathlib.WindowsPath

import warnings
warnings.filterwarnings(
    "ignore",
    message="Trying to convert audio automatically from float32 to 16-bit int format",
    category=UserWarning,
)

# ── Fix pydub ffmpeg path using imageio-ffmpeg bundled binary ──────────────
# conda ffmpeg may crash (error 0xC0000005) on Windows due to DLL issues.
# imageio-ffmpeg provides a self-contained binary that always works.
# Auto-install if missing so the app is self-healing on first run.
try:
    import imageio_ffmpeg as _iio_ffmpeg
except ImportError:
    import subprocess as _pip_sp
    _pip_sp.run([sys.executable, "-m", "pip", "install", "imageio-ffmpeg", "-q"], check=False)
    try:
        import imageio_ffmpeg as _iio_ffmpeg
    except ImportError:
        _iio_ffmpeg = None

try:
    if _iio_ffmpeg is not None:
        from pydub import AudioSegment as _AS
        _ffmpeg_exe = _iio_ffmpeg.get_ffmpeg_exe()
        _AS.converter = _ffmpeg_exe
        _AS.ffmpeg = _ffmpeg_exe
        print(f"[pydub] using imageio-ffmpeg: {_ffmpeg_exe}")
    else:
        print("[pydub] imageio-ffmpeg not available, using system ffmpeg")
except Exception as _e:
    print(f"[pydub] ffmpeg setup warning: {_e}")

import gradio as gr
import torch
import yaml
import argparse
from modules.commons import str2bool

# Set up device — verify CUDA actually works before committing to it
if torch.cuda.is_available():
    try:
        torch.zeros(1, device="cuda")
        device = torch.device("cuda")
    except Exception as _cuda_err:
        print(f"⚠️ CUDA available but failed smoke-test ({_cuda_err}) — falling back to CPU")
        device = torch.device("cpu")
elif torch.backends.mps.is_available():
    device = torch.device("mps")
else:
    device = torch.device("cpu")

print(f"[device] using: {device}")

dtype = torch.float16

# ─────────────────────────────────────────────
# Self-healing loader
# ─────────────────────────────────────────────

import re as _re
import subprocess as _sp

def _autofix_load(label, loader_fn, max_retries=8):
    """
    Call loader_fn(). If it raises because of a missing Python package,
    auto-install that package with pip and retry — up to max_retries times.
    After each install, the affected modules are flushed from sys.modules
    so the fresh import picks up the newly installed code.
    """
    last_exc = None
    tried_pkgs = set()

    for attempt in range(1, max_retries + 1):
        try:
            return loader_fn()
        except Exception as exc:
            last_exc = exc
            err = str(exc)
            pkg = None

            # "Missing optional dependency 'tabulate'"
            m = _re.search(r"Missing optional dependency ['\"]([^'\"]+)['\"]", err)
            if m:
                pkg = m.group(1)

            # "No module named 'vocos'"  /  "No module named 'vocos.something'"
            if not pkg:
                m = _re.search(r"No module named ['\"]([^'\"]+)['\"]", err)
                if m:
                    pkg = m.group(1).split(".")[0]

            # Unknown / already tried → stop retrying
            if not pkg or pkg in tried_pkgs:
                break

            tried_pkgs.add(pkg)
            print(f"  ↳ [{attempt}/{max_retries}] ขาด '{pkg}' — auto-install ...")
            # resemble-enhance must be installed without its optional deepspeed dep
            # (deepspeed fails to build on Windows without CUDA toolkit / nvcc)
            if pkg in ("resemble_enhance", "resemble-enhance"):
                _sp.run(
                    [sys.executable, "-m", "pip", "install", "resemble-enhance", "--no-deps", "-q"],
                    check=False,
                )
                _sp.run(
                    [sys.executable, "-m", "pip", "install", "vocos", "tabulate", "-q"],
                    check=False,
                )
                _stub = os.path.join(os.path.dirname(os.path.abspath(__file__)), "make_deepspeed_stub.py")
                if os.path.exists(_stub):
                    _sp.run([sys.executable, _stub], check=False)
            else:
                _sp.run(
                    [sys.executable, "-m", "pip", "install", pkg, "-q"],
                    check=False,
                )
            # Flush cached (broken) imports so retry gets fresh modules
            flush_prefixes = (pkg, pkg.replace("-", "_"),
                              label, label.replace("-", "_"))
            for k in list(sys.modules.keys()):
                if any(k == p or k.startswith(p + ".") for p in flush_prefixes):
                    del sys.modules[k]

    raise last_exc


# ─────────────────────────────────────────────
# Persistent state — survives app restarts
# ─────────────────────────────────────────────
import json as _json
import shutil as _shutil

_PROJ_ROOT   = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE  = os.path.join(_PROJ_ROOT, "session_state.json")
_UPLOADS_DIR = os.path.join(_PROJ_ROOT, "user_uploads")
os.makedirs(_UPLOADS_DIR, exist_ok=True)

# ─────────────────────────────────────────────
# Shared Save-to-Folder utilities
# ─────────────────────────────────────────────

def _default_save_dir(subfolder: str) -> str:
    """Return absolute path to downloads/<subfolder>, create if needed."""
    p = os.path.join(_PROJ_ROOT, "downloads", subfolder)
    os.makedirs(p, exist_ok=True)
    return p


def _save_audio_to_dir(audio, save_dir: str, prefix: str = "output") -> str:
    """Copy/write an audio result into save_dir.

    audio may be:
      - str  : filepath already on disk (copy it)
      - tuple: (sample_rate, np.ndarray) — write as WAV
      - None : nothing to save
    Returns a human-readable status string.
    """
    import datetime, soundfile as _sf

    if audio is None:
        return ""
    if not save_dir or not save_dir.strip():
        return "⚠️ กรุณาระบุโฟลเดอร์ปลายทาง"

    save_dir = save_dir.strip()
    os.makedirs(save_dir, exist_ok=True)
    ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        if isinstance(audio, str) and os.path.isfile(audio):
            ext = os.path.splitext(audio)[1] or ".wav"
            dst = os.path.join(save_dir, f"{prefix}_{ts}{ext}")
            _shutil.copy2(audio, dst)
        elif isinstance(audio, tuple) and len(audio) == 2:
            sr, arr = audio
            dst = os.path.join(save_dir, f"{prefix}_{ts}.wav")
            _sf.write(dst, arr, sr)
        else:
            return "⚠️ ไม่รู้จักรูปแบบ audio output"

        size_mb = os.path.getsize(dst) / 1_048_576
        return f"💾 บันทึกที่: {dst}  ({size_mb:.1f} MB)"
    except Exception as e:
        return f"❌ บันทึกไม่สำเร็จ: {e}"


def _open_folder(path: str) -> str:
    """Open a folder in the system file explorer (Windows/macOS/Linux)."""
    import subprocess as _sp2
    path = (path or "").strip()
    if not path:
        return "⚠️ ระบุ path ก่อน"
    os.makedirs(path, exist_ok=True)
    try:
        if sys.platform == "win32":
            _sp2.Popen(f'explorer "{path}"')
        elif sys.platform == "darwin":
            _sp2.Popen(["open", path])
        else:
            _sp2.Popen(["xdg-open", path])
        return f"📂 เปิด: {path}"
    except Exception as e:
        return f"❌ {e}"


def _save_dir_row(default_subfolder: str, label: str = "📁 บันทึกที่"):
    """Render a standardised save-dir row and return (textbox, open_btn, status)."""
    with gr.Row():
        save_dir = gr.Textbox(
            value=_default_save_dir(default_subfolder),
            label=label,
            scale=5,
            interactive=True,
        )
        open_btn = gr.Button("📂 เปิดโฟลเดอร์", variant="secondary", scale=1, min_width=130)
    save_status = gr.Textbox(label="สถานะการบันทึก", interactive=False, lines=1, visible=True)
    open_btn.click(fn=_open_folder, inputs=[save_dir], outputs=[save_status])
    return save_dir, save_status


def _load_state() -> dict:
    """Load session state JSON. Distinguishes missing file from corrupt JSON."""
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            return _json.load(f)
    except FileNotFoundError:
        return {}
    except _json.JSONDecodeError as e:
        # Back up the corrupted file so the user can inspect it
        _bad = _STATE_FILE + ".corrupt"
        try:
            _shutil.copy2(_STATE_FILE, _bad)
        except Exception:
            pass
        print(f"⚠️ session_state.json corrupt ({e}) — backed up to {_bad}, starting fresh")
        # Overwrite the corrupt file with a valid empty JSON immediately so
        # concurrent requests stop seeing the corrupt content.
        try:
            with open(_STATE_FILE, "w", encoding="utf-8") as _f:
                _f.write("{}\n")
        except Exception:
            pass
        return {}
    except Exception as e:
        print(f"⚠️ Could not load session state: {e}")
        return {}


_STATE_LOCK = __import__("threading").Lock()


def _save_state(**kv):
    """Atomically update session state (write → rename prevents corruption).

    Uses a threading.Lock + unique tmp filename to prevent concurrent writes
    from corrupting the JSON (two threads writing to the same .tmp file).
    """
    import tempfile as _tmpfile
    with _STATE_LOCK:
        state = _load_state()
        state.update(kv)
        fd, tmp = _tmpfile.mkstemp(
            prefix=".session_state_",
            suffix=".tmp",
            dir=os.path.dirname(_STATE_FILE),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                _json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, _STATE_FILE)   # atomic on Win32 + POSIX
        except Exception as e:
            print(f"⚠️ Could not save session state: {e}")
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                os.remove(tmp)
            except Exception:
                pass


def _persist_audio(filepath, key):
    """Copy uploaded file → user_uploads/ so path survives restarts."""
    if not filepath:
        _save_state(**{key: None})
        return filepath
    # Always normalise to .wav so Gradio is happy on reload
    ext = os.path.splitext(filepath)[1].lower() or ".wav"
    dest = os.path.join(_UPLOADS_DIR, f"{key}{ext}")
    try:
        _shutil.copy2(filepath, dest)
        _save_state(**{key: dest})
    except Exception as e:
        print(f"⚠️ Could not persist audio '{key}': {e}")
    return filepath


def _s(key, default=None):
    """Get one saved-state value."""
    return _load_state().get(key, default)


def _saved_audio(key):
    """Return saved audio path only if the file still exists and is readable."""
    p = _s(key)
    if p and os.path.isfile(p):
        try:
            if os.access(p, os.R_OK):
                return p
        except Exception:
            pass
    return None


def _logo_html():
    """
    Return the premium Lover Clinic header block.
    Prefers logo_white.png (white-on-transparent) for dark theme,
    then logo.png, then logo_black.png.
    Falls back to a styled SVG text logo if no file found.
    """
    import base64

    img_tag = ""
    for fname in ("logo_white.png", "logo.png", "logo_black.png"):
        logo_path = os.path.join(_PROJ_ROOT, fname)
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            ext = os.path.splitext(fname)[1].lstrip(".").replace("jpg", "jpeg")
            img_tag = (
                f'<img src="data:image/{ext};base64,{b64}" '
                f'style="height:80px; max-width:340px; object-fit:contain; '
                f'display:block; margin:0 auto; filter:drop-shadow(0 0 12px rgba(220,0,0,0.45));" '
                f'alt="Lover Clinic">'
            )
            break

    if not img_tag:
        # SVG text fallback
        img_tag = (
            '<svg width="320" height="68" viewBox="0 0 320 68" xmlns="http://www.w3.org/2000/svg">'
            '<text x="10" y="50" font-family="Segoe UI,Arial" font-weight="900" font-size="46" '
            'fill="#ffffff" letter-spacing="2">LO</text>'
            '<text x="98" y="50" font-family="Segoe UI,Arial" font-weight="900" font-size="46" '
            'fill="#d40000" letter-spacing="2">V</text>'
            '<text x="132" y="50" font-family="Segoe UI,Arial" font-weight="900" font-size="46" '
            'fill="#ffffff" letter-spacing="2">ER</text>'
            '<text x="72" y="66" font-family="Segoe UI,Arial" font-weight="400" font-size="13" '
            'fill="#888" letter-spacing="10">CLINIC</text>'
            '</svg>'
        )

    return (
        '<div style="'
        'background: linear-gradient(160deg, #1a0000 0%, #0d0d0d 45%, #0d0d0d 55%, #1a0000 100%);'
        'border-bottom: 1px solid #d40000;'
        'box-shadow: 0 4px 32px rgba(212,0,0,0.18), 0 1px 0 rgba(255,255,255,0.04);'
        'padding: 24px 0 20px 0;'
        'margin-bottom: 4px;'
        '">'
        + img_tag +
        '<div style="'
        'margin-top: 10px;'
        'display: flex; align-items: center; justify-content: center; gap: 16px;'
        '">'
        '<div style="height:1px; width:60px; background:linear-gradient(to right,transparent,#d40000);"></div>'
        '<span style="font-size:0.65rem; color:#d40000; letter-spacing:0.25em; '
        'font-weight:600; text-transform:uppercase;">AI Voice System</span>'
        '<div style="height:1px; width:60px; background:linear-gradient(to left,transparent,#d40000);"></div>'
        '</div>'
        '</div>'
    )


# ─────────────────────────────────────────────
# Thai TTS — Expression / Mood presets
# (speed, cfg_strength) tuned per emotional register
# ─────────────────────────────────────────────
_TTS_MOOD_PRESETS = {
    "🎯 ปกติ (Normal)":                  (1.00, 2.0),
    "😊 ร่าเริง / ดีใจ (Happy)":          (1.12, 2.5),
    "😢 เศร้า / อ่อนแรง (Sad)":           (0.82, 1.7),
    "⚡ ตื่นเต้น / พลังสูง (Excited)":    (1.28, 3.0),
    "🌸 อ่อนโยน / เป็นกันเอง (Gentle)":   (0.92, 1.6),
    "💼 เป็นทางการ (Professional)":        (0.95, 2.3),
    "😤 หนักแน่น / จริงจัง (Serious)":    (0.88, 2.8),
    "✨ ปลอบโยน / อ่อนหวาน (Soothing)":   (0.78, 1.5),
    "🔥 มั่นใจ / กล้าหาญ (Confident)":    (1.05, 3.2),
    "🎤 นักพากย์ / บรรยาย (Narrator)":    (0.90, 2.6),
}


# ── Global model instances (all pre-loaded at startup) ──
vc_wrapper_v1  = None
tts_models     = {}          # {"v1": TTS}
demucs_model   = None
re_denoise_fn  = None        # resemble_enhance.enhancer.inference.denoise
re_enhance_fn  = None        # resemble_enhance.enhancer.inference.enhance
RESEMBLE_ENHANCE_OK = False  # set to True once re_denoise_fn / re_enhance_fn loaded


# ─────────────────────────────────────────────
# Trailing silence helper (shared by V1, TTS, Enhancer)
# ─────────────────────────────────────────────

def _append_trailing_silence(audio_result, trailing_seconds):
    """Append silent samples to audio for CapCut lip-sync spacing.

    audio_result: tuple (sr, np.ndarray) or raw np.ndarray
    trailing_seconds: float >= 0
    Returns same format as input with silence appended.
    """
    import numpy as np
    if not trailing_seconds or trailing_seconds <= 0:
        return audio_result
    if isinstance(audio_result, tuple) and len(audio_result) == 2:
        sr, wav = audio_result
        silence = np.zeros(int(sr * trailing_seconds), dtype=wav.dtype)
        return (sr, np.concatenate([wav.ravel(), silence]))
    if isinstance(audio_result, np.ndarray):
        return audio_result  # no SR info, skip
    return audio_result


# ─────────────────────────────────────────────
# Voice Conversion helpers
# ─────────────────────────────────────────────

def convert_voice_v1_wrapper(source_audio_path, target_audio_path, diffusion_steps=30,
                             length_adjust=1.0, inference_cfg_rate=0.7, f0_condition=True,
                             auto_f0_adjust=True, pitch_shift=0, trailing_silence=0.0):
    global vc_wrapper_v1
    if vc_wrapper_v1 is None:
        gr.Warning("V1 Voice Conversion ยังไม่โหลด — กรุณา Restart")
        return None
    if not source_audio_path:
        gr.Warning("กรุณาอัปโหลดเสียงต้นทาง (Source Audio)")
        return None
    if not target_audio_path:
        gr.Warning("กรุณาอัปโหลดเสียงอ้างอิง (Reference Audio)")
        return None
    full_audio = None
    for _, audio in vc_wrapper_v1.convert_voice(
        source=source_audio_path,
        target=target_audio_path,
        diffusion_steps=diffusion_steps,
        length_adjust=length_adjust,
        inference_cfg_rate=inference_cfg_rate,
        f0_condition=f0_condition,
        auto_f0_adjust=auto_f0_adjust,
        pitch_shift=pitch_shift,
        stream_output=True
    ):
        if audio is not None:
            full_audio = audio
    if full_audio is not None and trailing_silence > 0:
        full_audio = _append_trailing_silence(full_audio, trailing_silence)
    return full_audio


# ─────────────────────────────────────────────
# Thai TTS helper
# ─────────────────────────────────────────────

def _ensure_valid_wav(audio_path: str) -> str:
    """Convert any audio to a fresh PCM-16 WAV using ffmpeg.

    Handles paths with no extension, webm/ogg data inside .wav files
    (browser MediaRecorder quirk in Gradio 5.x), and any other format
    that soundfile cannot read directly.
    Returns the original path unchanged if ffmpeg conversion fails.
    """
    import subprocess as _sub
    import tempfile as _tmp

    ffmpeg_exe = _get_ffmpeg_exe()
    _fd, wav_path = _tmp.mkstemp(suffix=".wav")
    os.close(_fd)
    try:
        r = _sub.run(
            [ffmpeg_exe, "-y", "-i", audio_path, "-acodec", "pcm_s16le", wav_path],
            capture_output=True,
        )
        if r.returncode == 0 and os.path.getsize(wav_path) > 0:
            return wav_path
        print(f"[warn] _ensure_valid_wav: ffmpeg exit {r.returncode}")
    except Exception as _e:
        print(f"[warn] _ensure_valid_wav: {_e}")
    return audio_path


def _transcribe_with_soundfile(wav_path: str) -> str:
    """Transcribe a WAV file by reading it as numpy array and passing directly
    to the Whisper ASR pipeline — bypasses transformers' ffmpeg_read which
    calls the system ffmpeg subprocess and fails on Windows (DLL issues).
    """
    import soundfile as _sf
    from f5_tts_th import utils_infer as _tts_utils

    if _tts_utils.asr_pipe is None:
        # initialize_asr_pipeline does `"cuda" in device` — needs a string, not torch.device
        _tts_utils.initialize_asr_pipeline(device=str(device))

    audio, sr = _sf.read(wav_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)  # stereo → mono

    # Whisper expects 16 kHz; resample if needed
    if sr != 16000:
        try:
            import scipy.signal as _sig
            num_samples = int(len(audio) * 16000 / sr)
            audio = _sig.resample(audio, num_samples)
            sr = 16000
        except ImportError:
            pass  # pipeline can handle other sample rates too

    result = _tts_utils.asr_pipe(
        {"array": audio, "sampling_rate": sr},
        chunk_length_s=30,
        batch_size=128,
        generate_kwargs={"task": "transcribe"},
        return_timestamps=False,
    )
    return result["text"].strip()


def generate_thai_speech(gen_text, ref_audio, ref_text, model_version, speed, nfe_steps, cfg_strength, trailing_silence=0.0):
    if not gen_text or not gen_text.strip():
        gr.Warning("กรุณาพิมพ์ข้อความที่ต้องการแปลงเป็นเสียง")
        return None
    if not ref_audio:
        gr.Warning("กรุณาอัปโหลดเสียงอ้างอิงสำหรับการ clone เสียง")
        return None

    tts = tts_models.get(model_version)
    if tts is None:
        gr.Warning(f"โมเดล Thai TTS {model_version} ยังไม่โหลด — กรุณา Restart")
        return None
    try:
        ref_audio = _ensure_valid_wav(ref_audio)
        # When ref_text is empty, f5-tts-th triggers ASR via transformers'
        # ffmpeg_read which calls the system ffmpeg subprocess — this fails on
        # Windows with conda's ffmpeg (DLL-not-found / malformed-soundfile).
        # Fix: transcribe ourselves using soundfile (reads numpy array directly,
        # no ffmpeg subprocess) so we can pass a real ref_text to tts.infer.
        safe_ref_text = ref_text.strip() if ref_text else ""
        if not safe_ref_text:
            try:
                safe_ref_text = _transcribe_with_soundfile(ref_audio)
                print(f"[transcribe] auto ref_text: {safe_ref_text!r}")
            except Exception as _trans_err:
                print(f"[warn] transcribe fallback failed: {_trans_err}")
                safe_ref_text = "สวัสดี"  # valid Thai → skip ASR & survive V2 th_to_g2p
        wav = tts.infer(
            ref_audio=ref_audio,
            ref_text=safe_ref_text,
            gen_text=gen_text.strip(),
            step=int(nfe_steps),
            cfg=float(cfg_strength),
            speed=float(speed),
        )
        result = (24000, wav)
        return _append_trailing_silence(result, trailing_silence)
    except Exception as e:
        import traceback
        traceback.print_exc()
        gr.Warning(f"TTS เกิดข้อผิดพลาด: {e}")
        return None
    finally:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


# ─────────────────────────────────────────────
# Vocal Isolation + Enhancement helpers
# ─────────────────────────────────────────────

def load_demucs():
    """Load Demucs htdemucs. Called once at startup."""
    global demucs_model
    from demucs.pretrained import get_model
    demucs_model = get_model("htdemucs")
    demucs_model.eval()
    # MPS does not support all ops used by demucs — fall back to CPU
    target = device if str(device) == "cuda" else torch.device("cpu")
    demucs_model.to(target)
    return demucs_model


def process_audio_enhancement(input_audio, do_isolate, do_denoise, do_enhance, trailing_silence=0.0):
    if input_audio is None:
        gr.Warning("กรุณาอัปโหลดไฟล์เสียง")
        return None
    if not do_isolate and not do_denoise and not do_enhance:
        gr.Warning("กรุณาเลือกอย่างน้อย 1 ขั้นตอน")
        return None

    import torchaudio

    try:
        wav, sr = torchaudio.load(input_audio)
        wav = wav.mean(0)  # mix to mono [T]

        # ── Step 1: Vocal isolation with Demucs ──
        if do_isolate:
            print("กำลังแยกเสียงพูด/ร้องออกจากพื้นหลัง (Demucs htdemucs)...")
            # Use preloaded model; only reload if preload failed at startup
            model = demucs_model if demucs_model is not None else load_demucs()
            demucs_device = device if str(device) == "cuda" else torch.device("cpu")

            # Demucs requires stereo input [2, T] at model.samplerate (44100)
            wav_stereo = wav.unsqueeze(0).repeat(2, 1)  # [2, T]
            if sr != model.samplerate:
                wav_stereo = torchaudio.functional.resample(wav_stereo, sr, model.samplerate)
            sr = model.samplerate

            # Normalize (same as demucs CLI)
            ref = wav_stereo.mean(0)
            wav_stereo = (wav_stereo - ref.mean()) / (ref.std() + 1e-8)

            from demucs.apply import apply_model
            with torch.no_grad():
                # sources: [batch=1, n_sources=4, channels=2, time]
                sources = apply_model(model, wav_stereo.unsqueeze(0).to(demucs_device))[0]

            # htdemucs source order: drums=0, bass=1, other=2, vocals=3
            wav = sources[3].mean(0).cpu()  # mono vocals [T]

            # Free GPU memory used by Demucs before resemble-enhance runs
            del sources
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        # ── Steps 2 & 3: resemble-enhance ──
        if do_denoise or do_enhance:
            if not RESEMBLE_ENHANCE_OK or re_denoise_fn is None:
                gr.Warning("resemble-enhance ไม่พร้อมใช้งาน — กรุณากด Install อีกครั้ง")
                return (int(sr), wav.cpu().numpy()) if do_isolate else None
            enh_device = device if str(device) in ("cuda", "cpu") else torch.device("cpu")
            wav = wav.float()

            if do_denoise:
                print("กำลังลดเสียงรบกวน (resemble-enhance denoise)...")
                wav, sr = re_denoise_fn(wav, sr, enh_device)
                wav = wav.squeeze()

            if do_enhance:
                print("กำลังเพิ่มความคมชัดของเสียง (resemble-enhance enhance)...")
                wav, sr = re_enhance_fn(
                    wav, sr, enh_device,
                    nfe=32, solver="midpoint", lambd=0.5, tau=0.5,
                )
                wav = wav.squeeze()

        result = (int(sr), wav.cpu().numpy())
        if trailing_silence > 0:
            result = _append_trailing_silence(result, trailing_silence)
        # Final GPU cleanup
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return result

    except Exception as e:
        import traceback
        traceback.print_exc()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gr.Warning(f"เกิดข้อผิดพลาด: {str(e)}")
        return None


# ─────────────────────────────────────────────
# Gradio interfaces
# ─────────────────────────────────────────────

def build_v1_tab():
    """Renders V1 Voice Conversion components directly into the current Gradio context."""
    gr.Markdown(
        "แปลงเสียงแบบ Zero-shot ไม่ต้องเทรนโมเดล "
        "อัปโหลดเสียงต้นทางและเสียงอ้างอิง ระบบจะแปลงเสียงต้นทางให้มีลักษณะเสียงเหมือนเสียงอ้างอิง\n\n"
        "**สำหรับภาษาไทย:** เปิด **ใช้โมเดล F0** ทุกครั้ง — "
        "ภาษาไทยเป็นภาษาวรรณยุกต์ โมเดล F0 จะรักษาระดับเสียง (pitch) ของแต่ละวรรณยุกต์ไว้ได้อย่างแม่นยำ "
        "และใช้ sample rate 44100 Hz ซึ่งให้คุณภาพสูงกว่า\n\n"
        "**หมายเหตุ:** เสียงอ้างอิงที่ยาวเกิน 25 วินาทีจะถูกตัดอัตโนมัติ | "
        "ถ้าความยาวรวมของเสียงต้นทาง + อ้างอิงเกิน 30 วินาที เสียงต้นทางจะถูกแบ่งประมวลผลเป็นช่วงๆ"
    )

    with gr.Row():
        source_audio = gr.Audio(
            type="filepath", label="เสียงต้นทาง (Source Audio)",
            value=_saved_audio("v1_source_audio"),
        )
        target_audio = gr.Audio(
            type="filepath", label="เสียงอ้างอิง (Reference Audio)",
            value=_saved_audio("v1_target_audio"),
        )

    diffusion_steps = gr.Slider(
        minimum=1, maximum=200, value=_s("v1_diffusion_steps", 30), step=1,
        label="Diffusion Steps",
        info="จำนวนขั้นตอน diffusion — ยิ่งมากยิ่งได้คุณภาพดีแต่ใช้เวลานานขึ้น (แนะนำ 30-50 สำหรับภาษาไทย, 50-100 สำหรับคุณภาพสูงสุด)",
    )
    length_adjust = gr.Slider(
        minimum=0.5, maximum=2.0, step=0.1, value=_s("v1_length_adjust", 1.0),
        label="ปรับความยาว (Length Adjust)",
        info="ปรับความเร็วของเสียงผลลัพธ์ — น้อยกว่า 1.0 เร็วขึ้น, มากกว่า 1.0 ช้าลง (แนะนำ 1.0 สำหรับภาษาไทยเพื่อรักษาจังหวะ)",
    )
    cfg_rate = gr.Slider(
        minimum=0.0, maximum=1.0, step=0.1, value=_s("v1_cfg_rate", 0.7),
        label="Inference CFG Rate",
        info="ควบคุมระดับ guidance ของโมเดล — มีผลเล็กน้อยต่อคุณภาพเสียง (ค่าเริ่มต้น 0.7)",
    )
    f0_condition = gr.Checkbox(
        label="ใช้โมเดล F0 — แนะนำสำหรับภาษาไทย",
        value=_s("v1_f0_condition", True),
        info="จำเป็นสำหรับภาษาไทยและเสียงร้อง — ดึงและรักษา pitch/วรรณยุกต์ได้แม่นยำ ใช้ sample rate 44100 Hz",
    )
    auto_f0 = gr.Checkbox(
        label="ปรับ F0 อัตโนมัติ (Auto F0 Adjust)",
        value=_s("v1_auto_f0", True),
        info="ปรับ pitch โดยรวมของผลลัพธ์ให้ตรงกับช่วงเสียงของ reference — ใช้งานได้เฉพาะเมื่อเปิดโมเดล F0",
    )
    pitch_shift = gr.Slider(
        label="เลื่อน Pitch (Pitch Shift)", minimum=-24, maximum=24, step=1,
        value=_s("v1_pitch_shift", 0),
        info="เลื่อน pitch ขึ้น/ลงเป็น semitone — แนะนำให้คงไว้ที่ 0 สำหรับภาษาไทยเพื่อไม่ให้วรรณยุกต์เพี้ยน (ใช้งานได้เฉพาะเมื่อเปิดโมเดล F0)",
    )
    trailing_silence = gr.Slider(
        value=_s("v1_trailing_silence", 0.0),
        label="เสียงเงียบท้ายคลิป (วินาที)",
        info="ต่อเสียงเงียบท้ายคลิป สำหรับ lip sync ใน CapCut — 0 = ไม่เพิ่ม",
        minimum=0.0, maximum=10.0, step=0.5,
    )

    run_btn = gr.Button("แปลงเสียง", variant="primary")
    output_audio = gr.Audio(label="เสียงผลลัพธ์ (Output Audio)", format="wav")

    save_dir_v1, save_status_v1 = _save_dir_row("voice_v1")

    # ── State persistence ──
    source_audio.change(fn=lambda p: _persist_audio(p, "v1_source_audio"), inputs=source_audio, outputs=None)
    target_audio.change(fn=lambda p: _persist_audio(p, "v1_target_audio"), inputs=target_audio, outputs=None)
    diffusion_steps.change(fn=lambda v: _save_state(v1_diffusion_steps=v), inputs=diffusion_steps, outputs=None)
    length_adjust.change(fn=lambda v: _save_state(v1_length_adjust=v), inputs=length_adjust, outputs=None)
    cfg_rate.change(fn=lambda v: _save_state(v1_cfg_rate=v), inputs=cfg_rate, outputs=None)
    f0_condition.change(fn=lambda v: _save_state(v1_f0_condition=v), inputs=f0_condition, outputs=None)
    auto_f0.change(fn=lambda v: _save_state(v1_auto_f0=v), inputs=auto_f0, outputs=None)
    pitch_shift.change(fn=lambda v: _save_state(v1_pitch_shift=v), inputs=pitch_shift, outputs=None)
    trailing_silence.change(fn=lambda v: _save_state(v1_trailing_silence=v), inputs=trailing_silence, outputs=None)

    run_btn.click(
        fn=convert_voice_v1_wrapper,
        inputs=[source_audio, target_audio, diffusion_steps, length_adjust, cfg_rate, f0_condition, auto_f0, pitch_shift, trailing_silence],
        outputs=output_audio,
    ).then(
        fn=lambda audio, d: _save_audio_to_dir(audio, d, "voice_v1"),
        inputs=[output_audio, save_dir_v1],
        outputs=[save_status_v1],
    )


def build_downloader_tab():
    """YouTube / Facebook Reels video downloader — fetch qualities then download."""
    import os, tempfile

    DOWNLOAD_DIR = _default_save_dir("videos")

    gr.Markdown(
        "### ⬇️ Video Downloader\n"
        "1) วางลิ้งก์แล้วกด **ดึงข้อมูล** เพื่อดูความละเอียดที่มี\n"
        "2) เลือกความละเอียดที่ต้องการ แล้วกด **ดาวน์โหลด**\n\n"
        "รองรับ: YouTube, Facebook, Instagram, TikTok และอีกกว่า 1,000 เว็บไซต์"
    )

    # shared state ระหว่าง download generator กับ pause/stop handlers
    _current = {"state": None}

    class _StopDownload(BaseException):
        """Raised inside yt-dlp progress hook to abort download (BaseException bypasses except Exception)."""

    # ── URL + Fetch ──────────────────────────────────────────────────────────
    with gr.Row():
        url_input = gr.Textbox(
            label="URL วีดีโอ",
            placeholder="https://www.youtube.com/watch?v=...  หรือ  https://www.facebook.com/reel/...",
            lines=1, scale=5,
        )
        fetch_btn = gr.Button("🔍 ดึงข้อมูล", variant="secondary", scale=1, min_width=120)

    info_out = gr.Textbox(label="ข้อมูลวีดีโอ", interactive=False, lines=3, visible=False)

    # ── Quality selector (hidden until fetch) ────────────────────────────────
    quality_dd    = gr.Dropdown(label="เลือกความละเอียด", choices=[], visible=False, interactive=True)
    quality_state = gr.State({})   # { label: yt-dlp format string }

    # ── Save location + Download controls ───────────────────────────────────
    save_dir_dl, save_status_dl = _save_dir_row("videos", label="📁 บันทึกวีดีโอที่")
    with gr.Row():
        download_btn = gr.Button("⬇️ ดาวน์โหลด", variant="primary",  visible=False, scale=4)
        pause_btn    = gr.Button("⏸ พัก",         variant="secondary", visible=False, scale=1, min_width=100)
        stop_btn     = gr.Button("⏹ หยุด",        variant="stop",      visible=False, scale=1, min_width=100)
    dl_status  = gr.Textbox(label="สถานะ", interactive=False, lines=3)
    video_out  = gr.Video(label="วีดีโอที่ดาวน์โหลด", interactive=False, visible=True)
    audio_out  = gr.Audio(label="🎵 ฟังเพลง (Audio Only)", interactive=False, visible=False)
    file_out   = gr.File(label="บันทึกไฟล์", interactive=False)

    # ── Helper: get imageio-ffmpeg path ──────────────────────────────────────
    def _ffmpeg():
        try:
            import imageio_ffmpeg as _iio
            return _iio.get_ffmpeg_exe()
        except Exception:
            return None

    # ── Helper: shared base ydl options ──────────────────────────────────────
    def _base_ydl_opts():
        import shutil as _sh
        opts = {"quiet": True, "no_warnings": False, "color": False}
        ffmpeg = _ffmpeg()
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
        # js_runtimes must be dict: {runtime_name: {config}}
        for bin_name, rt_key in [("node", "nodejs"), ("nodejs", "nodejs"), ("deno", "deno")]:
            path = _sh.which(bin_name)
            if path:
                opts["js_runtimes"] = {rt_key: {"path": path}}
                break
        return opts

    # ── Step 1: Fetch available qualities ────────────────────────────────────
    def _fetch(url):
        url = (url or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return (
                gr.update(value="❌ URL ไม่ถูกต้อง", visible=True),
                gr.update(choices=[], visible=False),
                {},
                gr.update(visible=False),
            )

        try:
            import yt_dlp
        except ImportError:
            return (
                gr.update(value="❌ ไม่พบ yt-dlp — กรุณากด Fix แล้ว Start ใหม่", visible=True),
                gr.update(choices=[], visible=False),
                {},
                gr.update(visible=False),
            )

        ydl_opts = {**_base_ydl_opts(), "skip_download": True}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            return (
                gr.update(value=f"❌ ดึงข้อมูลไม่สำเร็จ:\n{str(e)[-400:]}", visible=True),
                gr.update(choices=[], visible=False),
                {},
                gr.update(visible=False),
            )

        title    = info.get("title", "Unknown")
        duration = info.get("duration") or 0
        dur_str  = f"{int(duration)//60}:{int(duration)%60:02d}" if duration else "?"
        uploader = info.get("uploader") or info.get("channel") or ""

        # ── Parse available video heights ────────────────────────────────────
        formats = info.get("formats") or []
        heights = sorted(
            {f["height"] for f in formats
             if f.get("height") and f.get("vcodec", "none") != "none" and f["height"] > 0},
            reverse=True,
        )

        HEIGHT_LABEL = {
            2160: "4K (2160p)", 1440: "2K (1440p)", 1080: "Full HD (1080p)",
            720:  "HD (720p)",  480:  "SD (480p)",  360:  "360p",
            240:  "240p",       144:  "144p",
        }

        quality_map  = {}
        quality_list = []

        # Best auto — prefer H.264 (avc) for browser compatibility
        lbl = "🏆 ดีที่สุด (อัตโนมัติ)"
        quality_map[lbl] = (
            "bestvideo[vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        )
        quality_list.append(lbl)

        for h in heights:
            lbl = f"📹 {HEIGHT_LABEL.get(h, str(h)+'p')}"
            quality_map[lbl] = (
                f"bestvideo[height<={h}][vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            )
            quality_list.append(lbl)

        # Audio only
        lbl = "🎵 Audio เท่านั้น (mp3)"
        quality_map[lbl] = "bestaudio/best"
        quality_list.append(lbl)

        info_text = f"📺 {title}"
        if uploader:
            info_text += f"  |  {uploader}"
        info_text += f"\n⏱ {dur_str}  |  {len(heights)} ความละเอียด: {', '.join(str(h)+'p' for h in heights)}"

        return (
            gr.update(value=info_text, visible=True),
            gr.update(choices=quality_list, value=quality_list[0], visible=True),
            quality_map,
            gr.update(visible=True),
        )

    # ── Step 2: Download with real-time progress + pause/stop ────────────────
    def _download(url, selected, quality_map, save_dir):
        import threading, time, re as _re

        _noop      = gr.update()
        _btn_dl_on  = gr.update(interactive=False, visible=True)
        _btn_dl_off = gr.update(interactive=True,  visible=True)
        _vis_on     = gr.update(visible=True)
        _vis_off    = gr.update(visible=False)

        def _err(msg):
            return msg, _noop, _noop, _noop, _btn_dl_off, _vis_off, _vis_off

        url = (url or "").strip()
        if not url:
            yield _err("กรุณาใส่ URL แล้วดึงข้อมูลก่อน"); return
        if not selected or not quality_map:
            yield _err("กรุณากด ดึงข้อมูล แล้วเลือกความละเอียดก่อน"); return

        try:
            import yt_dlp
        except ImportError:
            yield _err("ไม่พบ yt-dlp — กรุณากด Fix แล้ว Start ใหม่"); return

        fmt_str  = quality_map.get(selected, "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best")
        is_audio = selected.startswith("🎵")
        out_dir  = (save_dir or "").strip() or DOWNLOAD_DIR
        os.makedirs(out_dir, exist_ok=True)
        out_tmpl = os.path.join(out_dir,
            "%(title).80s_audio.%(ext)s" if is_audio else "%(title).80s_%(height)sp.%(ext)s")

        # ── Shared state ─────────────────────────────────────────────────────
        state = {"line": "⏳ กำลังเตรียม...", "done": False,
                 "error": None, "info": None,
                 "stop": False, "paused": False, "stopped_by_user": False}
        _current["state"] = state

        _ansi = _re.compile(r"\x1b\[[0-9;]*m")
        def _clean(s): return _ansi.sub("", s or "").strip()

        def _hook(d):
            # pause: block download thread until unpaused or stopped
            while state["paused"] and not state["stop"]:
                time.sleep(0.1)
            if state["stop"]:
                raise _StopDownload()

            status = d.get("status", "")
            if status == "downloading":
                pct    = _clean(d.get("_percent_str")  or "?%")
                speed  = _clean(d.get("_speed_str")    or "?")
                eta    = _clean(d.get("_eta_str")       or "?")
                done_b = _clean(d.get("_downloaded_bytes_str") or "?")
                tot_b  = _clean(d.get("_total_bytes_str") or
                                d.get("_total_bytes_estimate_str") or "?")
                try:
                    filled = int(float(pct.replace("%","")) / 5)
                    bar = "█"*filled + "░"*(20-filled)
                except Exception:
                    bar = "░"*20
                pause_note = "  ⏸ พักอยู่" if state["paused"] else ""
                state["line"] = (f"⬇️  [{bar}] {pct}{pause_note}\n"
                                 f"📦 {done_b} / {tot_b}   🚀 {speed}   ⏱ ETA {eta}")
            elif status == "finished":
                state["line"] = (f"✅ ดาวน์โหลดไฟล์เสร็จ — กำลัง merge/convert...\n"
                                 f"📄 {os.path.basename(d.get('filename',''))}")

        ydl_opts = {
            **_base_ydl_opts(),
            "format": fmt_str, "outtmpl": out_tmpl,
            "noplaylist": True, "progress_hooks": [_hook],
            "format_sort": ["vcodec:h264","acodec:aac","ext:mp4:m4a"],
            "prefer_free_formats": False,
        }
        if not is_audio:
            ydl_opts["merge_output_format"] = "mp4"
        else:
            ydl_opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                           "preferredcodec": "mp3",
                                           "preferredquality": "192"}]

        def _run():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    state["info"] = ydl.extract_info(url, download=True)
            except _StopDownload:
                state["stopped_by_user"] = True
            except Exception as e:
                state["error"] = str(e)
            finally:
                state["done"] = True

        threading.Thread(target=_run, daemon=True).start()

        # ── Stream loop ───────────────────────────────────────────────────────
        try:
            while not state["done"]:
                yield state["line"], _noop, _noop, _noop, _btn_dl_on, _vis_on, _vis_on
                time.sleep(0.4)

            # ── Stopped by user ───────────────────────────────────────────────
            if state["stopped_by_user"]:
                yield "⏹ หยุดดาวน์โหลดแล้ว", _noop, _noop, _noop, _btn_dl_off, _vis_off, _vis_off
                return

            # ── Error ─────────────────────────────────────────────────────────
            if state["error"]:
                yield (f"❌ ดาวน์โหลดไม่สำเร็จ:\n{state['error'][-500:]}",
                       _noop, _noop, _noop, _btn_dl_off, _vis_off, _vis_off)
                return

            # ── Success ───────────────────────────────────────────────────────
            all_files = [os.path.join(out_dir, f) for f in os.listdir(out_dir)
                         if os.path.isfile(os.path.join(out_dir, f))]
            if not all_files:
                yield "ดาวน์โหลดสำเร็จแต่หาไฟล์ไม่พบ", _noop, _noop, _noop, _btn_dl_off, _vis_off, _vis_off
                return

            filepath = max(all_files, key=os.path.getmtime)
            size_mb  = os.path.getsize(filepath) / 1_048_576
            fname    = os.path.basename(filepath)
            ext      = os.path.splitext(fname)[1].upper().lstrip(".")
            title    = (state["info"] or {}).get("title", "video")
            msg      = f"✅ {title}\n📁 {fname}  ({size_mb:.1f} MB)  [{ext}]\n📂 {out_dir}"

            if is_audio:
                yield msg, gr.update(value=None,visible=False), gr.update(value=filepath,visible=True), filepath, _btn_dl_off, _vis_off, _vis_off
            else:
                yield msg, gr.update(value=filepath,visible=True), gr.update(value=None,visible=False), filepath, _btn_dl_off, _vis_off, _vis_off

        except Exception as e:
            yield f"❌ {e}", _noop, _noop, _noop, _btn_dl_off, _vis_off, _vis_off
        finally:
            _current["state"] = None

    # ── Pause toggle ──────────────────────────────────────────────────────────
    def _pause_toggle():
        s = _current.get("state")
        if not s:
            return gr.update()
        s["paused"] = not s["paused"]
        return gr.update(value="▶ ต่อ" if s["paused"] else "⏸ พัก")

    # ── Stop ──────────────────────────────────────────────────────────────────
    def _stop_download():
        s = _current.get("state")
        if s:
            s["stop"] = True

    # ── Wire events ──────────────────────────────────────────────────────────
    fetch_btn.click(
        fn=_fetch,
        inputs=[url_input],
        outputs=[info_out, quality_dd, quality_state, download_btn],
    )

    download_btn.click(
        fn=_download,
        inputs=[url_input, quality_dd, quality_state, save_dir_dl],
        outputs=[dl_status, video_out, audio_out, file_out, download_btn, pause_btn, stop_btn],
    )

    pause_btn.click(fn=_pause_toggle, inputs=[], outputs=[pause_btn])
    stop_btn.click(fn=_stop_download, inputs=[], outputs=[])


def build_tts_tab():
    """Thai TTS tab with Expression / Mood preset system."""

    gr.HTML(
        '<div style="background:rgba(212,0,0,0.06); border:1px solid rgba(212,0,0,0.18); '
        'border-radius:10px; padding:14px 18px; margin-bottom:4px;">'
        '<span style="font-size:0.88rem; color:#ccc; line-height:1.7;">'
        'แปลงข้อความภาษาไทยเป็นเสียงพูดด้วย <strong style="color:#fff;">F5-TTS-THAI</strong> '
        '— Zero-shot Voice Cloning อัปโหลดเสียงอ้างอิง 2–8 วินาที '
        'ระบบจะสังเคราะห์เสียงในลักษณะเสียงเดียวกับตัวอย่าง<br>'
        '<strong style="color:#ff6666;">V1</strong> เสียงธรรมชาติ เหมาะงานทั่วไป &nbsp;·&nbsp; '
        '<strong style="color:#ff6666;">V2</strong> ออกเสียงแม่นยำกว่า ลด error การอ่านคำ'
        '</span>'
        '</div>'
    )

    with gr.Row():
        # ── Left column: text + controls ──
        with gr.Column(scale=3):
            gen_text = gr.Textbox(
                label="ข้อความภาษาไทย",
                placeholder="พิมพ์ข้อความที่ต้องการแปลงเป็นเสียง เช่น สวัสดีครับ ยินดีต้อนรับสู่ระบบแปลงเสียง",
                lines=4,
                value=_s("tts_gen_text", ""),
            )

            # ── Expression / Mood ──────────────────────────
            gr.HTML(
                '<div style="margin:10px 0 4px 0; padding:8px 12px; '
                'border-left:3px solid #d40000; background:rgba(212,0,0,0.06); border-radius:0 8px 8px 0;">'
                '<span style="font-size:0.8rem; font-weight:700; color:#ff6666; letter-spacing:0.06em;">'
                '🎭 EXPRESSION / MOOD — น้ำเสียงและอารมณ์</span><br>'
                '<span style="font-size:0.73rem; color:#666;">'
                'เลือก preset เพื่อตั้งค่า Speed + CFG อัตโนมัติ '
                'หรือกำหนดค่าเองด้านล่าง</span>'
                '</div>'
            )

            mood_preset = gr.Dropdown(
                choices=list(_TTS_MOOD_PRESETS.keys()),
                value=_s("tts_mood", "🎯 ปกติ (Normal)"),
                label="Mood Preset",
                info="เลือก preset — speed + CFG จะปรับอัตโนมัติ แต่ยังสามารถ fine-tune ค่าด้านล่างได้",
            )

            with gr.Row():
                speed = gr.Slider(
                    minimum=0.5, maximum=2.0, step=0.05,
                    value=_s("tts_speed", 1.0), label="Speed (ความเร็ว)",
                    info="< 1.0 ช้าลง · 1.0 ปกติ · > 1.0 เร็วขึ้น",
                )
                cfg_strength = gr.Slider(
                    minimum=0.5, maximum=5.0, step=0.1,
                    value=_s("tts_cfg_strength", 2.0), label="CFG Strength (ความเข้ม)",
                    info="ค่าสูง = เสียงเข้มข้น ใกล้เคียง ref มากขึ้น · ค่าต่ำ = เสียงนุ่มนวล",
                )

            with gr.Row():
                nfe_steps = gr.Slider(
                    minimum=8, maximum=64, step=8,
                    value=_s("tts_nfe_steps", 32), label="NFE Steps (คุณภาพ)",
                    info="ยิ่งมากยิ่งดีแต่ช้า · 32 = ค่าเริ่มต้น · 64 = คุณภาพสูงสุด",
                )
                model_version = gr.Radio(
                    choices=["v1", "v2"],
                    value=_s("tts_model_version", "v1"),
                    label="Model Version",
                    info="V1 ธรรมชาติ · V2 แม่นยำ",
                )

            trailing_silence = gr.Number(
                value=_s("tts_trailing_silence", 0.0),
                label="เสียงเงียบท้ายคลิป (วินาที)",
                info="ต่อเสียงเงียบท้ายคลิป สำหรับ lip sync ใน CapCut — 0 = ไม่เพิ่ม",
                minimum=0.0, maximum=30.0, step=0.1,
            )

        # ── Right column: reference audio ──
        with gr.Column(scale=2):
            gr.HTML(
                '<div style="font-size:0.78rem; color:#888; padding:4px 0 8px 0; '
                'letter-spacing:0.03em; font-weight:600;">🎤 REFERENCE VOICE</div>'
            )
            ref_audio = gr.Audio(
                type="filepath",
                label="เสียงอ้างอิง (จำเป็น) — 2–8 วินาที",
                value=_saved_audio("tts_ref_audio"),
            )
            ref_text = gr.Textbox(
                label="คำพูดในเสียงอ้างอิง (optional)",
                placeholder="พิมพ์คำที่พูดใน reference เพื่อเพิ่มคุณภาพ (ถ้าไม่ใส่ โมเดลจะ transcribe เอง)",
                lines=3,
                value=_s("tts_ref_text", ""),
            )
            gr.HTML(
                '<div style="margin-top:10px; padding:10px 12px; background:#111; '
                'border-radius:8px; border:1px solid #1e1e1e;">'
                '<span style="font-size:0.73rem; color:#555; line-height:1.8;">'
                '<strong style="color:#888;">เคล็ดลับ Expression:</strong><br>'
                '😊 ร่าเริง → อัปโหลด ref เสียงสดใส + เลือก Happy preset<br>'
                '😢 เศร้า → ref เสียงช้า + เลือก Sad preset<br>'
                '💼 ทางการ → ref เสียงนิ่ง + เลือก Professional preset<br>'
                '🎤 พากย์ → ref เสียงพากย์ + เลือก Narrator preset'
                '</span>'
                '</div>'
            )

    with gr.Row():
        generate_btn = gr.Button("🎙️ สร้างเสียง", variant="primary", scale=2)

    output_audio = gr.Audio(label="เสียงผลลัพธ์", format="wav")

    # ── Mood preset → auto-fill speed + cfg ──
    def _apply_mood(mood):
        if mood in _TTS_MOOD_PRESETS:
            s, c = _TTS_MOOD_PRESETS[mood]
            return gr.update(value=s), gr.update(value=c)
        return gr.update(), gr.update()

    mood_preset.change(fn=_apply_mood, inputs=mood_preset, outputs=[speed, cfg_strength])

    # ── State persistence ──
    gen_text.change(fn=lambda v: _save_state(tts_gen_text=v), inputs=gen_text, outputs=None)
    mood_preset.change(fn=lambda v: _save_state(tts_mood=v), inputs=mood_preset, outputs=None)
    model_version.change(fn=lambda v: _save_state(tts_model_version=v), inputs=model_version, outputs=None)
    speed.change(fn=lambda v: _save_state(tts_speed=v), inputs=speed, outputs=None)
    nfe_steps.change(fn=lambda v: _save_state(tts_nfe_steps=v), inputs=nfe_steps, outputs=None)
    cfg_strength.change(fn=lambda v: _save_state(tts_cfg_strength=v), inputs=cfg_strength, outputs=None)
    trailing_silence.change(fn=lambda v: _save_state(tts_trailing_silence=v), inputs=trailing_silence, outputs=None)
    ref_audio.change(fn=lambda p: _persist_audio(p, "tts_ref_audio"), inputs=ref_audio, outputs=None)
    ref_text.change(fn=lambda v: _save_state(tts_ref_text=v), inputs=ref_text, outputs=None)

    save_dir_tts, save_status_tts = _save_dir_row("tts")

    generate_btn.click(
        fn=generate_thai_speech,
        inputs=[gen_text, ref_audio, ref_text, model_version, speed, nfe_steps, cfg_strength, trailing_silence],
        outputs=output_audio,
    ).then(
        fn=lambda audio, d: _save_audio_to_dir(audio, d, "tts"),
        inputs=[output_audio, save_dir_tts],
        outputs=[save_status_tts],
    )


def _get_ffmpeg_exe():
    """Return path to a working ffmpeg binary.

    Prefers imageio-ffmpeg (bundled, no external DLL deps) so we avoid
    the Windows 0xC0000135 (DLL-not-found) issue with conda's ffmpeg.
    Falls back to shutil.which if imageio-ffmpeg is not available.
    """
    import shutil as _shutil
    try:
        import imageio_ffmpeg as _iio_ff
        return _iio_ff.get_ffmpeg_exe()
    except ImportError:
        _sp.run([sys.executable, "-m", "pip", "install", "imageio-ffmpeg", "-q"], check=False)
        try:
            import imageio_ffmpeg as _iio_ff
            return _iio_ff.get_ffmpeg_exe()
        except ImportError:
            pass
    # Final fallback: system ffmpeg
    return _shutil.which("ffmpeg") or "ffmpeg"


def extract_video_audio(video_path, output_format, mp3_quality):
    """Extract audio stream from a video file using ffmpeg."""
    import subprocess as _ffmpeg_sp

    # Gradio 5.x returns gr.File as a FileData object or dict, not a plain string.
    # Normalise to a plain path string before doing anything else.
    if video_path is not None:
        if hasattr(video_path, "path"):          # gradio.FileData namedtuple
            video_path = video_path.path
        elif isinstance(video_path, dict):        # older Gradio 5.x dict
            video_path = video_path.get("path") or video_path.get("name", "")

    if not video_path:
        gr.Warning("กรุณาอัปโหลดไฟล์วีดีโอ")
        return None, "⚠️ ยังไม่ได้อัปโหลดไฟล์"

    output_dir = os.path.join(_PROJ_ROOT, "video_output")
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    output_path = os.path.join(output_dir, f"{base}_audio.{output_format}")

    ffmpeg_exe = _get_ffmpeg_exe()

    try:
        if output_format == "mp3":
            cmd = [
                ffmpeg_exe, "-y", "-i", video_path,
                "-vn", "-acodec", "libmp3lame",
                "-q:a", str(int(mp3_quality)),
                output_path,
            ]
        else:  # wav
            cmd = [
                ffmpeg_exe, "-y", "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                output_path,
            ]

        result = _ffmpeg_sp.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            err_tail = result.stderr[-600:] if result.stderr else "ไม่มี output"
            gr.Warning(f"ffmpeg ล้มเหลว (code {result.returncode})")
            return None, f"✗ ffmpeg error:\n{err_tail}"

        size_mb = os.path.getsize(output_path) / 1_048_576
        return output_path, f"✓ แยกเสียงสำเร็จ — {os.path.basename(output_path)}  ({size_mb:.2f} MB)"

    except FileNotFoundError:
        gr.Warning("ไม่พบ ffmpeg — กรุณากด Install ใหม่อีกครั้ง")
        return None, "✗ ไม่พบ ffmpeg ในระบบ"
    except Exception as exc:
        import traceback; traceback.print_exc()
        gr.Warning(f"เกิดข้อผิดพลาด: {exc}")
        return None, f"✗ {exc}"


def build_video_tab():
    """แยกเสียงออกจากไฟล์วีดีโอ (.mp4 .avi .mov .mkv .webm .m4v .flv)"""
    gr.HTML(
        '<div style="background:rgba(212,0,0,0.06); border:1px solid rgba(212,0,0,0.18); '
        'border-radius:10px; padding:14px 18px; margin-bottom:4px;">'
        '<span style="font-size:0.88rem; color:#ccc; line-height:1.7;">'
        'แยกเสียงออกจากไฟล์วีดีโอด้วย <strong style="color:#fff;">ffmpeg</strong> '
        '— รองรับ .mp4 .avi .mov .mkv .webm .m4v .flv .wmv<br>'
        'บันทึกผลลัพธ์เป็น <strong style="color:#ff6666;">MP3</strong> (ไฟล์เล็ก) '
        'หรือ <strong style="color:#ff6666;">WAV</strong> (คุณภาพสูง ไม่มีการ compress)'
        '</span>'
        '</div>'
    )

    with gr.Row():
        with gr.Column(scale=1):
            video_input = gr.File(
                label="อัปโหลดไฟล์วีดีโอ",
                file_types=[".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".flv", ".wmv"],
                file_count="single",
            )
            output_format = gr.Radio(
                choices=["mp3", "wav"],
                value="mp3",
                label="รูปแบบเสียงผลลัพธ์",
                info="MP3 = ไฟล์เล็ก เหมาะทั่วไป · WAV = ไม่บีบอัด คุณภาพสูงสุด",
            )
            mp3_quality = gr.Slider(
                minimum=0, maximum=9, step=1, value=2,
                label="MP3 Quality (ใช้เฉพาะ MP3)",
                info="0 = คุณภาพสูงสุด ไฟล์ใหญ่ · 9 = คุณภาพต่ำสุด ไฟล์เล็กสุด · แนะนำ 2",
                visible=True,
            )
            extract_btn = gr.Button("🎬 แยกเสียง", variant="primary", size="lg")

        with gr.Column(scale=1):
            output_audio = gr.Audio(
                label="เสียงที่แยกได้",
                type="filepath",
                interactive=False,
            )
            status_box = gr.Textbox(
                label="สถานะ",
                lines=3,
                interactive=False,
                value="",
            )
            gr.HTML(
                '<div style="margin-top:12px; padding:10px 12px; background:#111; '
                'border-radius:8px; border:1px solid #1e1e1e;">'
                '<span style="font-size:0.73rem; color:#555; line-height:1.8;">'
                '<strong style="color:#888;">เคล็ดลับ:</strong><br>'
                '• นำเสียงที่แยกได้ไปใช้เป็น Reference Audio ใน V1/V2<br>'
                '• หรือนำไป Vocal Enhancer เพื่อทำความสะอาดก่อน<br>'
                '• WAV เหมาะสำหรับนำไปต่อยอดใน Voice Conversion<br>'
                '• WAV เหมาะสำหรับนำไปต่อยอดใน Voice Conversion'
                '</span>'
                '</div>'
            )

    save_dir_vid, save_status_vid = _save_dir_row("video_audio")

    # Show/hide quality slider based on format
    def _toggle_quality(fmt):
        return gr.update(visible=(fmt == "mp3"))

    def _extract_and_save(video_path, fmt, mp3q, save_dir):
        # Gradio 5.x passes a FileData object; unwrap to a plain path string
        if hasattr(video_path, "path"):
            video_path = video_path.path
        elif hasattr(video_path, "name"):
            video_path = video_path.name
        audio_path, status = extract_video_audio(video_path, fmt, mp3q)
        if audio_path:
            save_msg = _save_audio_to_dir(audio_path, save_dir, "video_audio")
            return audio_path, status + "\n" + save_msg
        return audio_path, status

    output_format.change(fn=_toggle_quality, inputs=output_format, outputs=mp3_quality)

    extract_btn.click(
        fn=_extract_and_save,
        inputs=[video_input, output_format, mp3_quality, save_dir_vid],
        outputs=[output_audio, status_box],
    )


def build_enhancer_tab():
    """Renders Vocal Isolator + Enhancer components into the current Gradio context."""
    gr.Markdown(
        "## ถอดเสียงพูด/ร้องออกจากพื้นหลัง และปรับปรุงคุณภาพเสียงอัตโนมัติ\n\n"
        "ใช้เป็น **pre-processing** ก่อนนำเสียงไปใช้ใน Voice Conversion หรือ TTS ด้านบน\n\n"
        "| ขั้นตอน | โมเดล | หน้าที่ |\n"
        "|---|---|---|\n"
        "| **1. แยกเสียงพูด** | Demucs htdemucs | ถอดเสียงร้อง/พูดออกจากดนตรีหรือเสียงพื้นหลัง |\n"
        "| **2. ลดเสียงรบกวน** | resemble-enhance | ลบ noise ที่เหลืออยู่ |\n"
        "| **3. เพิ่มความคมชัด** | resemble-enhance | ปรับปรุงคุณภาพและความชัดของเสียงโดยรวม |\n\n"
        "โมเดลจะดาวน์โหลดอัตโนมัติเมื่อใช้ครั้งแรก: Demucs ~80 MB, resemble-enhance ~1 GB"
    )

    with gr.Row():
        with gr.Column(scale=1):
            input_audio = gr.Audio(
                type="filepath",
                label="เสียงต้นทาง (อัปโหลดไฟล์ที่ต้องการปรับปรุง)",
                value=_saved_audio("enh_input_audio"),
            )
            gr.Markdown("**เลือกขั้นตอนที่ต้องการ (เปิดทั้งหมด = Auto)**")
            do_isolate = gr.Checkbox(
                label="1. แยกเสียงพูด/ร้องออกจากดนตรี/พื้นหลัง (Vocal Isolation)",
                value=_s("enh_do_isolate", True),
                info="ใช้ Demucs htdemucs — เปิดเมื่อไฟล์มีดนตรีหรือเสียงพื้นหลังที่ต้องการตัดออก",
            )
            do_denoise = gr.Checkbox(
                label="2. ลดเสียงรบกวน (Denoise)",
                value=_s("enh_do_denoise", True),
                info="ใช้ resemble-enhance denoiser — ลบ noise และเสียงรบกวนออกจากเสียงพูด",
            )
            do_enhance = gr.Checkbox(
                label="3. เพิ่มความคมชัดและคุณภาพเสียง (Enhance)",
                value=_s("enh_do_enhance", True),
                info="ใช้ resemble-enhance enhancer — ปรับปรุงความชัดเจน ความสมบูรณ์ และ bandwidth ของเสียง",
            )
            trailing_silence = gr.Number(
                value=_s("enh_trailing_silence", 0.0),
                label="เสียงเงียบท้ายคลิป (วินาที)",
                info="ต่อเสียงเงียบท้ายคลิป สำหรับ lip sync ใน CapCut — 0 = ไม่เพิ่ม",
                minimum=0.0, maximum=30.0, step=0.1,
            )
            process_btn = gr.Button("ประมวลผล", variant="primary", size="lg")

        with gr.Column(scale=1):
            output_audio = gr.Audio(
                label="เสียงผลลัพธ์ (พร้อมใช้งานใน V1 / V2 / Thai TTS)",
                format="wav",
            )
            gr.Markdown(
                "**เคล็ดลับการใช้งาน:**\n"
                "- มีแค่เสียงพูดบน noise พื้นหลัง → ปิดขั้นตอนที่ 1, เปิดแค่ 2+3\n"
                "- มีเสียงพูดผสมดนตรี → เปิดทั้ง 3 ขั้นตอน\n"
                "- เสียงสะอาดแต่คุณภาพต่ำ → เปิดแค่ขั้นตอนที่ 3\n"
                "- หลังได้เสียงที่สะอาดแล้ว ให้นำไปใช้เป็น Reference Audio ใน V1/V2"
            )

    save_dir_enh, save_status_enh = _save_dir_row("enhanced")

    # ── State persistence ──
    input_audio.change(fn=lambda p: _persist_audio(p, "enh_input_audio"), inputs=input_audio, outputs=None)
    do_isolate.change(fn=lambda v: _save_state(enh_do_isolate=v), inputs=do_isolate, outputs=None)
    do_denoise.change(fn=lambda v: _save_state(enh_do_denoise=v), inputs=do_denoise, outputs=None)
    do_enhance.change(fn=lambda v: _save_state(enh_do_enhance=v), inputs=do_enhance, outputs=None)
    trailing_silence.change(fn=lambda v: _save_state(enh_trailing_silence=v), inputs=trailing_silence, outputs=None)

    process_btn.click(
        fn=process_audio_enhancement,
        inputs=[input_audio, do_isolate, do_denoise, do_enhance, trailing_silence],
        outputs=output_audio,
    ).then(
        fn=lambda audio, d: _save_audio_to_dir(audio, d, "enhanced"),
        inputs=[output_audio, save_dir_enh],
        outputs=[save_status_enh],
    )


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def preload_all_models(args):
    """Eagerly load every model at startup so there is zero wait on first use."""
    global vc_wrapper_v1, demucs_model
    global tts_models, re_denoise_fn, re_enhance_fn, RESEMBLE_ENHANCE_OK

    total = sum([args.enable_v1, True, True, True])
    step  = 0

    def _step(name):
        nonlocal step
        step += 1
        print(f"\n[{step}/{total}] กำลังโหลด {name} ...")

    # ── V1 ──────────────────────────────────────────
    if args.enable_v1:
        _step("V1 Voice Conversion (Whisper + BigVGAN F0)")
        try:
            from seed_vc_wrapper import SeedVCWrapper
            vc_wrapper_v1 = SeedVCWrapper()
            print(f"[{step}/{total}] ✓ V1 พร้อมใช้งาน")
        except Exception as e:
            print(f"[{step}/{total}] ✗ V1 โหลดไม่สำเร็จ: {e}")

    # ── Demucs ──────────────────────────────────────
    _step("Demucs htdemucs (Vocal Isolation, ดาวน์โหลด ~80 MB ถ้าครั้งแรก)")
    try:
        _autofix_load("demucs", load_demucs)
        print(f"[{step}/{total}] ✓ Demucs พร้อมใช้งาน")
    except Exception as e:
        print(f"[{step}/{total}] ✗ Demucs โหลดไม่สำเร็จ: {e}")

    # ── resemble-enhance ────────────────────────────
    _step("resemble-enhance (Denoise + Enhance, ดาวน์โหลด ~1 GB ถ้าครั้งแรก)")
    def _load_resemble():
        from resemble_enhance.enhancer.inference import (
            denoise as _re_d,
            enhance as _re_e,
        )
        # Warm-up: trigger model download + GPU load with a short silent clip
        _dummy = torch.zeros(16000).float()
        _dev = device if str(device) in ("cuda", "cpu") else torch.device("cpu")
        _re_d(_dummy, 16000, _dev)
        _re_e(_dummy, 16000, _dev)
        return _re_d, _re_e

    try:
        re_denoise_fn, re_enhance_fn = _autofix_load("resemble_enhance", _load_resemble)
        RESEMBLE_ENHANCE_OK = True
        print(f"[{step}/{total}] ✓ resemble-enhance พร้อมใช้งาน")
    except Exception as e:
        RESEMBLE_ENHANCE_OK = False
        print(f"[{step}/{total}] ✗ resemble-enhance โหลดไม่สำเร็จ: {e}")

    # ── Thai TTS ────────────────────────────────────
    _step("Thai TTS F5-TTS-THAI v1 + v2 (ดาวน์โหลด ~1-2 GB ถ้าครั้งแรก)")
    try:
        from f5_tts_th.tts import TTS
        tts_models["v1"] = TTS(model="v1")
        tts_models["v2"] = TTS(model="v2")
        print(f"[{step}/{total}] ✓ Thai TTS v1 + v2 พร้อมใช้งาน")
    except Exception as e:
        print(f"[{step}/{total}] ✗ Thai TTS โหลดไม่สำเร็จ: {e}")

    print("\n" + "="*55)
    print("✓ โหลดโมเดลทั้งหมดเสร็จแล้ว — กำลังเปิด Web UI...")
    print("="*55 + "\n")


def main(args):
    global vc_wrapper_v1

    preload_all_models(args)

    _CSS = """
/* ══════════════════════════════════════════════════════
   LOVER CLINIC — AI VOICE SYSTEM
   Theme: Premium Dark  ·  Red / Black / White
══════════════════════════════════════════════════════ */

/* ── Reset & Base ── */
*, *::before, *::after { box-sizing: border-box; }

body,
.gradio-container,
.gradio-container > .main {
    background: #0a0a0a !important;
    color: #f0f0f0 !important;
    font-family: 'Segoe UI', 'Noto Sans Thai', 'Inter', sans-serif !important;
}

.gradio-container {
    max-width: 1120px !important;
    margin: 0 auto !important;
    padding: 0 !important;
}

footer { display: none !important; }

/* ── Blocks / Cards ── */
.block, .form {
    background: #141414 !important;
    border: 1px solid #222 !important;
    border-radius: 12px !important;
    box-shadow: 0 2px 16px rgba(0,0,0,0.45) !important;
    padding: 14px 18px !important;
}
/* inner content keeps breathing room */
.block > .label-wrap { margin-bottom: 6px !important; }
.block .prose, .block .md { padding: 2px 0 !important; }

/* ── Tab navigation bar ── */
div[role="tablist"] {
    background: #0e0e0e !important;
    border-bottom: 2px solid #d40000 !important;
    padding: 0 !important;
    gap: 0 !important;
    flex-wrap: nowrap !important;
    overflow-x: auto !important;
    overflow-y: hidden !important;
    scrollbar-width: thin !important;
    scrollbar-color: #d40000 #111 !important;
}
div[role="tablist"]::-webkit-scrollbar { height: 3px; }
div[role="tablist"]::-webkit-scrollbar-thumb { background: #d40000; border-radius: 2px; }

button[role="tab"] {
    background: transparent !important;
    color: #666 !important;
    border: none !important;
    border-bottom: 3px solid transparent !important;
    border-radius: 8px 8px 0 0 !important;
    padding: 9px 14px 7px !important;
    font-weight: 600 !important;
    font-size: 0.75rem !important;
    letter-spacing: 0.03em !important;
    text-transform: uppercase !important;
    transition: all 0.18s ease !important;
    white-space: nowrap !important;
    flex-shrink: 0 !important;
}

button[role="tab"]:hover {
    color: #ff4444 !important;
    background: rgba(212,0,0,0.07) !important;
    border-bottom-color: rgba(212,0,0,0.3) !important;
}

button[role="tab"][aria-selected="true"],
button[role="tab"].selected {
    color: #fff !important;
    background: rgba(212,0,0,0.13) !important;
    border-bottom-color: #d40000 !important;
    text-shadow: 0 0 12px rgba(212,0,0,0.5) !important;
}

/* ── Primary Button ── */
button.primary,
button[variant="primary"],
.btn-primary {
    background: linear-gradient(135deg, #d40000 0%, #8b0000 100%) !important;
    border: 1px solid rgba(255,80,80,0.15) !important;
    color: #fff !important;
    font-weight: 700 !important;
    font-size: 0.88rem !important;
    letter-spacing: 0.1em !important;
    text-transform: uppercase !important;
    border-radius: 8px !important;
    padding: 10px 28px !important;
    box-shadow:
        0 2px 20px rgba(212,0,0,0.45),
        inset 0 1px 0 rgba(255,255,255,0.1) !important;
    transition: all 0.22s ease !important;
    cursor: pointer !important;
}

button.primary:hover,
button[variant="primary"]:hover {
    background: linear-gradient(135deg, #ff2020 0%, #bb0000 100%) !important;
    box-shadow:
        0 4px 36px rgba(212,0,0,0.65),
        inset 0 1px 0 rgba(255,255,255,0.15) !important;
    transform: translateY(-2px) !important;
}

button.primary:active,
button[variant="primary"]:active {
    transform: translateY(0) !important;
    box-shadow: 0 1px 8px rgba(212,0,0,0.4) !important;
}

/* ── Secondary Button ── */
button.secondary,
button[variant="secondary"] {
    background: #1c1c1c !important;
    border: 1px solid #333 !important;
    color: #bbb !important;
    border-radius: 8px !important;
    transition: all 0.2s ease !important;
}

button.secondary:hover,
button[variant="secondary"]:hover {
    border-color: #d40000 !important;
    color: #ff5555 !important;
    background: rgba(212,0,0,0.06) !important;
}

/* ── Text Inputs ── */
input:not([type="range"]):not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]):not([type="file"]),
textarea,
.scroll-hide {
    background: #111 !important;
    border: 1px solid #2a2a2a !important;
    color: #f0f0f0 !important;
    border-radius: 8px !important;
    font-size: 0.9rem !important;
    transition: border-color 0.18s, box-shadow 0.18s !important;
}

input:focus:not([type="range"]):not([type="checkbox"]):not([type="radio"]),
textarea:focus {
    border-color: #d40000 !important;
    box-shadow: 0 0 0 3px rgba(212,0,0,0.14) !important;
    outline: none !important;
}

input::placeholder, textarea::placeholder { color: #3a3a3a !important; }

/* ── Number Input (slider readout) ── */
input[type="number"] {
    background: #181818 !important;
    border-color: #2a2a2a !important;
    color: #f0f0f0 !important;
    border-radius: 6px !important;
}

/* ── Sliders ── */
input[type="range"] {
    accent-color: #d40000 !important;
    cursor: pointer !important;
    height: 5px !important;
}

/* ── Checkboxes & Radios ── */
input[type="checkbox"],
input[type="radio"] {
    accent-color: #d40000 !important;
    width: 15px !important;
    height: 15px !important;
    cursor: pointer !important;
}

/* ── Labels ── */
.label-wrap > span,
label > span,
span.svelte-1f354aw,
.block > label > span {
    color: #ddd !important;
    font-weight: 600 !important;
    font-size: 0.86rem !important;
    letter-spacing: 0.025em !important;
}

/* ── Info / hint text below controls ── */
span.info,
.info,
.description {
    color: #555 !important;
    font-size: 0.76rem !important;
    font-style: italic !important;
}

/* ── Markdown ── */
.prose, .md {
    color: #ccc !important;
}
.prose h1, .md h1 { color: #fff !important; font-size: 1.35rem !important; }
.prose h2, .md h2 { color: #eee !important; font-size: 1.1rem !important; }
.prose h3, .md h3 { color: #ddd !important; font-size: 0.97rem !important; }
.prose strong, .md strong, b { color: #f0f0f0 !important; }
.prose a, .md a { color: #ff4444 !important; text-decoration: none !important; }
.prose a:hover, .md a:hover { color: #ff7777 !important; text-decoration: underline !important; }
.prose p, .md p { color: #bbb !important; line-height: 1.65 !important; }

.prose table, .md table {
    border-collapse: collapse !important;
    width: 100% !important;
    border-radius: 8px !important;
    overflow: hidden !important;
}
.prose th, .md th {
    background: rgba(212,0,0,0.18) !important;
    color: #ff7777 !important;
    padding: 9px 14px !important;
    border: 1px solid #2a2a2a !important;
    font-size: 0.83rem !important;
    letter-spacing: 0.05em !important;
    text-transform: uppercase !important;
}
.prose td, .md td {
    border: 1px solid #222 !important;
    color: #bbb !important;
    padding: 8px 14px !important;
    font-size: 0.88rem !important;
}
.prose tr:nth-child(even) td, .md tr:nth-child(even) td {
    background: rgba(255,255,255,0.025) !important;
}

/* ── Audio waveform ── */
.waveform-container,
.audio-container,
.component-wrapper {
    background: #111 !important;
    border-radius: 10px !important;
}

/* ── Row / Column spacing ── */
.gap { gap: 14px !important; }

/* ── Warning / Error banners ── */
.gr-warning, .warning {
    background: rgba(212,0,0,0.1) !important;
    border-left: 4px solid #d40000 !important;
    color: #ff9999 !important;
    border-radius: 0 8px 8px 0 !important;
    padding: 10px 14px !important;
}

/* ── Dropdown / Select ── */
.wrap-inner, ul.options {
    background: #1a1a1a !important;
    border: 1px solid #2a2a2a !important;
    border-radius: 8px !important;
}
ul.options li:hover { background: rgba(212,0,0,0.1) !important; }

/* ── Dividers ── */
hr {
    border: none !important;
    border-top: 1px solid #222 !important;
    margin: 16px 0 !important;
}

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #0a0a0a; }
::-webkit-scrollbar-thumb { background: #d40000; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #ff2222; }

/* ── Selection ── */
::selection { background: rgba(212,0,0,0.35) !important; color: #fff !important; }
"""

    with gr.Blocks(title="Lover Clinic - AI Voice System", css=_CSS) as demo:
        gr.HTML(_logo_html())
        gr.HTML(
            '<div style="'
            'background:#0f0f0f; border-bottom:1px solid #1e1e1e;'
            'padding:10px 0 12px 0; text-align:center;'
            '">'
            '<span style="'
            'display:inline-flex; align-items:center; gap:22px;'
            'font-size:0.78rem; color:#555; letter-spacing:0.04em;'
            '">'
            '<span>🎙️ <span style="color:#aaa; font-weight:600;">VOICE CONVERSION</span>'
            ' <span style="color:#333">—</span>'
            ' <span style="color:#666">ใช้แท็บ V1 + เปิด F0</span></span>'
            '<span style="color:#d40000;">·</span>'
            '<span>🔊 <span style="color:#aaa; font-weight:600;">THAI TTS</span>'
            ' <span style="color:#333">—</span>'
            ' <span style="color:#666">แปลงข้อความ→เสียง</span></span>'
            '<span style="color:#d40000;">·</span>'
            '<span>✨ <span style="color:#aaa; font-weight:600;">VOCAL ENHANCER</span>'
            ' <span style="color:#333">—</span>'
            ' <span style="color:#666">ทำความสะอาดเสียง</span></span>'
            '<span style="color:#d40000;">·</span>'
            '<span>🎬 <span style="color:#aaa; font-weight:600;">VIDEO → AUDIO</span>'
            ' <span style="color:#333">—</span>'
            ' <span style="color:#666">แยกเสียงจากวีดีโอ</span></span>'
            '<span style="color:#d40000;">·</span>'
            '<span>⬇️ <span style="color:#aaa; font-weight:600;">DOWNLOADER</span>'
            ' <span style="color:#333">—</span>'
            ' <span style="color:#666">YouTube / Facebook</span></span>'
            '</span>'
            '</div>'
        )

        with gr.Tabs():
            if args.enable_v1:
                with gr.TabItem("🎙️ V1 Voice"):
                    build_v1_tab()

            with gr.TabItem("🇹🇭 Thai TTS"):
                build_tts_tab()

            with gr.TabItem("✨ Enhancer"):
                build_enhancer_tab()

            with gr.TabItem("🎬 Video → Audio"):
                build_video_tab()

            with gr.TabItem("⬇️ Downloader"):
                build_downloader_tab()

    # allow Gradio to serve output files that live outside the app/ working dir
    _downloads_root = os.path.join(_PROJ_ROOT, "downloads")
    _video_out_root = os.path.join(_PROJ_ROOT, "video_output")
    os.makedirs(_downloads_root, exist_ok=True)
    os.makedirs(_video_out_root, exist_ok=True)
    demo.launch(allowed_paths=[_downloads_root, _video_out_root])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compile", action="store_true", help="Compile the model using torch.compile")
    parser.add_argument("--enable-v1", action="store_true",
                        help="Enable V1 (Voice & Singing Voice Conversion)")
    args = parser.parse_args()
    main(args)
