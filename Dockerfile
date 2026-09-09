FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY curbside ./curbside
RUN pip install --no-cache-dir ".[api]"

COPY data ./data

# Persistent state lives here; mount an EFS volume at this path in production.
RUN mkdir -p var
VOLUME ["/app/var"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "curbside.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
