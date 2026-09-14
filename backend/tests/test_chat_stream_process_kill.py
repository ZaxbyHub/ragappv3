"""kill -9 process-resume DoD test (issue #555, finding E01b, AC6).

The CONNECTION-DROP resume case is fully covered by test_chat_stream_replay.py
(producer outlives the HTTP connection; reconnect replays exactly the missed
frames). THIS test is the PROCESS-KILL case, which the issue explicitly gates
on I4 (#559, the DB-claimed job lease model): only when a generation can
outlive its own request does a killed server process resume a turn. While I4
has not landed, this test SKIPS — by design it never fails, and the skip
reason names the dependency (a runtime import probe of the lease-model
surface, not a hardcoded version check).

When I4 lands AND RAGAPP_KILL_DRILL=1 is set, the drill runs for real: it
spawns a server subprocess against a temp data dir with a scripted engine,
severs a stream mid-generation with a hard process kill, restarts, reconnects
sending the client's Last-Event-ID, and asserts the documented contract.
Until I4's out-of-request execution changes what a restart can deliver, the
drill asserts the CURRENT honest contract: the restarted process replays the
persisted frames after the client's id and NEVER fabricates a done marker —
an I4 landing that makes true process-kill resume possible must flip this
assertion to require completion (that flip belongs to the I4 change).

The hard-kill uses Process.kill(): SIGKILL-equivalent on POSIX;
TerminateProcess on Windows (this drill is opt-in and CI never sets the env).
"""
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _i4_job_lease_model_available() -> bool:
    try:
        importlib.import_module("app.services.job_leases")
    except ImportError:
        return False
    return True


_I4_LANDED = _i4_job_lease_model_available()

_REASON_I4 = (
    "I4 (#559) DB-claimed job lease model is not landed: a killed server "
    "process cannot resume a generation that only lives inside its request, "
    "so the process-kill resume claim is not made (connection-drop resume is "
    "covered by test_chat_stream_replay.py). This skip is the issue's own "
    "rollout contract for the kill -9 definition-of-done test."
)
_REASON_OPTIN = (
    "The kill/restart drill spawns real server subprocesses; it runs only "
    "when RAGAPP_KILL_DRILL=1 is set in the environment."
)


@pytest.mark.skipif(not _I4_LANDED, reason=_REASON_I4)
@pytest.mark.skipif(os.environ.get("RAGAPP_KILL_DRILL") != "1", reason=_REASON_OPTIN)
def test_process_kill_resume_drill():
    import httpx
    import uvicorn

    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    launcher = os.path.join(BACKEND_DIR, "tests", "_kill_drill_server.py")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = tempfile.mkdtemp(prefix="issue555-kill-drill-")
    env = dict(os.environ)
    env.update(
        ADMIN_SECRET_TOKEN="drill-admin-secret-token-0123456789",
        USERS_ENABLED="false",
        JWT_SECRET_KEY="drill-jwt-secret-key-for-testing-only-0123",
        DATA_DIR=data_dir,
        DRILL_PORT=str(port),
    )

    def _spawn():
        return subprocess.Popen(
            [sys.executable, launcher],
            cwd=BACKEND_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _wait_ready(client, deadline_s=30.0):
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                if client.get(f"{base}/api/health").status_code in (200, 503):
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("drill server did not become ready")

    turn_body = {
        "messages": [{"role": "user", "content": "What is the plan?"}],
        "vault_id": 1,
        "session_id": 1,
        "turn_id": "drill-kill-9-turn",
    }
    last_event_id = None
    process = _spawn()
    try:
        with httpx.Client(timeout=30.0) as client:
            _wait_ready(client)
            with client.stream(
                "POST", f"{base}/api/chat/stream", json=turn_body
            ) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if line.startswith("id:"):
                        last_event_id = line[3:].strip()
                    if line.startswith("data:") and '"type": "content"' in line:
                        # Mid-answer: hard-kill the server process.
                        break
        assert last_event_id is not None
    finally:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Slow Windows reap: kill again so the drill cannot leave a zombie.
            process.kill()
            process.wait(timeout=10)

    # Restart against the SAME data dir: the event log survived the kill.
    process = _spawn()
    try:
        with httpx.Client(timeout=30.0) as client:
            _wait_ready(client)
            replayed_types = []
            fabricated_done = False
            with client.stream(
                "POST",
                f"{base}/api/chat/stream",
                json=turn_body,
                headers={"Last-Event-ID": str(last_event_id)},
            ) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = json.loads(line[len("data: "):])
                    kind = payload.get("type")
                    if kind == "done":
                        # A done AFTER the kill would mean the restarted
                        # process fabricated a completion it never generated.
                        fabricated_done = True
                    replayed_types.append(kind)
            assert replayed_types, "restart replayed nothing"
            assert not fabricated_done, (
                "restarted process fabricated a done marker for a generation "
                "that died with the killed process"
            )
    finally:
        process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Slow Windows reap: kill again so the drill cannot leave a zombie.
            process.kill()
            process.wait(timeout=10)
