param(
    [string]$RepoRoot = $PSScriptRoot,
    [string]$OllamaExe = $env:OLLAMA_EXE,
    [string]$BackendPython = $env:BACKEND_PYTHON,
    [string]$BackendPort = $(if ($env:PORT) { $env:PORT } else { "9090" }),
    [string]$FrontendPort = $(if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { "3000" }),
    [string]$EmbeddingDataDir = $(if ($env:EMBEDDING_DATA_DIR) { $env:EMBEDDING_DATA_DIR } else { (Join-Path $RepoRoot ".cache\harrier-embed") }),
    [string]$EmbeddingImage = $(if ($env:EMBEDDING_IMAGE) { $env:EMBEDDING_IMAGE } else { "ghcr.io/huggingface/text-embeddings-inference:cuda-latest" }),
    [string]$EmbeddingContainerName = $(if ($env:EMBEDDING_CONTAINER) { $env:EMBEDDING_CONTAINER } else { "harrier-embed" }),
    [string]$EmbeddingPort = $(if ($env:EMBEDDING_PORT) { $env:EMBEDDING_PORT } else { "8080" }),
    [string]$EmbeddingModel = $(if ($env:EMBEDDING_MODEL) { $env:EMBEDDING_MODEL } else { "microsoft/harrier-oss-v1-0.6b" }),
    [switch]$StopExisting,
    [switch]$SkipOllama,
    [switch]$SkipEmbedding
)

$ErrorActionPreference = "SilentlyContinue"

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"

if (-not $BackendPython) {
    $VenvPython = Join-Path $BackendDir "venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython) {
        $BackendPython = $VenvPython
    } else {
        $BackendPython = "python"
    }
}

if (-not $OllamaExe) {
    $OllamaCommand = Get-Command "ollama.exe" -ErrorAction SilentlyContinue
    if (-not $OllamaCommand) {
        $OllamaCommand = Get-Command "ollama" -ErrorAction SilentlyContinue
    }
    if ($OllamaCommand) {
        $OllamaExe = $OllamaCommand.Source
    }
}

function Test-LocalPort {
    param([string]$Port)
    return [bool](Get-NetTCPConnection -LocalPort ([int]$Port) -ErrorAction SilentlyContinue)
}

# Graceful stop (#799 AC4/#854): taskkill-without-/F and Stop-Process-without-
# -Force do NOT deliver a Python signal for console apps (CTRL_CLOSE_EVENT is
# unmapped by CPython), so the only sound native mechanism is a targeted
# CTRL_BREAK to the backend's process GROUP (created at launch with
# CREATE_NEW_PROCESS_GROUP): CPython maps it to SIGBREAK, uvicorn handles
# SIGBREAK, and the lifespan shutdown sequence runs. The signal dance runs in
# a SACRIFICIAL CHILD powershell (FreeConsole before AttachConsole, so the
# parent's console is never touched and there is nothing to restore) and only
# when the pid marker proves the owner is our grouped backend (pid + start
# time); anything else takes the bounded wait + force fallback.
function Stop-BackendProcessGracefully {
    param($Proc, [string]$Label)
    $markerPath = Join-Path $RepoRoot ".cache\backend.pid"
    $grouped = $false
    if (Test-Path -LiteralPath $markerPath) {
        try {
            $parts = (Get-Content -LiteralPath $markerPath -ErrorAction Stop) -split '\s+'
            $markedPid = [int]$parts[0]
            $markedTicks = [math]::Floor([double]$parts[1])
            if ($markedPid -eq $Proc.Id -and $null -ne $Proc.StartTime) {
                # .NET FILETIME tolerance: the wrapper stamps wall-clock at
                # spawn; allow 60 s of skew. A reused PID is minutes apart.
                $liveTicks = [math]::Floor($Proc.StartTime.ToFileTime())
                if ([math]::Abs($liveTicks - $markedTicks) -lt 600000000) {
                    $grouped = $true
                }
            }
        } catch { $grouped = $false }
    }
    if ($grouped) {
        # Sacrificial sender: scripts/win_send_ctrl_break.py detaches ITS OWN
        # console, attaches to the backend's, sends the targeted CTRL_BREAK,
        # and exits — the parent script's console is never touched.
        $Sender = Join-Path $RepoRoot "scripts\win_send_ctrl_break.py"
        $SenderVenvPy = Join-Path $RepoRoot "backend\venv\Scripts\python.exe"
        $SenderPy = if ($env:BACKEND_PYTHON) { $env:BACKEND_PYTHON } elseif (Test-Path $SenderVenvPy) { $SenderVenvPy } else { "python" }
        Start-Process $SenderPy -ArgumentList ($Sender, "$($Proc.Id)") -WindowStyle Hidden | Out-Null
        Write-Output "  ${Label}: sent graceful CTRL_BREAK to grouped backend (PID $($Proc.Id)); waiting..."
        # 140 s > the composed shutdown envelope, so the force fallback
        # never fires before a graceful shutdown that stayed inside the
        # envelope's ENFORCED 100 s completes. The derivation, quoted
        # VERBATIM by docker-compose.yml, the lifespan module, the release
        # fragment and stop-services.ps1:
        #   enforced: 20 s (uvicorn request drain) + 65 s (lifespan aggregate) + 15 s (turn-finalizer drain) = 100 s; allowance: 30 s (SQLite-family executor-join tail) + 5 s (teardown) = 35 s; total 135 s
        # scripts/check_p03_shutdown_envelope.py owns that arithmetic.
        if (-not $Proc.WaitForExit(140000)) {
            Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
            Write-Output "  ${Label}: graceful window expired; force-stopped (PID $($Proc.Id))"
        } else {
            Write-Output "  ${Label}: stopped gracefully (PID $($Proc.Id))"
        }
    } else {
        Write-Output "  ${Label}: no grouped-backend marker match (PID $($Proc.Id)); bounded wait + force"
        if (-not $Proc.WaitForExit(15000)) {
            Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
            Write-Output "  ${Label}: force-stopped (PID $($Proc.Id))"
        }
    }
}

function Stop-ProcessOnPort {
    param([string]$Port)
    $connections = Get-NetTCPConnection -LocalPort ([int]$Port) -ErrorAction SilentlyContinue
    $seen = @{}
    foreach ($connection in $connections) {
        $proc = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) {
            if ($seen.ContainsKey($proc.Id)) { continue }
            $seen[$proc.Id] = $true
            Stop-BackendProcessGracefully $proc "port $Port"
        }
    }
}

if ($StopExisting) {
    Stop-ProcessOnPort $BackendPort
    Stop-ProcessOnPort $FrontendPort
    Start-Sleep -Seconds 2
}

if (-not $SkipOllama) {
    if ($OllamaExe) {
        Start-Process $OllamaExe -ArgumentList "serve" -WindowStyle Hidden
    } else {
        Write-Output "Ollama executable not found. Set OLLAMA_EXE or pass -OllamaExe to start it from this script."
    }
}

if (-not $SkipEmbedding) {
    # Three-state model (issue #753): docker-compose.yml reuses this exact
    # container name, so after a host restart or `docker compose stop` the
    # container exists in EXITED state — a running-only probe misses it and
    # `docker run --name` then fails on the name conflict. Probe ALL
    # containers by exact name and carry the state.
    # Probe failure must not be mistaken for an absent container: a failed
    # docker query would otherwise fall through to `docker run --name` (review
    # ST-001/CS-002). Escape the configured name so a `.` in a valid Docker
    # name is matched literally rather than as a regex wildcard. Check for
    # the executable first: a missing docker must not leave $LASTEXITCODE
    # stale and print an empty "(exit )" message (Stage B test probe).
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Output "ERROR: docker executable not found; cannot probe or start $EmbeddingContainerName."
        exit 1
    }
    $embeddingState = docker ps -a --filter "name=^$([regex]::Escape($EmbeddingContainerName))$" --format "{{.State}}"
    if ($LASTEXITCODE -ne 0) {
        Write-Output "ERROR: could not query docker for $EmbeddingContainerName (exit $LASTEXITCODE); is the docker daemon available?"
        exit $LASTEXITCODE
    }
    $embeddingState = $embeddingState | Select-Object -First 1
    if ($embeddingState -eq "running") {
        Write-Output "$EmbeddingContainerName container already running"
    } elseif ($embeddingState -eq "paused") {
        # A paused container is RUNNING but suspended: docker start cannot
        # resume it ("You cannot start a paused container"); unpause is the
        # correct operation (implementation-review CR-1).
        Write-Output "Unpausing $EmbeddingContainerName container..."
        docker unpause $EmbeddingContainerName
        if ($LASTEXITCODE -ne 0) {
            Write-Output "ERROR: docker unpause $EmbeddingContainerName failed (exit $LASTEXITCODE). Run 'docker inspect $EmbeddingContainerName' for the cause."
            exit $LASTEXITCODE
        }
    } elseif ($embeddingState -in @("exited", "created", "restarting", "dead")) {
        Write-Output "Starting existing stopped $EmbeddingContainerName container..."
        docker start $EmbeddingContainerName
        if ($LASTEXITCODE -ne 0) {
            Write-Output "ERROR: docker start $EmbeddingContainerName failed (exit $LASTEXITCODE). Run 'docker start $EmbeddingContainerName' manually and inspect 'docker inspect $EmbeddingContainerName' for the cause."
            exit $LASTEXITCODE
        }
    } else {
        Write-Output "Starting $EmbeddingContainerName embedding container..."
        New-Item -ItemType Directory -Force -Path $EmbeddingDataDir | Out-Null
        $dockerArgs = @(
            "run", "-d",
            "--name", $EmbeddingContainerName,
            "--gpus", "all",
            "-p", "${EmbeddingPort}:8080",
            "-v", "${EmbeddingDataDir}:/data"
        )

        if ($env:HF_TOKEN) {
            $dockerArgs += @("-e", "HUGGING_FACE_HUB_TOKEN=$env:HF_TOKEN")
        }

        # --max-client-batch-size matches docker-compose.yml (128): TEI's own
        # default client batch cap is 32, while the app's default
        # EMBEDDING_BATCH_SIZE is 64 — without the flag, every ingestion batch
        # over 32 texts fails with a 422 on this launch path (issue #698).
        $dockerArgs += @($EmbeddingImage, "--model-id", $EmbeddingModel, "--port", "8080", "--max-batch-tokens", "16384", "--max-client-batch-size", "128")
        docker @dockerArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Output "ERROR: docker run for $EmbeddingContainerName failed (exit $LASTEXITCODE). A stopped container with this name existing is the usual cause - run 'docker ps -a --filter name=$EmbeddingContainerName' and 'docker start' it, or remove it first."
            exit $LASTEXITCODE
        }
    }
}

# Backend launch (#799 AC4/AC5, #854 envelope): the uvicorn args carry the
# integer request-drain bound (keep in sync with Dockerfile CMD and
# docker-compose.yml's derivation), and the process is started through the
# scripts/win_grouped_backend.py wrapper that spawns the backend with CREATE_NEW_PROCESS_GROUP so
# the uvicorn python becomes a process-GROUP leader (pgid == pid) sharing the
# wrapper's console — stop-services.ps1 then delivers a TARGETED
# CTRL_BREAK to that nonzero group id for a graceful native stop. The
# wrapper records the child pid + an approximate .NET FILETIME start stamp
# so the stop script can verify ownership (a reused PID mismatches the
# live process start time and takes the ungrouped fallback).
# RAGAPP_BOUNDED_EXIT=1 (inherited by the child only for this launch)
# enables the lifespan's bounded tail exit — same contract as the Dockerfile.
$BackendArgs = @("-m","uvicorn","app.main:app","--host","0.0.0.0","--port",$BackendPort,"--no-access-log","--timeout-graceful-shutdown","20")
$env:RAGAPP_BOUNDED_EXIT = "1"
$env:RAGAPP_MARKER_DIR = $RepoRoot
Start-Process $BackendPython `
    -ArgumentList (@((Join-Path $RepoRoot "scripts\win_grouped_backend.py"), $BackendPython) + $BackendArgs) `
    -WorkingDirectory $BackendDir `
    -WindowStyle Hidden
$env:RAGAPP_BOUNDED_EXIT = $null
$env:RAGAPP_MARKER_DIR = $null

# Use cmd /c to avoid npm .cmd being opened in Notepad by Start-Process.
Start-Process cmd `
    -ArgumentList "/c","npm run dev -- --host 0.0.0.0 --port $FrontendPort" `
    -WorkingDirectory $FrontendDir `
    -WindowStyle Hidden

Write-Output "Waiting for services to start..."
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 5
    $backend = Test-LocalPort $BackendPort
    $frontend = Test-LocalPort $FrontendPort
    if ($backend -and $frontend) { break }
    Write-Output "  Waiting... ($(($i + 1) * 5)s)"
}

$ollama = if (Test-LocalPort "11434") { "OK" } else { "DOWN" }
$backend = if (Test-LocalPort $BackendPort) { "OK" } else { "DOWN" }
$frontend = if (Test-LocalPort $FrontendPort) { "OK" } else { "DOWN" }
$embedding = if (Test-LocalPort $EmbeddingPort) { "OK" } else { "DOWN" }

Write-Output "=== Service Status ==="
Write-Output "  11434 (Ollama):      $ollama"
Write-Output "  $EmbeddingPort (Harrier TEI): $embedding"
Write-Output "  $BackendPort  (Backend):     $backend"
Write-Output "  $FrontendPort  (Frontend):    $frontend"

if ($ollama -eq "OK" -and $backend -eq "OK" -and $frontend -eq "OK") {
    Write-Output "ALL SERVICES RUNNING"
    if (-not $SkipEmbedding -and $embedding -ne "OK") {
        Write-Output "NOTE: $EmbeddingContainerName was started this run and may still be loading models; its container start/exit code was checked above (issue #753)."
    }
} else {
    Write-Output "WARNING: Some services failed to start"
}
