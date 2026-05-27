# KnowledgeVault Admin Guide

Administrative tasks for maintaining KnowledgeVault.

---

## Table of Contents

1. [Backups](#backups)
2. [Data Locations](#data-locations)
3. [Updates](#updates)
4. [Health Checks](#health-checks)
5. [Logs](#logs)
6. [Performance Tuning](#performance-tuning)
7. [Security](#security)
8. [Troubleshooting](#troubleshooting)

---

## Backups

### What to Back Up

KnowledgeVault stores data in the following locations:

| Component | Location | Backup Priority |
|-----------|----------|-----------------|
| SQLite Database | `{DATA_DIR}/app.db` | Critical |
| Vector Database | `{DATA_DIR}/lancedb/` | Critical |
| Documents | `{DATA_DIR}/documents/` | High |
| Configuration | `.env` file | High |
| Logs | `{DATA_DIR}/logs/` | Low |

### Automated Backup Script

Create a backup script for regular backups:

**backup.sh (Linux/Mac):**
```bash
#!/bin/bash

# Configuration
BACKUP_DIR="/backups/knowledgevault"
DATA_DIR="/data/knowledgevault"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="knowledgevault_backup_${DATE}"

# Create backup directory
mkdir -p "${BACKUP_DIR}/${BACKUP_NAME}"

# Stop containers to ensure consistency
docker compose down

# Copy data (run from project root where .env is located)
cp -r "${DATA_DIR}" "${BACKUP_DIR}/${BACKUP_NAME}/"
cp ./.env "${BACKUP_DIR}/${BACKUP_NAME}/"

# Create archive
cd "${BACKUP_DIR}"
tar -czf "${BACKUP_NAME}.tar.gz" "${BACKUP_NAME}"
rm -rf "${BACKUP_NAME}"

# Restart containers
docker compose up -d

# Keep only last 7 backups
ls -t ${BACKUP_DIR}/*.tar.gz | tail -n +8 | xargs rm -f

echo "Backup complete: ${BACKUP_NAME}.tar.gz"
```

**backup.ps1 (Windows):**
```powershell
# Configuration
$BackupDir = "C:\Backups\KnowledgeVault"
$DataDir = "C:\KnowledgeVault\data"
$Date = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupName = "knowledgevault_backup_$Date"

# Create backup directory
New-Item -ItemType Directory -Force -Path "$BackupDir\$BackupName"

# Stop containers
docker compose down

# Copy data (run from project root where .env is located)
Copy-Item -Recurse -Path $DataDir -Destination "$BackupDir\$BackupName\data"
Copy-Item -Path ".\env" -Destination "$BackupDir\$BackupName\"

# Create archive
Compress-Archive -Path "$BackupDir\$BackupName" -DestinationPath "$BackupDir\$BackupName.zip"
Remove-Item -Recurse -Path "$BackupDir\$BackupName"

# Restart containers
docker compose up -d

# Keep only last 7 backups
Get-ChildItem -Path $BackupDir -Filter "*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 7 | Remove-Item

Write-Host "Backup complete: $BackupName.zip"
```

### Schedule Backups

**Linux (cron):**
```bash
# Edit crontab
crontab -e

# Add daily backup at 2 AM
0 2 * * * /path/to/backup.sh
```

**Windows (Task Scheduler):**
1. Open Task Scheduler
2. Create Basic Task
3. Set trigger to Daily
4. Set action to Start a Program
5. Program: `powershell.exe`
6. Arguments: `-File "C:\path\to\backup.ps1"`

### Restore from Backup

1. Stop KnowledgeVault:
   ```bash
   docker compose down
   ```

2. Extract backup:
   ```bash
   # Linux/Mac
   tar -xzf knowledgevault_backup_20240101_120000.tar.gz
   
   # Windows
   Expand-Archive -Path "knowledgevault_backup_20240101_120000.zip"
   ```

3. Restore data (from project root):
   ```bash
   cp -r knowledgevault_backup_*/data/* /data/knowledgevault/
   cp knowledgevault_backup_*/.env ./
   ```

4. Start KnowledgeVault:
   ```bash
   docker compose up -d
   ```

---

## Data Locations

### Default Directory Structure

```
/data/knowledgevault/
├── uploads/                  # [LEGACY] Flat uploads directory (deprecated, auto-migrated)
├── vaults/                   # Vault-specific directories
│   ├── 1/                    # Vault 1 (default/orphan vault)
│   │   └── uploads/          # Uploads for vault 1
│   ├── 2/                    # Vault 2
│   │   └── uploads/          # Uploads for vault 2
│   └── ...                   # Additional vaults
├── documents/                # Legacy documents directory (kept for compatibility)
├── library/                  # Library files
├── processing/               # Temporary processing
├── lancedb/                  # Vector database
│   └── chunks.lance/
│       ├── data/
│       └── _transactions/
├── app.db                    # SQLite database
└── logs/
    └── knowledgevault.log
```

**Note:** The system now stores uploads in vault-specific directories (`/data/knowledgevault/vaults/{vault_id}/uploads/`). On first startup, the system automatically migrates files from the legacy flat `uploads/` directory to the appropriate vault-specific directories. Files are renamed with `.migrated` suffix to create a safe backup. If a file cannot be associated with a specific vault, it defaults to the orphan vault (vault 1).

### Changing Data Location

1. Stop KnowledgeVault:
   ```bash
   docker compose down
   ```

2. Move existing data (optional):
   ```bash
   mv /old/data/path /new/data/path
   ```

3. Update `.env`:
   ```bash
   HOST_DATA_DIR=/new/data/path
   ```

4. Start KnowledgeVault:
   ```bash
   docker compose up -d
   ```

### Disk Space Monitoring

Monitor disk usage:

```bash
# Check overall usage
df -h

# Check KnowledgeVault data usage
du -sh /data/knowledgevault/*

# Find largest files
find /data/knowledgevault -type f -exec ls -lh {} \; | sort -k5 -hr | head -20
```

**Recommended minimums:**
- Documents: 5GB+ (depends on your files)
- Vector DB: 2GB+ (scales with document count)
- SQLite: 500MB
- Logs: 1GB

---

## Updates

### Updating KnowledgeVault

1. Backup current data (see Backups section)

2. Pull latest code:
   ```bash
   git pull
   ```

3. Rebuild containers:
   ```bash
   docker compose down
   docker compose build --no-cache
   docker compose up -d
   ```

4. Verify health:
   ```bash
   curl http://localhost:9090/health
   ```

### Updating Ollama Models

List available updates:
```bash
ollama list
```

Update a chat model:
```bash
ollama pull qwen2.5:32b
```

Note: The embedding service (Harrier TEI) is managed by docker-compose and updates via `docker compose pull`.


Remove old model versions:
```bash
# List all models
ollama list

# Remove specific model
ollama rm old-model:tag
```

### Docker Image Updates

Update base images:
```bash
docker compose pull
docker compose up -d
```

Clean up old images:
```bash
docker image prune -a
```

---

## Health Checks

### Built-in Health Endpoint

Check service health:
```bash
curl http://localhost:9090/health
```

Expected response:
```json
{
  "status": "ok",
  "version": "1.0.0",
  "components": {
    "database": "ok",
    "vector_store": "ok",
    "llm": "ok"
  }
}
```

### Component Health Checks

**Database:**
```bash
# Check SQLite
docker compose exec knowledgevault sqlite3 /data/knowledgevault/app.db ".tables"
```

**Vector Store:**
```bash
# Check LanceDB
docker compose exec knowledgevault python -c "import lancedb; db = lancedb.connect('/data/knowledgevault/lancedb'); print(db.table_names())"
```

**LLM Connection:**
```bash
# Check Ollama
curl http://localhost:11434/api/tags
```

### Automated Health Monitoring

**health_check.sh:**
```bash
#!/bin/bash

HEALTH_URL="http://localhost:9090/health"
WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

response=$(curl -s -o /dev/null -w "%{http_code}" $HEALTH_URL)

if [ $response -ne 200 ]; then
    message="KnowledgeVault health check failed (HTTP $response)"
    curl -X POST -H 'Content-type: application/json' \
        --data "{\"text\":\"$message\"}" \
        $WEBHOOK_URL
fi
```

---

## Logs

### Viewing Logs

**Docker Compose:**
```bash
# View all logs
docker compose logs

# Follow logs in real-time
docker compose logs -f

# View last 100 lines
docker compose logs --tail 100

# View logs since specific time
docker compose logs --since 10m
```

**Docker:**
```bash
# View specific container
docker logs knowledgevault

# Follow logs
docker logs -f knowledgevault
```

### Log Files

Application logs are stored in:
```
/data/knowledgevault/logs/knowledgevault.log
```

View from host:
```bash
docker compose exec knowledgevault tail -f /data/knowledgevault/logs/knowledgevault.log
```

### Log Levels

Configure in `.env`:
```bash
LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL
```

### Log Rotation

Docker automatically rotates logs. Configure in `docker-compose.yml`:
```yaml
services:
  knowledgevault:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### Common Log Messages

| Message | Meaning | Action |
|---------|---------|--------|
| `Processing file: ...` | Document being processed | Normal |
| `Embedding generation failed` | Ollama embedding error | Check Ollama |
| `LLM unavailable` | Cannot connect to chat model | Check Ollama |
| `Vector search returned N results` | Search completed | Normal |
| `Memory saved: ...` | Memory stored successfully | Normal |

---

## Performance Tuning

### System Requirements

| Usage Level | RAM | CPU | Disk | GPU |
|-------------|-----|-----|------|-----|
| Light (<1000 docs) | 8GB | 4 cores | 50GB | Optional |
| Medium (1000-5000) | 16GB | 8 cores | 100GB | Recommended |
| Heavy (>5000 docs) | 32GB+ | 16 cores | 500GB+ | Strongly recommended |

### Optimization Settings

**In `.env`:**

```bash
# Document chunking (affects memory and vector store size)
CHUNK_SIZE_CHARS=1000      # Smaller chunks = more embeddings, better granularity
CHUNK_OVERLAP_CHARS=100    # Faster processing (less accurate)
CHUNK_OVERLAP_CHARS=400    # Slower processing (more accurate)
MULTI_SCALE_INDEXING_ENABLED=true
MULTI_SCALE_CHUNK_SIZES=768,1536  # Fewer default scales for faster indexing

# Existing deployments can keep MULTI_SCALE_CHUNK_SIZES=512,1024,2048
# if they want the previous three-scale indexing footprint.

# Embedding batch processing (critical for performance)
# Default: 32 (safe for most TEI deployments)
# Valid range: 1-128
# Increase for higher throughput if your embedding service has capacity
# Decrease if you see "batch size exceeds maximum" errors
EMBEDDING_BATCH_SIZE=32

# Retrieval tuning
RETRIEVAL_TOP_K=5          # Fewer results = faster retrieval
MAX_DISTANCE_THRESHOLD=0.3 # Improve response quality
```

#### Embedding Batch Size Tuning

The `EMBEDDING_BATCH_SIZE` setting controls how many document chunks are sent to the embedding service in a single request:

| Setting | Best For | Notes |
|---------|----------|-------|
| 1-16 | Memory-constrained environments | Slowest throughput, minimal memory impact |
| 32 (default) | Most production deployments | Good balance of speed and stability |
| 64-128 | High-capacity embedding services | Faster throughput if your service allows it |

**Important:** TEI (Text Embeddings Inference) and similar services have hard limits on batch sizes:
- Exceeding the limit will cause `422 Validation: batch size X > maximum allowed batch size Y` errors
- Default TEI limit is 32 sequences per request
- When using remote embedding services, verify their batch size limit before increasing this setting

#### Spreadsheet Handling

Wide spreadsheets (100+ columns) are automatically split into column groups to ensure chunks stay within the embedding model's input limit (8192 characters):

- **No data loss:** All columns are preserved; only extremely wide cell values (>8192 chars) are truncated
- **Pre-embedding validation:** Check logs for warnings about oversized chunks
- **Column-group metadata:** Each chunk is tagged with `col_group` index for identification

If you process many wide spreadsheets, monitor logs for chunk size warnings and consider adjusting `CHUNK_SIZE_CHARS` if needed.

### Database Optimization

**SQLite:**
```bash
# Optimize database
docker compose exec knowledgevault sqlite3 /data/knowledgevault/app.db "VACUUM;"

# Analyze for query optimization
docker compose exec knowledgevault sqlite3 /data/knowledgevault/app.db "ANALYZE;"
```

**LanceDB:**
```bash
# Compact vector database
docker compose exec knowledgevault python -c "
import lancedb
db = lancedb.connect('/data/knowledgevault/lancedb')
table = db.open_table('chunks')
table.compact_files()
"
```

### Monitoring Performance

**Resource Usage:**
```bash
# Container stats
docker stats knowledgevault

# System resources
htop  # Linux/Mac
Task Manager  # Windows
```

**Response Times:**
```bash
# Time API response
time curl http://localhost:9090/health

# Load test
ab -n 100 -c 10 http://localhost:9090/health
```

---

## Security

### Authentication

KnowledgeVault has built-in JWT-based authentication with role-based access control:

**User Roles:**
- `superadmin` — Full system access, manages other admins
- `admin` — Full system access, can manage members and viewers
- `member` — Can create and update documents
- `viewer` — Read-only access

**Setup:**
- When `USERS_ENABLED=True`, set `ADMIN_SECRET_TOKEN` in `.env` to create the first admin user
- The initial admin can then invite other users via email or create accounts manually
- JWT tokens are stored in httpOnly refresh cookies for security

**Options:**
1. **Single-Admin Mode** (`USERS_ENABLED=false`): The `ADMIN_SECRET_TOKEN` is the sole authentication mechanism — whoever possesses the token is the admin.

2. **Multi-User Mode** (`USERS_ENABLED=true`): Requires `ADMIN_SECRET_TOKEN` to be set for the initial admin account, then allows user management via the UI.

### Network Security

Deploy KnowledgeVault behind a reverse proxy for additional security layers:

**Option 1: Localhost Only (Safest)**
- Keep default configuration
- Access only from the same machine

**Option 2: Reverse Proxy with TLS**
```nginx
# nginx.conf
server {
    listen 443 ssl http2;
    server_name knowledgevault.example.com;
    
    # TLS configuration
    ssl_certificate /etc/ssl/certs/knowledgevault.crt;
    ssl_certificate_key /etc/ssl/private/knowledgevault.key;
    
    # Optional: additional auth layer (beyond app-level JWT)
    auth_basic "KnowledgeVault";
    auth_basic_user_file /etc/nginx/.htpasswd;
    
    location / {
        proxy_pass http://localhost:9090;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

#### Subpath Deployment

For a subpath deployment such as `https://example.com/knowledgevault/`, configure these variables and rebuild the Docker image:

| Variable | Type | Purpose |
|----------|------|---------|
| `APP_ROOT_PATH` | Runtime env | Cookie paths and OpenAPI docs (backend) |
| `VITE_APP_BASENAME` | Build arg | Frontend base path and asset URLs |
| `VITE_API_URL` | Build arg (optional) | API URL override. Derived from `VITE_APP_BASENAME` when empty |
| `BACKEND_CORS_ORIGINS` | Runtime env | Allowed CORS origins for your domain |
| `FORWARDED_ALLOW_IPS` | Runtime env | Trusted proxy IPs for forwarded headers |

**Minimal configuration** (`.env` or environment):

```env
APP_ROOT_PATH=/knowledgevault
VITE_APP_BASENAME=/knowledgevault
BACKEND_CORS_ORIGINS=https://example.com
FORWARDED_ALLOW_IPS=*
```

`VITE_API_URL` is automatically derived as `/knowledgevault/api` when left empty. To set it explicitly (e.g., for a custom API gateway), add it to your config:

```env
VITE_API_URL=/knowledgevault/api
```

**Rebuild and restart** after changing build args:

```bash
docker compose build --no-cache
docker compose up -d
```

> **Important:** `VITE_APP_BASENAME` and `VITE_API_URL` are baked into the JavaScript bundle at Docker build time. Changing them at runtime has no effect — you must rebuild the image.

##### Changing the Prefix

To change from `/knowledgevault` to `/meridian` (or any path, including multi-segment like `/apps/meridian`):

1. Update `.env`:
   ```env
   APP_ROOT_PATH=/meridian
   VITE_APP_BASENAME=/meridian
   ```
2. Update your reverse proxy config to strip `/meridian` instead of `/knowledgevault`.
3. Rebuild: `docker compose build --no-cache`
4. Restart: `docker compose up -d`

##### Reverse Proxy Configuration

The reverse proxy **must strip the prefix** before forwarding to the container. The backend receives bare paths (`/api`, `/assets`, `/health`).

**NGINX:**

```nginx
location = /knowledgevault {
    return 301 /knowledgevault/;
}

location /knowledgevault/ {
    proxy_pass http://knowledgevault:9090/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Prefix /knowledgevault;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600;
}
```

**Caddy:**

```caddyfile
handle_path /knowledgevault/* {
    reverse_proxy knowledgevault:9090
}
```

> `handle_path` strips the prefix automatically. No additional rewrite configuration is needed.

##### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Failed to load module script: MIME type "text/html"` | Docker image built without `VITE_APP_BASENAME` | Rebuild with `--build-arg VITE_APP_BASENAME=/yourprefix` |
| 404 with "Proxy misconfiguration" message | Reverse proxy not stripping prefix | Add trailing `/` to `proxy_pass` (nginx) or use `handle_path` (Caddy) |
| SSE/streaming appears in bursts | nginx buffering enabled | Add `proxy_buffering off;` to nginx location block |
| Login/auth failures after prefix change | `APP_ROOT_PATH` doesn't match `VITE_APP_BASENAME` | Ensure both use the same prefix value |
| Blank page, no console errors | `VITE_APP_BASENAME` set but image not rebuilt | Run `docker compose build --no-cache` |

##### Deployment Compatibility Matrix

| Frontend Build | Backend Config | Proxy | Result |
|---------------|---------------|-------|--------|
| Root (default) | Root | None | Works |
| Prefixed | Prefixed (matching) | Stripping | Works |
| Root | Prefixed | Stripping | Assets 404 — rebuild frontend |
| Prefixed | Root | Stripping | Cookie/auth failures |
| Prefixed | Prefixed | Non-stripping | MIME errors, 404 diagnostic |

##### Multi-Instance Deployments

Multiple KnowledgeVault instances can share a domain using distinct prefixes (e.g., `/team-a` and `/team-b`). Cookies are scoped to each prefix path, preventing unintentional cross-instance interference. However, path-scoped cookies are not a strong security boundary — for actual tenant isolation, use separate domains.

**Option 3: VPN/Private Network**
- Deploy behind corporate VPN
- Use private subnet access controls

**Reverse Proxy Purpose:**
- TLS termination (HTTPS)
- Optional additional authentication layer (e.g., Basic Auth for extranet access)
- Request rate limiting
- DDoS protection

### File Upload Security

Upload restrictions are configured in the application. Monitor for:
- Large file uploads (>100MB)
- Executable file uploads
- Path traversal attempts

Binary formats (`.pdf`, `.docx`, `.xlsx`, `.xls`) are validated against their magic byte signatures at upload time. A file with a mismatched extension (e.g. a renamed executable with a `.pdf` extension) is rejected with HTTP 400 before being written to disk.

### Data Encryption

**At Rest:**
- Encrypt data directory at OS level
- Use LUKS (Linux), BitLocker (Windows), or FileVault (Mac)

**In Transit:**
- Use HTTPS with reverse proxy
- Example with Caddy:
```
knowledgevault.example.com {
    reverse_proxy localhost:9090
}
```

### Regular Security Tasks

- [ ] Review access logs monthly
- [ ] Update Docker images quarterly
- [ ] Rotate backup encryption keys annually
- [ ] Rotate `ADMIN_SECRET_TOKEN` and `JWT_SECRET_KEY` annually
- [ ] Audit user roles (superadmin/admin/member/viewer) quarterly
- [ ] Rotate JWT secret key when team members with admin access leave

---

## Troubleshooting

### Container Won't Start

**Check logs:**
```bash
docker compose logs --tail 50
```

**Common causes:**
1. Port conflict - Change PORT in .env
2. Permission denied - Fix data directory permissions
3. Out of disk space - Clean up old files

### Database Corruption

**Symptoms:** SQLite errors, missing data

**Recovery:**
1. Stop KnowledgeVault
2. Backup corrupted database
3. Attempt recovery:
   ```bash
   sqlite3 knowledgevault.db ".recover" | sqlite3 knowledgevault_recovered.db
   ```
4. Replace database:
   ```bash
   mv knowledgevault_recovered.db knowledgevault.db
   ```
5. Start KnowledgeVault

### Vector Search Not Working

**Check LanceDB:**
```bash
docker compose exec knowledgevault python -c "
import lancedb
db = lancedb.connect('/data/knowledgevault/lancedb')
print('Tables:', db.table_names())
table = db.open_table('chunks')
print('Rows:', len(table))
"
```

**Rebuild if corrupted:**
1. Stop KnowledgeVault
2. Backup and remove lancedb directory
3. Restart - documents will be re-indexed

### Ollama Connection Issues

**Test connection:**
```bash
curl http://localhost:11434/api/tags
```

**Docker network issues (Linux):**
```bash
# Use host IP instead of host.docker.internal
OLLAMA_CHAT_URL=http://192.168.1.100:11434
```

### Performance Degradation

**Check for:**
- Large log files (rotate logs)
- Fragmented database (run VACUUM)
- Memory leaks (restart container)
- Too many documents (increase RAM or reduce RETRIEVAL_TOP_K)

### Reset to Clean State

**WARNING: This deletes all data!**

```bash
# Stop and remove containers
docker compose down

# Remove all data
rm -rf /data/knowledgevault/*

# Start fresh
docker compose up -d
```

---

## Quick Reference

### Essential Commands

```bash
# Start
docker compose up -d

# Stop
docker compose down

# Restart
docker compose restart

# View logs
docker compose logs -f

# Check health
curl http://localhost:9090/health

# Backup
tar -czf backup.tar.gz /data/knowledgevault .env

# Update
docker compose pull && docker compose up -d
```

### File Locations

| File | Path |
|------|------|
| Config | `./.env` (at project root) |
| Database | `{DATA_DIR}/app.db` |
| Vectors | `{DATA_DIR}/lancedb/` |
| Documents | `{DATA_DIR}/documents/` |
| Logs | `{DATA_DIR}/logs/app.log` |

### Support Resources

- Main README: `README.md`
- Setup Guide: `docs/non-technical-setup.md`
- API Docs: `http://localhost:9090/docs`
