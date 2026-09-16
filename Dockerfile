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

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:8000/health')" || exit 1

# Run the application
CMD ["uvicorn", "homestew.main:app", "--host", "0.0.0.0", "--port", "8000"]
