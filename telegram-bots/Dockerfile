# syntax=docker/dockerfile:1
# Build context is the repo ROOT (see docker-compose.yml).
FROM python:3.11-slim AS bots
WORKDIR /app/telegram-bots

# gcc is a fallback in case a C-extension wheel (e.g. tgcrypto) is unavailable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY telegram-bots/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram-bots/ ./

# Sessions and data are mounted as volumes by compose so they survive restarts.
CMD ["python3", "main.py"]
