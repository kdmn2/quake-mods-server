"""Importing from a folder on the application server (a read-only mount such as /imports in the container).

Safety model
* Only explicitly configured roots are reachable. A browser request can name a root by number and a relative path, never an
  absolute one, and every component is checked (no '', '.', '..', separators).
* Nothing is followed: directories and files are opened component by component with O_NOFOLLOW relative to the previous
  directory descriptor, so a symlink anywhere on the path (including one swapped in after the scan) is refused instead of
  followed out of the root. There is no check-then-open window to race.
* Only regular files qualify. The final open uses O_NONBLOCK so a FIFO cannot hang the worker, and the descriptor is
  fstat()ed to confirm it is a regular file before a byte is read.
* Source files are only ever opened read-only. Nothing in this module writes, renames, deletes or chmods anything under a root.
* Between listing and import the file's identity (size, mtime, inode, device) is compared with what the listing recorded and
  again after the copy, so a file that was replaced or still being written is reported instead of imported half-way.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CHUNK = 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class LocalImportError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ImportRoot:
    index: int
    path: str
    label: str


def parse_roots(spec: str | None) -> list[ImportRoot]:
    """`IMPORT_ROOTS`: absolute paths separated by ':' or ',' or newlines. Relative entries are ignored."""
    roots: list[ImportRoot] = []
    seen: set[str] = set()
    for part in re.split(r"[:,\n]", spec or ""):
        part = part.strip()
        if not part or not part.startswith("/"):
            continue
        norm = os.path.normpath(part)
        if norm in seen:
            continue
        seen.add(norm)
        roots.append(ImportRoot(len(roots), norm, os.path.basename(norm) or norm))
    return roots


def root_problem(root: ImportRoot) -> str | None:
    """Why this root cannot be used right now, or None."""
    try:
        real = os.path.realpath(root.path)
        st = os.stat(real)
    except OSError:
        return "The folder is not available (not mounted, or it does not exist)."
    if not stat.S_ISDIR(st.st_mode):
        return "The configured path is not a folder."
    if not os.access(real, os.R_OK | os.X_OK):
        return "The application is not allowed to read this folder (permissions)."
    return None


def validate_relpath(rel: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(rel, str):
        raise LocalImportError("bad-path", "Invalid path")
    if rel == "" and allow_empty:
        return ()
    if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or len(rel) > 1024:
        raise LocalImportError("bad-path", "The path must be a relative path inside the import folder")
    parts = tuple(rel.split("/"))
    if any(p in ("", ".", "..") for p in parts):
        raise LocalImportError("bad-path", "The path may not contain '.', '..' or empty parts")
    if any(ord(c) < 32 or ord(c) == 127 for c in rel):
        raise LocalImportError("bad-path", "The path contains control characters")
    return parts


def _open_dir(root: ImportRoot, parts: tuple[str, ...]) -> int:
    try:
        fd = os.open(os.path.realpath(root.path), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        raise LocalImportError("unavailable", "The import folder is not available") from None
    try:
        for part in parts:
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise LocalImportError("not-found" if exc.errno in (2, 20) else "symlink" if exc.errno == 40 else "permission",
                                       "A folder on the path is missing, is a symbolic link, or cannot be read") from None
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclass
class Candidate:
    rel: str
    size: int
    mtime_ns: int
    ino: int
    dev: int
    kind: str                 # "zip" | "pak"
    eligible: bool
    reason: str = ""

    def identity(self) -> tuple[int, int, int, int]:
        return (self.size, self.mtime_ns, self.ino, self.dev)


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    visited: int = 0
    truncated: bool = False
    other_files: int = 0
    skipped_links: int = 0
    errors: list[str] = field(default_factory=list)


def scan_root(root: ImportRoot, subpath: str = "", recursive: bool = False, kinds: tuple[str, ...] = ("zip",), *, max_depth: int = 8,
              max_entries: int = 20000, max_results: int = 2000, max_file_bytes: int = 2 * 1024 ** 3) -> ScanResult:
    """List candidate packages. Reads directory entries and their stat data only; no file is opened or hashed."""
    start = validate_relpath(subpath, allow_empty=True)
    result = ScanResult()
    _close = os.close
    _close(_open_dir(root, start))                       # fail fast (and clearly) if the starting folder itself is unusable
    wanted = {ext: k for ext, k in {".zip": "zip", ".pak": "pak"}.items() if k in kinds}
    pending: list[tuple[tuple[str, ...], int]] = [(start, 0)]
    while pending and not result.truncated:
        here, depth = pending.pop()
        try:
            fd = _open_dir(root, here)                  # every directory is reached by the same no-symlink chain
        except LocalImportError:
            result.errors.append("/".join(here) or "(top folder)")
            continue
        subdirs: list[tuple[str, ...]] = []
        try:
            with os.scandir(fd) as it:                   # entries must be stat()ed while this descriptor is still open
                entries = sorted(it, key=lambda e: e.name.casefold())
                for entry in entries:
                    result.visited += 1
                    if result.visited > max_entries:
                        result.truncated = True
                        break
                    try:
                        if entry.is_symlink():
                            result.skipped_links += 1
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    name = entry.name
                    if stat.S_ISDIR(st.st_mode):
                        if recursive and depth < max_depth and not name.startswith("."):
                            subdirs.append(here + (name,))
                        continue
                    kind = wanted.get(os.path.splitext(name)[1].lower())
                    if kind is None:
                        result.other_files += 1
                        continue
                    rel = "/".join(here + (name,))
                    eligible, reason = True, ""
                    if not stat.S_ISREG(st.st_mode):
                        eligible, reason = False, "Not a regular file"
                    elif any(ord(c) < 32 or ord(c) == 127 for c in rel):
                        eligible, reason = False, "File name contains control characters"
                    elif not _is_utf8(rel):
                        eligible, reason = False, "File name is not valid UTF-8"
                    elif st.st_size == 0:
                        eligible, reason = False, "Empty file"
                    elif st.st_size > max_file_bytes:
                        eligible, reason = False, "Larger than the configured limit"
                    if len(result.candidates) >= max_results:
                        result.truncated = True
                        continue
                    shown = rel if _is_utf8(rel) else rel.encode("utf-8", "replace").decode("utf-8")
                    result.candidates.append(Candidate(shown, int(st.st_size), int(st.st_mtime_ns), int(st.st_ino), int(st.st_dev), kind, eligible, reason))
        except OSError:
            result.errors.append("/".join(here) or "(top folder)")
        finally:
            _close(fd)
        for sub in reversed(subdirs):
            pending.append((sub, depth + 1))
    result.candidates.sort(key=lambda c: c.rel.casefold())
    return result


def _is_utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def open_candidate(root: ImportRoot, rel: str) -> tuple[int, os.stat_result]:
    """Open `rel` for reading, refusing symlinks anywhere on the path and anything that is not a regular file."""
    parts = validate_relpath(rel)
    dirfd = _open_dir(root, parts[:-1])
    try:
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dirfd)
        except OSError as exc:
            raise LocalImportError("not-found" if exc.errno == 2 else "symlink" if exc.errno == 40 else "permission",
                                   {2: "The file no longer exists", 40: "The file is now a symbolic link, which is not followed"}.get(exc.errno, "The file cannot be read (permissions)")) from None
    finally:
        os.close(dirfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LocalImportError("not-regular", "The path is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def copy_candidate(root: ImportRoot, rel: str, dest: Path, expected: tuple[int, int, int, int] | None, max_bytes: int,
                   progress: Callable[[str, int, int], None] | None = None, stage: str = "Copying from the import folder") -> tuple[str, int]:
    """Copy the file into `dest` (writable staging storage) and return (sha256, size). Raises LocalImportError('changed') if the source is
    not the file that was listed, or changes while being read."""
    fd, st = open_candidate(root, rel)
    try:
        if expected is not None and (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev) != tuple(expected):
            raise LocalImportError("changed", "The file changed since the folder was scanned. Scan the folder again and re-select it.")
        if st.st_size > max_bytes:
            raise LocalImportError("too-large", "The file is larger than the configured limit")
        digest = hashlib.sha256()
        copied = 0
        if progress:
            progress(stage, 0, st.st_size)
        with os.fdopen(fd, "rb", closefd=False) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise LocalImportError("too-large", "The file grew beyond the configured limit while it was being read")
                digest.update(chunk)
                out.write(chunk)
                if progress:
                    progress(stage, copied, st.st_size)
        after = os.fstat(fd)
        if copied != st.st_size or (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise LocalImportError("changed", "The file changed while it was being copied (is it still being written?). Try again when it is complete.")
        return digest.hexdigest(), copied
    except OSError as exc:
        if isinstance(exc, LocalImportError):
            raise
        raise LocalImportError("disk", "The staging area could not be written (disk full or permissions)") from None
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)
