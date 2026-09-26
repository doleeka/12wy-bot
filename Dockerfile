FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/data/bot.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot

# /data — точка монтирования persistent volume на Fly.io (см. fly.toml).
# Без volume база жила бы в файловой системе контейнера и пропадала при каждом деплое.
VOLUME ["/data"]

CMD ["python", "-m", "bot.main"]
