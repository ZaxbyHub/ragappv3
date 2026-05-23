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
| Node.js | 18+ | Frontend build |
| npm | 9+ | Package management |
| Git | 2.40+ | Source control |
| Ollama | 0.1.0+ | Local LLM inference |

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
# Choose LTS version (18.x or 20.x)

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
brew install node@18

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
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
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
cp backend/.env.example backend/.env

# Edit backend/.env with your settings
# See Configuration section below
```

#### Step 5: Database Initialization

```bash
# Navigate to backend
cd backend

# Activate virtual environment
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate

# Initialize database
python -c "from app.models.database import init_db; init_db()"

# Verify database created
ls -la *.db
```

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

### Method 2: Docker Desktop (Windows 11)

#### Step 1: Install Docker Desktop

1. Download from https://www.docker.com/products/docker-desktop
2. Run installer
3. Enable WSL2 backend when prompted
4. Restart computer

#### Step 2: Verify Docker Installation

```powershell
# In PowerShell
docker --version
docker-compose --version

# Test Docker
docker run hello-world
```

#### Step 3: Create Docker Configuration

Create `docker-compose.yml` in project root:

```yaml
version: '3.8'

services:
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    ports:
      - "9090:9090"
    environment:
      - SQLITE_PATH=/app/data/ragapp.db
      - JWT_SECRET_KEY=${JWT_SECRET_KEY}
      - ADMIN_SECRET_TOKEN=${ADMIN_SECRET_TOKEN}
      - APP_ROOT_PATH=${APP_ROOT_PATH:-}
      - FORWARDED_ALLOW_IPS=${FORWARDED_ALLOW_IPS:-127.0.0.1}
      - OLLAMA_EMBEDDING_URL=http://harrier-embed:8080/v1/embeddings
      - OLLAMA_CHAT_URL=http://ollama:11434
      - EMBEDDING_MODEL=microsoft/harrier-oss-v1-0.6b
      - CHAT_MODEL=gemma-4-26b-a4b-it-apex
    volumes:
      - ./data:/app/data
      - ./uploads:/app/uploads
    depends_on:
      - ollama
    networks:
      - ragapp-network

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
      args:
        VITE_APP_BASENAME: ${VITE_APP_BASENAME:-/}
        VITE_API_URL: ${VITE_API_URL:-/api}
    ports:
      - "3000:3000"
    depends_on:
      - backend
    networks:
      - ragapp-network

  ollama:
    image: ollama/ollama:latest
    ports:
      - "11434:11434"
    volumes:
      - ollama-data:/root/.ollama
    networks:
      - ragapp-network

volumes:
  ollama-data:

networks:
  ragapp-network:
    driver: bridge
```

#### Step 4: Create Backend Dockerfile

Create `backend/Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory
RUN mkdir -p /app/data /app/uploads

# Expose port
EXPOSE 9090

# Start command
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 9090 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
```

#### Step 5: Create Frontend Dockerfile

Create `frontend/Dockerfile`:

```dockerfile
FROM node:18-alpine

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy application code
COPY . .

ARG VITE_APP_BASENAME=/
ARG VITE_API_URL=/api
ENV VITE_APP_BASENAME=${VITE_APP_BASENAME}
ENV VITE_API_URL=${VITE_API_URL}

# Build application
RUN npm run build

# Expose port
EXPOSE 3000

# Start command
CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "3000"]
```

#### Step 6: Build and Run

```powershell
# Navigate to project root
cd ragappv3

# Create environment file
@"
JWT_SECRET_KEY=your-super-secret-jwt-key-change-this-in-production
ADMIN_SECRET_TOKEN=your-admin-token-change-this
"@ | Out-File -FilePath .env -Encoding utf8

# Build and start services
docker-compose up --build

# Or run in background
docker-compose up --build -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

#### Step 7: Pull Ollama Models (in Docker)

```powershell
# Wait for Ollama container to be ready
docker-compose ps

# Pull models
docker-compose exec ollama ollama pull llama3.2
# Harrier TEI embeddings are started by docker-compose; no Ollama embedding pull is required.
```

---

## Configuration

### Backend Environment Variables (.env)

```bash
# Required
JWT_SECRET_KEY=change-me-to-a-random-64-char-string
ADMIN_SECRET_TOKEN=

# Database
SQLITE_PATH=./ragapp.db

# Model services
OLLAMA_EMBEDDING_URL=http://harrier-embed:8080/v1/embeddings
OLLAMA_CHAT_URL=http://localhost:11434
INSTANT_CHAT_URL=http://localhost:1234
CHAT_MODEL=gemma-4-26b-a4b-it-apex
EMBEDDING_MODEL=microsoft/harrier-oss-v1-0.6b
INSTANT_CHAT_MODEL=nvidia/nemotron-3-nano-4b

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
# API URL
VITE_API_URL=/api
VITE_APP_BASENAME=/

# For a subpath deployment behind a prefix-stripping proxy:
# VITE_API_URL=/knowledgevault/api
# VITE_APP_BASENAME=/knowledgevault

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
# Verify VITE_API_URL in frontend/.env

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
   - Docker: `docker-compose logs`

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

# 5. Database
cd backend
python -c "from app.models.database import init_db; init_db()"
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
