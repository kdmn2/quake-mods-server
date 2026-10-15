FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    PORT=5000

WORKDIR ${APP_HOME}

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./app.py
COPY templates ./templates

RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /data /public /uploads && \
    chown -R appuser:appuser /app /data /public /uploads

USER appuser

EXPOSE 5000

CMD ["python", "app.py"]
