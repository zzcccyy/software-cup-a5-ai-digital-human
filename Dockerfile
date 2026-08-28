FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# APP_ROOT resolves to /app, so runtime source, knowledge data, and root VRM models
# remain available to the Flask service.
COPY . /app

EXPOSE 8088

CMD ["sh", "-c", "gunicorn --chdir backend --bind 0.0.0.0:${PORT:-8088} --workers ${GUNICORN_WORKERS:-1} --threads ${GUNICORN_THREADS:-8} --timeout 300 main:app"]
