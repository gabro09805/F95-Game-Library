FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY app.py ./app.py
COPY web ./web
RUN mkdir -p /data && chown -R nobody:nogroup /app /data
USER nobody

EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/api/health', timeout=2)"

CMD ["python", "app.py"]
