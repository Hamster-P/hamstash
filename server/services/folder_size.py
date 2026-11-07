"""Persistent directory-size cache. Only changed directories enumerate/stat files.

Directory timestamps detect additions, removals and renames, including nested folders.
Files are counted by logical size; symlinks are excluded to stay inside the library.
"""
import json
import os
import threading
from pathlib import Path

_lock = threading.Lock()


def measure_folder(path: Path, cached: str | None = None) -> tuple[int, str]:
    try:
        old = json.loads(cached) if cached else {}
        if old.get("root") != str(path.resolve()):
            old = {}
    except (ValueError, TypeError, AttributeError):
        old = {}
    previous = old.get("directories", {})
    directories = {}

    def visit(directory: Path, relative: str) -> int:
        stamp = directory.stat().st_mtime_ns
        entry = previous.get(relative)
        if not entry or entry["stamp"] != stamp:
            size = 0
            children = []
            with os.scandir(directory) as entries:
                for item in entries:
                    if item.is_symlink():
                        continue
                    if item.is_dir(follow_symlinks=False):
                        children.append(item.name)
                    elif item.is_file(follow_symlinks=False):
                        size += item.stat(follow_symlinks=False).st_size
            # Retry next time if the directory changed during measurement.
            if directory.stat().st_mtime_ns != stamp:
                raise OSError("Directory changed while measuring")
            entry = {"stamp": stamp, "size": size, "children": children}
        directories[relative] = entry
        return entry["size"] + sum(
            visit(directory / name, f"{relative}/{name}") for name in entry["children"]
        )

    total = visit(path, ".")
    return total, json.dumps({"root": str(path.resolve()), "directories": directories})


def refresh_folder_size(media, library_root: Path) -> None:
    try:
        size, cache = measure_folder(library_root / media.folder_name, media.folder_size_cache)
    except OSError:
        # Offline shares or concurrent moves must not overwrite a valid cache with zero.
        return
    media.folder_size_bytes = size
    media.folder_size_cache = cache


def refresh_library_sizes(library_root: Path) -> None:
    from database import SessionLocal
    import models

    _lock.acquire()
    db = SessionLocal()
    try:
        for media in db.query(models.LocalMedia).all():
            refresh_folder_size(media, library_root)
        db.commit()
    finally:
        db.close()
        _lock.release()
