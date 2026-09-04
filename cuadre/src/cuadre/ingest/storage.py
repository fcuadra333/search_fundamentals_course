"""Content-addressed blob store.

Files are keyed by the SHA-256 of their bytes, which gives three things the
specification asks for at once: versioning (different content, different key),
deduplication of a re-uploaded file, and the source hash that every workpaper
has to carry.

Local disk in v1 behind a narrow interface, so object storage is a swap of this
one class and nothing else.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore(Protocol):
    def put(self, data: bytes, *, suffix: str = "") -> tuple[str, str]: ...
    def get(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, digest: str, suffix: str) -> Path:
        return self.root / digest[:2] / digest[2:4] / f"{digest}{suffix}"

    def put(self, data: bytes, *, suffix: str = "") -> tuple[str, str]:
        digest = content_hash(data)
        target = self._path_for(digest, suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            # Write once: identical content is never rewritten, so a stored
            # file cannot change under a workpaper that already cites its hash.
            target.write_bytes(data)
        return digest, str(target.relative_to(self.root))

    def get(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    def exists(self, path: str) -> bool:
        return (self.root / path).exists()


def default_store() -> LocalBlobStore:
    from ..config import get_settings

    return LocalBlobStore(get_settings().blob_dir)
