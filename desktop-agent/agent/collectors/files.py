"""
Endpoint file collector.

Mirrors the 1Password collector's shape: it enumerates drives, reports a
filesystem tree on demand (so the operator can pick folders in the portal), and
collects the selected files through the same client-encrypted pipeline.

Selection is driven by the cloud Data Map (which folders to include, which file
types/sizes to exclude) and delivered to the agent in the collect command, so the
agent never decides on its own what to back up.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import List

# Noise/system directories skipped even when a parent folder is selected, so a
# broad selection stays sane and fast.
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".cache", ".Trash", ".npm", ".venv",
    "venv", ".gradle", ".m2", "Caches", "CachedData", ".DS_Store",
}
# Extra system trees skipped when indexing from the system root so a full-tree
# index stays fast and relevant (user data lives under Home / Volumes).
_ROOT_SKIP = {
    "System", "private", "dev", "proc", "sys", "usr", "bin", "sbin", "cores",
    "opt", "var", "Library", "Applications", "lost+found", "run", "boot",
}
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024   # 100 MiB per file
# Bounds for the cached folder index so a rebuild is fast and the payload sane.
_INDEX_MAX_DEPTH = 5
_INDEX_ROOT_DEPTH = 2      # system root ("/") is only indexed shallowly
_INDEX_MAX_ENTRIES = 600   # subfolders indexed per directory
_INDEX_MAX_NODES = 20000   # total folders across the whole index
_DEFAULT_MAX_FILES = 5000                # safety cap per collection run

# Extension -> canonical kind (matches the server taxonomy KINDS).
_EXT_KIND = {
    "pdf": "pdf",
    "doc": "text", "docx": "text", "txt": "text", "md": "text", "rtf": "text",
    "pages": "text",
    "xls": "spreadsheet", "xlsx": "spreadsheet", "csv": "spreadsheet",
    "numbers": "spreadsheet",
    "ppt": "presentation", "pptx": "presentation", "key": "presentation",
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "heic": "image", "webp": "image", "tiff": "image", "bmp": "image", "svg": "image",
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video", "webm": "video",
    "mp3": "audio", "wav": "audio", "aac": "audio", "flac": "audio", "m4a": "audio",
    "zip": "archive", "tar": "archive", "gz": "archive", "7z": "archive", "rar": "archive",
}


def available() -> bool:
    """The file collector is always available on a desktop agent."""
    return True


def _drive_of(p: Path) -> str:
    s = str(p)
    if s.startswith("/Volumes/"):
        return s.split("/", 3)[2]  # /Volumes/<name>/...
    return "Local"


def _kind_for(name: str) -> str:
    ext = Path(name).suffix.lstrip(".").lower()
    return _EXT_KIND.get(ext, "file")


def _has_subdir(path: str) -> bool:
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def list_roots() -> List[dict]:
    """Top-level roots the operator can browse: home, mounted volumes, root."""
    roots: List[dict] = []
    home = str(Path.home())
    roots.append({"path": home, "name": "Home", "kind": "home", "hasChildren": True})
    if platform.system() == "Darwin":
        vol = Path("/Volumes")
        if vol.exists():
            for v in sorted(vol.iterdir()):
                try:
                    if v.is_dir():
                        roots.append({"path": str(v), "name": v.name,
                                      "kind": "volume", "hasChildren": True})
                except OSError:
                    continue
    else:
        for base in ("/mnt", "/media"):
            p = Path(base)
            if p.exists():
                for v in sorted(p.iterdir()):
                    try:
                        if v.is_dir():
                            roots.append({"path": str(v), "name": v.name,
                                          "kind": "volume", "hasChildren": True})
                    except OSError:
                        continue
    roots.append({"path": "/", "name": "System root", "kind": "root", "hasChildren": True})
    return roots


def scan(path: str, max_entries: int = 400) -> dict:
    """Return the immediate subfolders of ``path`` plus a shallow file summary."""
    if not path:
        return {"path": "", "name": "This Mac", "dirs": list_roots(), "files": 0, "bytes": 0}
    p = Path(path)
    dirs: List[dict] = []
    files = 0
    total = 0
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name in _SKIP_DIRS:
                            continue
                        dirs.append({"path": e.path, "name": e.name,
                                     "hasChildren": _has_subdir(e.path)})
                    elif e.is_file(follow_symlinks=False):
                        files += 1
                        try:
                            total += e.stat().st_size
                        except OSError:
                            pass
                except OSError:
                    continue
    except OSError as exc:
        return {"path": path, "name": p.name or path, "error": str(exc),
                "dirs": [], "files": 0, "bytes": 0}
    dirs.sort(key=lambda d: d["name"].lower())
    return {"path": path, "name": p.name or path,
            "dirs": dirs[:max_entries], "files": files, "bytes": total}


def build_index(max_depth: int = _INDEX_MAX_DEPTH, max_entries: int = _INDEX_MAX_ENTRIES,
                max_nodes: int = _INDEX_MAX_NODES) -> dict:
    """Build the full (bounded) folder hierarchy in one pass so the portal can
    navigate it instantly instead of scanning per-folder.

    Each node carries its immediate file count/bytes and its child folders down to
    ``max_depth`` (the system root is only walked ``_INDEX_ROOT_DEPTH`` deep).
    ``hasMore`` marks folders whose children were truncated or depth-limited."""
    counter = [0]

    def walk(path: str, depth: int, depth_limit: int, extra_skip: set) -> dict:
        node: dict = {"path": path, "name": Path(path).name or path}
        subdirs: list = []
        files = 0
        total = 0
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name in _SKIP_DIRS or e.name in extra_skip or e.name.startswith("."):
                                continue
                            subdirs.append((e.name, e.path))
                        elif e.is_file(follow_symlinks=False):
                            files += 1
                            try:
                                total += e.stat().st_size
                            except OSError:
                                pass
                    except OSError:
                        continue
        except OSError as exc:
            node["error"] = str(exc)
        node["files"] = files
        node["bytes"] = total
        subdirs.sort(key=lambda d: d[0].lower())
        truncated = len(subdirs) > max_entries
        subdirs = subdirs[:max_entries]
        children: list = []
        if depth < depth_limit:
            for _name, cpath in subdirs:
                if counter[0] >= max_nodes:
                    truncated = True
                    break
                counter[0] += 1
                children.append(walk(cpath, depth + 1, depth_limit, extra_skip))
            node["children"] = children
            node["hasMore"] = truncated
        else:
            # Depth limit: don't descend, but note whether deeper folders exist.
            node["children"] = []
            node["hasMore"] = len(subdirs) > 0
        return node

    roots: list = []
    for r in list_roots():
        counter[0] += 1
        is_root = r.get("kind") == "root"
        limit = _INDEX_ROOT_DEPTH if is_root else max_depth
        # Skip big system trees when indexing from "/" so the build stays fast.
        node = walk(r["path"], 1, limit, _ROOT_SKIP if is_root else set())
        node["name"] = r["name"]
        node["kind"] = r.get("kind")
        roots.append(node)
    return {"built_at": datetime.now(timezone.utc).isoformat(),
            "roots": roots, "nodes": counter[0]}


def _excluded(fp: Path, excl_ext: set, excl_glob: List[str]) -> bool:
    ext = fp.suffix.lstrip(".").lower()
    if ext in excl_ext:
        return True
    s = str(fp)
    return any(fnmatch.fnmatch(s, g) for g in excl_glob)


def collect(config: dict) -> List[dict]:
    """Walk the selected roots and return normalized file objects.

    ``config`` = {roots:[...], excludeExts:[...], excludeGlobs:[...],
    maxSizeBytes:int, maxFiles:int}. Files over the size cap, in skipped/noise
    dirs, or matching an exclude are left out."""
    config = config or {}
    roots = config.get("roots") or []
    max_bytes = int(config.get("maxSizeBytes") or _DEFAULT_MAX_BYTES)
    max_files = int(config.get("maxFiles") or _DEFAULT_MAX_FILES)
    excl_ext = {str(e).lower().lstrip(".") for e in (config.get("excludeExts") or [])}
    excl_glob = list(config.get("excludeGlobs") or [])

    objects: List[dict] = []
    for root in roots:
        rp = Path(root)
        if not rp.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(rp):
            # Prune noise/system dirs in-place so os.walk doesn't descend them.
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                fp = Path(dirpath) / fn
                try:
                    st = fp.stat()
                except OSError:
                    continue
                if st.st_size > max_bytes:
                    continue
                if _excluded(fp, excl_ext, excl_glob):
                    continue
                try:
                    data = fp.read_bytes()
                except OSError:
                    continue
                oid = "endpoint_files:" + hashlib.sha256(str(fp).encode()).hexdigest()[:24]
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
                objects.append({
                    "object_id": oid,
                    "kind": _kind_for(fn),
                    "title": fn,
                    "content_b64": base64.b64encode(data).decode(),
                    "preview": str(fp.parent),
                    "meta": {
                        "path": str(fp),
                        "folder": str(fp.parent),
                        "drive": _drive_of(fp),
                        "extension": fp.suffix.lstrip(".").lower(),
                        "bytes": len(data),
                        "modified": mtime,
                    },
                    "labels": [_drive_of(fp)],
                    "size_bytes": len(data),
                })
                if len(objects) >= max_files:
                    return objects
    return objects
