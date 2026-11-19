$ErrorActionPreference = 'Stop'

# Regenerate both backend lockfiles as UNIVERSAL locks (issue #567 / E10):
# one lock per source spec that resolves on Linux and Windows. Seeding the
# output file with the committed lock keeps every pinned version; only
# markers and platform-conditional additions change. See
# docs/engineering/lockfiles.md for the full procedure.
#
# Native-command failure handling (issue #753 AC4): $ErrorActionPreference
# does NOT govern native exit codes in Windows PowerShell 5.1, so every
# native invocation is followed by an explicit $LASTEXITCODE check that stops
# the script with the real exit code instead of printing "Done." on failure.
# The failure message uses Write-Host, not Write-Error: under the Stop
# preference at line 1, Write-Error is terminating and would skip the `exit
# $code` that follows (review SP-001).

# Install uv if it is not available (the lock compiler since #567).
# Pinned to the same version CI pins (ci.yml: uv==0.12.15): the CI freshness
# gate byte-compares regeneration output, so a different uv version can
# produce spurious byte diffs.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found; installing the CI-pinned version (pip install uv==0.12.15)."
    & python -m pip install "uv==0.12.15"
    if ($LASTEXITCODE -ne 0) {
        $exitCode = $LASTEXITCODE
        Write-Host "ERROR: python -m pip install uv failed (exit $exitCode); cannot compile lockfiles."
        exit $exitCode
    }
}

# Generate production lockfile (universal, hash-pinned)
& uv pip compile --universal --generate-hashes --no-strip-extras --no-header `
    --python-version 3.11 backend/requirements.txt `
    --output-file backend/requirements-lock.txt
if ($LASTEXITCODE -ne 0) {
    $exitCode = $LASTEXITCODE
    Write-Host "ERROR: uv pip compile failed for backend/requirements-lock.txt (exit $exitCode)."
    exit $exitCode
}

# Generate CI lockfile (universal, hash-pinned)
& uv pip compile --universal --generate-hashes --no-strip-extras --no-header `
    --python-version 3.11 backend/requirements-ci.txt `
    --output-file backend/requirements-lock-ci.txt
if ($LASTEXITCODE -ne 0) {
    $exitCode = $LASTEXITCODE
    Write-Host "ERROR: uv pip compile failed for backend/requirements-lock-ci.txt (exit $exitCode)."
    exit $exitCode
}

Write-Host "Done. Diff the two lockfiles and justify every delta beyond markers/platform additions."
