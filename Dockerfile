FROM python:3.11-slim

COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --pre --upgrade "yt-dlp[default]" \
    && python -m yt_dlp --version \
    && deno --version

COPY bot.py .

EXPOSE 10000

CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "10000"]
