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

function Stop-ProcessOnPort {
    param([string]$Port)
    $connections = Get-NetTCPConnection -LocalPort ([int]$Port) -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        $proc = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
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
    $embeddingExists = docker ps --format "{{.Names}}" 2>&1 | Select-String -SimpleMatch $EmbeddingContainerName
    if (-not $embeddingExists) {
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
        docker @dockerArgs 2>&1 | Out-Null
    } else {
        Write-Output "$EmbeddingContainerName container already running"
    }
}

Start-Process $BackendPython `
    -ArgumentList "-m","uvicorn","app.main:app","--host","0.0.0.0","--port",$BackendPort `
    -WorkingDirectory $BackendDir `
    -WindowStyle Hidden

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
        Write-Output "NOTE: $EmbeddingContainerName may still be loading models."
    }
} else {
    Write-Output "WARNING: Some services failed to start"
}
