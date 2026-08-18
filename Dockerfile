# =============================================================================
# SpotifyForge — Multi-stage Dockerfile
# =============================================================================
# Build:  docker build -t spotifyforge .
# Run:    docker run -p 8000:8000 --env-file .env spotifyforge

# ---------------------------------------------------------------------------
# Stage 1: Build — install Python dependencies into a virtual environment
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build-time system dependencies (needed for some pip packages)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Create a virtual environment so we can copy it cleanly to the runtime stage
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the source and install. (A deps-only layer via `pip install .` from
# pyproject.toml alone does not work: hatchling builds the *project*, which
# needs README.md and the package sources present in the build context.)
COPY pyproject.toml README.md ./
COPY spotifyforge/ ./spotifyforge/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# ---------------------------------------------------------------------------
# Stage 2: Runtime — minimal image with only what we need
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

LABEL maintainer="SpotifyForge Contributors"
LABEL description="SpotifyForge — the all-in-one platform for serious Spotify playlist curators"

# Create a non-root user for security
RUN groupadd --gid 1000 spotifyforge && \
    useradd --uid 1000 --gid spotifyforge --create-home spotifyforge

# Copy the virtual environment from the build stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create the default database directory
RUN mkdir -p /home/spotifyforge/.spotifyforge && \
    chown -R spotifyforge:spotifyforge /home/spotifyforge/.spotifyforge

# Copy only the application source (not tests, docs, etc.)
WORKDIR /app
COPY --chown=spotifyforge:spotifyforge spotifyforge/ ./spotifyforge/
COPY --chown=spotifyforge:spotifyforge pyproject.toml ./

# Expose the default web server port
EXPOSE 8000

# Health check against the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Drop to non-root user
USER spotifyforge

# Run the FastAPI application with uvicorn
CMD ["uvicorn", "spotifyforge.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
