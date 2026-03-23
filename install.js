module.exports = {
  run: [

    // ── 1. Clone Seed-VC ──────────────────────────────────────────────────
    {
      method: "shell.run",
      params: {
        message: "git clone https://github.com/Plachtaa/seed-vc app",
        when: "{{!exists('app')}}"
      }
    },

    // ── 2. Install Python dependencies ────────────────────────────────────
    // • Filter out lines with --index-url from requirements.txt to avoid the
    //   torch nightly vs. pinned-version conflict (lines 1-3 vs lines 5-7).
    //   torch.js (step 3) installs the correct CUDA/MPS/CPU build anyway.
    // • Wrap pip in Python subprocess so that pip's dependency-conflict
    //   warnings (which make pip exit 1) don't halt the Pinokio install.
    //   The actual packages still get installed — only non-fatal warnings
    //   (e.g. modelscope forcing protobuf<3.20) are suppressed.
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "python -c \"lines=[l for l in open('requirements.txt') if '--index-url' not in l]; open('../req_base.txt','w').writelines(lines)\"",
          "python -c \"import subprocess,sys; r=subprocess.run(['pip','install','-r','../req_base.txt'],capture_output=True,text=True); [print(l) for l in r.stdout.splitlines() if 'dependency resolver' not in l and \\\"pip's dependency\\\" not in l]; sys.exit(0)\"",
          "uv pip install f5-tts-th",
        ]
      }
    },

    // ── 3. Install PyTorch optimised for this machine ─────────────────────
    // torch.js auto-detects CUDA / MPS / CPU and installs the matching build.
    // This overwrites the torch==2.4.0 installed in step 2 with the correct
    // GPU-accelerated version for this specific machine.
    {
      method: "script.start",
      params: {
        uri: "torch.js",
        params: {
          venv: "env",
          path: "app",
        }
      }
    },

    // ── 4. Install ffmpeg (cross-platform via conda) ───────────────────────
    // Required by the "Video → Audio" tab to extract audio from video files.
    {
      method: "shell.run",
      params: {
        message: "conda install -c conda-forge ffmpeg -y"
      }
    },

    // ── 5. Install audio processing tools (must run after torch) ──────────
    // demucs           — vocal separation (htdemucs), needs torch at install time
    // resemble-enhance — enhancement/denoising; uses deepspeed only for
    //                    training — deepspeed fails to compile on Windows so
    //                    install with --no-deps and add vocos separately
    // vocos             — the one missing dep of resemble-enhance
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "uv pip install demucs",
          "uv pip install resemble-enhance --no-deps",
          "uv pip install vocos tabulate",
          // Create a minimal deepspeed stub so resemble-enhance can import on
          // Windows without needing the real deepspeed (which requires MSVC).
          // The stub is skipped automatically if real deepspeed is installed.
          "python ../make_deepspeed_stub.py",
        ]
      }
    },

  ]
}
