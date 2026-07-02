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
7. [Evaluation](#evaluation)
8. [Security](#security)
9. [Troubleshooting](#troubleshooting)

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

#### Shared Embedding Cache (Redis L2)

KnowledgeVault uses a two-tier embedding cache to reduce redundant embedding requests across the cluster:

- **L1 — In-process LRU cache:** Fastest, per-worker in-memory cache. Evicted under memory pressure.
- **L2 — Shared Redis cache:** Cluster-wide cache backed by Redis. Reduces duplicate embedding work when multiple workers handle the same query.

Redis is **already used** for query transforms and CSRF token storage — enabling the shared embedding cache introduces no new dependencies. If Redis is unavailable, the system falls back to the L1 in-process cache only. No data is lost; cache hit rate may be lower until Redis is restored.

To control how long cached embeddings are stored:

```bash
EMBEDDING_CACHE_TTL_SECONDS=604800   # 7 days (default)
```

Lower values reduce Redis memory usage but increase duplicate embedding requests. Higher values improve cache hit rates for repeated queries but consume more Redis memory.

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

## Evaluation

### Live Retrieval Benchmark

The `/api/eval/live` endpoint runs a retrieval-quality benchmark against the **live** RAG pipeline, computing MRR, nDCG@k, and recall@k from ground-truth query results. This bridges the offline eval harness with production retrieval quality measurement.

**Access:** Admin-only (`require_admin_role`).
**Feature flag:** Requires `EVAL_ENABLED=true` in `.env` (defaults to `false`).

When disabled, the endpoint returns HTTP 501:
```
Set EVAL_ENABLED=true to enable.
```

**Run records** (timestamp, release ID, per-query and aggregate metrics) are persisted as JSONL to `data/eval-runs/runs.jsonl` for trend comparison across releases.

**Metrics computed:**

| Metric | Description |
|--------|-------------|
| MRR (Mean Reciprocal Rank) | Average reciprocal rank of the first relevant result |
| nDCG@k | Normalized Discounted Cumulative Gain at k |
| recall@k | Fraction of relevant docs retrieved in top-k results |

Metrics are computed in `backend/app/services/eval_metrics.py`.

---

## Advanced Retrieval

### Feedback-Driven Re-Ranking (FR-010)

User feedback on chat messages (thumbs up/down via `PATCH /api/chat/sessions/{id}/messages/{message_id}/feedback`) is used to adjust retrieval rankings for that session.

**Behavior:**
- Feedback is stored as `"up"` or `"down"` on the message record, scoped to the current user's signal on sessions they own
- A positive vote (`"up"`) boosts the ranking score of cited documents by **+0.10** for that session
- A negative vote (`"down"`) penalizes the ranking score by **−0.10** for that session
- Re-ranking is applied per-session and does not affect other users or persist across sessions
- Admins and superadmins with write access to a session can moderate feedback on any message within it

**Effect:** Over multiple turns, sessions with consistent feedback gradually surface more relevant documents and deprioritize less useful ones.

### Agentic RAG (FR-008)

Agentic RAG replaces the standard single-step retrieval with a multi-step iterative loop using a tool registry and an LLM-driven planner.

**Feature flag — off by default:**
```env
AGENTIC_RAG_ENABLED=true
```

**How it works:**
1. The planner receives the user query and decides which tools to call (e.g., `search`, `memory_lookup`, `document_retrieve`)
2. Each tool call returns intermediate results
3. The planner evaluates results and decides whether to call additional tools or proceed to answer distillation
4. Iteration continues until the planner signals done or the maximum step count is reached

**Tools available to the planner:**
- Vector search (semantic similarity)
- Memory retrieval
- Document lookup by ID or vault
- (Extensible via the tool registry)

**Enabling:**
```bash
# In .env
AGENTIC_RAG_ENABLED=true
```

Restart the container after changing the flag. When disabled (default), the standard single-step retrieval pipeline is used.

### Image Ingestion (FR-009)

Images embedded within supported document formats (e.g., PDF pages) are processed using OCR when optional dependencies are installed.

**Requirements:**
```bash
# Install optional OCR dependencies
pip install Pillow pytesseract

# On Linux, also install Tesseract OCR:
# sudo apt-get install tesseract-ocr

# On macOS:
# brew install tesseract

# On Windows:
# Download and install from https://github.com/UB-Mannheim/tesseract/wiki
```

**Behavior:**
- When Pillow and pytesseract are importable, images within documents are extracted and passed through Tesseract OCR
- The resulting text is chunked and embedded like any other document content
- If the dependencies are not installed, image ingestion is silently skipped (no error; documents without images are processed normally)
- Re-upload existing documents after installing the dependencies to index any previously skipped images

**Verification:**
```bash
# Check that tesseract is installed and reachable
tesseract --version
```

## Query Intelligence

### Query Planner (FR-002 pt1)

The query planner automatically decomposes complex, multi-facet questions into distinct sub-queries for independent retrieval, then fuses the results before answer distillation.

**Behavior:**
- **Complex queries** (multi-facet or requiring multiple pieces of information): The LLM generates 2–3 semantically distinct sub-queries covering different aspects of the original question
- **Simple queries** (single-facet): Bypass the planner entirely and use standard single retrieval
- The planner runs once per chat query at the start of the RAG pipeline

**How it works:**
1. User submits a chat query
2. If the query is determined to be multi-facet, the LLM generates sub-queries
3. Each sub-query runs independent retrieval against the vector store
4. Results are fused using Reciprocal Rank Fusion (RRF, k=60)
5. The fused, deduplicated result set is passed to the answer distillation step

**Sub-Query RRF Fusion (FR-002 pt2):**
- Each sub-query检索 independently retrieves its own candidate set
- Results from all sub-queries are combined using RRF with k=60
- RRF score = Σ 1/(k + rank) across all sub-query result lists
- Duplicates by (file_id, text) are removed, preserving highest-ranked occurrence
- Single-facet queries skip fusion entirely (RRF applied to single list is a no-op)

### Chunk Enrichment

Chunk enrichment adds metadata (title, section headers, concept tags) to document chunks during ingestion, improving retrieval precision and answer quality.

**Global toggle:** `CHUNK_ENRICHMENT_ENABLED=true` in `.env` enables enrichment globally.

**Per-Vault Enrichment Toggle (FR-006):**

Override enrichment at the vault level. The effective enrichment state follows this precedence:

```
file override > vault override > global CHUNK_ENRICHMENT_ENABLED
```

**PUT /api/vaults/{id}/enrichment-toggle**

Set or clear a vault's enrichment override.

```bash
# Enable enrichment for this vault
curl -X PUT http://localhost:9090/api/vaults/1/enrichment-toggle \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Disable enrichment for this vault
curl -X PUT http://localhost:9090/api/vaults/1/enrichment-toggle \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Clear override (inherit global)
curl -X PUT http://localhost:9090/api/vaults/1/enrichment-toggle \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": null}'
```

Response includes `enrichment_enabled` (the override value) and `effective_enrichment_enabled` (the resolved state accounting for file/vault/global precedence).

Requires vault admin permission.

**Per-File Enrichment Toggle (FR-006):**

Override enrichment at the individual document level.

**PUT /api/documents/{id}/enrichment-toggle**

```bash
# Disable enrichment for a specific file (overrides vault/global)
curl -X PUT http://localhost:9090/api/documents/42/enrichment-toggle \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'

# Clear override (inherit vault/global)
curl -X PUT http://localhost:9090/api/documents/42/enrichment-toggle \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled": null}'
```

Requires vault admin permission on the file's vault.

---

## Prompt Versioning & A/B Testing

KnowledgeVault supports versioned prompts with per-organization overrides and A/B experimentation for prompt evaluation.

### Prompt Versioning (FR-007 pt1)

All prompt versions are stored in the `prompt_versions` table. The effective prompt for any query is resolved in this order:

```
org override > global active > built-in
```

**Admin Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/prompts` | List all prompt versions (metadata only) |
| GET | `/api/prompts/active` | Get the currently active prompt version (includes content) |
| POST | `/api/prompts` | Create a new prompt version |
| POST | `/api/prompts/{version}/activate` | Activate a specific version by name |
| GET | `/api/prompts/{version}` | Recover a prior version by name (returns full content) |

**List prompt versions:**
```bash
curl http://localhost:9090/api/prompts \
  -H "Authorization: Bearer $TOKEN"
```

**Create a new version:**
```bash
curl -X POST http://localhost:9090/api/prompts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"version": "v2", "content": "You are a helpful assistant...", "activate": true}'
```

**Activate a specific version:**
```bash
curl -X POST http://localhost:9090/api/prompts/v2/activate \
  -H "Authorization: Bearer $TOKEN"
```

**Recover a prior version:**
```bash
curl http://localhost:9090/api/prompts/v1 \
  -H "Authorization: Bearer $TOKEN"
```

All prompt endpoints require `admin:config` scope.

### Per-Org Prompt Overrides (FR-007 pt2)

Organizations can override the global active prompt version with their own version. The override applies to all queries from users belonging to that organization.

**Org Override Resolution:** org override > global active > built-in

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/organizations/{id}/prompt-override` | Get effective prompt for org (any org member) |
| PUT | `/api/organizations/{id}/prompt-override` | Set org's prompt override (org admin+) |
| DELETE | `/api/organizations/{id}/prompt-override` | Clear org override (org admin+) |

**Set an org override:**
```bash
curl -X PUT http://localhost:9090/api/organizations/1/prompt-override \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"version": "org-special-v1"}'
```

**Clear an org override:**
```bash
curl -X DELETE http://localhost:9090/api/organizations/1/prompt-override \
  -H "Authorization: Bearer $TOKEN"
```

**Get effective prompt:**
```bash
curl http://localhost:9090/api/organizations/1/prompt-override \
  -H "Authorization: Bearer $TOKEN"
```

Response includes `is_override: true` if the org has an active override, or `is_override: false` if using the global active version.

Requires org admin or owner for PUT/DELETE; any org member can read the effective version.

### A/B Prompt Experiments (FR-007 pt3)

A/B experiments compare two prompt versions (control vs. challenger) by assigning users to each variant. Assignment is deterministic and sticky — the same user always receives the same variant.

**Experiment Management:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/prompts/ab-experiments` | Create a new A/B experiment |
| GET | `/api/prompts/ab-experiments` | List all experiments with exposure counts |
| POST | `/api/prompts/ab-experiments/{id}/end` | End an experiment and declare winner |

**Create an experiment:**
```bash
curl -X POST http://localhost:9090/api/prompts/ab-experiments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "helpful-vs-concise", "control_version": "v1", "challenger_version": "v2", "split_pct": 50}'
```

- `split_pct`: Percentage of users assigned to the challenger variant (default 50). Control gets the remainder.
- Only one active experiment should exist at a time. End the current experiment before creating a new one.

**List experiments:**
```bash
curl http://localhost:9090/api/prompts/ab-experiments \
  -H "Authorization: Bearer $TOKEN"
```

Response includes per-variant exposure counts tracking how many users have been assigned to each arm.

**End an experiment:**
```bash
curl -X POST http://localhost:9090/api/prompts/ab-experiments/1/end \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"winner": "challenger"}'
```

**Chat Response Metadata:**

Chat responses served under an active experiment include these fields:

| Field | Type | Description |
|-------|------|-------------|
| `prompt_version` | string | The prompt version actually used |
| `ab_experiment_id` | int | The active experiment ID (if any) |
| `ab_variant` | string | `"control"` or `"challenger"` |

In streaming responses, these fields appear in the final `done` SSE event alongside sources and memories.

All A/B endpoints require `admin:config` scope.

---

## Security

### Authentication

KnowledgeVault has built-in JWT-based authentication with role-based access control.

**User Roles:**
- `superadmin` — Full system access, manages other admins
- `admin` — Full system access, can manage members and viewers
- `member` — Can create and update documents
- `viewer` — Read-only access

**Setup:**
- When `USERS_ENABLED=True`, set `ADMIN_SECRET_TOKEN` in `.env` to create the first admin user
- The initial admin can then invite other users via email or create accounts manually
- JWT tokens are stored in httpOnly refresh cookies for security

**Password Hashing:**
- New passwords are hashed with **Argon2id** (memory-hard, CPU-intensive)
- Existing **bcrypt** hashes are verified transparently and upgraded to Argon2id on next successful login — no forced password resets required

**Token Revocation:**
- Access tokens are **short-lived (15 minutes)** and can be **denylisted before expiry** (e.g., on logout)
- All active sessions for a user can be revoked at once via `POST /api/auth/revoke-all`

**Client Fingerprint Binding:**
- Access tokens are bound to the client fingerprint (User-Agent + other signals)
- Requests with a mismatched fingerprint are **rejected fail-closed** (HTTP 401)

**Options:**
1. **Single-Admin Mode** (`USERS_ENABLED=false`): The `ADMIN_SECRET_TOKEN` is the sole authentication mechanism — whoever possesses the token is the admin.

2. **Multi-User Mode** (`USERS_ENABLED=true`): Requires `ADMIN_SECRET_TOKEN` to be set for the initial admin account, then allows user management via the UI.

### Service Accounts

Service accounts provide scoped, rotatable API keys for programmatic access (CI/CD, automation, server-to-server integrations).

**Key Properties:**
- API keys are prefixed `sak_` and stored as SHA-256 hashes (keys themselves are shown only once at creation)
- Keys are scoped to specific permissions and can be rotated without disrupting other keys
- Rotation immediately invalidates the old key — there is no grace period

**Managing Service Accounts:**

| Action | How |
|--------|-----|
| Create | `POST /api/service-accounts` — returns the raw key once; store it securely |
| List | `GET /api/service-accounts` — shows metadata only, not keys |
| Rotate | `POST /api/service-accounts/{id}/rotate` — issues a new key, invalidates old |
| Revoke | `POST /api/service-accounts/{id}/revoke` — permanently invalidates the key |

### Organization Invites

Organization invites allow admins to invite users via a token-based flow with expiry and revocation.

**Invite Properties:**
- Tokens are prefixed `inv_` and expire after a configurable window (default 7 days)
- Invites can be **resent** (new expiry) or **revoked** (immediate invalidation)
- Each invite is tied to the inviting organization

**Managing Invites:**

| Action | How |
|--------|-----|
| Create | `POST /api/orgs/{id}/invites` — returns the raw `inv_` token to share |
| List | `GET /api/orgs/{id}/invites` — shows all invites and their status |
| Resend | `POST /api/orgs/{id}/invites/{invite_id}/resend` — resets expiry |
| Revoke | `POST /api/orgs/{id}/invites/{invite_id}/revoke` — invalidates token |
| Accept | `POST /api/orgs/{id}/invites/accept` — redeems the `inv_` token |

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
    # Must be larger than MAX_FILE_SIZE_MB to allow multipart overhead.
    client_max_body_size 125m;
    
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
| `ALLOWED_HOSTS` | Runtime env | Allowed Host header values for TrustedHostMiddleware |

**Minimal configuration** (`.env` or environment):

```env
APP_ROOT_PATH=/knowledgevault
VITE_APP_BASENAME=/knowledgevault
BACKEND_CORS_ORIGINS=https://example.com
FORWARDED_ALLOW_IPS=172.16.0.0/12
```

> **Security:** `FORWARDED_ALLOW_IPS` must be the CIDR or IP of your **trusted
> reverse proxy** — never `*`. With `*`, uvicorn trusts `X-Forwarded-*` from any
> peer, making `X-Forwarded-For` and `X-Forwarded-Proto` attacker-controlled. The
> Docker bridge subnet (commonly `172.16.0.0/12`) is safe for a Docker-Compose
> deployment where the proxy runs in a sibling container; substitute the
> appropriate IP or CIDR for your environment (e.g. `10.0.0.5` for a host-level
> nginx). Only use `*` inside a trusted private network where untrusted clients
> cannot connect directly to port 9090.

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
    # Must be larger than MAX_FILE_SIZE_MB to allow multipart overhead.
    client_max_body_size 125m;
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
| 404 with "Proxy misconfiguration" message | Reverse proxy not stripping prefix | Add trailing `/` to `proxy_pass` (nginx) or use `handle_path` (Caddy). Note: the detection is case-sensitive — `APP_ROOT_PATH` must exactly match the casing of the path forwarded by the proxy. |
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

### Rate Limiting

KnowledgeVault uses `slowapi` for per-IP rate limiting. The default limits are:

| Endpoint | Limit |
|----------|-------|
| Chat endpoints | 30/minute |
| Search endpoints | 30/minute |
| Vault creation | 30/minute |
| Memory mutations | 30/minute |

**Trusting reverse proxy headers:** When deployed behind a reverse proxy, you may want the rate limiter to use the `X-Forwarded-For` header to identify clients instead of the direct connection IP. Set `TRUST_PROXY_HEADERS=true` in `.env`:

```env
TRUST_PROXY_HEADERS=true
```

> **Security note:** Only enable `TRUST_PROXY_HEADERS` when behind a trusted reverse proxy (nginx, Caddy, etc.) that you control. The direct connection IP is used by default to prevent IP spoofing.

### Reverse Proxy Purpose
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
- [ ] Audit service account keys quarterly — revoke any unused keys and rotate annually
- [ ] Audit org invites monthly — revoke expired or superseded invites

---

## Chat UX Features

### Per-Pane Error Boundaries (FR-017)

The chat workspace is divided into three isolated panes — Session Rail, Transcript, and Sources. Each pane runs in its own error boundary. If one pane encounters an unhandled error and crashes, the other two continue operating normally. A "Retry" button appears on the crashed pane to reset it without disrupting the rest of the session.

### KaTeX + Mermaid Rendering (FR-016)

Chat responses render LaTeX math (inline `$...$` and block `$$...$$`) and Mermaid diagrams directly in the message body. Mermaid diagrams are rendered with `securityLevel='strict'`, disabling scripts and external resource access.

Supported KaTeX contexts:
- **Inline math:** `$E = mc^2$`
- **Block math:** `$$\int_0^\infty e^{-x^2} dx$$`

Supported Mermaid diagram types: flowchart, sequence diagram, class diagram, state diagram, entity relationship diagram, gantt chart, pie chart, and others supported by the Mermaid `strict` mode.

### Inline Composer Controls (FR-018)

The message composer contains inline selectors for three per-session settings:

| Control | Description | Persistence |
|---------|-------------|-------------|
| **Temperature** | LLM creativity/randomness slider (0–1 scale) | Per session, wired to the LLM on every request |
| **Retrieval Mode** | Controls retrieval strategy (e.g., `thinking`, `instant`) | Per session |
| **Citation Mode** | Toggles citation display style | Per session |

Settings are stored with the session and restored when the session is reopened.

### Reconnecting Banner (FR-019)

When the SSE connection to the backend is lost during a streaming chat response, a prominent banner appears at the top of the chat pane:

- **Red banner:** Connection dropped unexpectedly (server error, network failure)
- **Amber banner:** Connection is being re-established (temporary outage)

The banner uses accessible color coding and labeling so it remains distinguishable even for users with color vision deficiencies. When connectivity is restored, the banner dismisses automatically.

### SSE Staged Progress (FR-015)

Before the first token of a streaming answer arrives, the UI displays a stage indicator showing the current RAG pipeline stage:

1. **Searching** — Query decomposition and vector retrieval in progress
2. **Reading** — Retrieved chunks are being read and scored
3. **Drafting** — Answer is being generated and streamed

Stage events arrive as SSE comments or a dedicated field before the answer token stream begins. The stage indicator updates in real time as the pipeline transitions between stages.

### Citation Confidence (FR-003/FR-004 Frontend)

Citations in chat responses include a confidence indicator:

- **Colored dots:** Green (high confidence), Amber (medium), Red (low/unverifiable)
- **Citation popover:** Clicking a `[S#]` citation opens a popover showing the exact source span, document name, and relevance score
- **Unverifiable claims:** When a sentence in the answer cannot be traced to a retrieved chunk, the sentence is flagged and listed separately in the RAG trace panel

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
