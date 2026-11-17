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

# Install uv if it is not available (the lock compiler since #567).
# Pinned to the same version CI pins (ci.yml: uv==0.12.15): the CI freshness
# gate byte-compares regeneration output, so a different uv version can
# produce spurious byte diffs.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found; installing the CI-pinned version (pip install uv==0.12.15)."
    & python -m pip install "uv==0.12.15"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "python -m pip install uv failed (exit $LASTEXITCODE); cannot compile lockfiles."
        exit $LASTEXITCODE
    }
}

# Generate production lockfile (universal, hash-pinned)
& uv pip compile --universal --generate-hashes --no-strip-extras --no-header `
    --python-version 3.11 backend/requirements.txt `
    --output-file backend/requirements-lock.txt
if ($LASTEXITCODE -ne 0) {
    Write-Error "uv pip compile failed for backend/requirements-lock.txt (exit $LASTEXITCODE)."
    exit $LASTEXITCODE
}

# Generate CI lockfile (universal, hash-pinned)
& uv pip compile --universal --generate-hashes --no-strip-extras --no-header `
    --python-version 3.11 backend/requirements-ci.txt `
    --output-file backend/requirements-lock-ci.txt
if ($LASTEXITCODE -ne 0) {
    Write-Error "uv pip compile failed for backend/requirements-lock-ci.txt (exit $LASTEXITCODE)."
    exit $LASTEXITCODE
}

Write-Host "Done. Diff the two lockfiles and justify every delta beyond markers/platform additions."
