module.exports = {
  daemon: true,
  run: [

    // ── Smart auto-update: เช็ค remote ก่อน pull ──────────────────────
    // fetch → นับ commits ที่ remote ใหม่กว่า → pull เฉพาะเมื่อมี update
    // ถ้า local มี uncommitted changes → ff-only จะ skip เอง
    {
      method: "shell.run",
      params: {
        message: [
          "echo [version] local: $(cat VERSION 2>/dev/null || echo unknown)",
          "git fetch origin --quiet 2>/dev/null && BEHIND=$(git rev-list HEAD..origin/main --count 2>/dev/null || echo 0) && if [ \"$BEHIND\" -gt \"0\" ]; then echo \"[update] $BEHIND new commits — pulling...\" && git pull --ff-only && echo \"[version] updated: $(cat VERSION 2>/dev/null || echo unknown)\"; else echo \"[update] already up to date\"; fi || echo [update] offline — skipped",
        ]
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
