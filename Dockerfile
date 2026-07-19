FROM python:3.11-slim

# Install system deps — ffmpeg, nodejs for yt-dlp JS challenge solving
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install latest yt-dlp binary directly (always up to date)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp \
    && chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir faster-whisper && \
    pip install --no-cache-dir torch==2.2.2 torchaudio==2.2.2 \
        --index-url https://download.pytorch.org/whl/cpu

COPY . .

RUN mkdir -p uploads clips watermarks previews
RUN python -c "from db.database import init_db; init_db()"

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
