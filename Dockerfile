FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home novaapp

WORKDIR /app
COPY --chown=novaapp:novaapp app.py openai_http.py runtime_logging.py index.html ./
USER novaapp

EXPOSE 8080
CMD ["python", "app.py"]
