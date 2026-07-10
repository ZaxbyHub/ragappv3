$ErrorActionPreference = 'Stop'

# Locate Python 3.11
$python311 = $null
if (Test-Path 'C:\Python311\python.exe') {
    $python311 = 'C:\Python311\python.exe'
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python311 = (py -3.11 -c "import sys; print(sys.executable)")
}
if (-not $python311) {
    Write-Error "ERROR: Python 3.11 is not installed. CI targets Python 3.11. Install Python 3.11 and ensure it is accessible."
    exit 1
}

# Install pip-tools if pip-compile is not available
if (-not (Get-Command pip-compile -ErrorAction SilentlyContinue)) {
    & $python311 -m pip install --upgrade pip-tools
}

# Generate production lockfile
& $python311 -m piptools compile backend/requirements.txt --output-file backend/requirements-lock.txt --generate-hashes --no-header --allow-unsafe --verbose

# Generate CI lockfile
& $python311 -m piptools compile backend/requirements-ci.txt --output-file backend/requirements-lock-ci.txt --generate-hashes --no-header --allow-unsafe --verbose
