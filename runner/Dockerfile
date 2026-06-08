FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        ffmpeg \
        fonts-liberation \
        fonts-noto-color-emoji \
        fonts-noto-core \
        fonts-noto-cjk \
        espeak-ng \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

COPY runner/requirements.txt /tmp/requirements.txt

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cargo rustc \
    && pip install --no-cache-dir \
        --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/ \
        -r /tmp/requirements.txt \
    && apt-get purge -y --auto-remove build-essential cargo rustc \
    && rm -rf /var/lib/apt/lists/*
RUN python -m playwright install chromium

WORKDIR /workspace

CMD ["uvicorn", "runner.app:app", "--host", "0.0.0.0", "--port", "8000"]
