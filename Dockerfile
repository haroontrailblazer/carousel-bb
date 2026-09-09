# Carousel Factory - one image serving the console, the API and the review
# pages. Google ADK drives the pipeline inside the same process; none of its
# own web surface is exposed.
#
# Two stages: Node builds the React bundle, Python runs everything. The bundle
# is copied into the runtime image, so there is no Node in production and no
# separate frontend host - which is what keeps this a single Render service and
# lets the browser talk to the API same-origin (cookies, SSE, no CORS).

# --------------------------------------------------------------------------
# Stage 1: build the SPA
# --------------------------------------------------------------------------
FROM node:22-slim AS frontend

WORKDIR /build

# Copy manifests first so a dependency install is cached across source edits.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# --------------------------------------------------------------------------
# Stage 2: the runtime
# --------------------------------------------------------------------------
FROM python:3.13-slim

# ffmpeg is a hard requirement, not an optional extra: the cover video is
# trimmed and rendered with it. The Liberation and DejaVu fonts are what the
# slide compositor draws with - without them Pillow silently falls back to a
# bitmap default and every slide's typography is wrong.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-liberation \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /build/dist ./frontend/dist

# Scratch space for downloads, trimmed clips and rendered slides. Ephemeral by
# design - the canonical copies go to Supabase Storage, so losing this on a
# redeploy costs nothing.
ENV WORKDIR=/tmp/carousel-work
RUN mkdir -p /tmp/carousel-work

EXPOSE 8000

# --proxy-headers and --forwarded-allow-ips let uvicorn trust the reverse
# proxy's forwarded scheme and client IP, so cookies are marked Secure
# correctly and access logs show real client addresses.
CMD ["sh", "-c", "uvicorn web_app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
