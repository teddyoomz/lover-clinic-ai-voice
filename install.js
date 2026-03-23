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
    // • Filter out torch/torchaudio/torchvision + --index-url lines.
    //   torch.js (step 3) installs the correct CUDA/MPS/CPU build anyway.
    // • Wrap pip in Python subprocess so pip dependency-conflict warnings
    //   (exit 1) don't halt Pinokio. Packages still install fine.
    // • imageio-ffmpeg and yt-dlp are installed HERE (early) so they are
    //   available even if later steps fail. imageio-ffmpeg bundles a static
    //   ffmpeg binary — no conda/DLL dependency needed on Windows.
    //   (conda install ffmpeg was removed: gdk-pixbuf post-link script
    //    crashes on Windows with error 3221225785, causing blue-screen halt.)
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "python -c \"import re; skip={'torch','torchvision','torchaudio'}; lines=[l for l in open('requirements.txt') if '--index-url' not in l and re.split(r'[=<>! \\t]',l.strip())[0].lower() not in skip]; open('../req_base.txt','w').writelines(lines)\"",
          "python -c \"import subprocess,sys; r=subprocess.run(['pip','install','-r','../req_base.txt'],capture_output=True,text=True); [print(l) for l in r.stdout.splitlines() if 'dependency resolver' not in l and \\\"pip's dependency\\\" not in l]; sys.exit(0)\"",
          "uv pip install f5-tts-th",
          "uv pip install imageio-ffmpeg",
          "uv pip install yt-dlp",
        ]
      }
    },

    // ── 3. Install PyTorch optimised for this machine ─────────────────────
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

    // ── 4. Install audio processing tools (must run after torch) ──────────
    // demucs           — vocal separation (htdemucs), needs torch at install time
    // resemble-enhance — enhancement/denoising; install --no-deps (deepspeed
    //                    requires MSVC on Windows — use stub instead)
    // vocos            — missing dep of resemble-enhance
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "uv pip install demucs",
          "uv pip install resemble-enhance --no-deps",
          "uv pip install vocos tabulate",
          // Create minimal deepspeed stub so resemble-enhance can import
          // without the real deepspeed (skipped if real deepspeed present).
          "python ../make_deepspeed_stub.py",
        ]
      }
    },

    // ── 5. Auto-launch after install ──────────────────────────────────────
    {
      method: "script.start",
      params: {
        uri: "start.js"
      }
    },

  ]
}
