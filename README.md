# Lover Clinic — AI Voice System

ระบบ AI Voice System สำหรับ **Lover Clinic** — ครบวงจรในแอปเดียว:
- **Voice Conversion** (V1 + V2) — แปลงเสียงแบบ Zero-shot ไม่ต้องเทรนโมเดล
- **Thai TTS** — Text-to-Speech ภาษาไทย ด้วย F5-TTS-THAI
- **Vocal Enhancer** — ถอดเสียงจากพื้นหลัง + ลด noise + เพิ่มความคมชัดเสียง

Powered by [Seed-VC](https://github.com/Plachtaa/seed-vc), [F5-TTS-THAI](https://huggingface.co/VIZINTZOR), [Demucs](https://github.com/facebookresearch/demucs) & [resemble-enhance](https://github.com/resemble-ai/resemble-enhance).

## How to Use

1. Click **Install** to set up dependencies (first time only)
2. Click **Start** to launch the web interface
3. In the web UI:
   - Upload a **Reference Audio** — this is the voice you want to copy
   - Upload a **Source Audio** — this is the audio content you want to convert
   - Click Convert and download the result

## API

The Gradio app exposes a REST API automatically.

### JavaScript (fetch)

```javascript
const formData = new FormData()
formData.append("data", JSON.stringify([
  { path: "ref.wav" },   // reference audio
  { path: "src.wav" },   // source audio
  // additional params depending on Gradio component order
]))

const response = await fetch("http://localhost:7860/run/predict", {
  method: "POST",
  body: formData
})
const result = await response.json()
console.log(result)
```

### Python

```python
from gradio_client import Client

client = Client("http://localhost:7860")
result = client.predict(
    reference_audio="ref.wav",
    source_audio="src.wav",
    api_name="/predict"
)
print(result)
```

### curl

```bash
curl -X POST http://localhost:7860/run/predict \
  -H "Content-Type: application/json" \
  -d '{
    "data": [
      {"path": "ref.wav"},
      {"path": "src.wav"}
    ]
  }'
```

> Note: Port may vary. Check the terminal for the actual URL when the app starts.
