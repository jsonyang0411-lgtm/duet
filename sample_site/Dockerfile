# ACE7CN sample picking site: standard library only, no pip install.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SAMPLE_SITE_HOST=0.0.0.0 \
    SAMPLE_SITE_PORT=8080 \
    SAMPLE_SITE_DB=/data/sample_site.db

RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin site \
    && mkdir -p /data \
    && chown site:site /data \
    && chmod 700 /data

WORKDIR /app
COPY . /app

USER site
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('SAMPLE_SITE_PORT', '8080'), timeout=4)"]

CMD ["python3", "app.py"]
