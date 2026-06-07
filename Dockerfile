# Stage 1: Build Frontend
FROM node:26-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ARG VITE_APP_BASENAME=/
ARG VITE_API_URL=
ENV VITE_APP_BASENAME=${VITE_APP_BASENAME}
ENV VITE_API_URL=${VITE_API_URL}
# validate_vite_env inlined to avoid dependency on external file
RUN node -e "
const raw = process.env.VITE_APP_BASENAME ?? '';
const hasCtrl = function(s) {
  for (const ch of s) { const c = ch.charCodeAt(0); if (c < 32 || c === 127) return true; }
  return false;
};
const validate = function(raw) {
  if (!raw || raw === '/') return;
  if (raw !== raw.trim()) throw new Error('VITE_APP_BASENAME cannot contain leading or trailing whitespace');
  if (/^https?:\/\//i.test(raw) || (raw.startsWith('//') && /[^/]/.test(raw))) throw new Error('VITE_APP_BASENAME must be a path, not a URL');
  if (/[\s;\\\\?#]/.test(raw) || hasCtrl(raw)) throw new Error('VITE_APP_BASENAME contains unsafe characters');
  const inner = raw.replace(/^\/+|\/+$/g, '');
  if (/\/{2,}/.test(inner)) throw new Error('VITE_APP_BASENAME cannot contain duplicate slashes');
  if (inner && inner.split('/').some(function(p) { return p === '.' || p === '..'; })) throw new Error('VITE_APP_BASENAME cannot contain relative path segments');
};
try {
  validate(raw);
  const d = raw || '(empty - root deployment)';
  console.log('validate_vite_env: VITE_APP_BASENAME=' + d + ' OK');
} catch (e) {
  console.error('validate_vite_env: FATAL: ' + e.message);
  console.error('Fix VITE_APP_BASENAME and rebuild.');
  process.exit(1);
}
"
RUN npm run build

# Stage 2: Backend with Unstructured dependencies
FROM python:3.14-slim AS backend

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

# Pre-download the spaCy model used by unstructured's NLP paths. spaCy installs
# models into root-owned site-packages, so a first-parse runtime download as the
# non-root appuser fails with a permission error. Fetching it here (as root)
# bakes it into the image so document parsing works on a fresh container.
# Like the apt/pip steps above, this needs network access at build time; for
# restricted/airgapped builds pass a proxy via Docker's standard build args
# (HTTP_PROXY/HTTPS_PROXY) or pre-seed the model from a mirror.
RUN python -m spacy download en_core_web_sm

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
