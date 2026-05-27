"""Public path helpers for deployments behind a path-prefixing proxy."""

import re

UNSAFE_ROOT_PATH_PATTERN = re.compile(r"[\s\x00-\x1F\x7F;\\?#]")


def normalize_root_path(value: str | None) -> str:
    """Normalize an external app root path to ``/prefix`` or ``""``."""
    raw = value or ""
    if not raw:
        return ""
    if raw != raw.strip():
        raise ValueError("app root path cannot contain leading or trailing whitespace")
    if re.match(r"^https?://", raw, flags=re.IGNORECASE) or (
        raw.startswith("//") and re.search(r"[^/]", raw)
    ):
        raise ValueError("app root path must be a path, not a URL")
    if UNSAFE_ROOT_PATH_PATTERN.search(raw):
        raise ValueError("app root path contains unsafe characters")
    if re.search(r"/{2,}", raw.strip("/")):
        raise ValueError("app root path cannot contain duplicate slashes")

    stripped = raw.strip("/")
    if not stripped:
        return ""
    if any(part in {".", ".."} for part in stripped.split("/")):
        raise ValueError("app root path cannot contain relative path segments")
    return f"/{stripped}"


def external_path(suffix: str, root_path: str | None = None) -> str:
    """Build a browser-visible path from the configured external root path."""
    if root_path is None:
        from app.config import settings

        root_path = settings.app_root_path
    root = normalize_root_path(root_path)
    normalized_suffix = "/" + suffix.strip("/")
    return f"{root}{normalized_suffix}" if root else normalized_suffix


def refresh_cookie_path(root_path: str | None = None) -> str:
    return external_path("/api/auth/refresh", root_path=root_path)


def csrf_cookie_path(root_path: str | None = None) -> str:
    if root_path is None:
        from app.config import settings

        root_path = settings.app_root_path
    return normalize_root_path(root_path) or "/"


def is_unstripped_prefix(full_path: str, root_path: str) -> bool:
    """Detect if a request path still carries the external prefix (proxy misconfiguration)."""
    prefix = normalize_root_path(root_path)
    if not prefix:
        return False
    bare = prefix.lstrip("/")
    return full_path == bare or full_path.startswith(bare + "/")
