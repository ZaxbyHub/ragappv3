"""
Adversarial security tests for _save_attachment sentinel pattern.

ATTACK VECTORS:
1. Exception in except block AFTER os.close but BEFORE fd=None (os.unlink raises)
2. os.close raises unexpected exception in except block — fd=None skipped, double-close in finally
3. Concurrent threads calling _save_attachment on same fd (fd reuse race)
4. Negative or invalid fd values

These tests attempt to BREAK the sentinel pattern.
"""

import asyncio
import os
import sys
import tempfile
import types
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Stub missing optional dependencies
try:
    import lancedb
except ImportError:
    sys.modules['lancedb'] = types.ModuleType('lancedb')

try:
    import pyarrow
except ImportError:
    sys.modules['pyarrow'] = types.ModuleType('pyarrow')

try:
    from unstructured.partition.auto import partition
except ImportError:
    _unstructured = types.ModuleType('unstructured')
    _unstructured.__path__ = []
    _unstructured.partition = types.ModuleType('unstructured.partition')
    _unstructured.partition.__path__ = []
    _unstructured.partition.auto = types.ModuleType('unstructured.partition.auto')
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType('unstructured.chunking')
    _unstructured.chunking.__path__ = []
    _unstructured.chunking.title = types.ModuleType('unstructured.chunking.title')
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType('unstructured.documents')
    _unstructured.documents.__path__ = []
    _unstructured.documents.elements = types.ModuleType('unstructured.documents.elements')
    _unstructured.documents.elements.Element = type('Element', (), {})
    sys.modules['unstructured'] = _unstructured
    sys.modules['unstructured.partition'] = _unstructured.partition
    sys.modules['unstructured.partition.auto'] = _unstructured.partition.auto
    sys.modules['unstructured.chunking'] = _unstructured.chunking
    sys.modules['unstructured.chunking.title'] = _unstructured.chunking.title
    sys.modules['unstructured.documents'] = _unstructured.documents
    sys.modules['unstructured.documents.elements'] = _unstructured.documents.elements

try:
    import aioimaplib
except ImportError:
    sys.modules['aioimaplib'] = types.ModuleType('aioimaplib')

from app.config import Settings
from app.models.database import SQLiteConnectionPool
from app.services.email_service import EmailIngestionService


class FakeBackgroundProcessor:
    def __init__(self):
        self.enqueued = []

    async def enqueue(self, file_path, source=None, email_subject=None, email_sender=None):
        self.enqueued.append({
            'file_path': file_path,
            'source': source,
            'email_subject': email_subject,
            'email_sender': email_sender,
        })


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    d = tempfile.mkdtemp()
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def service(temp_dir):
    """Create an EmailIngestionService for tests."""
    settings = Settings()
    settings.data_dir = Path(temp_dir)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    db_path = os.path.join(temp_dir, 'test.db')
    from app.models.database import init_db
    init_db(db_path)
    pool = SQLiteConnectionPool(db_path, max_size=2)
    background_processor = FakeBackgroundProcessor()
    svc = EmailIngestionService(settings, pool, background_processor)
    yield svc
    pool.close_all()


def create_attachment_part(payload):
    """Helper to create an email attachment part."""
    msg = EmailMessage()
    msg.set_content(payload, maintype='text', subtype='plain')
    msg.add_header('Content-Disposition', 'attachment', filename='test.txt')
    return msg


# ========================================================================
# ATTACK VECTOR 1: Exception in except block AFTER os.close but BEFORE fd=None
# If os.unlink raises, fd=None is skipped, and finally block will double-close
# ========================================================================
@pytest.mark.asyncio
async def test_attack_os_unlink_raises_prevents_fd_none(service, temp_dir):
    """ATTACK: os.unlink raises AFTER os.close but BEFORE fd=None.

    If os.unlink(temp_path) raises an exception in the except block,
    the fd=None assignment is skipped, and the finally block will
    attempt to close an already-closed fd (double-close).
    """
    part = create_attachment_part(b'test content')

    close_calls = []

    def track_close(fd):
        close_calls.append(fd)

    # Custom exception for os.unlink to raise AFTER os.close
    class UnlinkAfterCloseError(OSError):
        pass

    unlink_raised = [False]

    def malicious_unlink(path):
        if not unlink_raised[0]:
            unlink_raised[0] = True
            raise UnlinkAfterCloseError("os.unlink failed after os.close")
        os.unlink(path)

    # Patch both — os.write fails, os.close succeeds, os.unlink raises
    with patch('app.services.email_service.os.close', side_effect=track_close):
        with patch('app.services.email_service.os.unlink', side_effect=malicious_unlink):
            with patch('app.services.email_service.os.write', side_effect=OSError("Simulated write failure")):
                try:
                    await service._save_attachment(part, 1)
                except Exception:
                    pass

    # CRITICAL: Without proper sentinel, we expect 2 close calls (double-close)
    # The sentinel pattern should prevent this by setting fd=None even when
    # os.unlink raises. But if the exception occurs between os.close and fd=None,
    # the sentinel never gets set.
    print(f"Close calls: {close_calls}")
    assert len(close_calls) == 1, (
        f"VULNERABILITY: Double-close detected! got {len(close_calls)} close calls. "
        "os.unlink exception between os.close and fd=None bypassed sentinel."
    )


# ========================================================================
# ATTACK VECTOR 2: os.close raises unexpected exception in except block
# If os.close raises (not FileNotFoundError), fd=None is never set
# ========================================================================
@pytest.mark.asyncio
async def test_attack_os_close_raises_unexpected_exception(service, temp_dir):
    """ATTACK: os.close raises unexpected exception in except block.

    If os.close(fd) in the except block raises an exception that is NOT
    OSError or FileNotFoundError, the fd=None assignment is never reached.
    Then finally block will try to close fd again (double-close).
    """
    part = create_attachment_part(b'test content')

    close_calls = []

    def track_close(fd):
        close_calls.append(fd)
        raise ValueError("Unexpected fatal error in close")  # NOT OSError/FileNotFoundError

    with patch('app.services.email_service.os.close', side_effect=track_close):
        with patch('app.services.email_service.os.write', side_effect=OSError("Simulated write failure")):
            try:
                await service._save_attachment(part, 1)
            except Exception:
                pass

    # If os.close raises ValueError (not OSError/FileNotFoundError), sentinel is bypassed
    # because the exception propagates and fd=None never executes
    print(f"Close calls: {close_calls}")
    assert len(close_calls) == 1, (
        f"VULNERABILITY: Double-close detected! got {len(close_calls)} close calls. "
        "os.close raised unexpected exception, bypassing fd=None."
    )


# ========================================================================
# ATTACK VECTOR 3: Concurrent threads sharing same fd
# If two threads somehow share fd, race condition could cause double-close
# ========================================================================
@pytest.mark.asyncio
async def test_attack_concurrent_access_same_fd(service, temp_dir):
    """ATTACK: Concurrent threads access same fd.

    Tests race condition where two coroutines might access the same fd.
    Note: tempfile.mkstemp returns unique fds, but we simulate fd reuse
    by patching mkstemp to return a fixed fd.
    """
    part = create_attachment_part(b'test content')

    close_calls = []
    original_close = os.close

    def track_close(fd):
        close_calls.append(fd)
        original_close(fd)

    # Use a shared fd to simulate reuse
    shared_fd = None
    first_call = [True]
    original_mkstemp = tempfile.mkstemp

    def mock_mkstemp(prefix, suffix, dir):
        nonlocal shared_fd, first_call
        if first_call[0]:
            first_call[0] = False
            fd, path = original_mkstemp(prefix, suffix, dir)
            shared_fd = fd
            return fd, path
        else:
            # Return the SAME fd (simulating fd reuse bug)
            return shared_fd, original_mkstemp(prefix, suffix, dir)[1]

    with patch('tempfile.mkstemp', side_effect=mock_mkstemp):
        with patch('app.services.email_service.os.close', side_effect=track_close):
            try:
                # Call twice with same mocked mkstemp (simulating fd leak/reuse)
                await service._save_attachment(part, 1)
            except Exception:
                pass

    # If mkstemp returns same fd twice, we might see double-close
    print(f"Close calls: {close_calls}")
    # This test documents the vulnerability if fd reuse occurs
    # In practice, mkstemp always returns unique fds


# ========================================================================
# ATTACK VECTOR 4: Negative or invalid fd
# What happens if fd is -1 or 0 (stdin)?
# ========================================================================
@pytest.mark.asyncio
async def test_attack_negative_fd(service, temp_dir):
    """ATTACK: fd is negative (-1).

    tempfile.mkstemp should never return -1, but we test what happens
    if somehow an invalid fd gets through.
    """
    part = create_attachment_part(b'test content')

    original_mkstemp = tempfile.mkstemp

    def mock_mkstemp_negative(prefix, suffix, dir):
        fd, path = original_mkstemp(prefix, suffix, dir)
        return -1, path  # Invalid fd

    with patch('tempfile.mkstemp', side_effect=mock_mkstemp_negative):
        with patch('app.services.email_service.os.write', side_effect=OSError("Simulated write failure")):
            try:
                await service._save_attachment(part, 1)
            except Exception as e:
                print(f"Exception: {e}")

    # Should not crash — the error handling should catch the issue
    # A negative fd being closed is a programming error, not a double-close


@pytest.mark.asyncio
async def test_attack_fd_zero_stdin(service, temp_dir):
    """ATTACK: fd is 0 (stdin).

    Tests if the code handles closing stdin without side effects.
    """
    part = create_attachment_part(b'test content')

    original_mkstemp = tempfile.mkstemp

    def mock_mkstemp_stdin(prefix, suffix, dir):
        fd, path = original_mkstemp(prefix, suffix, dir)
        return 0, path  # stdin fd (0)

    close_calls = []

    def track_close(fd):
        close_calls.append(fd)
        # Don't actually close stdin

    with patch('tempfile.mkstemp', side_effect=mock_mkstemp_stdin):
        with patch('app.services.email_service.os.close', side_effect=track_close):
            with patch('app.services.email_service.os.write', side_effect=OSError("Simulated write failure")):
                try:
                    await service._save_attachment(part, 1)
                except Exception as e:
                    print(f"Exception: {e}")

    # Verify fd=0 was attempted to be closed
    assert 0 in close_calls, "fd=0 (stdin) should have been attempted to close"


# ========================================================================
# ATTACK VECTOR 5: Multiple sequential errors in except block
# Test that sentinel works even when multiple exceptions occur
# ========================================================================
@pytest.mark.asyncio
async def test_attack_multiple_exceptions_in_except_block(service, temp_dir):
    """ATTACK: Multiple exceptions occur within except block.

    If os.close raises, then os.unlink raises, fd=None is never set.
    Tests that the sentinel cannot be bypassed by chained exceptions.
    """
    part = create_attachment_part(b'test content')

    close_calls = []

    def track_close(fd):
        close_calls.append(fd)

    exception_count = [0]

    def malicious_close(fd):
        close_calls.append(fd)
        exception_count[0] += 1
        if exception_count[0] == 1:
            # First close raises unexpected exception
            raise OSError("Unexpected close error")
        # Second close succeeds
        os.close(fd)

    unlink_exception_count = [0]

    def malicious_unlink(path):
        unlink_exception_count[0] += 1
        if unlink_exception_count[0] <= 2:
            # First two unlinks raise
            raise OSError("Unexpected unlink error")
        os.unlink(path)

    with patch('app.services.email_service.os.close', side_effect=malicious_close):
        with patch('app.services.email_service.os.unlink', side_effect=malicious_unlink):
            with patch('app.services.email_service.os.write', side_effect=OSError("Simulated write failure")):
                try:
                    await service._save_attachment(part, 1)
                except Exception:
                    pass

    print(f"Close calls: {close_calls}")
    # The sentinel should ensure exactly 2 close calls max
    # (one in except, one in finally if sentinel wasn't bypassed)
    # But if both close and unlink fail in except, sentinel is bypassed
    assert len(close_calls) <= 2, (
        f"Too many close calls: {len(close_calls)}. Sentinel may be bypassed."
    )


# ========================================================================
# BASELINE TESTS: Verify sentinel pattern works correctly
# ========================================================================
@pytest.mark.asyncio
async def test_sentinel_normal_error_path(service, temp_dir):
    """BASELINE: Normal error path sets sentinel correctly."""
    part = create_attachment_part(b'test content')

    close_calls = []
    original_close = os.close

    def track_close(fd):
        close_calls.append(fd)
        original_close(fd)

    with patch('app.services.email_service.os.close', side_effect=track_close):
        with patch('app.services.email_service.os.write', side_effect=OSError("Write failed")):
            try:
                await service._save_attachment(part, 1)
            except Exception:
                pass

    assert len(close_calls) == 1, "Normal error path should close exactly once"


@pytest.mark.asyncio
async def test_sentinel_success_path(service, temp_dir):
    """BASELINE: Success path closes exactly once."""
    part = create_attachment_part(b'test content')

    close_calls = []
    original_close = os.close

    def track_close(fd):
        close_calls.append(fd)
        original_close(fd)

    with patch('app.services.email_service.os.close', side_effect=track_close):
        result = await service._save_attachment(part, 1)

    assert len(close_calls) == 1, "Success path should close exactly once"
    assert result.endswith('.txt')
