# Stage 1: Build Frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ARG VITE_APP_BASENAME=/
ARG VITE_API_URL=
ENV VITE_APP_BASENAME=${VITE_APP_BASENAME}
ENV VITE_API_URL=${VITE_API_URL}
COPY scripts/validate_vite_env.mjs /tmp/validate_vite_env.mjs
RUN node /tmp/validate_vite_env.mjs
RUN npm run build

# Stage 2: Backend with Unstructured dependencies
FROM python:3.11-slim AS backend

# Install system dependencies for Unstructured
# Note: libmagic1 needed for python-magic on Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    libreoffice \
    pandoc \
    curl \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/app ./app

# Copy built frontend
COPY --from=frontend-builder /app/frontend/dist ./static

# Create data directory
RUN mkdir -p /data/knowledgevault

# Create non-root runtime user
RUN adduser --disabled-password --gecos '' appuser

# Set ownership for runtime user
RUN chown -R appuser:appuser /app /data

# Switch to non-root user
USER appuser

EXPOSE 9090

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 9090 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
