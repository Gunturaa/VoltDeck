"""File registry, active streaming transfer tracker, safe filenames, and thumbnails."""

from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None
    ImageOps = None


STREAM_CHUNK_SIZE = 256 * 1024  # 256 KB per streaming chunk
THUMBNAIL_MAX_DIM = 360

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac"}
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar"}
DOC_EXTENSIONS = {".pdf", ".txt", ".md", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".json"}


def classify_file_kind(filename: str, mime: str = "") -> str:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS or mime.startswith("image/"):
        return "image"
    if ext in VIDEO_EXTENSIONS or mime.startswith("video/"):
        return "video"
    if ext in AUDIO_EXTENSIONS or mime.startswith("audio/"):
        return "audio"
    if ext in ARCHIVE_EXTENSIONS:
        return "archive"
    if ext in DOC_EXTENSIONS or mime.startswith("text/") or mime == "application/pdf":
        return "document"
    return "file"


def sanitize_filename(raw_name: str) -> str:
    """Strip paths and dangerous characters while preserving Unicode filenames."""
    name = (raw_name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.strip(".")
    if not name:
        name = f"file_{int(time.time())}"
    if len(name) > 200:
        stem = Path(name).stem[:160]
        suffix = Path(name).suffix[:30]
        name = f"{stem}{suffix}"
    return name


def unique_destination_path(directory: Path, filename: str) -> Path:
    """Return a non-colliding file path inside directory."""
    directory.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(filename)
    candidate = directory / safe
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        alt = directory / f"{stem} ({counter}){suffix}"
        if not alt.exists():
            return alt
        counter += 1


@dataclass
class SharedFile:
    id: str
    name: str
    path: Path
    size: int
    mime: str
    kind: str
    direction: str       # "outbound" (Linux -> iPhone) | "inbound" (iPhone -> Linux)
    sender: str          # "Linux Mint" or "iPhone · Safari"
    created_at: float
    owned_by_receive_dir: bool = False
    downloads: int = 0

    def to_dict(self) -> dict:
        exists = self.path.exists()
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "size": self.size,
            "mime": self.mime,
            "kind": self.kind,
            "direction": self.direction,
            "sender": self.sender,
            "created_at": self.created_at,
            "exists": exists,
            "has_thumb": self.kind == "image" and exists and Image is not None,
            "downloads": self.downloads,
        }


@dataclass
class ActiveTransfer:
    id: str
    filename: str
    direction: str       # "inbound" (uploading to Linux) | "outbound" (downloading to iPhone)
    peer: str
    total_bytes: int
    transferred_bytes: int
    started_at: float
    updated_at: float
    speed_bps: float = 0.0
    status: str = "active"  # active | completed | cancelled | error
    error: str = ""
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        pct = 0.0
        if self.total_bytes > 0:
            pct = min(100.0, round((self.transferred_bytes / self.total_bytes) * 100.0, 1))
        return {
            "id": self.id,
            "filename": self.filename,
            "direction": self.direction,
            "peer": self.peer,
            "total_bytes": self.total_bytes,
            "transferred_bytes": self.transferred_bytes,
            "percent": pct,
            "speed_bps": int(self.speed_bps),
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
        }


class FileStorageManager:
    """Thread-safe registry for shared files, received files, thumbnails, and live transfers."""

    def __init__(self, receive_dir: Path, device_name: str = "Linux Mint") -> None:
        self._lock = threading.RLock()
        self.receive_dir = receive_dir
        self.receive_dir.mkdir(parents=True, exist_ok=True)
        self.device_name = device_name
        self._files: dict[str, SharedFile] = {}
        self._order: list[str] = []
        self._transfers: dict[str, ActiveTransfer] = {}
        self._thumb_cache: dict[str, bytes] = {}
        self._gallery_index: dict[str, Path] = {}
        self._gallery_thumb_cache: dict[str, bytes] = {}

    def _gallery_roots(self) -> list[tuple[str, Path]]:
        home = Path.home()
        candidates = [
            ("Pictures", home / "Pictures"),
            ("Downloads", home / "Downloads"),
            ("Desktop", home / "Desktop"),
            ("Documents", home / "Documents"),
            ("LANShare", self.receive_dir),
        ]
        seen: set[Path] = set()
        roots: list[tuple[str, Path]] = []
        for label, p in candidates:
            if p.is_dir():
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    roots.append((label, resolved))
        return roots

    def scan_laptop_images(self, folder_filter: str = "all", limit: int = 80) -> dict:
        """Safely scan laptop user media folders for images pullable from iOS."""
        roots = self._gallery_roots()
        folder_names = [label for label, _ in roots]
        found: list[tuple[float, str, Path, int]] = []

        for label, root in roots:
            if folder_filter not in ("", "all") and label.lower() != folder_filter.lower():
                continue
            try:
                # Scan root and 1 level of non-hidden subfolders for speed
                dirs_to_scan = [root]
                for child in root.iterdir():
                    if child.is_dir() and not child.name.startswith("."):
                        dirs_to_scan.append(child)
                for scan_dir in dirs_to_scan[:24]:
                    try:
                        for item in scan_dir.iterdir():
                            if not item.is_file() or item.name.startswith("."):
                                continue
                            if item.suffix.lower() not in IMAGE_EXTENSIONS:
                                continue
                            try:
                                st = item.stat()
                                found.append((st.st_mtime, label, item.resolve(), st.st_size))
                            except OSError:
                                continue
                    except OSError:
                        continue
            except OSError:
                continue

        # Sort newest first, deduplicate by resolved path
        found.sort(key=lambda x: x[0], reverse=True)
        unique_items = []
        seen_paths: set[Path] = set()
        new_index: dict[str, Path] = {}
        for mtime, label, path_obj, size in found:
            if path_obj in seen_paths:
                continue
            seen_paths.add(path_obj)
            gid = hashlib.sha1(str(path_obj).encode("utf-8", errors="ignore")).hexdigest()[:12]
            new_index[gid] = path_obj
            unique_items.append(
                {
                    "id": gid,
                    "name": path_obj.name,
                    "folder": label,
                    "size": size,
                    "modified_at": mtime,
                }
            )
            if len(unique_items) >= limit:
                break

        with self._lock:
            self._gallery_index.update(new_index)
        return {"folders": folder_names, "images": unique_items}

    def get_gallery_image_path(self, gid: str) -> Optional[Path]:
        with self._lock:
            p = self._gallery_index.get(gid)
        if p and p.is_file():
            return p
        return None

    # --- Safe Sandboxed Laptop File Explorer (On-Demand, Zero Background Cost) ---

    def _explorer_roots(self) -> dict[str, Path]:
        home = Path.home()
        candidates = [
            ("Documents", home / "Documents"),
            ("Downloads", home / "Downloads"),
            ("Desktop", home / "Desktop"),
            ("Pictures", home / "Pictures"),
            ("Music", home / "Music"),
            ("Videos", home / "Videos"),
            ("LANShare", self.receive_dir),
        ]
        roots: dict[str, Path] = {}
        for label, p in candidates:
            if p.is_dir():
                roots[label] = p.resolve()
        return roots

    def resolve_explorer_path(self, root_name: str, rel_path: str = "") -> Optional[tuple[str, Path, Path]]:
        """Safely resolve (root_name, root_dir, target_path) preventing traversal or symlink escape."""
        roots = self._explorer_roots()
        if not roots:
            return None
        if root_name not in roots:
            return None
        root_dir = roots[root_name]
        cleaned = (rel_path or "").replace("\\", "/").strip("/")
        if cleaned:
            parts = [p for p in cleaned.split("/") if p]
            if any(p in (".", "..") or p.startswith(".") for p in parts):
                return None
            candidate = (root_dir / Path(*parts)).resolve()
        else:
            candidate = root_dir

        try:
            if not candidate.is_relative_to(root_dir):
                return None
        except AttributeError:  # pragma: no cover
            if root_dir not in candidate.parents and candidate != root_dir:
                return None

        if not candidate.exists():
            return None
        return root_name, root_dir, candidate

    def list_explorer_dir(self, root_name: str = "", rel_path: str = "", limit: int = 200) -> dict:
        """List non-hidden folders and files inside an allowlisted user folder."""
        roots = self._explorer_roots()
        root_names = list(roots.keys())
        if not root_names:
            return {"roots": [], "root": "", "path": "", "parent": None, "entries": []}

        chosen_root = root_name if root_name in roots else root_names[0]
        resolved = self.resolve_explorer_path(chosen_root, rel_path)
        if not resolved:
            resolved = (chosen_root, roots[chosen_root], roots[chosen_root])
        chosen_root, root_dir, target_dir = resolved
        if not target_dir.is_dir():
            target_dir = root_dir

        cur_rel = "" if target_dir == root_dir else str(target_dir.relative_to(root_dir)).replace("\\", "/")
        parent_rel: Optional[str] = None
        if cur_rel:
            parent_p = str(Path(cur_rel).parent).replace("\\", "/")
            parent_rel = "" if parent_p == "." else parent_p

        folders: list[dict] = []
        files: list[dict] = []
        try:
            with os.scandir(target_dir) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        full = Path(entry.path).resolve()
                        if not full.is_relative_to(root_dir):
                            continue
                        st = entry.stat()
                    except OSError:
                        continue
                    rel_item = f"{cur_rel}/{entry.name}" if cur_rel else entry.name
                    if entry.is_dir():
                        folders.append(
                            {
                                "name": entry.name,
                                "rel_path": rel_item,
                                "is_dir": True,
                                "size": 0,
                                "kind": "folder",
                                "modified_at": st.st_mtime,
                            }
                        )
                    elif entry.is_file():
                        mime, _ = mimetypes.guess_type(entry.name)
                        files.append(
                            {
                                "name": entry.name,
                                "rel_path": rel_item,
                                "is_dir": False,
                                "size": st.st_size,
                                "kind": classify_file_kind(entry.name, mime or ""),
                                "modified_at": st.st_mtime,
                            }
                        )
        except OSError:
            pass

        folders.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["modified_at"], reverse=True)
        return {
            "roots": root_names,
            "root": chosen_root,
            "path": cur_rel,
            "parent": parent_rel,
            "entries": (folders + files)[:limit],
        }

    def create_explorer_folder(self, root_name: str, rel_path: str, folder_name: str) -> tuple[bool, str, Optional[dict]]:
        """Safely create a new non-hidden subfolder inside an allowlisted Explorer directory."""
        resolved = self.resolve_explorer_path(root_name, rel_path)
        if not resolved:
            return False, "Folder tujuan tidak ditemukan.", None
        chosen_root, root_dir, parent_dir = resolved
        if not parent_dir.is_dir():
            return False, "Lokasi tujuan bukan folder.", None

        raw = str(folder_name or "").strip()
        if not raw or raw.startswith(".") or "/" in raw or "\\" in raw or ".." in raw:
            return False, "Nama folder tidak valid.", None
        clean = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]+", "_", raw).strip(" .")[:80]
        if not clean:
            return False, "Nama folder tidak valid.", None

        target = (parent_dir / clean).resolve()
        try:
            if not target.is_relative_to(root_dir):
                return False, "Akses ditolak.", None
            target.mkdir(exist_ok=False)
        except FileExistsError:
            return False, f"Folder '{clean}' sudah ada.", None
        except OSError as exc:
            return False, f"Gagal membuat folder: {exc}", None

        return True, "", self.list_explorer_dir(chosen_root, rel_path)

    def reserve_explorer_upload_target(
        self,
        root_name: str,
        rel_path: str,
        raw_filename: str,
    ) -> Optional[Path]:
        """Return a collision-safe target file path inside an allowlisted Explorer directory."""
        resolved = self.resolve_explorer_path(root_name, rel_path)
        if not resolved:
            return None
        _chosen_root, root_dir, target_dir = resolved
        if not target_dir.is_dir():
            return None
        safe_name = sanitize_filename(raw_filename)
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        candidate = (target_dir / safe_name).resolve()
        counter = 1
        while candidate.exists():
            candidate = (target_dir / f"{stem} ({counter}){suffix}").resolve()
            counter += 1
        try:
            if not candidate.is_relative_to(root_dir):
                return None
        except AttributeError:  # pragma: no cover
            if root_dir not in candidate.parents:
                return None
        return candidate

    def get_gallery_thumbnail_jpeg(self, gid: str) -> Optional[bytes]:
        with self._lock:
            if gid in self._gallery_thumb_cache:
                return self._gallery_thumb_cache[gid]
            path_obj = self._gallery_index.get(gid)
        if not path_obj or not path_obj.is_file() or Image is None:
            return None
        try:
            with Image.open(path_obj) as img:
                if ImageOps is not None:
                    img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                img.thumbnail((320, 320))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=78, optimize=True)
                data = buf.getvalue()
            with self._lock:
                if len(self._gallery_thumb_cache) > 100:
                    oldest = next(iter(self._gallery_thumb_cache))
                    self._gallery_thumb_cache.pop(oldest, None)
                self._gallery_thumb_cache[gid] = data
            return data
        except Exception:
            return None

    def set_receive_dir(self, new_dir: Path) -> Path:
        new_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.receive_dir = new_dir
        return new_dir

    def register_local_file(
        self,
        file_path: Path,
        direction: str = "outbound",
        sender: Optional[str] = None,
        owned_by_receive_dir: bool = False,
    ) -> Optional[SharedFile]:
        resolved = file_path.expanduser().resolve()
        if not resolved.is_file():
            return None
        stat = resolved.stat()
        mime, _ = mimetypes.guess_type(resolved.name)
        mime = mime or "application/octet-stream"
        kind = classify_file_kind(resolved.name, mime)

        with self._lock:
            # Avoid duplicating exact same outbound path
            if direction == "outbound":
                for existing in self._files.values():
                    if existing.direction == "outbound" and existing.path == resolved:
                        existing.size = stat.st_size
                        existing.created_at = time.time()
                        if existing.id in self._order:
                            self._order.remove(existing.id)
                        self._order.insert(0, existing.id)
                        return existing

            fid = secrets.token_hex(8)
            entry = SharedFile(
                id=fid,
                name=resolved.name,
                path=resolved,
                size=stat.st_size,
                mime=mime,
                kind=kind,
                direction=direction,
                sender=sender or self.device_name,
                created_at=time.time(),
                owned_by_receive_dir=owned_by_receive_dir,
            )
            self._files[fid] = entry
            self._order.insert(0, fid)
            return entry

    def get_file(self, file_id: str) -> Optional[SharedFile]:
        with self._lock:
            return self._files.get(file_id)

    def increment_downloads(self, file_id: str) -> None:
        with self._lock:
            entry = self._files.get(file_id)
            if entry:
                entry.downloads += 1

    def remove_file(self, file_id: str, delete_from_disk: bool = False) -> Optional[SharedFile]:
        with self._lock:
            entry = self._files.pop(file_id, None)
            if not entry:
                return None
            if file_id in self._order:
                self._order.remove(file_id)
            self._thumb_cache.pop(file_id, None)

        if delete_from_disk and entry.owned_by_receive_dir and entry.path.exists():
            try:
                entry.path.unlink()
            except OSError:
                pass
        return entry

    def clear_files(self, direction: Optional[str] = None) -> int:
        with self._lock:
            to_remove = [
                fid
                for fid in list(self._order)
                if direction is None or self._files[fid].direction == direction
            ]
            for fid in to_remove:
                self._files.pop(fid, None)
                self._order.remove(fid)
                self._thumb_cache.pop(fid, None)
            return len(to_remove)

    def list_files(self) -> list[dict]:
        with self._lock:
            result = []
            for fid in self._order:
                entry = self._files.get(fid)
                if entry:
                    result.append(entry.to_dict())
            return result

    def allocate_receive_temp_path(self, raw_filename: str) -> tuple[Path, Path]:
        """Return (temp_part_path, final_destination_path) inside receive_dir."""
        with self._lock:
            rdir = self.receive_dir
        final_path = unique_destination_path(rdir, raw_filename)
        part_name = f".{final_path.name}.{secrets.token_hex(4)}.lanshare.part"
        return final_path.parent / part_name, final_path

    def finalize_received_file(
        self,
        part_path: Path,
        preferred_final: Path,
        sender: str,
    ) -> SharedFile:
        """Atomically move completed .part file to final unique destination and register it."""
        with self._lock:
            dest = preferred_final
            if dest.exists():
                dest = unique_destination_path(self.receive_dir, preferred_final.name)
            os.replace(part_path, dest)
        entry = self.register_local_file(
            dest,
            direction="inbound",
            sender=sender,
            owned_by_receive_dir=True,
        )
        assert entry is not None
        return entry

    def get_thumbnail_jpeg(self, file_id: str) -> Optional[bytes]:
        with self._lock:
            if file_id in self._thumb_cache:
                return self._thumb_cache[file_id]
            entry = self._files.get(file_id)

        if not entry or entry.kind != "image" or not entry.path.exists() or Image is None:
            return None
        try:
            with Image.open(entry.path) as img:
                if ImageOps is not None:
                    img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                img.thumbnail((THUMBNAIL_MAX_DIM, THUMBNAIL_MAX_DIM))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=82, optimize=True)
                data = buf.getvalue()
            with self._lock:
                # Cap thumbnail cache to ~60 items
                if len(self._thumb_cache) > 60:
                    oldest_key = next(iter(self._thumb_cache))
                    self._thumb_cache.pop(oldest_key, None)
                self._thumb_cache[file_id] = data
            return data
        except Exception:
            return None

    # --- Active Transfer Tracking ---

    def start_transfer(
        self,
        filename: str,
        direction: str,
        peer: str,
        total_bytes: int,
        transfer_id: Optional[str] = None,
    ) -> ActiveTransfer:
        tid = transfer_id or secrets.token_hex(6)
        now = time.time()
        tx = ActiveTransfer(
            id=tid,
            filename=sanitize_filename(filename),
            direction=direction,
            peer=peer,
            total_bytes=max(0, total_bytes),
            transferred_bytes=0,
            started_at=now,
            updated_at=now,
        )
        with self._lock:
            self._transfers[tid] = tx
        return tx

    def update_transfer(self, transfer_id: str, transferred_bytes: int) -> Optional[ActiveTransfer]:
        now = time.time()
        with self._lock:
            tx = self._transfers.get(transfer_id)
            if not tx:
                return None
            tx.transferred_bytes = transferred_bytes
            elapsed = max(0.05, now - tx.started_at)
            tx.speed_bps = transferred_bytes / elapsed
            tx.updated_at = now
            return tx

    def finish_transfer(
        self,
        transfer_id: str,
        status: str = "completed",
        error: str = "",
    ) -> Optional[ActiveTransfer]:
        with self._lock:
            tx = self._transfers.get(transfer_id)
            if not tx:
                return None
            tx.status = status
            tx.error = error
            tx.updated_at = time.time()
            if status == "completed" and tx.total_bytes > 0:
                tx.transferred_bytes = tx.total_bytes
            return tx

    def request_cancel(self, transfer_id: str) -> bool:
        with self._lock:
            tx = self._transfers.get(transfer_id)
            if not tx or tx.status != "active":
                return False
            tx.cancel_requested = True
            return True

    def is_cancelled(self, transfer_id: str) -> bool:
        with self._lock:
            tx = self._transfers.get(transfer_id)
            return bool(tx and tx.cancel_requested)

    def remove_transfer(self, transfer_id: str) -> None:
        with self._lock:
            self._transfers.pop(transfer_id, None)

    def list_active_transfers(self) -> list[dict]:
        now = time.time()
        with self._lock:
            stale = [
                tid
                for tid, tx in self._transfers.items()
                if tx.status != "active" and (now - tx.updated_at) > 4.0
            ]
            for tid in stale:
                self._transfers.pop(tid, None)
            return [tx.to_dict() for tx in self._transfers.values()]
