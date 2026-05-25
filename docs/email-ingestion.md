# KnowledgeVault Email Ingestion Guide

Email-based document ingestion for KnowledgeVault allows users to upload documents by sending emails with attachments. This guide covers configuration, usage, security, and troubleshooting for the email ingestion feature.

---

## Table of Contents

1. [Overview](#overview)
2. [How It Works](#how-it-works)
3. [Configuration](#configuration)
4. [MailEnable Setup](#mailenable-setup)
5. [Usage Guide](#usage-guide)
6. [Vault Routing](#vault-routing)
7. [Security Considerations](#security-considerations)
8. [Status Endpoint](#status-endpoint)
9. [Troubleshooting](#troubleshooting)
10. [Examples](#examples)

---

## Overview

Email ingestion enables KnowledgeVault to automatically process documents sent via email. Users can email documents as attachments, and the system will:

- **Poll** an IMAP inbox for new emails at configurable intervals
- **Extract** document attachments from emails
- **Route** documents to specific vaults based on subject tags
- **Process** attachments through the standard document processor (chunking, embedding, vector storage)
- **Track** metadata including original sender, subject, and source

This feature is ideal for:
- Users who prefer email workflow over web UI
- Automated document forwarding from other systems
- Bulk document uploads from multiple contributors
- Mobile users who need to quickly share documents

---

## How It Works

### Architecture

```
┌─────────────┐      IMAP (SSL)      ┌──────────────┐
│   User      │ ───────────────────► │   MailEnable │
│  (Email)    │   Poll Every 60s     │  IMAP Server │
└─────────────┘                      └──────┬───────┘
                                             │
                                             │ IMAP Client (aioimaplib)
                                             │
┌─────────────┐   Email Queue    ┌───────────▼──────────┐
│   Document  │ ◄────────────────│ EmailIngestionService │
│ Processor   │  BackgroundTask │  (Polling & Parsing) │
└──────┬──────┘                  └──────────┬───────────┘
       │                                    │
       │ Vector Store                       │
       ▼                                    ▼
┌─────────────┐                      ┌──────────────┐
│  LanceDB    │                      │ SQLite DB    │
│  Vectors    │                      │  Metadata    │
└─────────────┘                      └──────────────┘
```

### Processing Flow

1. **IMAP Polling** (every 30-60 seconds)
   - Connects to IMAP server using SSL
   - Searches for `UNSEEN` emails
   - Implements exponential backoff on connection failures (5s → 15s → 30s → 60s)

2. **Email Validation**
   - Checks email size (max 50MB)
   - Validates sender and subject
   - Skips oversized emails to prevent DoS attacks

3. **Attachment Extraction**
   - Iterates through all MIME parts
   - Filters by allowed MIME types (PDF, DOCX, TXT, etc.)
   - Validates attachment size (max 10MB per file)
   - Limits to 10 attachments per email
   - Skips invalid/non-document attachments

4. **Vault Resolution**
    - Extracts vault name from subject using `[VaultName]` or `#vaultname` tags
    - Case-insensitive vault lookup in database
    - Raises ValueError if vault not found or no tag present — email is rejected

5. **Background Processing**
    - Saves attachments to vault-specific upload directory based on target vault
   - Enqueues documents to `BackgroundProcessor`
   - Standard processing pipeline: parse → chunk → embed → vector store
   - Tracks metadata: `source='email'`, `email_subject`, `email_sender`

6. **Error Handling**
   - Continues processing next email if one fails
   - Logs errors with sanitized values (prevents log injection)
   - Implements automatic retry with backoff
   - Tracks failed processing in database for troubleshooting

---

## Configuration

### Environment Variables

Configure email ingestion via environment variables in your `.env` file:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `IMAP_ENABLED` | boolean | `False` | Enable/disable email ingestion service |
| `IMAP_HOST` | string | `""` | IMAP server hostname or IP address |
| `IMAP_PORT` | integer | `993` | IMAP server port (993 for SSL, 143 for non-SSL) |
| `IMAP_USE_SSL` | boolean | `True` | Use SSL/TLS encryption (set to `false` for port 143) |
| `IMAP_USERNAME` | string | `""` | IMAP account username |
| `IMAP_PASSWORD` | string | `""` | IMAP account password (use SecretStr for .env) |
| `IMAP_MAILBOX` | string | `"INBOX"` | IMAP mailbox to poll (default: INBOX) |
| `IMAP_POLL_INTERVAL` | integer | `60` | Polling interval in seconds (30-300 recommended) |
| `IMAP_MAX_ATTACHMENT_SIZE` | integer | `10485760` | Max attachment size in bytes (10MB) |
| `IMAP_ALLOWED_MIME_TYPES` | set | See below | Comma-separated list of allowed MIME types |

### Default Allowed MIME Types

```python
{
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/csv",
    "application/json",
    "application/sql",
    "text/x-python",
    "application/javascript",
    "text/html",
    "text/css",
    "application/xml",
    "application/x-yaml",
    "text/x-log",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
```

### Example Configuration

#### With SSL (Recommended - Port 993)

```bash
# .env file

# Enable email ingestion
IMAP_ENABLED=True

# IMAP server settings with SSL
IMAP_HOST=mail.yourdomain.com
IMAP_PORT=993
IMAP_USE_SSL=True
IMAP_USERNAME=knowledgevault@yourdomain.com
IMAP_PASSWORD=your_secure_password_here

# Mailbox settings
IMAP_MAILBOX=INBOX

# Polling interval (60 seconds = 1 minute)
IMAP_POLL_INTERVAL=60

# Attachment size limit (10MB = 10485760 bytes)
IMAP_MAX_ATTACHMENT_SIZE=10485760

# Allowed MIME types (whitelist)
IMAP_ALLOWED_MIME_TYPES=application/pdf,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document
```

#### Without SSL (Internal Networks - Port 143)

```bash
# .env file

# Enable email ingestion
IMAP_ENABLED=True

# IMAP server settings without SSL
IMAP_HOST=172.16.50.114
IMAP_PORT=143
IMAP_USE_SSL=False
IMAP_USERNAME=afmostrag
IMAP_PASSWORD=your_password_here

# Mailbox settings
IMAP_MAILBOX=INBOX
IMAP_POLL_INTERVAL=60
```

### Important Notes

- **Never commit passwords** to version control
- **Use environment variables** or secrets management
- **Restart the application** after changing `.env` configuration
- **Verify IMAP connectivity** after configuration changes
- **Monitor logs** for connection or processing errors

---

## MailEnable Setup

MailEnable is a Windows-based mail server commonly used with KnowledgeVault. This section covers configuring MailEnable for email ingestion.

### Prerequisites

- MailEnable Professional or Enterprise edition
- Admin access to MailEnable Management Console
- An existing mail server configured with SMTP/IMAP

### Step 1: Create Dedicated Email Account

1. Open **MailEnable Management Console**
2. Navigate to **Servers → localhost → Services and Connectors → MailEnable → Messaging Manager → Domains**
3. Right-click your domain → **New → Mailbox**
4. Create mailbox with these settings:
   - **Mailbox Name:** `knowledgevault` (or your preferred name)
   - **Email Address:** `knowledgevault@yourdomain.com`
   - **Password:** Generate a strong password (minimum 16 characters, mixed case, numbers, symbols)
   - **Mailbox Size:** Set appropriate limit (e.g., 500MB or 1GB)

5. Click **OK** to create the mailbox

### Step 2: Configure IMAP Service

1. Navigate to **Servers → localhost → Services and Connectors → MailEnable → Messaging Manager → Services and Connectors → IMAP**
2. Ensure **IMAP Service Status** is **Running**
3. Right-click **IMAP** → **Properties**
4. Configure SSL settings:
   - **Enable SSL:** Checked
   - **SSL Port:** 993
   - **SSL Certificate:** Select or import your SSL certificate
5. Click **Apply** and restart IMAP service if prompted

### Step 3: Test IMAP Connection

Use OpenSSL to verify IMAP connectivity:

**Windows PowerShell:**
```powershell
# Test IMAP SSL connection
Test-NetConnection -ComputerName mail.yourdomain.com -Port 993

# Alternative: Use Telnet (if SSL not required)
telnet mail.yourdomain.com 143
```

**Linux/Mac:**
```bash
# Test IMAP SSL connection
openssl s_client -connect mail.yourdomain.com:993 -crlf

# Manual IMAP commands after connection:
# . LOGIN knowledgevault@yourdomain.com your_password
# . LIST "" *
# . SELECT INBOX
# . SEARCH UNSEEN
# . LOGOUT
```

### Step 4: Configure MailEnable Security

1. **Enable IP Filtering** (optional, for added security)
   - Navigate to **IMAP → Access Control**
   - Add IP ranges that should access IMAP (e.g., your application server IP)
   - Deny all other IPs or set appropriate restrictions

2. **Configure Authentication**
   - Navigate to **IMAP → Authentication**
   - Ensure **Plain Authentication** is enabled (required for most clients)
   - Consider enabling **CRAM-MD5** for more secure auth (if supported by client)

3. **Set Resource Limits**
   - Navigate to **IMAP → Limits**
   - Set **Maximum Message Size:** 52428800 (50MB)
   - Set **Maximum Connections Per IP:** 5
   - Set **Connection Timeout:** 300 (5 minutes)

### Step 5: Update KnowledgeVault Configuration

Update your `.env` file with MailEnable settings:

```bash
IMAP_ENABLED=True
IMAP_HOST=mail.yourdomain.com
IMAP_PORT=993
IMAP_USERNAME=knowledgevault@yourdomain.com
IMAP_PASSWORD=<your_strong_password>
IMAP_MAILBOX=INBOX
IMAP_POLL_INTERVAL=60
```

### Step 6: Verify Email Delivery

1. Send a test email to `knowledgevault@yourdomain.com` with:
   - Subject: `[TestVault] Test document`
   - Attachment: A small PDF or TXT file

2. Check MailEnable logs:
   - Navigate to **Messaging Manager → Services → IMAP → Logs**
   - Verify login attempts from KnowledgeVault
   - Check for any authentication errors

3. Monitor KnowledgeVault logs:
   - Look for `INFO` messages about email processing
   - Verify attachments are being enqueued

### Common MailEnable Issues

| Issue | Solution |
|-------|----------|
| Authentication fails | Verify username/password; check for case sensitivity |
| Connection timeout | Check firewall rules; verify port 993 is open |
| SSL certificate error | Import valid SSL certificate or use self-signed cert with proper trust |
| IMAP service not starting | Check MailEnable service status in Windows Services |
| Mailbox full | Increase mailbox size or implement email cleanup policy |

---

## Usage Guide

### Sending Documents via Email

#### Basic Usage

To upload a document, simply send an email to your configured email address (e.g., `knowledgevault@yourdomain.com`):

**Email Format:**
```
To: knowledgevault@yourdomain.com
Subject: [MyVault] Important Contract Review
From: user@company.com
Attachment: contract.pdf
```

**What happens:**
1. KnowledgeVault receives the email via IMAP
2. Extracts `contract.pdf` attachment
3. Routes to vault named "MyVault" (or creates if doesn't exist)
4. Processes document through standard pipeline
5. Document becomes searchable in chat and search

#### Multiple Attachments

You can attach multiple documents in a single email:

```
To: knowledgevault@yourdomain.com
Subject: #projectx Q4 Reports
From: manager@company.com
Attachments:
- report_q4.pdf
- financials.xlsx
- summary.txt
- presentation.pptx
```

**Result:**
- `report_q4.pdf` → Processed ✓
- `financials.xlsx` → Processed ✓
- `summary.txt` → Processed ✓
- `presentation.pptx` → Processed ✓

#### Email Without Vault Tag

If no vault tag is specified in the subject:

```
To: knowledgevault@yourdomain.com
Subject: Meeting Notes from Jan 15
From: user@company.com
Attachment: meeting_notes.txt
```

**Result:**
- Document is rejected with error if vault cannot be resolved

#### Email With No Attachments

```
To: knowledgevault@yourdomain.com
Subject: Just checking in
From: user@company.com
Attachments: None
```

**Result:**
- Email is processed but no documents are added
- Logged as: "No valid document attachments found in email"
- No error, no data added

### Supported File Types

| Extension | MIME Type | Supported |
|-----------|-----------|-----------|
| `.pdf` | `application/pdf` | ✓ Yes |
| `.txt` | `text/plain` | ✓ Yes |
| `.md` | `text/markdown` | ✓ Yes |
| `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | ✓ Yes |
| `.csv` | `text/csv` | ✓ Yes |
| `.json` | `application/json` | ✓ Yes |
| `.sql` | `application/sql` | ✓ Yes |
| `.py` | `text/x-python` | ✓ Yes |
| `.js` | `application/javascript` | ✓ Yes |
| `.html` | `text/html` | ✓ Yes |
| `.css` | `text/css` | ✓ Yes |
| `.xml` | `application/xml` | ✓ Yes |
| `.yaml`/`.yml` | `application/x-yaml` | ✓ Yes |
| `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | ✓ Yes |
| `.pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | ✓ Yes |
| `.zip` | `application/zip` | ✗ No |
| `.jpg`/`.png` | Image types | ✗ No |

**Note:** To add support for additional file types:
1. Add the MIME type to `IMAP_ALLOWED_MIME_TYPES` in `.env`
2. Ensure the document processor supports parsing that type
3. Restart the application

### Best Practices

1. **Use descriptive subjects**
   - Include vault tags: `[HR]`, `[Engineering]`, `[Marketing]`
   - Add context: "Q4 Report", "Meeting Notes", "Contract Review"

2. **Organize by vault**
   - Create dedicated vaults for different departments/projects
   - Use consistent naming conventions
   - Example: `[Engineering] API Documentation`, `[Sales] Proposal Template`

3. **Batch related documents**
   - Attach multiple related files in a single email
   - Keep under 10 attachments per email
   - Use clear filenames: `contract_v2.pdf`, `meeting_notes_2025-01-15.txt`

4. **Monitor processing status**
   - Check `/api/email/status` endpoint regularly
   - Review logs for failed emails
   - Verify documents appear in search results

5. **Secure your email account**
   - Use strong passwords
   - Enable IP filtering on IMAP service
   - Rotate passwords periodically
   - Limit mailbox size to prevent overflow

---

## Vault Routing

### How Vault Routing Works

Vault routing uses subject line tags to determine which vault a document should be stored in. This enables automated organization of documents into logical categories.

### Tag Formats

Two formats are supported:

#### 1. Bracket Format `[VaultName]`

```
Subject: [Engineering] API Documentation v2.0
```

Extracts: `Engineering`

#### 2. Hashtag Format `#vaultname`

```
Subject: #marketing Campaign Assets
```

Extracts: `marketing`

### Tag Resolution Rules

1. **First match wins** - Only the first matching tag is used
2. **Case-insensitive** - `[Engineering]`, `[engineering]`, `[ENGINEERING]` all resolve to the same vault
3. **Whitespace trimmed** - `[ Engineering ]` resolves to `Engineering`
4. **Case-sensitive vault names** - Vault names in database are matched case-insensitively

### Resolution Flow

```
Email Subject: "[Engineering] #marketing API Documentation [HR] Employee Handbook"
                    ↓
              Extract [Engineering]
                    ↓
        Check if vault "Engineering" exists
                    ↓
           ┌─────────┴─────────┐
       YES │                 │ NO
           ↓                 ↓
    Use vault_id           Try #marketing tag
    from Engineering
           ↓                    ↓
    Document stored        Check if vault "marketing"
    in Engineering vault    exists
                                  ↓
                             ┌────┴────┐
                          YES │        │ NO
                              ↓        ↓
                       Use vault_id  Email rejected
                       from marketing  (no fallback)
```

### Examples

#### Example 1: Single Tag, Existing Vault

```
Subject: [Engineering] System Architecture Document
Vaults in database:
  - Engineering (id=2)
  - HR (id=3)
  - Default (id=1)

Result: Document → Engineering vault (id=2)
```

#### Example 2: Hashtag Format, Existing Vault

```
Subject: #marketing Q4 Campaign Strategy
Vaults in database:
  - Engineering (id=2)
  - Marketing (id=4)
  - Default (id=1)

Result: Document → Marketing vault (id=4)
```

#### Example 3: Non-Existent Vault

```
Subject: [Sales] January Sales Report
Vaults in database:
  - Engineering (id=2)
  - Marketing (id=4)
  - Default (id=1)

Result: Email rejected with error: vault not found
```

#### Example 4: Multiple Tags (First Match Wins)

```
Subject: [Engineering] #marketing #sales Cross-functional Report
Vaults in database:
  - Engineering (id=2)
  - Marketing (id=4)
  - Sales (id=5)
  - Default (id=1)

Result: Document → Engineering vault (id=2)
Note: Only [Engineering] is used; #marketing and #sales are ignored
```

#### Example 5: No Tags

```
Subject: Important Meeting Notes
Vaults in database:
  - Engineering (id=2)
  - Marketing (id=4)
  - Default (id=1)

Result: Document → Default vault (id=1)
```

### Creating Vaults

Vaults can be created via:
1. **Web UI:** Navigate to Vaults → Create New Vault
2. **API:** `POST /api/vaults` with `name` parameter
3. **Admin API:** Requires admin scope

Ensure vaults exist before using them in email tags. Non-existent vaults cause the email to be rejected.

### Vault Naming Best Practices

1. **Use short, memorable names**
   - ✓ `Engineering`, `Marketing`, `HR`
   - ✗ `Engineering and Development Team Docs`

2. **Avoid special characters**
   - ✓ `[HR]`, `[Engineering]`
   - ✗ `[HR-Dept!]`, `[Eng/Dev]`

3. **Use consistent casing**
   - Choose either `Engineering` or `engineering` and stick with it
   - Resolution is case-insensitive, but consistency helps users

4. **Avoid spaces in tags** (brackets handle this automatically)
   - ✓ `[Engineering]`
   - ✗ `[Engineering Department]` (works but harder to type)

---

## Security Considerations

### Size Limits

| Resource | Limit | Reason |
|----------|-------|--------|
| Email size | 50 MB | Prevents DoS attacks, reduces processing time |
| Attachment size | 10 MB | Limits memory usage, ensures timely processing |
| Attachments per email | 10 | Prevents abuse, ensures manageable batch sizes |
| Polling interval | 30-300 seconds | Balances responsiveness with server load |

### MIME Type Filtering

**Whitelist Approach:** Only explicitly allowed MIME types are processed.

**Why whitelist?**
- Prevents execution of malicious files
- Blocks unwanted file types (executables, images, archives)
- Ensures only document types are indexed

**Allowed types:**
- Documents: PDF, DOCX, TXT, MD, CSV, JSON, SQL, XML, YAML
- Code: Python, JavaScript
- Markup: HTML, CSS

**Blocked types:**
- Executables: EXE, MSI, DMG, APK
- Archives: ZIP, RAR, TAR, 7Z
- Images: JPG, PNG, GIF, SVG
- Audio/Video: MP3, MP4, AVI, WAV
- Scripts (dangerous): BAT, SH, PS1, VBS

### Path Traversal Prevention

**Sanitization:** All filenames are sanitized using `os.path.basename()` before saving.

**Example attack:**
```python
# Malicious filename
filename = "../../../etc/passwd"

# After sanitization
filename = "passwd"  # Safe!
```

**Prevents:**
- Overwriting system files
- Accessing directories outside uploads folder
- Directory traversal attacks

### Log Injection Prevention

**Sanitization:** All values logged are sanitized to remove newlines and control characters.

**Why:** Prevents malicious email content from injecting fake log entries.

**Example:**
```python
# Malicious subject
subject = "Valid text\nERROR: Fake error message\nMore valid text"

# After sanitization
subject = "Valid text ERROR: Fake error message More valid text"
```

### HTML Sanitization

**Tool:** Uses `bleach` library for HTML sanitization.

**Allowed tags:** `p`, `br`, `strong`, `em`, `ul`, `ol`, `li`

**Stripped:**
- JavaScript (`<script>`)
- Iframes (`<iframe>`)
- On-event handlers (`onclick`, `onload`)
- Style attributes (`style="..."`)
- All other tags

**Example:**
```html
<!-- Input HTML -->
<p>Safe text <script>alert('xss')</script> <a href="javascript:alert('xss')">link</a></p>

<!-- Sanitized output -->
<p>Safe text  link</p>
```

### Authentication & Authorization

**IMAP Authentication:**
- Uses username/password stored in `IMAP_PASSWORD` (SecretStr)
- Never logs passwords
- Fails authentication after 5+ connection attempts (permanent error)

**API Authentication (Status Endpoint):**
- Requires Bearer token: `Authorization: Bearer <admin_secret_token>`
- Requires admin scope: `X-Scopes: admin:config`
- Token validation via `secrets.compare_digest()` (timing-safe)

### Secure Defaults

**If not configured:**
- `IMAP_ENABLED=False` - Service disabled by default
- Empty host/username/password - Service fails to start gracefully
- No default IMAP settings - Prevents accidental exposure

**Best practices:**
1. Enable only after configuring IMAP settings
2. Use dedicated email account (not personal admin email)
3. Implement IP filtering on IMAP server
4. Rotate passwords regularly (quarterly)
5. Monitor for unusual activity (high email volumes, many failures)

### Data Privacy

**Metadata stored:**
- Email sender (from `From:` header)
- Email subject (decoded and sanitized)
- Original filename (sanitized)
- Processing timestamp

**Not stored:**
- Email body content (unless it's a document attachment)
- Email headers (other than subject/sender)
- Recipient lists (To, Cc, Bcc)
- Reply-to addresses

**Note:** Document attachments are processed and their content is indexed in the vector store, but email body text is not processed separately.

---

## Status Endpoint

### Endpoint Details

**URL:** `GET /api/email/status`

**Authentication Required:** Yes
- Header: `Authorization: Bearer <admin_secret_token>`
- Header: `X-Scopes: admin:config`

### Response Schema

```json
{
  "enabled": true,
  "healthy": true,
  "last_poll": "2025-02-19T10:30:45.123456",
  "emails_processed_today": 15,
  "emails_failed_today": 2,
  "unseen_emails": 3,
  "current_backoff_delay": null
}
```

### Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Whether email ingestion is enabled via `IMAP_ENABLED` setting |
| `healthy` | boolean | Whether service is running without recent errors |
| `last_poll` | string/null | ISO timestamp of last successful poll, or `null` if never polled |
| `emails_processed_today` | integer | Number of documents successfully processed today |
| `emails_failed_today` | integer | Number of emails with processing errors today |
| `unseen_emails` | integer | Number of UNSEEN emails in IMAP inbox (`-1` if query failed) |
| `current_backoff_delay` | integer/null | Current backoff delay in seconds, or `null` if connected |

### Making Requests

**Using curl:**
```bash
curl -X GET http://localhost:8080/api/email/status \
  -H "Authorization: Bearer your_admin_secret_token" \
  -H "X-Scopes: admin:config"
```

**Using PowerShell:**
```powershell
$headers = @{
  "Authorization" = "Bearer your_admin_secret_token"
  "X-Scopes" = "admin:config"
}

$response = Invoke-WebRequest -Uri "http://localhost:8080/api/email/status" -Headers $headers
$response.Content | ConvertFrom-Json
```

**Using Python:**
```python
import requests

headers = {
    "Authorization": "Bearer your_admin_secret_token",
    "X-Scopes": "admin:config"
}

response = requests.get("http://localhost:8080/api/email/status", headers=headers)
status = response.json()
print(f"Healthy: {status['healthy']}")
print(f"Last poll: {status['last_poll']}")
print(f"Processed today: {status['emails_processed_today']}")
```

### Interpreting Responses

#### Healthy Service

```json
{
  "enabled": true,
  "healthy": true,
  "last_poll": "2025-02-19T10:30:45.123456",
  "emails_processed_today": 15,
  "emails_failed_today": 2,
  "unseen_emails": 3,
  "current_backoff_delay": null
}
```

**Indicates:**
- Service is enabled and running
- Last poll was successful (recent timestamp)
- Processing documents normally
- Some failures today (check logs for details)
- 3 unseen emails awaiting processing
- No backoff delay (connected to IMAP)

#### Service with Backoff

```json
{
  "enabled": true,
  "healthy": false,
  "last_poll": "2025-02-19T09:45:00.000000",
  "emails_processed_today": 8,
  "emails_failed_today": 0,
  "unseen_emails": 0,
  "current_backoff_delay": 30
}
```

**Indicates:**
- Service is enabled but unhealthy
- Last poll was 45+ minutes ago
- Experiencing connection issues (backoff delay active)
- Currently waiting 30 seconds before retry
- Check IMAP server connectivity and logs

#### Disabled Service

```json
{
  "enabled": false,
  "healthy": false,
  "last_poll": null,
  "emails_processed_today": 0,
  "emails_failed_today": 0,
  "unseen_emails": 0,
  "current_backoff_delay": null
}
```

**Indicates:**
- Email ingestion is disabled
- Service not running
- No processing activity
- Enable by setting `IMAP_ENABLED=True` and restarting

#### IMAP Connection Failed

```json
{
  "enabled": true,
  "healthy": false,
  "last_poll": "2025-02-19T09:00:00.000000",
  "emails_processed_today": 5,
  "emails_failed_today": 0,
  "unseen_emails": -1,
  "current_backoff_delay": 60
}
```

**Indicates:**
- Service enabled but unhealthy
- Cannot query IMAP for unseen emails (-1)
- Maximum backoff delay (60s) - persistent connection issue
- Check IMAP credentials, server availability, and network

### Monitoring Best Practices

1. **Check status regularly**
   - Every 5 minutes via automated monitoring
   - Alert if `healthy=false` for >5 minutes
   - Alert if `last_poll` > 5 minutes old

2. **Track metrics**
   - `emails_processed_today` - Daily volume
   - `emails_failed_today` - Error rate
   - Alert if failure rate > 10%

3. **Monitor backoff delays**
   - `current_backoff_delay` - Connection health
   - Alert if backoff > 30s for >5 minutes
   - Indicates IMAP server issues

4. **Correlate with logs**
   - When status shows unhealthy, check logs
   - Look for authentication errors, connection timeouts
   - Review IMAP server logs for denied connections

---

## Troubleshooting

### Common Issues and Solutions

#### Issue 1: Emails Not Being Processed

**Symptoms:**
- `last_poll` is `null` or very old
- `emails_processed_today` = 0
- Unseen emails in IMAP inbox

**Possible Causes:**

1. **Service not enabled**
   - Check: `IMAP_ENABLED=True` in `.env`
   - Solution: Enable and restart application

2. **IMAP credentials incorrect**
   - Check logs for: `"IMAP authentication failed"`
   - Solution: Verify username/password in `.env`

3. **IMAP server unreachable**
   - Check: Can you connect via Telnet/OpenSSL?
   - Solution: Verify network connectivity, firewall rules

4. **Service crashed**
   - Check logs for errors
   - Solution: Fix configuration error and restart

**Troubleshooting Steps:**
```bash
# 1. Check if service is enabled
grep IMAP_ENABLED .env

# 2. Check status endpoint
curl -H "Authorization: Bearer <token>" \
     -H "X-Scopes: admin:config" \
     http://localhost:8080/api/email/status

# 3. Test IMAP connection manually
openssl s_client -connect mail.yourdomain.com:993

# 4. Check logs for errors
tail -f /var/log/knowledgevault/app.log | grep -i email
```

---

#### Issue 2: Attachments Not Being Processed

**Symptoms:**
- Emails are received (incrementing processed count)
- Documents don't appear in search results
- Attachments being skipped

**Possible Causes:**

1. **MIME type not allowed**
   - Check logs for: `"Disallowed MIME type"`
   - Solution: Add MIME type to `IMAP_ALLOWED_MIME_TYPES`

2. **Attachment too large**
   - Check logs for: `"File too large"`
   - Solution: Increase `IMAP_MAX_ATTACHMENT_SIZE` or send smaller files

3. **Attachment limit exceeded**
   - Check logs for: `"more than 10 attachments"`
   - Solution: Send fewer attachments per email or increase limit

4. **File parsing error**
   - Check logs for processing errors
   - Solution: Verify file is valid and not corrupted

**Troubleshooting Steps:**
```bash
# 1. Check allowed MIME types
grep IMAP_ALLOWED_MIME_TYPES .env

# 2. Check attachment size limit
grep IMAP_MAX_ATTACHMENT_SIZE .env

# 3. Look for skipped attachments in logs
grep -i "skipping attachment" /var/log/knowledgevault/app.log

# 4. Verify file was enqueued
sqlite3 /data/knowledgevault/app.db "SELECT id, filename, source, status FROM files WHERE source='email' ORDER BY created_at DESC LIMIT 10;"
```

---

#### Issue 3: Vault Routing Not Working

**Symptoms:**
- Emails with unresolvable vault tags are rejected with ValueError

**Possible Causes:**

1. **Vault doesn't exist**
   - Check if vault exists in database
   - Solution: Create vault via UI or API

2. **Tag format incorrect**
   - Subject line missing `[VaultName]` or `#vaultname`
   - Solution: Use correct tag format

3. **Case mismatch** (should not happen, but double-check)
   - Resolution is case-insensitive
   - Solution: Vault names are matched case-insensitively

**Troubleshooting Steps:**
```bash
# 1. List all vaults
sqlite3 /data/knowledgevault/app.db "SELECT id, name FROM vaults;"

# 2. Check recent email uploads
sqlite3 /data/knowledgevault/app.db "SELECT id, filename, vault_id, email_subject FROM files WHERE source='email' ORDER BY created_at DESC LIMIT 5;"

# 3. Look for vault resolution warnings
grep -i "vault.*not found" /var/log/knowledgevault/app.log
```

---

#### Issue 4: High Failure Rate

**Symptoms:**
- `emails_failed_today` is high (>10% of processed)
- Many errors in logs

**Possible Causes:**

1. **Invalid file formats**
   - Users sending unsupported file types
   - Solution: Communicate allowed file types to users

2. **Corrupted attachments**
   - Files损坏 during email transmission
   - Solution: Ask users to resend files

3. **IMAP connection instability**
   - Network issues, server restarts
   - Solution: Check network, monitor IMAP server health

4. **Background processor overloaded**
   - Too many emails at once
   - Solution: Reduce polling interval or increase processing capacity

**Troubleshooting Steps:**
```bash
# 1. Check error rate
curl -s -H "Authorization: Bearer <token>" \
     -H "X-Scopes: admin:config" \
     http://localhost:8080/api/email/status | jq '{processed: .emails_processed_today, failed: .emails_failed_today}'

# 2. Check failed files in database
sqlite3 /data/knowledgevault/app.db "SELECT id, filename, error_message FROM files WHERE status='error' AND source='email' ORDER BY created_at DESC LIMIT 10;"

# 3. Review error logs
grep -i "error" /var/log/knowledgevault/app.log | grep -i email | tail -20
```

---

#### Issue 5: IMAP Authentication Fails

**Symptoms:**
- Logs: `"IMAP authentication failed: check username/password"`
- Service unhealthy with permanent error

**Possible Causes:**

1. **Incorrect username**
   - Typos in email address
   - Wrong format (needs full email address)
   - Solution: Verify `IMAP_USERNAME`

2. **Incorrect password**
   - Wrong password or expired
   - Special characters not properly escaped
   - Solution: Update `IMAP_PASSWORD`

3. **Account locked or disabled**
   - Account disabled in MailEnable
   - Too many failed login attempts
   - Solution: Check MailEnable admin console

4. **SSL/TLS mismatch**
   - MailEnable using SSL but client connecting plain
   - Solution: Verify `IMAP_PORT=993` for SSL

**Troubleshooting Steps:**
```bash
# 1. Test credentials manually
openssl s_client -connect mail.yourdomain.com:993 -crlf
# Then enter IMAP commands manually:
# . LOGIN knowledgevault@yourdomain.com your_password
# . LOGOUT

# 2. Check MailEnable logs
# In MailEnable Management Console:
# Navigate to IMAP → Logs → View current logs

# 3. Verify account is active
# In MailEnable Management Console:
# Messaging Manager → Domains → yourdomain.com → Mailboxes → knowledgevault
# Check account status and settings
```

---

#### Issue 6: Polling Delay / Slow Processing

**Symptoms:**
- Emails take a long time to appear in search
- `last_poll` timestamp is old
- High `current_backoff_delay`

**Possible Causes:**

1. **Polling interval too long**
   - Default is 60 seconds
   - Solution: Reduce `IMAP_POLL_INTERVAL` (e.g., 30 seconds)

2. **Network latency**
   - Slow connection to IMAP server
   - Solution: Optimize network, check firewall

3. **Server overload**
   - Too many emails queued for processing
   - Solution: Reduce email volume, increase resources

4. **IMAP server slow**
   - MailEnable overloaded
   - Solution: Optimize MailEnable, add resources

**Troubleshooting Steps:**
```bash
# 1. Check polling interval
grep IMAP_POLL_INTERVAL .env

# 2. Check current backoff delay
curl -s -H "Authorization: Bearer <token>" \
     -H "X-Scopes: admin:config" \
     http://localhost:8080/api/email/status | jq '.current_backoff_delay'

# 3. Monitor processing time
grep "Processing email" /var/log/knowledgevault/app.log | tail -10

# 4. Check IMAP response time
time openssl s_client -connect mail.yourdomain.com:993 -crlf </dev/null
```

---

### Getting Help

If you encounter issues not covered here:

1. **Check logs first**
   ```bash
   tail -100 /var/log/knowledgevault/app.log
   ```

2. **Verify configuration**
   ```bash
   grep IMAP_ .env
   ```

3. **Test IMAP manually**
   - Use OpenSSL or Telnet to verify connectivity
   - Try logging in with email client (Outlook, Thunderbird)

4. **Check status endpoint**
   - Review all fields for clues about service health

5. **Review database**
   - Check `files` table for recent email uploads
   - Verify `vaults` table has expected vaults

---

## Examples

### Example 1: Engineering Team Document Upload

**Scenario:** Engineering team needs to upload API documentation and code examples to the Engineering vault.

**Email:**
```
To: knowledgevault@yourdomain.com
From: dev-lead@company.com
Subject: [Engineering] API Documentation v2.1 - Code Examples

Attachments:
- api_overview.pdf
- authentication_guide.docx
- python_client_example.py
- javascript_client_example.js
```

**Result:**
- 4 documents processed
- All routed to Engineering vault
- Documents become searchable via chat
- Source metadata: `email`, `dev-lead@company.com`, `[Engineering] API Documentation v2.1 - Code Examples`

**Query Example:**
```
User: "How do I authenticate with the Python client?"
Response: "Based on the Python client example, you authenticate by..."
```

---

### Example 2: HR Policy Updates

**Scenario:** HR sends updated policy documents to multiple vaults.

**Email 1:**
```
To: knowledgevault@yourdomain.com
From: hr-manager@company.com
Subject: [HR] Employee Handbook 2025 Update

Attachments:
- employee_handbook_2025.pdf
- remote_work_policy.pdf
- benefits_summary.pdf
```

**Email 2:**
```
To: knowledgevault@yourdomain.com
From: hr-manager@company.com.com
Subject: [Engineering] Engineering PTO Policy 2025

Attachments:
- eng_pto_policy.pdf
```

**Result:**
- Email 1: 3 documents → HR vault
- Email 2: 1 document → Engineering vault
- Different routing based on subject tags
- Same sender, different recipients (vaults)

---

### Example 3: Marketing Campaign Assets

**Scenario:** Marketing team uploads campaign materials using hashtag format.

**Email:**
```
To: knowledgevault@yourdomain.com
From: creative-director@company.com
Subject: #marketing Q1 2025 Campaign - Brand Guidelines

Attachments:
- brand_guidelines.pdf
- logo_usage.pdf
- color_palette.csv
- tagline_suggestions.txt
```

**Result:**
- 4 documents → Marketing vault (via `#marketing` tag)
- Various file types processed
- All become searchable in Marketing vault

---

### Example 4: Automated Document Forwarding

**Scenario:** An external vendor sends documents to a generic email address, which is automatically forwarded to KnowledgeVault via MailEnable forwarding rules.

**MailEnable Setup:**
1. Create forwarding rule in MailEnable
2. Forward emails from `vendor-docs@yourdomain.com` to `knowledgevault@yourdomain.com`
3. Apply subject tag transformation (if supported) or manual tagging

**Email from Vendor:**
```
To: vendor-docs@yourdomain.com
From: vendor@external.com
Subject: Contract Draft v3

Attachments:
- contract_draft_v3.pdf
```

**Forwarded Email (after rule):**
```
To: knowledgevault@yourdomain.com
From: vendor-docs@yourdomain.com
Subject: [VendorContracts] Contract Draft v3

Attachments:
- contract_draft_v3.pdf
```

**Result:**
- Document automatically routed to VendorContracts vault
- Original sender preserved (`vendor-docs@yourdomain.com`)
- Subject tag added by forwarding rule

---

### Example 5: Bulk Upload from Meeting Notes

**Scenario:** After a team meeting, attendees collectively upload meeting notes and action items.

**Email 1 (from Manager):**
```
To: knowledgevault@yourdomain.com
From: manager@company.com
Subject: [Engineering] Sprint Planning Notes - Feb 19

Attachments:
- sprint_notes_feb19.pdf
- action_items.txt
```

**Email 2 (from Dev):**
```
To: knowledgevault@yourdomain.com
From: dev@company.com
Subject: [Engineering] Technical Debt Assessment

Attachments:
- technical_debt_assessment.pdf
- refactoring_proposal.md
```

**Email 3 (from QA):**
```
To: knowledgevault@yourdomain.com
From: qa@company.com
Subject: [Engineering] Test Plan for Sprint 23

Attachments:
- test_plan_sprint23.pdf
- test_cases.csv
```

**Result:**
- 6 documents across 3 emails
- All routed to Engineering vault
- Multiple contributors (manager, dev, qa)
- Documents searchable collectively

**Query Example:**
```
User: "What are the action items from the Feb 19 sprint planning?"
Response: "Based on the sprint notes, the action items are..."
```

---

### Example 6: Incorrect Tag Format (Fallback to Default)

**Scenario:** User sends email without vault tag or with incorrect format.

**Email:**
```
To: knowledgevault@yourdomain.com
From: user@company.com
Subject: Important Meeting Notes

Attachments:
- notes.txt
```

**Result:**
- Email rejected: vault tag must resolve to an existing vault
- Log: `"ValueError: Vault tag not found - email rejected"`

**Correction:** User should use correct tag format:
```
Subject: [Engineering] Important Meeting Notes
# or
Subject: #engineering Important Meeting Notes
```

---

### Example 7: Unsupported File Type (Skipped)

**Scenario:** User tries to upload unsupported file type.

**Email:**
```
To: knowledgevault@yourdomain.com
From: user@company.com
Subject: [Engineering] Project Screenshots

Attachments:
- screenshot1.jpg
- screenshot2.png
- project_specs.pdf
```

**Result:**
- `screenshot1.jpg` → Skipped (MIME type not allowed)
- `screenshot2.png` → Skipped (MIME type not allowed)
- `project_specs.pdf` → Processed ✓
- Log: `"Processed 1 attachment(s) from email from user@company.com"`
- Log: `"Skipping attachment: Disallowed MIME type: image/jpeg"` (×2)

**Solution:** Only upload supported file types, or add image support to configuration.

---

### Example 8: Large Email (Rejected)

**Scenario:** User sends email with large attachment.

**Email:**
```
To: knowledgevault@yourdomain.com
From: user@company.com
Subject: [Engineering] Large Dataset

Attachments:
- large_dataset.zip (45 MB)
```

**Result:**
- Email rejected
- Log: `"Email UID 1234 too large (45.00MB > 50.00MB), skipping"`
- No documents processed

**Solution:** Split file into smaller parts or reduce file size.

---

## Additional Resources

- **Admin Guide:** `/docs/admin-guide.md` - Administrative tasks and configuration
- **Release Notes:** `/docs/release.md` - Deployment and maintenance procedures
- **API Documentation:** Interactive API docs at `/docs` endpoint
- **MailEnable Documentation:** https://www.mailenable.com/documentation/

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2025-02-19 | Initial email ingestion feature documentation |

---

**Last Updated:** 2025-02-19
