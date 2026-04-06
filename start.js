module.exports = {
  daemon: true,
  run: [

    // ── 1. Version check + auto-update ───────────────────────────────────
    {
      method: "shell.run",
      params: {
        message: [
          "LOCAL_VER=$(cat VERSION 2>/dev/null || echo 0.0.0)",
          "echo \"========================================\"",
          "echo \"  Lover Clinic AI Voice v$LOCAL_VER\"",
          "echo \"========================================\"",
          "git fetch origin --quiet 2>/dev/null && REMOTE_VER=$(git show origin/main:VERSION 2>/dev/null || echo $LOCAL_VER) && if [ \"$LOCAL_VER\" != \"$REMOTE_VER\" ]; then echo \"[update] v$LOCAL_VER -> v$REMOTE_VER\" && git pull --ff-only && echo \"[update] Updated to v$(cat VERSION 2>/dev/null)\"; else echo \"[update] Up to date\"; fi || echo \"[update] Offline — skipped\"",
          "INSTALLED=$(cat .deps_installed 2>/dev/null || cat DEPS_VERSION 2>/dev/null || echo 0)",
          "NEEDED=$(cat DEPS_VERSION 2>/dev/null || echo 0)",
          "if [ \"$INSTALLED\" != \"$NEEDED\" ]; then echo \"[deps] Update required (v$INSTALLED -> v$NEEDED)\" && touch .needs_deps_update; else rm -f .needs_deps_update && echo \"[deps] Up to date\"; fi"
        ]
      }
    },

    // ── 2. Auto-reinstall deps if DEPS_VERSION changed ───────────────────
    {
      when: "{{exists('.needs_deps_update')}}",
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "echo \"[deps] Installing updated dependencies...\"",
          "python -c \"import re; skip={'torch','torchvision','torchaudio'}; lines=[l for l in open('requirements.txt') if '--index-url' not in l and re.split(r'[=<>! \\t]',l.strip())[0].lower() not in skip]; open('../req_base.txt','w').writelines(lines)\"",
          "python -c \"import subprocess,sys; r=subprocess.run(['pip','install','-r','../req_base.txt'],capture_output=True,text=True); [print(l) for l in r.stdout.splitlines() if 'dependency resolver' not in l and \\\"pip's dependency\\\" not in l]; sys.exit(0)\"",
          "uv pip install f5-tts-th",
          "uv pip install imageio-ffmpeg",
          "uv pip install yt-dlp",
          "uv pip install demucs",
          "uv pip install resemble-enhance --no-deps",
          "uv pip install vocos tabulate",
          "python ../make_deepspeed_stub.py",
          "echo \"[deps] Done\"",
        ]
      }
    },

    // ── 3. Mark deps installed + cleanup ─────────────────────────────────
    {
      when: "{{exists('.needs_deps_update')}}",
      method: "shell.run",
      params: {
        message: [
          "cp DEPS_VERSION .deps_installed",
          "rm -f .needs_deps_update"
        ]
      }
    },

    // ── 4. Launch app ────────────────────────────────────────────────────
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "python ../app_fixed.py --enable-v1",
        ],
        on: [{
          event: "/(http:\\/\\/[0-9.:]+)/",
          done: true
        }]
      }
    },
    {
      method: "local.set",
      params: {
        url: "{{input.event[1]}}"
      }
    },

    // ── 5. Open in default browser ───────────────────────────────────────
    {
      when: "{{platform === 'win32'}}",
      method: "shell.run",
      params: {
        message: "start \"\" \"{{local.url}}\""
      }
    },
    {
      when: "{{platform === 'darwin'}}",
      method: "shell.run",
      params: {
        message: "open \"{{local.url}}\""
      }
    },
    {
      when: "{{platform === 'linux'}}",
      method: "shell.run",
      params: {
        message: "xdg-open \"{{local.url}}\""
      }
    }
  ]
}
