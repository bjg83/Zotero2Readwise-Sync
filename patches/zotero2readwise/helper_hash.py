# patches/zotero2readwise/helper_hash.py
from __future__ import annotations

import hashlib
from typing import Optional


def content_hash(*parts: Optional[str]) -> str:
    """Compute a stable SHA256 hex digest from given string parts.

    Joins parts with a newline, treating None as empty string.
    """
    concat = "\n".join(p or "" for p in parts)
    return hashlib.sha256(concat.encode("utf-8")).hexdigest()
