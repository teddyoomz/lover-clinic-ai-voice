module.exports = {
  run: [

    // ── 1. Pull latest launcher scripts from GitHub ───────────────────────
    // reset --hard ให้ได้ latest เสมอ ไม่ติด local changes
    {
      method: "shell.run",
      params: {
        message: "git fetch origin && git reset --hard origin/main"
      }
    },

    // ── 2. Pull latest seed-vc app ────────────────────────────────────────
    {
      method: "shell.run",
      params: {
        path: "app",
        message: "git pull"
      }
    },

    // ── 3. Re-install Python deps + imageio-ffmpeg + yt-dlp ──────────────
    // imageio-ffmpeg: bundled static ffmpeg binary (no conda/DLL needed).
    // yt-dlp: YouTube/Facebook downloader tab.
    // Both installed early so later step failures don't affect these tabs.
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

    // ── 4. Re-install GPU-optimised PyTorch ───────────────────────────────
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

    // ── 5. Re-install audio tools + refresh deepspeed stub ───────────────
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "uv pip install demucs",
          "uv pip install resemble-enhance --no-deps",
          "uv pip install vocos tabulate",
          "python ../make_deepspeed_stub.py",
        ]
      }
    },

  ]
}
