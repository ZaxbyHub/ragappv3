param(
    [string]$BackendPort = $(if ($env:PORT) { $env:PORT } else { "9090" }),
    [string]$FrontendPort = $(if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { "3000" }),
    [string]$OllamaPort = "11434",
    [string]$EmbeddingPort = $(if ($env:EMBEDDING_PORT) { $env:EMBEDDING_PORT } else { "8080" }),
    [string]$EmbeddingContainerName = $(if ($env:EMBEDDING_CONTAINER) { $env:EMBEDDING_CONTAINER } else { "harrier-embed" }),
    [switch]$StopOllama,
    [switch]$StopEmbedding
)

$ErrorActionPreference = "SilentlyContinue"

$RepoRoot = $PSScriptRoot

Write-Output "Stopping RAGv3 development services..."
Write-Output "  Stops backend/frontend ports by default (backend gracefully: CTRL_BREAK)."
Write-Output "  Pass -StopEmbedding to stop/remove the Harrier TEI container."
Write-Output "  Pass -StopOllama to stop Ollama."

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
        $SenderPy = if ($env:BACKEND_PYTHON) { $env:BACKEND_PYTHON } elseif (Test-Path (Join-Path $BackendDir "venv\Scripts\python.exe")) { Join-Path $BackendDir "venv\Scripts\python.exe" } else { "python" }
        Start-Process $SenderPy -ArgumentList ($Sender, "$($Proc.Id)") -WindowStyle Hidden | Out-Null
        Write-Output "  ${Label}: sent graceful CTRL_BREAK to grouped backend (PID $($Proc.Id)); waiting..."
        # 125 s >= the 115 s natural worst case of the shutdown envelope
        # (20 request drain + 60 lifespan aggregate + 30 join tail + 5
        # teardown; docker-compose.yml carries the derivation) so the force
        # fallback never fires before a healthy graceful shutdown completes.
        if (-not $Proc.WaitForExit(125000)) {
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
    param(
        [string]$Port,
        [string]$Label
    )

    $connections = Get-NetTCPConnection -LocalPort ([int]$Port) -ErrorAction SilentlyContinue
    if (-not $connections) {
        Write-Output "  $Port ($Label): already stopped"
        return
    }

    $seen = @{}
    foreach ($connection in $connections) {
        $proc = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) {
            if ($seen.ContainsKey($proc.Id)) { continue }
            $seen[$proc.Id] = $true
            Stop-BackendProcessGracefully $proc "$Port ($Label)"
        }
    }
}

Stop-ProcessOnPort $BackendPort "Backend"
Stop-ProcessOnPort $FrontendPort "Frontend"

if ($StopEmbedding) {
    docker stop $EmbeddingContainerName 2>&1 | Out-Null
    docker rm $EmbeddingContainerName 2>&1 | Out-Null
} elseif (Get-NetTCPConnection -LocalPort ([int]$EmbeddingPort) -ErrorAction SilentlyContinue) {
    Write-Output "  $EmbeddingPort (Harrier TEI): still running; pass -StopEmbedding to stop container $EmbeddingContainerName"
}

if ($StopOllama) {
    Stop-ProcessOnPort $OllamaPort "Ollama"
} elseif (Get-NetTCPConnection -LocalPort ([int]$OllamaPort) -ErrorAction SilentlyContinue) {
    Write-Output "  $OllamaPort (Ollama): still running; pass -StopOllama to stop it"
}

Start-Sleep -Seconds 1

$remaining = @{
    $BackendPort = "Backend"
    $FrontendPort = "Frontend"
}

foreach ($port in $remaining.Keys) {
    if (Get-NetTCPConnection -LocalPort ([int]$port) -ErrorAction SilentlyContinue) {
        Write-Output "  WARNING: Port $port ($($remaining[$port])) is still in use"
    }
}
