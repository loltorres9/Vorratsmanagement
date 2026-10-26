FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# 1 Worker: der Keep-Sync läuft als Thread im Prozess und SQLite mag keine parallelen Schreiber.
CMD gunicorn --workers 1 --threads 4 --bind 0.0.0.0:${PORT:-8000} --access-logfile - wsgi:app
