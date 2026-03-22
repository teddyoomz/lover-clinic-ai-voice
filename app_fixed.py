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
        return {}
    except Exception as e:
        print(f"⚠️ Could not load session state: {e}")
        return {}


def _save_state(**kv):
    """Atomically update session state (write → rename prevents corruption)."""
    state = _load_state()
    state.update(kv)
    tmp = _STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _STATE_FILE)   # atomic on Win32 + POSIX
    except Exception as e:
        print(f"⚠️ Could not save session state: {e}")
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
vc_wrapper_v2  = None
tts_models     = {}          # {"v1": TTS, "v2": TTS}
demucs_model   = None
re_denoise_fn  = None        # resemble_enhance.enhancer.inference.denoise
re_enhance_fn  = None        # resemble_enhance.enhancer.inference.enhance
RESEMBLE_ENHANCE_OK = False  # set to True once re_denoise_fn / re_enhance_fn loaded


# ─────────────────────────────────────────────
# Voice Conversion helpers
# ─────────────────────────────────────────────

def load_v2_models(args):
    from hydra.utils import instantiate
    from omegaconf import DictConfig
    cfg = DictConfig(yaml.safe_load(open("configs/v2/vc_wrapper.yaml", "r")))
    vc_wrapper = instantiate(cfg)
    vc_wrapper.load_checkpoints()
    vc_wrapper.to(device)
    vc_wrapper.eval()
    vc_wrapper.setup_ar_caches(max_batch_size=1, max_seq_len=4096, dtype=dtype, device=device)

    if args.compile:
        if not hasattr(torch, "_inductor"):
            print("⚠️ torch.compile requested but torch._inductor unavailable — skipping")
        else:
            print("Compiling model with torch.compile...")
            torch._inductor.config.coordinate_descent_tuning = True
            torch._inductor.config.triton.unique_kernel_names = True
            if hasattr(torch._inductor.config, "fx_graph_cache"):
                torch._inductor.config.fx_graph_cache = True
            vc_wrapper.compile_ar()

    return vc_wrapper


def convert_voice_v1_wrapper(source_audio_path, target_audio_path, diffusion_steps=30,
                             length_adjust=1.0, inference_cfg_rate=0.7, f0_condition=True,
                             auto_f0_adjust=True, pitch_shift=0):
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
    return full_audio


def convert_voice_v2_wrapper(source_audio_path, target_audio_path, diffusion_steps=30,
                             length_adjust=1.0, intelligebility_cfg_rate=0.7, similarity_cfg_rate=0.7,
                             top_p=0.7, temperature=0.7, repetition_penalty=1.5,
                             convert_style=False, anonymization_only=False):
    global vc_wrapper_v2
    if vc_wrapper_v2 is None:
        gr.Warning("V2 Voice Conversion ยังไม่โหลด — กรุณา Restart")
        return None
    if not source_audio_path:
        gr.Warning("กรุณาอัปโหลดเสียงต้นทาง (Source Audio)")
        return None
    if not target_audio_path and not anonymization_only:
        gr.Warning("กรุณาอัปโหลดเสียงอ้างอิง (Reference Audio)")
        return None

    full_audio = None
    for _, audio in vc_wrapper_v2.convert_voice_with_streaming(
        source_audio_path=source_audio_path,
        target_audio_path=target_audio_path,
        diffusion_steps=diffusion_steps,
        length_adjust=length_adjust,
        intelligebility_cfg_rate=intelligebility_cfg_rate,
        similarity_cfg_rate=similarity_cfg_rate,
        top_p=top_p,
        temperature=temperature,
        repetition_penalty=repetition_penalty,
        convert_style=convert_style,
        anonymization_only=anonymization_only,
        device=device,
        dtype=dtype,
        stream_output=True
    ):
        if audio is not None:
            full_audio = audio
    return full_audio


# ─────────────────────────────────────────────
# Thai TTS helper
# ─────────────────────────────────────────────

def generate_thai_speech(gen_text, ref_audio, ref_text, model_version, speed, nfe_steps, cfg_strength):
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
        wav = tts.infer(
            ref_audio=ref_audio,
            ref_text=ref_text.strip() if ref_text else "",
            gen_text=gen_text.strip(),
            step=int(nfe_steps),
            cfg=float(cfg_strength),
            speed=float(speed),
        )
        return (24000, wav)
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


def process_audio_enhancement(input_audio, do_isolate, do_denoise, do_enhance):
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

    run_btn = gr.Button("แปลงเสียง", variant="primary")
    output_audio = gr.Audio(label="เสียงผลลัพธ์ (Output Audio)", format="wav")

    # ── State persistence ──
    source_audio.change(fn=lambda p: _persist_audio(p, "v1_source_audio"), inputs=source_audio, outputs=None)
    target_audio.change(fn=lambda p: _persist_audio(p, "v1_target_audio"), inputs=target_audio, outputs=None)
    diffusion_steps.change(fn=lambda v: _save_state(v1_diffusion_steps=v), inputs=diffusion_steps, outputs=None)
    length_adjust.change(fn=lambda v: _save_state(v1_length_adjust=v), inputs=length_adjust, outputs=None)
    cfg_rate.change(fn=lambda v: _save_state(v1_cfg_rate=v), inputs=cfg_rate, outputs=None)
    f0_condition.change(fn=lambda v: _save_state(v1_f0_condition=v), inputs=f0_condition, outputs=None)
    auto_f0.change(fn=lambda v: _save_state(v1_auto_f0=v), inputs=auto_f0, outputs=None)
    pitch_shift.change(fn=lambda v: _save_state(v1_pitch_shift=v), inputs=pitch_shift, outputs=None)

    run_btn.click(
        fn=convert_voice_v1_wrapper,
        inputs=[source_audio, target_audio, diffusion_steps, length_adjust, cfg_rate, f0_condition, auto_f0, pitch_shift],
        outputs=output_audio,
    )


def build_v2_tab():
    """Renders V2 Voice Conversion components directly into the current Gradio context."""
    gr.Markdown(
        "แปลงเสียงและสไตล์แบบ Zero-shot รองรับการแปลงทั้งเสียง (timbre), สไตล์, อารมณ์ และสำเนียงการพูด\n\n"
        "**ข้อควรทราบสำหรับภาษาไทย:** V2 ไม่มี F0 conditioning — "
        "หากต้องการแปลงเสียงภาษาไทยโดยรักษาวรรณยุกต์ให้ครบถ้วน แนะนำใช้ **V1** ที่แท็บข้างๆ แทน "
        "V2 เหมาะกว่าสำหรับการแปลงสำเนียงหรือสไตล์การพูด\n\n"
        "**หมายเหตุ:** เสียงอ้างอิงที่ยาวเกิน 25 วินาทีจะถูกตัดอัตโนมัติ | "
        "ถ้าความยาวรวมเกิน 30 วินาที เสียงต้นทางจะถูกแบ่งประมวลผลเป็นช่วงๆ\n\n"
        "เปิด **แปลงสไตล์/อารมณ์/สำเนียง** เพื่อแปลงมากกว่าแค่ลักษณะเสียง | "
        "เปิด **ไม่ระบุตัวตน** เพื่อแปลงเสียงเป็นเสียงกลางโดยไม่ใช้เสียงอ้างอิง"
    )

    with gr.Row():
        source_audio = gr.Audio(
            type="filepath", label="เสียงต้นทาง (Source Audio)",
            value=_saved_audio("v2_source_audio"),
        )
        target_audio = gr.Audio(
            type="filepath", label="เสียงอ้างอิง (Reference Audio)",
            value=_saved_audio("v2_target_audio"),
        )

    diffusion_steps = gr.Slider(
        minimum=1, maximum=200, value=_s("v2_diffusion_steps", 30), step=1,
        label="Diffusion Steps",
        info="จำนวนขั้นตอน diffusion — ยิ่งมากยิ่งได้คุณภาพดีแต่ใช้เวลานานขึ้น (ค่าเริ่มต้น 30, แนะนำ 50-100 สำหรับคุณภาพสูงสุด)",
    )
    length_adjust = gr.Slider(
        minimum=0.5, maximum=2.0, step=0.1, value=_s("v2_length_adjust", 1.0),
        label="ปรับความยาว (Length Adjust)",
        info="ปรับความเร็วของเสียงผลลัพธ์ — น้อยกว่า 1.0 เร็วขึ้น, มากกว่า 1.0 ช้าลง",
    )
    intel_cfg = gr.Slider(
        minimum=0.0, maximum=1.0, step=0.1, value=_s("v2_intel_cfg", 0.0),
        label="Intelligibility CFG Rate",
        info="ควบคุมความชัดเจนของการออกเสียง — ค่าสูงทำให้เสียงพูดชัดขึ้น แต่อาจลดความเหมือนเสียงอ้างอิง (ค่าเริ่มต้น 0.0)",
    )
    sim_cfg = gr.Slider(
        minimum=0.0, maximum=1.0, step=0.1, value=_s("v2_sim_cfg", 0.7),
        label="Similarity CFG Rate",
        info="ควบคุมความเหมือนกับเสียงอ้างอิง — ค่าสูงทำให้เสียงผลลัพธ์ใกล้เคียงอ้างอิงมากขึ้น (ค่าเริ่มต้น 0.7)",
    )
    top_p = gr.Slider(
        minimum=0.1, maximum=1.0, step=0.1, value=_s("v2_top_p", 0.9),
        label="Top-p",
        info="ควบคุมความหลากหลายในการสุ่มของโมเดล AR — ค่าต่ำลงทำให้ผลลัพธ์แน่นอนขึ้น (ค่าเริ่มต้น 0.9)",
    )
    temperature = gr.Slider(
        minimum=0.1, maximum=2.0, step=0.1, value=_s("v2_temperature", 1.0),
        label="Temperature",
        info="ควบคุมความสุ่มของโมเดล AR — ค่าต่ำทำให้เสียงสม่ำเสมอขึ้น, ค่าสูงทำให้หลากหลายขึ้น (ค่าเริ่มต้น 1.0)",
    )
    rep_penalty = gr.Slider(
        minimum=1.0, maximum=3.0, step=0.1, value=_s("v2_rep_penalty", 1.0),
        label="Repetition Penalty",
        info="ลงโทษการซ้ำของโมเดล AR — ค่าสูงขึ้นช่วยลดการติดซ้ำในเสียงผลลัพธ์ (ค่าเริ่มต้น 1.0)",
    )
    convert_style = gr.Checkbox(
        label="แปลงสไตล์/อารมณ์/สำเนียง (Convert Style/Emotion/Accent)",
        value=_s("v2_convert_style", False),
        info="เปิดเพื่อแปลงสไตล์การพูด อารมณ์ และสำเนียง ไม่ใช่แค่ลักษณะเสียง (timbre)",
    )
    anon_only = gr.Checkbox(
        label="ไม่ระบุตัวตน (Anonymization Only)",
        value=_s("v2_anon_only", False),
        info="แปลงเสียงเป็นเสียงกลางที่โมเดลกำหนดเอง โดยไม่สนใจเสียงอ้างอิงที่อัปโหลด",
    )

    run_btn = gr.Button("แปลงเสียง", variant="primary")
    output_audio = gr.Audio(label="เสียงผลลัพธ์ (Output Audio)", format="wav")

    # ── State persistence ──
    source_audio.change(fn=lambda p: _persist_audio(p, "v2_source_audio"), inputs=source_audio, outputs=None)
    target_audio.change(fn=lambda p: _persist_audio(p, "v2_target_audio"), inputs=target_audio, outputs=None)
    diffusion_steps.change(fn=lambda v: _save_state(v2_diffusion_steps=v), inputs=diffusion_steps, outputs=None)
    length_adjust.change(fn=lambda v: _save_state(v2_length_adjust=v), inputs=length_adjust, outputs=None)
    intel_cfg.change(fn=lambda v: _save_state(v2_intel_cfg=v), inputs=intel_cfg, outputs=None)
    sim_cfg.change(fn=lambda v: _save_state(v2_sim_cfg=v), inputs=sim_cfg, outputs=None)
    top_p.change(fn=lambda v: _save_state(v2_top_p=v), inputs=top_p, outputs=None)
    temperature.change(fn=lambda v: _save_state(v2_temperature=v), inputs=temperature, outputs=None)
    rep_penalty.change(fn=lambda v: _save_state(v2_rep_penalty=v), inputs=rep_penalty, outputs=None)
    convert_style.change(fn=lambda v: _save_state(v2_convert_style=v), inputs=convert_style, outputs=None)
    anon_only.change(fn=lambda v: _save_state(v2_anon_only=v), inputs=anon_only, outputs=None)

    run_btn.click(
        fn=convert_voice_v2_wrapper,
        inputs=[source_audio, target_audio, diffusion_steps, length_adjust, intel_cfg, sim_cfg,
                top_p, temperature, rep_penalty, convert_style, anon_only],
        outputs=output_audio,
    )


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
    ref_audio.change(fn=lambda p: _persist_audio(p, "tts_ref_audio"), inputs=ref_audio, outputs=None)
    ref_text.change(fn=lambda v: _save_state(tts_ref_text=v), inputs=ref_text, outputs=None)

    generate_btn.click(
        fn=generate_thai_speech,
        inputs=[gen_text, ref_audio, ref_text, model_version, speed, nfe_steps, cfg_strength],
        outputs=output_audio,
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

    # ── State persistence ──
    input_audio.change(fn=lambda p: _persist_audio(p, "enh_input_audio"), inputs=input_audio, outputs=None)
    do_isolate.change(fn=lambda v: _save_state(enh_do_isolate=v), inputs=do_isolate, outputs=None)
    do_denoise.change(fn=lambda v: _save_state(enh_do_denoise=v), inputs=do_denoise, outputs=None)
    do_enhance.change(fn=lambda v: _save_state(enh_do_enhance=v), inputs=do_enhance, outputs=None)

    process_btn.click(
        fn=process_audio_enhancement,
        inputs=[input_audio, do_isolate, do_denoise, do_enhance],
        outputs=output_audio,
    )


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def preload_all_models(args):
    """Eagerly load every model at startup so there is zero wait on first use."""
    global vc_wrapper_v1, vc_wrapper_v2, demucs_model
    global tts_models, re_denoise_fn, re_enhance_fn, RESEMBLE_ENHANCE_OK

    total = sum([args.enable_v2, args.enable_v1, True, True, True])
    step  = 0

    def _step(name):
        nonlocal step
        step += 1
        print(f"\n[{step}/{total}] กำลังโหลด {name} ...")

    # ── V2 ──────────────────────────────────────────
    if args.enable_v2:
        _step("V2 Voice Conversion (ASTRAL + BigVGAN)")
        try:
            vc_wrapper_v2 = load_v2_models(args)
            print(f"[{step}/{total}] ✓ V2 พร้อมใช้งาน")
        except Exception as e:
            print(f"[{step}/{total}] ✗ V2 โหลดไม่สำเร็จ: {e}")

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
        load_demucs()
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
    global vc_wrapper_v1, vc_wrapper_v2

    if not args.enable_v1 and not args.enable_v2:
        print("Error: At least one version (V1 or V2) must be enabled.")
        return

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
            '</span>'
            '</div>'
        )

        with gr.Tabs():
            if args.enable_v1:
                with gr.TabItem("🎙️ V1 — Voice (ภาษาไทย)"):
                    build_v1_tab()

            if args.enable_v2:
                with gr.TabItem("🔊 V2 — Style Conversion"):
                    build_v2_tab()

            with gr.TabItem("🇹🇭 Thai TTS"):
                build_tts_tab()

            with gr.TabItem("✨ Vocal Enhancer"):
                build_enhancer_tab()

    demo.launch()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--compile", action="store_true", help="Compile the model using torch.compile")
    parser.add_argument("--enable-v1", action="store_true",
                        help="Enable V1 (Voice & Singing Voice Conversion)")
    parser.add_argument("--enable-v2", action="store_true",
                        help="Enable V2 (Voice & Style Conversion)")
    args = parser.parse_args()
    main(args)
