module.exports = {
  daemon: true,
  run: [

    // ── Auto-update: ดึง launcher scripts ล่าสุดจาก GitHub ──────────────
    // ทำทุกครั้งที่กด Start — ถ้าไม่มี internet หรือ git fail ให้ข้ามต่อ
    {
      method: "shell.run",
      params: {
        message: "git pull --ff-only || echo [auto-update] git pull skipped"
      }
    },

    // ── Launch app ────────────────────────────────────────────────────────
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "python ../app_fixed.py --enable-v1 --enable-v2",
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
    }
  ]
}
