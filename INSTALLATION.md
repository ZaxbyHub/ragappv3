# RAGAPPv3 Installation Guide

## Overview

RAGAPPv3 is a Retrieval-Augmented Generation (RAG) knowledge base application with the following stack:
- **Backend**: FastAPI (Python 3.11+)
- **Frontend**: React + TypeScript + Vite
- **Database**: SQLite with connection pooling
- **Vector Store**: LanceDB
- **LLM**: Ollama (local) or OpenAI API
- **Embeddings**: Local embeddings or OpenAI
- **Authentication**: JWT with refresh tokens
- **Authorization**: RBAC with vault-level permissions

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Platform-Specific Setup](#platform-specific-setup)
3. [Installation Methods](#installation-methods)
4. [Configuration](#configuration)
5. [Verification](#verification)
6. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Required Software

| Component | Minimum Version | Purpose |
|-----------|----------------|---------|
| Python | 3.11+ | Backend runtime |
| Node.js | 22.12+ (LTS) | Frontend build |
| npm | Bundled with Node.js 22 | Package management |
| Git | 2.40+ | Source control |
| Ollama | 0.1.0+ | Local LLM inference |

> The runtime contract (Node.js 22.12 / Python 3.11) is enforced mechanically
> across CI, the Docker images and `frontend/package.json` engines by
> `scripts/check_runtime_contract.py` — this table agrees with that contract.

### Optional (but Recommended)

| Component | Purpose |
|-----------|---------|
| Docker Desktop | Containerized deployment |
| Redis | CSRF token caching (falls back to memory) |
| VS Code | Development IDE |

---

## Platform-Specific Setup

### Windows 11

#### Step 1: Install Python 3.11+

```powershell
# Download from https://python.org/downloads/windows
# During installation, CHECK "Add Python to PATH"

# Verify installation
python --version
# Expected: Python 3.11.x or higher

# Verify pip
pip --version
```

#### Step 2: Install Node.js

```powershell
# Download from https://nodejs.org/en/download
# Choose the Node.js 22 LTS line (>= 22.12.0)

# Verify installation
node --version
npm --version
```

#### Step 3: Install Git

```powershell
# Download from https://git-scm.com/download/win
# Use Git Bash or Windows Terminal

# Verify installation
git --version
```

#### Step 4: Install Ollama

```powershell
# Download from https://ollama.com/download/windows
# Run installer

# Verify installation
ollama --version

# Pull required models
ollama pull llama3.2
# Harrier TEI embeddings are started by docker-compose; no Ollama embedding pull is required.
```

#### Step 5: Windows-Specific Configuration

```powershell
# Enable long path support (for npm packages)
# Run as Administrator:
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force

# Configure PowerShell execution policy (if needed)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### macOS

#### Step 1: Install Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

#### Step 2: Install Python

```bash
brew install python@3.11

# Verify
python3.11 --version
```

#### Step 3: Install Node.js

```bash
brew install node@22

# Verify
node --version
npm --version
```

#### Step 4: Install Git

```bash
brew install git

# Verify
git --version
```

#### Step 5: Install Ollama

```bash
brew install ollama

# Start Ollama service
brew services start ollama

# Pull required models
ollama pull llama3.2
# Harrier TEI embeddings are started by docker-compose; no Ollama embedding pull is required.
```

### Linux (Ubuntu/Debian)

#### Step 1: System Updates

```bash
sudo apt update && sudo apt upgrade -y
```

#### Step 2: Install Python

```bash
sudo apt install python3.11 python3.11-venv python3-pip -y

# Verify
python3.11 --version
```

#### Step 3: Install Node.js

```bash
# Using NodeSource
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

# Verify
node --version
npm --version
```

#### Step 4: Install Git

```bash
sudo apt install git -y

# Verify
git --version
```

#### Step 5: Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh

# Start Ollama
sudo systemctl start ollama

# Pull required models
ollama pull llama3.2
# Harrier TEI embeddings are started by docker-compose; no Ollama embedding pull is required.
```

---

## Installation Methods

### Method 1: Native Installation (Recommended for Development)

#### Step 1: Clone Repository

```bash
# Clone the repository
git clone https://github.com/zaxbysauce/ragappv3.git

# Navigate to project directory
cd ragappv3

# Verify structure
ls -la
# Should see: backend/, frontend/, README.md, etc.
```

#### Step 2: Backend Setup

```bash
# Navigate to backend
cd backend

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

# Verify activation (should show venv in prompt)
which python

# Install dependencies
pip install -r requirements.txt

# Verify installation
pip list | grep -E "fastapi|uvicorn|sqlite"
```

#### Step 3: Frontend Setup

```bash
# Navigate to frontend (in new terminal)
cd frontend

# Install dependencies
npm install

# Verify installation
npm list | grep -E "react|vite|typescript"
```

#### Step 4: Environment Configuration

```bash
# From project root
# The repo's .env.example is the docker-compose template (root .env).
# For native development, create backend/.env — the backend loads .env from
# its working directory (backend/) — using the Configuration section below.
cp .env.example .env          # docker-compose deployments
# and/or create backend/.env  # native development (see Configuration)
```

#### Step 5: Database Initialization (Optional)

```bash
# Navigate to backend
cd backend

# Activate virtual environment
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

# Initialize the database schema at the path your SQLITE_PATH points to
# (./ragapp.db in the example configuration below)
python -c "from app.models.database import init_db; init_db('./ragapp.db')"

# Verify database created
ls -la *.db
```

> **Note:** this step is optional — starting the backend with
> `uvicorn app.main:app` (next step) runs the same migrations automatically
> on startup via the application lifespan (`app.models.database.run_migrations`,
> which applies the schema plus the startup migrations). The manual command
> exists for headless/first-run provisioning.

#### Step 6: Start Services

**Terminal 1 - Backend:**
```bash
cd backend
source venv/bin/activate  # or venv\Scripts\activate on Windows

# Start backend server
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 9090

# Should see: Application startup complete
```

**Terminal 2 - Frontend:**
```bash
cd frontend

# Start development server
npm run dev

# Should see: VITE v5.x ready in xxx ms
# Local: http://localhost:3000/
```

**Terminal 3 - Ollama (if not running as service):**
```bash
ollama serve
```

### Method 2: Docker Compose (All Platforms)

The repository ships a maintained `docker-compose.yml` at the project root.
It defines the full service set — `harrier-embed` (TEI embedding server),
`reranker` (bge-reranker-v2-m3 via TEI), `redis`, and `knowledgevault`
(the combined backend + frontend image built from the root `Dockerfile`) —
so there is no per-install compose recipe or Dockerfile to author.

#### Step 1: Install Docker

1. Windows 11 / macOS: download Docker Desktop from
   https://www.docker.com/products/docker-desktop (enable the WSL2 backend
   on Windows when prompted)
2. Linux: follow https://docs.docker.com/engine/install/

#### Step 2: Verify the Docker Installation

```bash
docker --version
docker compose version

# Test Docker
docker run hello-world
```

#### Step 3: Configure the Environment

```bash
# From the project root
cp .env.example .env

# .env requires two secrets with no defaults (the backend rejects empty values):
#   ADMIN_SECRET_TOKEN=<random string>
#   JWT_SECRET_KEY=<random string>
# generate each with: python -c "import secrets; print(secrets.token_urlsafe(48))"
```

#### Step 4: Start the Stack

```bash
# From the project root (builds the knowledgevault image on first run)
docker compose up -d --build

# All services healthy? (harrier-embed/reranker have a long first-start
# model download; their healthchecks report starting until it finishes)
docker compose ps
docker compose logs -f knowledgevault

# Stop the stack
docker compose down
```

The `knowledgevault` service wires the model endpoints to the bundled
containers. Excerpt from the repo's `docker-compose.yml` — see the file for
the full set (draft-room, multimodal and retrieval-tuning variables):

```yaml
services:

  # Harrier Embedding Server (replaces flag-embed-server)
  harrier-embed:
    image: ghcr.io/huggingface/text-embeddings-inference:cuda-latest@sha256:1bf10562b37c8b835693084e1bab4d80232188cb258da886eaf1e6eaf6770353
    command: --model-id microsoft/harrier-oss-v1-0.6b --port 8080 --max-batch-tokens 16384 --max-client-batch-size 128
    ports:
      - "8080:8080"
    volumes:
      - harrier-model-cache:/data
    # ... healthcheck + GPU reservation (see docker-compose.yml)

  # Reranker Server (bge-reranker-v2-m3 via TEI)
  reranker:
    image: ghcr.io/huggingface/text-embeddings-inference:cuda-latest@sha256:1bf10562b37c8b835693084e1bab4d80232188cb258da886eaf1e6eaf6770353
    command: --model-id BAAI/bge-reranker-v2-m3 --port 8081 --max-batch-tokens 8192
    ports:
      - "8081:8081"
    volumes:
      - reranker-model-cache:/data
    # ... healthcheck + GPU reservation (see docker-compose.yml)

  # Redis
  redis:
    image: redis:7-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    # ... healthcheck (see docker-compose.yml)

  # KnowledgeVault (backend + frontend, combined image)
  knowledgevault:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "${PORT:-9090}:9090"
    volumes:
      - ${HOST_DATA_DIR:-./data}:/app/data
    environment:
      - OLLAMA_EMBEDDING_URL=${OLLAMA_EMBEDDING_URL:-http://harrier-embed:8080/v1/embeddings}
      - RERANKER_URL=${RERANKER_URL:-http://reranker:8081}
      - REDIS_URL=${REDIS_URL:-redis://redis:6379/0}
      - EMBEDDING_MODEL=${EMBEDDING_MODEL:-microsoft/harrier-oss-v1-0.6b}
      - CHAT_MODEL=${CHAT_MODEL:-gemma-4-26b-a4b-it-apex}
      - INSTANT_CHAT_MODEL=${INSTANT_CHAT_MODEL:-nvidia/nemotron-3-nano-4b}
      # Chat endpoints reach the Docker host's Ollama / LM Studio via
      # host.docker.internal (extra_hosts: host-gateway in the full file):
      # - OLLAMA_CHAT_URL=${OLLAMA_CHAT_URL:-http://host.docker.internal:11434}
      # - INSTANT_CHAT_URL=${INSTANT_CHAT_URL:-http://host.docker.internal:1234}
      # REQUIRED — no default, backend rejects empty values
      - ADMIN_SECRET_TOKEN=${ADMIN_SECRET_TOKEN}
      - JWT_SECRET_KEY=${JWT_SECRET_KEY}
    depends_on:
      redis:
        condition: service_healthy
    # ... healthcheck + further environment defaults (see docker-compose.yml)

volumes:
  harrier-model-cache:
  reranker-model-cache:
  redis_data:
```

#### Step 5: Pull the Chat Models (on the Docker Host)

The bundled `harrier-embed`/`reranker` containers download their models on
first start — no manual pull is needed for embeddings or reranking. Chat
traffic, however, is served by the Ollama instance on your Docker host
(`OLLAMA_CHAT_URL` defaults to `http://host.docker.internal:11434`), so pull
every model you configure as `CHAT_MODEL` / `INSTANT_CHAT_MODEL` on the host:

```bash
# The default CHAT_MODEL used in this guide
ollama pull llama3.2

# If you override CHAT_MODEL / INSTANT_CHAT_MODEL in .env, pull those too,
# for example:
# ollama pull gemma-4-26b-a4b-it-apex
```

The frontend is served by the combined `knowledgevault` image on the port
mapped by `${PORT:-9090}` (open http://localhost:9090). A standalone
frontend image is also maintained at `frontend/Dockerfile` for deployments
that serve the SPA separately.

---

## Configuration

### Backend Environment Variables (.env)

```bash
# Required
JWT_SECRET_KEY=change-me-to-a-random-64-char-string
ADMIN_SECRET_TOKEN=

# Database
SQLITE_PATH=./ragapp.db

# Model services (native dev)
# Start the bundled embedding/reranker containers first — their ports are
# published to localhost:
#   docker compose up -d harrier-embed reranker
OLLAMA_EMBEDDING_URL=http://localhost:8080/v1/embeddings
RERANKER_URL=http://localhost:8081
OLLAMA_CHAT_URL=http://localhost:11434
CHAT_MODEL=llama3.2
EMBEDDING_MODEL=microsoft/harrier-oss-v1-0.6b
# Optional instant-chat model server (e.g. LM Studio) — uncomment and pull
# whichever model you point INSTANT_CHAT_MODEL at:
# INSTANT_CHAT_URL=http://localhost:1234
# INSTANT_CHAT_MODEL=nvidia/nemotron-3-nano-4b

# Embedding Configuration
# Batch size for embedding requests (default: 32)
# Valid range: 1-128. Higher values increase throughput but use more memory.
# Set based on your embedding service capacity. For TEI (Text Embeddings Inference),
# the default of 32 is safe for most deployments.
EMBEDDING_BATCH_SIZE=32

# Optional Features
USERS_ENABLED=true
HYBRID_SEARCH_ENABLED=true
RERANKING_ENABLED=true

# Security
CSRF_TOKEN_TTL=900
MAX_LOGIN_ATTEMPTS=5
LOCKOUT_DURATION_MINUTES=15

# Optional: Redis (falls back to memory if not set)
# REDIS_URL=redis://localhost:6379/0

# Optional: Email
# SMTP_HOST=smtp.gmail.com
# SMTP_PORT=587
# SMTP_USER=your-email@gmail.com
# SMTP_PASSWORD=your-app-password
```

### Frontend Environment Variables

Create `frontend/.env`:

```bash
# Base path (VITE_API_URL is derived when empty — defaults to /api)
VITE_API_URL=
VITE_APP_BASENAME=/

# For a subpath deployment behind a prefix-stripping proxy:
# VITE_APP_BASENAME=/knowledgevault
# VITE_API_URL is derived from VITE_APP_BASENAME when empty (e.g. /knowledgevault/api).
# Set VITE_API_URL explicitly only for custom API gateways.

# Feature Flags
VITE_USERS_ENABLED=true
```

---

## Verification

### Backend Health Check

```bash
# Test backend is running
curl http://localhost:9090/api/health

# Expected response:
# {"status": "healthy", "version": "3.x.x"}
```

### Frontend Check

Open browser to: http://localhost:3000

You should see the RAGAPPv3 login page.

### API Documentation

Open browser to: http://localhost:9090/docs

You should see the Swagger UI with all API endpoints.

### Test Registration

```bash
# Register first user (becomes superadmin)
curl -X POST http://localhost:9090/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "SecurePass123!", "full_name": "Administrator"}'

# Expected: Response with access_token and user data
```

### Test LLM Connection

```bash
# Test Ollama connection
curl http://localhost:11434/api/tags

# Should list available models including llama3.2
```

---

## Troubleshooting

### Common Issues

#### Issue: "Module not found" errors

**Solution:**
```bash
# Backend
cd backend
pip install -r requirements.txt

# Frontend
cd frontend
npm install
```

#### Issue: "Permission denied" on Windows

**Solution:**
```powershell
# Run PowerShell as Administrator
# Or use:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

#### Issue: "Address already in use" (port 9090 or 3000)

**Solution:**
```bash
# Find process using port
# Windows:
netstat -ano | findstr :9090
taskkill /PID <PID> /F

# macOS/Linux:
lsof -ti:9090 | xargs kill -9
```

#### Issue: Database locked (SQLite)

**Solution:**
```bash
# Check for hanging Python processes
# Windows:
taskkill /F /IM python.exe

# macOS/Linux:
pkill -f python

# Delete lock file if exists
rm -f *.db-journal
```

#### Issue: Ollama connection refused

**Solution:**
```bash
# Check Ollama is running
curl http://localhost:11434/api/tags

# Start Ollama if not running
ollama serve

# Or restart service
# Windows: Restart Ollama from system tray
# macOS: brew services restart ollama
# Linux: sudo systemctl restart ollama
```

#### Issue: Frontend can't connect to backend

**Solution:**
```bash
# Check backend is running
curl http://localhost:9090/api/health

# Check CORS settings in backend
# Verify VITE_API_URL in frontend/.env points at the backend API base:
# - Same-origin/proxied frontend: VITE_API_URL=/api
# - Separate dev backend: VITE_API_URL=http://localhost:9090/api

# Restart both services
```

#### Issue: CSRF token errors

**Solution:**
```bash
# Clear browser cookies for localhost
# Or use incognito/private window

# Check CSRF_TOKEN_TTL in backend .env
```

### Getting Help

1. Check logs:
   - Backend: Terminal running uvicorn
   - Frontend: Browser console (F12)
   - Docker: `docker compose logs`

2. Enable debug mode:
   ```bash
   # Backend .env
   DEBUG=true
   ```

3. Check GitHub Issues:
   https://github.com/zaxbysauce/ragappv3/issues

---

## LLM Installation Quick Reference

For automated deployment systems, here is the minimal command sequence:

```bash
#!/bin/bash
# LLM Installation Script for RAGAPPv3

set -e

# 1. Prerequisites
git clone https://github.com/zaxbysauce/ragappv3.git
cd ragappv3

# 2. Backend
python3.11 -m venv backend/venv
source backend/venv/bin/activate
pip install -r backend/requirements.txt

# 3. Frontend
cd frontend && npm install && cd ..

# 4. Configuration
cp backend/.env.example backend/.env
# (Edit backend/.env with secure keys)

# 5. Database (optional — uvicorn auto-initializes via the app lifespan)
cd backend
python -c "from app.models.database import init_db; init_db('./ragapp.db')"
cd ..

# 6. Start Services
# Terminal 1: ollama serve
# Terminal 2: cd backend && source venv/bin/activate && uvicorn app.main:app --host 0.0.0.0 --port 9090
# Terminal 3: cd frontend && npm run dev

echo "RAGAPPv3 installation complete"
echo "Backend: http://localhost:9090"
echo "Frontend: http://localhost:3000"
echo "API Docs: http://localhost:9090/docs"
```

---

## Security Checklist

Before production deployment:

- [ ] Change `JWT_SECRET_KEY` to a secure random string (64+ chars)
- [ ] Change `ADMIN_SECRET_TOKEN` to a secure random string
- [ ] Set `DEBUG=false` in production
- [ ] Enable HTTPS (use reverse proxy like nginx)
- [ ] Configure CORS for your domain only
- [ ] Set up proper backup strategy for SQLite database
- [ ] Configure log rotation
- [ ] Set up monitoring and alerting

---

## Next Steps

After installation:

1. Register your first user (becomes superadmin)
2. Create an organization
3. Create vaults for document storage
4. Upload documents
5. Start chatting with your knowledge base

See [README.md](README.md) for usage instructions.

---

**Version:** 3.0.0  
**Last Updated:** 2026-03-31  
**Support:** https://github.com/zaxbysauce/ragappv3/issues
