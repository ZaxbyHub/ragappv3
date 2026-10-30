"""Issue #740 (AC15): scripts/check_config_contract.py is test-pinned.

The gate had zero test references — a regression that made it always print
"pass" would leave the Quality contracts job green. These tests pin the
real-repo happy path (exit 0) and a seeded cross-surface mismatch (exit 1).
"""

import contextlib
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_config_contract.py"




@contextlib.contextmanager
def _no_bytecode():
    """Keep spec_from_file_location loads from writing scripts/__pycache__
    into the repository working tree (the frozen siblings do the same)."""
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = old

def _load():
    spec = importlib.util.spec_from_file_location("check_config_contract_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with _no_bytecode():
        spec.loader.exec_module(mod)
    return mod


def test_config_contract_passes_on_repo():
    """Happy path: the real repository satisfies every config contract."""
    mod = _load()
    assert mod.main() == 0


def test_config_contract_fails_on_env_cors_mismatch(monkeypatch):
    """Seeded violation: a .env.example CORS default drifting from the backend
    default must fail the gate (exit 1) naming the surface."""
    mod = _load()
    real_read = mod.read

    def doctored_read(relative_path: str) -> str:
        text = real_read(relative_path)
        if relative_path == ".env.example":
            text = text.replace(
                "BACKEND_CORS_ORIGINS=", "BACKEND_CORS_ORIGINS=http://drifted.invalid,", 1
            )
        return text

    monkeypatch.setattr(mod, "read", doctored_read)
    rc = mod.main()
    assert rc == 1


def test_config_contract_fails_on_nonempty_model_default(monkeypatch):
    """Seeded violation: a shipped chat_model default must fail (the
    empty-model-defaults contract)."""
    mod = _load()
    real_read = mod.read

    def doctored_read(relative_path: str) -> str:
        text = real_read(relative_path)
        if relative_path == "backend/app/config.py":
            text = text.replace(
                'chat_model: str = ""', 'chat_model: str = "gpt-drift"', 1
            )
        return text

    monkeypatch.setattr(mod, "read", doctored_read)
    assert mod.main() == 1


def test_config_contract_failure_names_the_drifted_surface(monkeypatch, capsys):
    """PRR-007/TC-31 supplement: a seeded violation must fail naming the
    drifted surface — a bare rc check would pass on ANY unrelated failure."""
    mod = _load()
    real_read = mod.read

    def doctored_read(relative_path: str) -> str:
        text = real_read(relative_path)
        if relative_path == ".env.example":
            text = text.replace(
                "BACKEND_CORS_ORIGINS=", "BACKEND_CORS_ORIGINS=http://drifted.invalid,", 1
            )
        return text

    monkeypatch.setattr(mod, "read", doctored_read)
    rc = mod.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "BACKEND_CORS_ORIGINS" in err, (
        f"the failure output must name the drifted surface; got: {err!r}"
    )
