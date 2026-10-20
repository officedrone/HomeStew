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

# Delete pip from the runtime image entirely — the app never installs
# packages at runtime (deps are baked in via COPY below), and every pip
# release drags its vendored tree into scan reports: pip itself
# (CVE-2025-8869, CVE-2026-13346, CVE-2026-6357, CVE-2026-3219,
# CVE-2026-8643, CVE-2026-1703) plus its vendored urllib3/setuptools/msgpack
# (CVE-2026-97689, CVE-2026-97687, CVE-2026-97688, CVE-2025-47273,
# CVE-2026-59890, CVE-2026-57585, GHSA-6v7p-g79w-8964). Upgrading pip is
# whack-a-mole: pip 26.2 still vendors urllib3 2.7.0 (< 2.8.0 fix). rm -rf
# because the base image ships pip without RECORD metadata, so `pip
# uninstall` cannot work anyway (and --ignore-installed upgrades leave the
# old tree on disk alongside the new one).
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip \
    /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
    /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

# Copy installed packages from builder (the builder's --user site never
# contains pip — `pip install --user` skips it — so nothing stale is copied)
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
