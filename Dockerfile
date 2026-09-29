# Multi-stage build for minimal image size
FROM python:3.12-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
  gcc \
  && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt


# Final stage - minimal runtime image
FROM python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Set PATH to include user-installed packages
ENV PATH=/root/.local/bin:$PATH

# Copy application code
COPY homestew/ ./homestew/
COPY frontend/ ./static/

# Create data directory with persistent volume mount point
RUN mkdir -p /data && chmod 755 /data

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Expose port
EXPOSE 8000

# Health check - deliberately stdlib-only (issue #9). The old command ran
# `python -c "import requests; ..."` every 30s, which cold-started an
# interpreter plus requests/urllib3/idna/charset_normalizer inside the
# container on every probe: that import burst WAS the periodic idle CPU
# spike. http.client is a tiny stdlib module, and it also checks the status
# code (the old command accepted ANY HTTP response, even a 500). 127.0.0.1
# instead of localhost avoids a wasted IPv6 ::1 connect attempt first.
# Compose users can override the interval without a rebuild via
# HEALTHCHECK_INTERVAL - see docker-compose.yml. 5-minute default: nothing
# auto-restarts on "unhealthy" here, so the probe is only a status badge -
# it does not need to be frequent.
HEALTHCHECK --interval=300s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import sys,http.client as h; r=h.HTTPConnection('127.0.0.1',8000,timeout=5); r.request('GET','/health'); sys.exit(0 if r.getresponse().status==200 else 1)" || exit 1

# Run the application
CMD ["uvicorn", "homestew.main:app", "--host", "0.0.0.0", "--port", "8000"]
