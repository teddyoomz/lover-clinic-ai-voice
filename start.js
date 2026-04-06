module.exports = {
  daemon: true,
  run: [

    // ── Auto-update: ดึง launcher scripts ล่าสุดจาก GitHub ──────────────
    // fetch → reset hard ให้ได้ latest เสมอ ไม่ติด local changes
    // ถ้า internet หรือ git fail ให้ข้ามต่อไปได้เลย
    {
      method: "shell.run",
      params: {
        message: "git pull --ff-only || echo [auto-update] skipped (local changes detected)"
      }
    },

    // ── Launch app ────────────────────────────────────────────────────────
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
    // ── Open system default browser automatically ─────────────────────────
    {
      method: "shell.run",
      params: {
        when: "{{platform === 'win32'}}",
        message: "start \"\" \"{{local.url}}\""
      }
    },
    {
      method: "shell.run",
      params: {
        when: "{{platform === 'darwin'}}",
        message: "open \"{{local.url}}\""
      }
    },
    {
      method: "shell.run",
      params: {
        when: "{{platform === 'linux'}}",
        message: "xdg-open \"{{local.url}}\""
      }
    }
  ]
}
