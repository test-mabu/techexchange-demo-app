# Multi-stage build für optimierte Container-Größe
FROM python:3.11-slim-bookworm AS builder

# Build dependencies (offline .deb packages staged by the workflow)
COPY .apt-debs /tmp/apt-debs
RUN dpkg -i /tmp/apt-debs/*.deb || apt-get install -y --no-download -f

# Python dependencies
COPY .wheelhouse /wheelhouse
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --no-index --find-links=/wheelhouse --prefix=/install -r /tmp/requirements.txt

# Production stage
FROM python:3.11-slim-bookworm

# Metadata
LABEL maintainer="mbx1010" \
      version="3.2" \
      description="Load Testing Application with Echo Service Integration - Security Hardened" \
      org.opencontainers.image.source="https://github.com/mbx1010/load-test-app"

# System dependencies from offline .deb packages (no apt-get update)
COPY .apt-debs /tmp/apt-debs
RUN dpkg -i /tmp/apt-debs/*.deb || apt-get install -y --no-download -f

# Non-root user erstellen
RUN groupadd -r appuser && useradd -r -g appuser -u 1000 appuser

# Working directory
WORKDIR /app

# Python packages von builder stage kopieren
COPY --from=builder /install /usr/local

# Application code kopieren
COPY --chown=appuser:appuser app.py /app/
COPY --chown=appuser:appuser requirements.txt /app/

# Environment variables
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    PORT=8080 \
    HOST=0.0.0.0 \
    ECHO_SERVICE_URL=http://vulnerable-echo-service:8085

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# User wechseln
USER appuser

# Port exposieren
EXPOSE 8080

# Application starten
CMD ["python", "app.py"]

