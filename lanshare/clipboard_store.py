"""Shared clipboard history & loop-free deduplication store."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional


MAX_CLIPBOARD_ITEMS = 30
MAX_CLIPBOARD_BYTES = 512 * 1024  # 512 KB text limit


@dataclass
class ClipboardEntry:
    id: str
    text: str
    source: str       # "linux" or device name like "iPhone · Safari"
    source_kind: str  # "desktop" | "mobile"
    created_at: float
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "source": self.source,
            "source_kind": self.source_kind,
            "created_at": self.created_at,
            "char_count": len(self.text),
        }


class ClipboardStore:
    """Thread-safe shared clipboard store with hash deduplication."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: list[ClipboardEntry] = []
        self._last_hash: str = ""

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    def push(
        self,
        text: str,
        source: str,
        source_kind: str,
        deduplicate_latest: bool = True,
    ) -> Optional[ClipboardEntry]:
        if not text:
            return None
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > MAX_CLIPBOARD_BYTES:
            text = encoded[:MAX_CLIPBOARD_BYTES].decode("utf-8", errors="ignore")

        digest = self.hash_text(text)
        with self._lock:
            if deduplicate_latest and digest == self._last_hash and self._items:
                return None

            entry = ClipboardEntry(
                id=secrets.token_hex(6),
                text=text,
                source=source,
                source_kind=source_kind,
                created_at=time.time(),
                content_hash=digest,
            )
            self._last_hash = digest
            self._items.insert(0, entry)
            if len(self._items) > MAX_CLIPBOARD_ITEMS:
                self._items = self._items[:MAX_CLIPBOARD_ITEMS]
            return entry

    def latest(self) -> Optional[ClipboardEntry]:
        with self._lock:
            return self._items[0] if self._items else None

    def latest_hash(self) -> str:
        with self._lock:
            return self._last_hash

    def list_items(self) -> list[dict]:
        with self._lock:
            return [item.to_dict() for item in self._items]

    def delete_item(self, item_id: str) -> bool:
        with self._lock:
            for idx, item in enumerate(self._items):
                if item.id == item_id:
                    self._items.pop(idx)
                    self._last_hash = self._items[0].content_hash if self._items else ""
                    return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._last_hash = ""
