module.exports = {
  run: [

    // ── Remove cloned app repo ─────────────────────────────────────────────
    {
      method: "fs.rm",
      params: { path: "app" }
    },

    // ── Remove Python virtual environment ─────────────────────────────────
    {
      method: "fs.rm",
      params: { path: "env" }
    },

    // ── Remove cached requirements file ───────────────────────────────────
    {
      method: "fs.rm",
      params: {
        path: "req_base.txt",
        when: "{{exists('req_base.txt')}}"
      }
    },

    // ── Remove user session state (settings, slider values, etc.) ─────────
    {
      method: "fs.rm",
      params: {
        path: "session_state.json",
        when: "{{exists('session_state.json')}}"
      }
    },

    // ── Remove persisted user audio uploads ───────────────────────────────
    {
      method: "fs.rm",
      params: {
        path: "user_uploads",
        when: "{{exists('user_uploads')}}"
      }
    },

  ]
}
