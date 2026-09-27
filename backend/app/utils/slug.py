"""Organization slug generation (issue #690, T1-05-K-06).

Shared by the organizations create/update routes and the
``migrate_add_org_slug_column`` startup migration so both derive identical
slugs. Names with no ASCII letters or digits (e.g. ``東京``, ``Москва``) used
to collapse to the empty string — one shared empty slug that the fresh
schema's UNIQUE constraint then rejected with an unactionable 409. The
fallback derives a stable, non-empty, name-distinct slug from the name's
SHA-256 instead.
"""

import hashlib
import re

_SLUG_MAX_LEN = 50
_HASH_LEN = 12
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DASHES = re.compile(r"-+")


def generate_org_slug(name: str) -> str:
    """Return a non-empty slug for an organization name.

    ASCII names keep the historical normalization byte-for-byte. Only when
    that normalization yields ``''`` does the deterministic ``org-<hash>``
    fallback apply, so distinct names always produce distinct non-empty
    slugs (bar a 48-bit hash coincidence, which callers surface as their
    normal conflict error).
    """
    slug = name.lower().strip()
    slug = _NON_ALNUM.sub("-", slug)
    slug = slug.strip("-")
    slug = _DASHES.sub("-", slug)
    if not slug:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_HASH_LEN]
        slug = f"org-{digest}"
    return slug[:_SLUG_MAX_LEN]
