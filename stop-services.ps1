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
# The marker names the GROUP LEADER the launcher spawned, with its
# wall-clock start stamp. On Windows a venv's Scripts\python.exe is a
# REDIRECTOR that starts the real interpreter as a CHILD, so the process that
# owns the backend port is a DESCENDANT of the marked process. Both the
# marker match and the CTRL_BREAK target depend on that distinction.
function Get-BackendGroupMarker {
    $markerPath = Join-Path $RepoRoot ".cache\backend.pid"
    if (-not (Test-Path -LiteralPath $markerPath)) { return $null }
    try {
        $parts = (Get-Content -LiteralPath $markerPath -ErrorAction Stop) -split '\s+'
        $markedPid = [int]$parts[0]
        $markedTicks = [math]::Floor([double]$parts[1])
    } catch { return $null }
    if ($markedPid -le 0) { return $null }
    return @{ Pid = $markedPid; Ticks = $markedTicks }
}

function Test-ProcessDescendsFrom {
    # True when $ChildPid is $AncestorPid or one of its descendants (bounded
    # walk up the parent chain). Used for the venv-redirector case: the port
    # owner is the redirector's child.
    param([int]$ChildPid, [int]$AncestorPid, [int]$MaxDepth = 8)
    $current = $ChildPid
    for ($i = 0; $i -lt $MaxDepth; $i++) {
        if ($current -eq $AncestorPid) { return $true }
        $info = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $current" -ErrorAction SilentlyContinue
        if ($null -eq $info -or $null -eq $info.ParentProcessId) { return $false }
        $parent = [int]$info.ParentProcessId
        if ($parent -le 0 -or $parent -eq $current) { return $false }
        $current = $parent
    }
    return $false
}

function Stop-BackendProcessGracefully {
    param($Proc, [string]$Label)
    $grouped = $false
    $groupPid = 0
    $marker = Get-BackendGroupMarker
    if ($null -ne $marker) {
        # The port owner is the marked process itself, or (a venv
        # redirector) its DESCENDANT.
        if ($marker.Pid -eq $Proc.Id -or (Test-ProcessDescendsFrom $Proc.Id $marker.Pid)) {
            $markedProc = Get-Process -Id $marker.Pid -ErrorAction SilentlyContinue
            if ($null -ne $markedProc -and $null -ne $markedProc.StartTime) {
                # .NET FILETIME tolerance: the wrapper stamps wall-clock at
                # spawn; allow 60 s of skew. A reused PID is minutes apart.
                # The check is on the MARKED process (the group leader),
                # which is the process the wrapper stamped.
                $liveTicks = [math]::Floor($markedProc.StartTime.ToFileTime())
                if ([math]::Abs($liveTicks - $marker.Ticks) -lt 600000000) {
                    $grouped = $true
                    $groupPid = $marker.Pid
                }
            }
        }
    }
    if ($grouped) {
        # Sacrificial sender: scripts/win_send_ctrl_break.py detaches ITS OWN
        # console, attaches to the backend's, sends the targeted CTRL_BREAK,
        # and exits — the parent script's console is never touched.
        $Sender = Join-Path $RepoRoot "scripts\win_send_ctrl_break.py"
        $SenderVenvPy = Join-Path $RepoRoot "backend\venv\Scripts\python.exe"
        $SenderPy = if ($env:BACKEND_PYTHON) { $env:BACKEND_PYTHON } elseif (Test-Path $SenderVenvPy) { $SenderVenvPy } else { "python" }
        # The CTRL_BREAK goes to the GROUP id (the group leader the marker
        # names) -- GenerateConsoleCtrlEvent's group argument must be the
        # group leader, and with a venv that is the redirector, not the port
        # owner.
        # The sender path is path-valued: quote it (Windows PowerShell 5.1
        # splits an unquoted -ArgumentList element on spaces). -Wait -PassThru
        # lets the script SEE the sender's exit status, so a failed attach or
        # a failed CTRL_BREAK is reported instead of silently waiting out the
        # whole graceful window.
        # The quoted path and the pid must be SEPARATE array elements:
        # `@('"{0}"' -f $Sender, "$groupPid")` makes the pid a FORMAT
        # OPERAND, so the array collapses to one element and the sender
        # never receives the pid (verified with a PowerShell probe).
        $QuotedSender = '"{0}"' -f $Sender
        $SenderProc = Start-Process $SenderPy -ArgumentList @($QuotedSender, "$groupPid") -WindowStyle Hidden -Wait -PassThru
        $SenderFailed = $SenderProc.ExitCode -ne 0
        if ($SenderFailed) {
            Write-Output "  ${Label}: the CTRL_BREAK sender reported exit code $($SenderProc.ExitCode) (attach or event delivery failed); the signal was NOT delivered"
        }
        Write-Output "  ${Label}: sent graceful CTRL_BREAK to grouped backend (group $groupPid, owner PID $($Proc.Id)); waiting..."
        # 140 s > the composed shutdown envelope, so the force fallback
        # never fires before a graceful shutdown that stayed inside the
        # envelope's ENFORCED 100 s completes. The derivation, quoted
        # VERBATIM by docker-compose.yml, the lifespan module, the release
        # fragment and start-services.ps1:
        #   enforced: 20 s (uvicorn request drain) + 65 s (lifespan aggregate) + 15 s (turn-finalizer drain) = 100 s; allowance: 30 s (SQLite-family executor-join tail) + 5 s (teardown) = 35 s; total 135 s
        # scripts/check_p03_shutdown_envelope.py owns that arithmetic.
        # The two windows are NAMED: the envelope gate reads
        # $GracefulWindowMs (the force-stop deadline the envelope bounds)
        # and $FailedSignalWaitMs (a different path, below), so a literal
        # inside a WaitForExit call can never be mistaken for the other.
        $GracefulWindowMs = 140000
        # A FAILED signal delivery means no graceful shutdown was started:
        # waiting the whole 140 s graceful window buys nothing, so fall back
        # to the short bounded wait and then force-stop.
        $FailedSignalWaitMs = 15000
        $WaitMs = if ($SenderFailed) { $FailedSignalWaitMs } else { $GracefulWindowMs }
        if (-not $Proc.WaitForExit($WaitMs)) {
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
        [string]$Label,
        [switch]$Graceful
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
            if ($Graceful) {
                Stop-BackendProcessGracefully $proc "$Port ($Label)"
            } else {
                # Only the backend is a signal-handling console app; the
                # frontend/Ollama processes get the immediate force stop they
                # had at base (a bounded wait bought nothing for them).
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                Write-Output "  $Port ($Label): force-stopped (PID $($proc.Id))"
            }
        }
    }
}

Stop-ProcessOnPort $BackendPort "Backend" -Graceful
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
