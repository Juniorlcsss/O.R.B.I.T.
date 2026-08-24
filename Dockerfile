# Project O.R.B.I.T. — production image for Cloud Run.
# python:3.11-slim keeps the base small; every dependency in requirements.txt
# publishes manylinux wheels (numpy, sgp4, grpcio, pydantic-core), so no
# compiler toolchain is needed — smaller image, faster Cloud Run cold starts.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency layer first — maximises Docker layer caching across builds.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Run as an unprivileged user (Cloud Run best practice).
RUN useradd --create-home --shell /usr/sbin/nologin orbit && \
    chown -R orbit:orbit /app
USER orbit

# Cloud Run injects PORT; default to 8080 for local container runs.
ENV PORT=8080
EXPOSE 8080

# Single worker: the fleet is stateless but this is a cost-conscious demo,
# and InMemorySessionService state lives per-process.
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --log-config logging.json
