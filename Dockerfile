# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: builder — install Python dependencies into a clean venv
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools needed for some packages (e.g. psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first so this layer is cache-friendly
COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ---------------------------------------------------------------------------
# Stage 2: runtime — lean production image
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Runtime system deps (libpq for psycopg2 at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

WORKDIR /app

# Copy application code
COPY app/ ./app/
COPY migrations/ ./migrations/

# Create a non-root user and switch to it
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
USER appuser

EXPOSE 5000

# Run with gunicorn; the Flask app factory is app:create_app
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:create_app()"]
