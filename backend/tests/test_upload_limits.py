import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("ADMIN_SECRET_TOKEN", "test-admin-key")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-for-testing-only")
os.environ.setdefault("USERS_ENABLED", "false")

from app.api.routes.documents import _do_upload
from app.config import settings


@pytest.mark.asyncio
async def test_upload_rejects_content_length_above_100_mb(tmp_path):
    original_data_dir = settings.data_dir
    settings.data_dir = tmp_path
    request = SimpleNamespace(
        headers={"content-length": str((settings.max_file_size_mb * 1024 * 1024) + 1)}
    )
    file = SimpleNamespace(filename="large.txt")

    try:
        with pytest.raises(HTTPException) as exc_info:
            await _do_upload(request, file, settings, None, None, None, None, 1)
    finally:
        settings.data_dir = original_data_dir

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "File too large. Max size: 100 MB"
