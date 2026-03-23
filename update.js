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

    // ── 3. Re-install Python deps (filter --index-url to avoid conflicts) ──
    // Same strategy as install.js: strip --index-url lines from requirements.txt
    // before running pip, to avoid torch nightly conflicts.
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

    // ── 4. Re-install GPU-optimised PyTorch ───────────────────────────────
    // Ensures correct torch build if machine GPU config has changed.
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

    // ── 5. Ensure ffmpeg is available (for Video → Audio tab) ────────────
    {
      method: "shell.run",
      params: {
        message: "conda install -c conda-forge ffmpeg -y"
      }
    },

    // ── 6. Re-install audio tools + refresh deepspeed stub ───────────────
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "uv pip install demucs",
          "uv pip install resemble-enhance --no-deps",
          "uv pip install vocos tabulate",
          "uv pip install imageio-ffmpeg",
          "uv pip install yt-dlp",
          "python ../make_deepspeed_stub.py",
        ]
      }
    },

  ]
}
