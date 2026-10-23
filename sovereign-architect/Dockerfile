# =========================================================================
# STAGE 1: The Builder Layer (Compiles dependencies and wheels)
# =========================================================================
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install compilation tools needed for C-extensions and WeasyPrint assets
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Compile Python packages into wheel binaries isolated from the runtime environment
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /build/wheels -r requirements.txt


# =========================================================================
# STAGE 2: The Secure Runtime Layer (The actual deployed production image)
# =========================================================================
FROM python:3.11-slim-bookworm

LABEL maintainer="NSK Devpreneur Hub <admin@nskdevpreneur-hub.com>" \
      environment="production"

# Ingest Knative / Cloud Run optimization settings
ENV APP_HOME=/app \
    PYTHONUNBUFFERED=True \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR $APP_HOME

# Install only the bare shared library dependencies required to run WeasyPrint
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled wheels from the builder layer and install them cleanly
COPY --from=builder /build/wheels /tmp/wheels
COPY --from=builder /build/requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels -r requirements.txt \
    && rm -rf /tmp/wheels

# Copy application source code into place
COPY . ./

# CYBERSECURITY HARDENING: Create an unprivileged system user to prevent root execution
RUN groupadd -g 10001 nskgroup && \
    useradd -u 10001 -g nskgroup -s /bin/bash -m nskuser && \
    chown -R nskuser:nskgroup $APP_HOME

# Switch context entirely to the secure, unprivileged user
USER nskuser

EXPOSE 8080

# Execute Gunicorn directly via exec vector syntax (bypasses risky intermediate shell wrappers)
CMD ["gunicorn", "--bind", ":8080", "--workers", "1", "--threads", "8", "--timeout", "0", "app:app"]
