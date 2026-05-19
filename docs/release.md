# KnowledgeVault Release Documentation

This document provides comprehensive guidance for deploying, maintaining, and operating the KnowledgeVault RAG application in production environments.

## Table of Contents

1. [Deployment Fixes & Configuration Changes](#deployment-fixes--configuration-changes)
2. [Pre-Deployment Checklist](#pre-deployment-checklist)
3. [Encrypted Backup & Rollback Process](#encrypted-backup--rollback-process)
4. [Maintenance Flag Usage](#maintenance-flag-usage)
5. [Connection Test Guidance](#connection-test-guidance)
6. [Observability & Monitoring](#observability--monitoring)

---

## Deployment Fixes & Configuration Changes

### Embedding Batch Size Configuration (Critical Fix)

**Issue:** Remote deployments with TEI (Text Embeddings Inference) were failing with:
```
422 Validation: batch size 512 > maximum allowed batch size 32
```

**Solution:** 
- `EMBEDDING_BATCH_SIZE` now defaults to 32 (aligned with TEI's actual limit)
- Valid range: 1-128 (validator caps at 128 for safety)
- Configure in `.env` or `docker-compose.yml`

**Migration:**
- If upgrading from a deployment with `EMBEDDING_BATCH_SIZE > 32`, you must:
  1. Update `.env` or docker-compose environment to set `EMBEDDING_BATCH_SIZE=32` (or appropriate value for your embedding service)
  2. Restart containers: `docker compose up -d`
  3. Verify: `docker exec knowledgevault python -c "from app.config import settings; print(settings.embedding_batch_size)"`

### Spreadsheet Chunking & Data Preservation

**Issue:** Wide spreadsheets (100+ columns) produced chunks exceeding the embedding model's 8192-character limit, causing data truncation.

**Solution:**
- Implemented column-group splitting: spreadsheets are now split by columns when a single row exceeds 8192 chars
- All column data is preserved; only single cell values exceeding 8192 chars are truncated (with logging)
- Each chunk includes sheet name + column headers + values for full context

**Behavior:**
- Wide spreadsheets are processed automatically without user action
- Check logs for warnings: `"Document '...' has ... chunk(s) exceeding max embedding length"`
- Retrieved chunks for wide spreadsheets may include multiple column-group chunks for the same row

### Pre-Embedding Validation

New validation logs chunk sizes before embedding:
- **Log level:** WARNING for oversized chunks
- **Location:** Application logs
- **Example:** `Document 'wide_sheet.xlsx' has 3 chunk(s) exceeding max embedding length (8192 chars): chunk 0 (8200 chars), chunk 2 (9150 chars)`
- **Action:** Monitor logs during initial document processing; this is observational (embedding service has its own safeguard)

---

## Pre-Deployment Checklist

Before deploying KnowledgeVault to production, ensure the following:

### Infrastructure Requirements

- [ ] SQLite database directory (`/data/knowledgevault`) with appropriate permissions
- [ ] LanceDB vector store directory with write access
- [ ] Vault directories (`/data/knowledgevault/vaults/{vault_id}/uploads/`) with sufficient disk space for each vault
- [ ] Redis instance running for CSRF token storage (default: `redis://localhost:6379/0`)
- [ ] Ollama or OpenAI-compatible embedding service accessible
- [ ] LLM chat service accessible (Ollama, OpenAI, or compatible API)

### Configuration

- [ ] Environment variables configured in `.env` file:
  ```bash
  # Required settings
  DATA_DIR=/data/knowledgevault
  OLLAMA_EMBEDDING_URL=http://harrier-embed:8080/v1/embeddings
  OLLAMA_CHAT_URL=http://host.docker.internal:11434
  INSTANT_CHAT_URL=http://host.docker.internal:1234
  EMBEDDING_MODEL=microsoft/harrier-oss-v1-0.6b
  CHAT_MODEL=gemma-4-26b-a4b-it-apex
  INSTANT_CHAT_MODEL=nvidia/nemotron-3-nano-4b
  DEFAULT_CHAT_MODE=thinking
  INSTANT_INITIAL_RETRIEVAL_TOP_K=10
  INSTANT_RERANKER_TOP_N=4
  INSTANT_MEMORY_CONTEXT_TOP_K=2
  INSTANT_MAX_TOKENS=4096
  
  # Security settings
  ADMIN_SECRET_TOKEN=<secure-random-token>
  AUDIT_HMAC_KEY_VERSION=v1
  AUDIT_HMAC_KEY=<your-hmac-key>
  AUDIT_HMAC_KEY_V1=<your-hmac-key-v1>
  
  # Rate limiting
  ADMIN_RATE_LIMIT=10/minute
  CSRF_TOKEN_TTL=900
  
   
  ```

### Health Verification

- [ ] Run connection tests (see [Connection Test Guidance](#connection-test-guidance))
- [ ] Verify `/health` endpoint returns `{"status": "ok"}`
- [ ] Verify `/api/health` returns detailed health status including LLM and models

---

## Encrypted Backup & Rollback Process

### Backup Strategy

KnowledgeVault requires backing up two primary data stores:

1. **SQLite Database** (`/data/knowledgevault/app.db`)
2. **LanceDB Vector Store** (`/data/knowledgevault/lancedb/`)

#### Automated Backup Script

```bash
#!/bin/bash
# backup-knowledgevault.sh

BACKUP_DIR="/backup/knowledgevault"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DATA_DIR="/data/knowledgevault"
ENCRYPTION_KEY_FILE="/secure/backup-key.pub"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup SQLite database (hot backup with WAL checkpoint)
sqlite3 "$DATA_DIR/app.db" ".backup '$BACKUP_DIR/app_$TIMESTAMP.db'"

# Backup LanceDB (copy with timestamp)
tar -czf "$BACKUP_DIR/lancedb_$TIMESTAMP.tar.gz" -C "$DATA_DIR" lancedb/

# Encrypt backups
gpg --encrypt --recipient-file "$ENCRYPTION_KEY_FILE" \
    --output "$BACKUP_DIR/app_$TIMESTAMP.db.gpg" \
    "$BACKUP_DIR/app_$TIMESTAMP.db"

gpg --encrypt --recipient-file "$ENCRYPTION_KEY_FILE" \
    --output "$BACKUP_DIR/lancedb_$TIMESTAMP.tar.gz.gpg" \
    "$BACKUP_DIR/lancedb_$TIMESTAMP.tar.gz"

# Remove unencrypted files
rm "$BACKUP_DIR/app_$TIMESTAMP.db"
rm "$BACKUP_DIR/lancedb_$TIMESTAMP.tar.gz"

# Retention: Keep last 30 days of backups
find "$BACKUP_DIR" -name "*.gpg" -mtime +30 -delete

echo "Backup completed: $TIMESTAMP"
```

#### Restore Process

```bash
#!/bin/bash
# restore-knowledgevault.sh

BACKUP_DIR="/backup/knowledgevault"
DATA_DIR="/data/knowledgevault"
TIMESTAMP=$1  # Pass timestamp as argument

if [ -z "$TIMESTAMP" ]; then
    echo "Usage: $0 <timestamp>"
    echo "Available backups:"
    ls -1 "$BACKUP_DIR"/*.gpg | xargs -n1 basename
    exit 1
fi

# Stop application
docker-compose down  # or systemctl stop knowledgevault

# Decrypt backups
gpg --decrypt \
    --output "$DATA_DIR/app.db" \
    "$BACKUP_DIR/app_$TIMESTAMP.db.gpg"

gpg --decrypt \
    --output "/tmp/lancedb_$TIMESTAMP.tar.gz" \
    "$BACKUP_DIR/lancedb_$TIMESTAMP.tar.gz.gpg"

# Restore LanceDB
rm -rf "$DATA_DIR/lancedb"
tar -xzf "/tmp/lancedb_$TIMESTAMP.tar.gz" -C "$DATA_DIR"
rm "/tmp/lancedb_$TIMESTAMP.tar.gz"

# Restart application
docker-compose up -d  # or systemctl start knowledgevault

echo "Restore completed from: $TIMESTAMP"
```

#### Verification After Restore

1. Check database integrity:
   ```bash
   sqlite3 /data/knowledgevault/app.db "PRAGMA integrity_check;"
   ```

2. Verify document count matches expected:
   ```bash
   sqlite3 /data/knowledgevault/app.db "SELECT COUNT(*) FROM files;"
   ```

3. Test search functionality via API:
   ```bash
   curl -X POST http://localhost:9090/api/chat \
     -H "Content-Type: application/json" \
     -d '{"message": "test", "history": [], "stream": false}'
   ```

---

## Maintenance Flag Usage

The maintenance flag system allows graceful degradation of service during deployments or maintenance windows.

### How It Works

When maintenance mode is enabled:
- **GET requests** continue to work normally (read-only access)
- **POST, PUT, PATCH, DELETE requests** return HTTP 503 with "maintenance" message
- RAG queries fallback to memory-only mode (no vector search)

### Enabling Maintenance Mode

```bash
# Via API (requires admin scope)
curl -X POST http://localhost:9090/api/admin/maintenance \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: <csrf-token>" \
  -H "Authorization: Bearer <admin-token>" \
  -d '{"enabled": true, "reason": "Database migration in progress"}'
```

### Checking Maintenance Status

```bash
curl http://localhost:9090/api/admin/maintenance
```

Response:
```json
{
  "enabled": true,
  "reason": "Database migration in progress",
  "version": 5,
  "updated_at": "2026-02-06T12:00:00Z"
}
```

### Disabling Maintenance Mode

```bash
curl -X POST http://localhost:9090/api/admin/maintenance \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: <csrf-token>" \
  -H "Authorization: Bearer <admin-token>" \
  -d '{"enabled": false}'
```

### Maintenance Mode Best Practices

1. **Before maintenance:**
   - Enable maintenance mode 5 minutes before starting
   - Communicate maintenance window to users
   - Verify no active uploads or processing jobs

2. **During maintenance:**
   - Monitor logs for any blocked write requests
   - Check `/api/health` for service status

3. **After maintenance:**
   - Disable maintenance mode
   - Verify all endpoints return 200
   - Run smoke tests (upload, search, chat)

---

## Connection Test Guidance

### Automated Connection Testing

Run the following tests to verify all external dependencies:

#### 1. Database Connection Test

```bash
#!/bin/bash
# test-database.sh

echo "Testing SQLite database..."
sqlite3 /data/knowledgevault/app.db "SELECT 1;" && echo "✓ Database OK" || echo "✗ Database FAILED"

echo "Testing required tables..."
for table in files chunks memories document_actions admin_toggles system_flags; do
    sqlite3 /data/knowledgevault/app.db "SELECT COUNT(*) FROM $table;" > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo "✓ Table $table exists"
    else
        echo "✗ Table $table missing"
    fi
done
```

#### 2. Embedding Service Test

```bash
#!/bin/bash
# test-embedding.sh

EMBEDDING_URL="${OLLAMA_EMBEDDING_URL:-http://localhost:8080/v1/embeddings}"
EMBEDDING_MODEL="${EMBEDDING_MODEL:-microsoft/harrier-oss-v1-0.6b}"

echo "Testing embedding service at $EMBEDDING_URL..."

response=$(curl -s -X POST "$EMBEDDING_URL" \
  -H "Content-Type: application/json" \
  -d "{\"model\": \"$EMBEDDING_MODEL\", \"input\": \"test\"}" \
  -w "\n%{http_code}")

http_code=$(echo "$response" | tail -n1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" = "200" ]; then
    echo "✓ Embedding service OK"
    # Verify embedding dimension
    dim=$(echo "$body" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['embedding']))")
    echo "  Embedding dimension: $dim"
else
    echo "✗ Embedding service FAILED (HTTP $http_code)"
    echo "  Response: $body"
fi
```

#### 3. LLM Chat Service Test

```bash
#!/bin/bash
# test-llm.sh

CHAT_URL="${OLLAMA_CHAT_URL:-http://localhost:11434}/api/chat"
CHAT_MODEL="${CHAT_MODEL:-gemma-4-26b-a4b-it-apex}"

echo "Testing LLM service at $CHAT_URL..."

response=$(curl -s -X POST "$CHAT_URL" \
  -H "Content-Type: application/json" \
  -d "{\"model\": \"$CHAT_MODEL\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}], \"stream\": false}" \
  -w "\n%{http_code}")

http_code=$(echo "$response" | tail -n1)

if [ "$http_code" = "200" ]; then
    echo "✓ LLM service OK"
else
    echo "✗ LLM service FAILED (HTTP $http_code)"
fi
```

#### 4. Instant Chat Service Test

```bash
#!/bin/bash
# test-instant-llm.sh

INSTANT_CHAT_URL="${INSTANT_CHAT_URL:-http://localhost:1234}/v1/chat/completions"
INSTANT_CHAT_MODEL="${INSTANT_CHAT_MODEL:-nvidia/nemotron-3-nano-4b}"

echo "Testing instant chat service at $INSTANT_CHAT_URL..."

response=$(curl -s -X POST "$INSTANT_CHAT_URL" \
  -H "Content-Type: application/json" \
  -d "{\"model\": \"$INSTANT_CHAT_MODEL\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}], \"stream\": false}" \
  -w "\n%{http_code}")

http_code=$(echo "$response" | tail -n1)

if [ "$http_code" = "200" ]; then
    echo "Instant chat service OK"
else
    echo "Instant chat service FAILED (HTTP $http_code)"
fi
```

#### 5. Redis Connection Test

```bash
#!/bin/bash
# test-redis.sh

REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

echo "Testing Redis at $REDIS_URL..."

# Extract host and port from URL
redis_host=$(echo "$REDIS_URL" | sed -E 's|redis://([^:]+):.*|\1|')
redis_port=$(echo "$REDIS_URL" | sed -E 's|redis://[^:]+:([0-9]+).*|\1|')

if redis-cli -h "$redis_host" -p "$redis_port" ping | grep -q "PONG"; then
    echo "✓ Redis OK"
else
    echo "✗ Redis FAILED"
fi
```

#### 6. Full Integration Test

```bash
#!/bin/bash
# test-integration.sh

API_BASE="${API_BASE:-http://localhost:9090}"

echo "Running integration tests against $API_BASE..."

# Test health endpoint
echo -n "Health check... "
if curl -sf "$API_BASE/health" > /dev/null; then
    echo "✓"
else
    echo "✗ FAILED"
    exit 1
fi

# Test detailed health
echo -n "Detailed health... "
health=$(curl -sf "$API_BASE/api/health")
if echo "$health" | grep -q '"status": "ok"'; then
    echo "✓"
    echo "  LLM: $(echo "$health" | python3 -c "import sys,json; print('OK' if json.load(sys.stdin)['llm']['ok'] else 'FAIL')")"
    echo "  Models: $(echo "$health" | python3 -c "import sys,json; d=json.load(sys.stdin)['models']; print(f\"embed={d['embedding_model']['available']}, chat={d['chat_model']['available']}, instant={d['instant_chat_model']['available']}\")")"
else
    echo "✗ FAILED"
fi

# Test document upload (if auth available)
echo -n "Document upload... "
upload_response=$(curl -sf -X POST "$API_BASE/api/documents/upload" \
  -F "file=@/tmp/test_doc.txt" 2>/dev/null)
if [ $? -eq 0 ]; then
    echo "✓"
else
    echo "⚠ SKIPPED (auth required)"
fi

# Test memory operations
echo -n "Memory operations... "
if curl -sf "$API_BASE/api/memories" > /dev/null; then
    echo "✓"
else
    echo "✗ FAILED"
fi

echo "Integration tests complete"
```

### Python Test Suite

Run the comprehensive Python test suite:

```bash
# Run all tests
cd backend
python -m pytest tests/ -v

# Run specific test categories
python -m pytest tests/test_integration.py -v
python -m pytest tests/test_rag_engine.py -v
python -m pytest tests/test_document_processor.py -v

# Run with coverage
python -m pytest tests/ --cov=app --cov-report=html
```

---

## Observability & Monitoring

### Request ID Tracking

Every HTTP request is assigned a unique `X-Request-ID` header for distributed tracing:

- **Request:** Client may provide `X-Request-ID` header (optional)
- **Response:** Server always returns `X-Request-ID` header
- **Logs:** All request logs include the `request_id` field

#### Example Usage

```bash
# Make request with custom request ID
curl -H "X-Request-ID: req-123-abc" http://localhost:8080/api/health

# Response includes the same ID
# X-Request-ID: req-123-abc
```

#### Correlating Logs

```bash
# Find all logs for a specific request
grep "request_id.*req-123-abc" /var/log/knowledgevault/app.log

# Or using structured logging (JSON)
jq 'select(.request_id == "req-123-abc")' /var/log/knowledgevault/app.log
```

### Log Redaction Fields

Sensitive fields are automatically redacted in logs:

| Field | Redaction Reason |
|-------|-----------------|
| `message` | May contain user input with PII |
| `content` | Chat messages may contain sensitive data |
| `user_input` | Direct user queries |
| `token` | Authentication tokens |
| `email` | User email addresses |
| `username` | User identifiers |
| `file_path` | Internal file system paths |
| `ip` | Client IP addresses (privacy) |
| `session_id` | Session identifiers |
| `api_key` | API credentials |
| `authorization` | Auth headers |
| `secret` | Any secret values |

#### Redaction Behavior

Values are replaced with `[redacted]` in logs:

```python
# Before redaction
{"message": "My password is secret123", "user": "john"}

# After redaction
{"message": "[redacted]", "user": "john"}
```

### Structured Logging Format

All HTTP requests are logged in structured JSON format:

```json
{
  "timestamp": "2026-02-06T12:34:56.789Z",
  "level": "INFO",
  "logger": "http",
  "message": "http_request",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "method": "POST",
  "path": "/api/chat",
  "query": "",
  "status_code": 200,
  "client_ip": "[redacted]",
  "duration_ms": 145.32
}
```

### Key Metrics to Monitor

#### Application Metrics

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `http_request_duration_ms` | Request latency | P99 > 5000ms |
| `http_requests_total` | Request rate | Baseline ± 50% |
| `http_errors_total` | Error rate | > 1% of total |
| `rag_query_duration_ms` | RAG query latency | P99 > 10000ms |
| `embedding_request_duration_ms` | Embedding latency | P99 > 3000ms |
| `llm_request_duration_ms` | LLM latency | P99 > 30000ms |

#### Database Metrics

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `sqlite_connection_pool_size` | Active connections | > 80% of max |
| `lancedb_query_duration_ms` | Vector search latency | P99 > 1000ms |
| `files_total` | Total indexed documents | Track growth |
| `chunks_total` | Total indexed chunks | Track growth |

#### External Service Metrics

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `ollama_embedding_availability` | Embedding service up | < 99% |
| `ollama_chat_availability` | LLM service up | < 99% |
| `redis_availability` | Redis connection | < 99% |

### Log Analysis Queries

#### Find Slow Requests

```bash
# Using jq with structured logs
jq 'select(.duration_ms > 5000) | {path, method, duration_ms, request_id}' app.log
```

#### Error Rate by Endpoint

```bash
# Count errors by endpoint
jq -r 'select(.status_code >= 400) | "\(.status_code) \(.method) \(.path)"' app.log | \
  sort | uniq -c | sort -rn | head -20
```

#### RAG Query Performance

```bash
# Extract RAG query timings from logs
grep "rag_query" app.log | \
  jq -s 'map(.duration_ms) | {count: length, avg: (add/length), max: max}'
```

### Health Check Endpoints

| Endpoint | Purpose | Frequency |
|----------|---------|-----------|
| `GET /health` | Basic liveness | Every 30s |
| `GET /api/health` | Full health with dependencies | Every 60s |
| `GET /api/settings` | Configuration verification | On demand |

### Alerting Rules

Example Prometheus alerting rules:

```yaml
groups:
  - name: knowledgevault
    rules:
      - alert: HighErrorRate
        expr: rate(http_requests_total{status=~"5.."}[5m]) > 0.01
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High error rate detected"
          
      - alert: SlowRAGQueries
        expr: histogram_quantile(0.99, rate(rag_query_duration_ms_bucket[5m])) > 10000
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "RAG queries are slow"
          
      - alert: EmbeddingServiceDown
        expr: up{job="ollama-embedding"} == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Embedding service is down"
          
      - alert: MaintenanceModeActive
        expr: maintenance_mode_enabled == 1
        for: 1h
        labels:
          severity: info
        annotations:
          summary: "Maintenance mode has been active for over 1 hour"
```

---

## Support & Troubleshooting

### Common Issues

#### 1. Document Upload Fails

**Symptoms:** 413 Payload Too Large or 500 Server Error

**Resolution:**
- Check `max_file_size_mb` setting (default: 50MB)
- Verify uploads directory has disk space
- Check document processor logs for parsing errors

#### 2. Search Returns No Results

**Symptoms:** Chat responses don't reference documents

**Resolution:**
- Verify documents show as "indexed" in database
- Check vector store connectivity
- Verify `max_distance_threshold` setting (default: 0.5)
- Test embedding service is generating vectors

#### 3. Chat Stream Interrupted

**Symptoms:** Streaming responses cut off mid-response

**Resolution:**
- Check LLM service timeout settings
- Verify network stability between app and LLM
- Review `llm_request_duration_ms` metrics

### Getting Help

1. Check application logs with request ID
2. Run connection test scripts
3. Verify health endpoint responses
4. Review this documentation for relevant section

---

## Release Notes

### Version 0.3.1 (Docs/config/supply-chain cleanup - Issues #47, #38)

#### Security and Auth

- Browser authentication documentation now reflects the current JWT username/password flow with httpOnly refresh cookies. The removed frontend `kv_api_key` fallback is no longer documented as a supported login path.
- Expired and invalid JWTs now surface as HTTP 401 token errors for API consumers, with structured token-expired/token-invalid details instead of the older generic auth response.

#### Settings and Connection API

- `POST /api/settings` and `PUT /api/settings` are documented as admin-only settings write operations.
- `GET /api/settings/connection` is documented as an authenticated connection probe that checks embedding, thinking-chat, instant-chat, and reranker availability.
- Release connection-test guidance now covers the instant chat endpoint and reports instant model health alongside embedding and thinking-chat model health.

#### Configuration and Supply Chain

- Installation and README defaults now align with Harrier TEI embeddings, `microsoft/harrier-oss-v1-0.6b`, backend port 9090, frontend dev port 3000, and the current thinking/instant model defaults.
- `numpy` is declared directly for vector-store production code and tests; unused `pybreaker` declarations were removed from backend requirements.
- Local machine-specific Windows paths and generated build/test output artifacts were removed or replaced with repo-relative/configurable helper scripts.

### Version 0.3.0 (PDF/document viewer - Issue #54)

#### New Features

- Added an authenticated raw-document endpoint for users with vault read access.
- Added a code-split PDF preview panel that can open cited source documents at the referenced page.
- Added a non-PDF fallback that downloads original files instead of rendering active content inline.

#### Security

- Raw document responses now validate stored file paths against configured document roots before serving bytes.
- Non-PDF raw responses are served as attachments with `X-Content-Type-Options: nosniff`.

#### Migration Steps

No database or configuration migration is required.

#### Breaking Changes

None.

---

### Version 0.2.1 (PR1 performance quick wins — Issue #20)

#### Performance Improvements

No configuration changes or breaking changes. All improvements are internal.

- **Query embedding parallelisation (PERF-1):** `RAGEngine` now embeds query variants (original, step-back, HyDE) concurrently using `asyncio.gather`, reducing RAG latency proportional to the number of active variants.
- **O(1) LRU cache in QueryTransformer (PERF-2):** Replaced `dict+list` LRU implementation with `collections.OrderedDict`. Access reordering (`move_to_end`) and eviction (`popitem`) are now O(1) instead of O(n).
- **Shared VectorStore semaphore (PERF-4):** LanceDB search operations are now rate-limited by a class-level semaphore shared across all concurrent callers (`_MULTI_SCALE_CONCURRENCY = 4`). Previously the semaphore was per-call local, providing no cross-request limiting. Single-scale searches (the default path) are now also guarded.
- **Adaptive document polling (PERF-11):** The Documents page polling interval starts at 2 s and backs off 1.5× per cycle (capped at 30 s) during sustained processing, resetting to 2 s when processing completes. This reduces unnecessary backend load while remaining responsive at the start of each processing batch.

#### Deferred

- **PERF-10 (parallel message saves):** Deferred — `chat_messages.created_at` uses second-level SQLite precision; concurrent inserts within the same second produce identical timestamps and break `ORDER BY created_at ASC` retrieval order. Requires a schema migration before this is safe.

---

### Version 0.2.0 (Phase 7 — Issues #2, #12, #13, #14)

#### Breaking Changes

**vault_id is now required on document upload (Issue #14)**
The `/api/documents` and `/api/documents/upload` POST endpoints no longer default
`vault_id` to `1`. Requests without `vault_id` receive HTTP 422. Update any upload
clients to pass `?vault_id=<id>` explicitly.

#### Re-embedding Required for Existing Deployments (Issue #2)

Deployments upgrading from BGE-M3 (768-dim) to Harrier (1024-dim) must re-embed all
documents. Existing LanceDB vectors are incompatible with the new embedding dimension
and will produce empty search results.

**Migration steps:**
1. Backup LanceDB and SQLite data.
2. Run `python scripts/migrate_embeddings.py` (wipes LanceDB, resets file statuses).
3. Restart the service — the background processor re-indexes all documents.

The `/api/health?deep=true` endpoint now returns `"stale_embeddings": true` in the
`vector_store` section when a dimension mismatch is detected, surfacing this issue
without a silent failure mode.

#### New Features

- **Parent-document retrieval / small-to-big (Issue #12):** When `PARENT_RETRIEVAL_ENABLED=true`,
  the RAG engine retrieves a ±3000-character window around each matched chunk and surfaces
  it to the LLM with a `[[MATCH: ...]]` anchor so the model sees precise evidence in context.
  Enabled by default. New config: `PARENT_RETRIEVAL_ENABLED`, `PARENT_WINDOW_CHARS`.

- **Group-aware dedup (Issue #12):** Replaces the UID-strip dedup that collapsed multiple
  strong chunks from the same document into a single result. The best document now contributes
  up to 5 chunks (configurable via `PER_DOC_CHUNK_CAP`), preserving evidence density. Up to
  5 distinct documents are returned (`UNIQUE_DOCS_IN_TOP_K`).

- **Atomic ingestion visibility (Issue #13):** Chunks from documents still being processed
  (status `pending` or `processing`) are hidden from RAG search results until the file
  reaches `indexed` status, preventing partial-document answers.

- **Safe re-upload ordering (Issue #13):** When `REUPLOAD_SAFE_ORDER=true` (default), new
  chunk generations are inserted before old ones are deleted, eliminating the zero-chunk
  window during re-upload that could produce empty results.

- **ANN index lifecycle management (Issue #13):** The IVF_PQ vector index is now automatically
  rebuilt after ≥20% row churn from deletes, and dropped when row count falls below 256
  (brute-force threshold). `INDEX_REBUILD_DELTA` controls the churn threshold.

- **Schema migration — parent window columns (Issue #12):** LanceDB schema now includes
  `parent_doc_id`, `parent_window_start`, `parent_window_end`, and `chunk_position` columns.
  Run `python -m app.migrations.add_parent_window` to backfill existing databases.

- **Vault-1 default audit (Issue #13):** `python -m app.migrations.audit_vault_defaults`
  reports documents that were silently assigned to vault 1 before the vault_id-required change.

#### Configuration Changes

New environment variables (all optional, sensible defaults):

| Variable | Default | Description |
|----------|---------|-------------|
| `PARENT_RETRIEVAL_ENABLED` | `true` | Enable small-to-big context expansion |
| `PARENT_WINDOW_CHARS` | `6000` | Characters per parent window (±3000 around chunk) |
| `NEW_DEDUP_POLICY` | `true` | Use group-aware dedup (replaces UID-strip) |
| `PER_DOC_CHUNK_CAP` | `5` | Max chunks per document in final result set |
| `UNIQUE_DOCS_IN_TOP_K` | `5` | Max distinct documents in final result set |
| `INDEX_REBUILD_DELTA` | `0.2` | Churn fraction (deletes/last_build) to trigger ANN rebuild |
| `REUPLOAD_SAFE_ORDER` | `true` | Insert new chunks before deleting old on re-upload |

---

### Version 0.1.0 (Phase 6)

- Added comprehensive integration test suite
- Implemented encrypted backup/rollback procedures
- Added maintenance mode flag system
- Enhanced observability with request ID tracking
- Structured logging with sensitive field redaction
- Rate limiting on admin endpoints
- CSRF protection for state-changing operations
- HMAC audit logging for document operations
