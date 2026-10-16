FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    PORT=5000 \
    PERSIST_ROOT=/appdata \
    DB_PATH=/appdata/addons.sqlite3 \
    PUBLIC_ROOT=/public \
    UPLOAD_FOLDER=/uploads

WORKDIR ${APP_HOME}

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends unzip && rm -rf /var/lib/apt/lists/*

COPY app.py ./app.py
COPY tools ./tools
COPY templates ./templates

RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /appdata /public /uploads && \
    chown -R appuser:appuser /app /appdata /public /uploads

USER appuser

EXPOSE 5000

CMD ["python", "app.py"]
