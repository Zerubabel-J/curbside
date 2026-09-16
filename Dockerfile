FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# The postcard builder draws with DejaVu and silently falls back to PIL's
# bitmap default when it is absent - the card still renders, and every test
# still passes, but the headline comes out unreadably small. python:*-slim
# ships no fonts, so this layer is load-bearing.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY curbside ./curbside
RUN pip install --no-cache-dir ".[api]"

COPY data ./data

# Working state - database, fetched imagery, generated postcards. On App Runner
# this is container-local and cleared on restart; mount a volume here if the
# deployment needs it to survive.
RUN mkdir -p var

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

# App Runner health-checks the port it injects; default to 8000 locally.
ENV PORT=8000
CMD ["sh", "-c", "uvicorn curbside.api.app:app --host 0.0.0.0 --port ${PORT}"]
