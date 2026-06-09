# Research Alignment — single portable image used both on the managed host
# (Railway/Render) now and on the UCSD on-prem VM later. The same image serves
# the web app and runs the in-process scheduler; all mutable state lives on the
# /data volume, never in the image or the repo.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    FACULTY_DB_PATH=/data/app.db \
    DATA_STATE_DIR=/data/state \
    EAH_CSV_PATH="/data/private/EAH Active Academics.csv"

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (the committed faculty JSON ships as the seed snapshot).
COPY . .

RUN chmod +x docker-entrypoint.sh

EXPOSE 8000

# Entrypoint seeds the /data volume from the committed JSON on first boot,
# builds the SQLite DB if absent, then starts gunicorn.
ENTRYPOINT ["./docker-entrypoint.sh"]
