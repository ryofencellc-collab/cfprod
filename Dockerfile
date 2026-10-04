FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf \
    VOICES_DIR=/app/voices

# ffmpeg for video, nodejs for yt-dlp's YouTube challenge solving,
# fontconfig + Liberation fonts for caption/watermark rendering.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates nodejs fontconfig fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Latest yt-dlp binary (sites change often; keep it current on every build).
RUN curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
    && chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Narration voice: LibriTTS (CC BY 4.0) — licensed for commercial use with credit.
# Do not swap in lessac/amy/ryan-based voices; they are non-commercial.
RUN mkdir -p /app/voices && cd /app/voices \
    && base=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts/high \
    && curl -fsSL -o en_US-libritts-high.onnx "$base/en_US-libritts-high.onnx" \
    && curl -fsSL -o en_US-libritts-high.onnx.json "$base/en_US-libritts-high.onnx.json"

# Pre-download the Whisper model so the first job doesn't stall.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"

COPY . .

# Caption / hook / watermark fonts (SIL OFL) + name aliases used by the editor.
RUN mkdir -p /usr/share/fonts/truetype/clipforge \
    && cp fonts/*.ttf /usr/share/fonts/truetype/clipforge/ \
    && cp fonts/99-clipforge-aliases.conf /etc/fonts/conf.d/ \
    && fc-cache -f

EXPOSE 8000

# Runtime data lives in DATA_DIR — mount a Railway volume there (e.g. /data).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
