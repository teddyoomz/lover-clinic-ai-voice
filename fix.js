// fix.js — ติดตั้งส่วนที่ขาดโดยไม่ต้อง Reset ทั้งหมด
module.exports = {
  run: [
    {
      method: "log",
      params: {
        text: "🔧 กำลัง Fix — ตรวจสอบและแก้ไข package..."
      }
    },

    // ── 1. Reinstall torch + torchvision + torchaudio ให้ตรงกับ GPU ──────
    // แก้ปัญหา CPU-only torch / torchvision mismatch
    // torch.js จะ detect GPU และลง CUDA build ที่ถูกต้องอัตโนมัติ
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

    // ── 2. ติดตั้ง package ที่อาจขาด ────────────────────────────────────
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        message: [
          "uv pip install yt-dlp",
          "uv pip install f5-tts-th",
          "uv pip install transformers==4.46.3",
          "uv pip install imageio-ffmpeg",
          "uv pip install demucs",
          "uv pip install resemble-enhance --no-deps",
          "uv pip install vocos tabulate",
          "python ../make_deepspeed_stub.py",
        ]
      }
    },

    {
      method: "log",
      params: {
        text: "✅ Fix เสร็จแล้ว — กด Start เพื่อเปิดแอปใหม่"
      }
    }
  ]
}
