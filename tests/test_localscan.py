import hashlib
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tools import localscan
from tools.localscan import LocalImportError, parse_roots

NOT_ROOT = hasattr(os, "geteuid") and os.geteuid() != 0
ZIPISH = b"PK\x03\x04" + b"payload"


def snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(root))] = (stat.S_IFMT(st.st_mode), st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-scan-tests-"))
        self.addCleanup(self.fix_perms_and_remove)
        self.root_dir = self.tmp / "imports"
        self.root_dir.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.zip").write_bytes(ZIPISH + b"-secret")
        self.root = parse_roots(str(self.root_dir))[0]

    def fix_perms_and_remove(self):
        for p in self.tmp.rglob("*"):
            try:
                if p.is_dir() and not p.is_symlink():
                    p.chmod(0o755)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, rel: str, data: bytes = ZIPISH) -> Path:
        p = self.root_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def scan(self, **kw):
        return localscan.scan_root(self.root, **kw)

    def rels(self, result, eligible_only=True):
        return [c.rel for c in result.candidates if c.eligible or not eligible_only]


class RootTests(Base):
    def test_parse_roots(self):
        roots = parse_roots("/imports:/data/more, /imports\n/x/../y\nrelative/path::  ")
        self.assertEqual([(r.index, r.path) for r in roots], [(0, "/imports"), (1, "/data/more"), (2, "/y")])
        self.assertEqual(roots[1].label, "more")
        self.assertEqual(parse_roots(""), [])
        self.assertEqual(parse_roots(None), [])
        self.assertEqual(parse_roots("not-absolute"), [])

    def test_root_problem(self):
        self.assertIsNone(localscan.root_problem(self.root))
        self.assertIn("not available", localscan.root_problem(parse_roots(str(self.tmp / "missing"))[0]))
        (self.tmp / "afile").write_text("x")
        self.assertIn("not a folder", localscan.root_problem(parse_roots(str(self.tmp / "afile"))[0]))

    @unittest.skipUnless(NOT_ROOT, "permission checks are meaningless as root")
    def test_unreadable_root(self):
        locked = self.tmp / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.assertIn("permissions", localscan.root_problem(parse_roots(str(locked))[0]))

    def test_validate_relpath(self):
        self.assertEqual(localscan.validate_relpath("a/b/c.zip"), ("a", "b", "c.zip"))
        self.assertEqual(localscan.validate_relpath("", allow_empty=True), ())
        for bad in ("", "/etc/passwd", "../x", "a/../../x", "a//b", "./a", "a/./b", "a\\b", "a\0b", "a\nb", "x" * 2000, "a/.."):
            with self.assertRaises(LocalImportError, msg=repr(bad)):
                localscan.validate_relpath(bad)


class ScanTests(Base):
    def test_lists_zip_files_case_insensitively_and_ignores_others(self):
        for name in ("one.zip", "Two.ZIP", "three.Zip", "notes.txt", "mod.pak", "archive.zip.bak", "readme"):
            self.make(name)
        res = self.scan()
        self.assertEqual(self.rels(res), ["one.zip", "three.Zip", "Two.ZIP"])
        self.assertEqual(res.other_files, 4)                                      # counted, never listed: no unsupported format is added silently
        self.assertEqual([c.kind for c in res.candidates], ["zip"] * 3)

    def test_pak_files_only_when_asked_for(self):
        self.make("a.zip")
        self.make("b.PAK")
        self.assertEqual(self.rels(self.scan()), ["a.zip"])
        res = self.scan(kinds=("zip", "pak"))
        self.assertEqual({(c.rel, c.kind) for c in res.candidates}, {("a.zip", "zip"), ("b.PAK", "pak")})

    def test_non_recursive_by_default_and_recursive_on_request(self):
        self.make("top.zip")
        self.make("sub/mid.zip")
        self.make("sub/deeper/low.zip")
        self.make(".hidden/h.zip")
        self.assertEqual(self.rels(self.scan()), ["top.zip"])
        self.assertEqual(self.rels(self.scan(recursive=True)), ["sub/deeper/low.zip", "sub/mid.zip", "top.zip"])      # hidden folders are skipped
        self.assertEqual(self.rels(self.scan(subpath="sub")), ["sub/mid.zip"])
        self.assertEqual(self.rels(self.scan(subpath="sub", recursive=True)), ["sub/deeper/low.zip", "sub/mid.zip"])

    def test_depth_limit(self):
        self.make("a/b/c/d/deep.zip")
        self.make("a/shallow.zip")
        self.assertEqual(self.rels(self.scan(recursive=True, max_depth=1)), ["a/shallow.zip"])
        self.assertEqual(self.rels(self.scan(recursive=True, max_depth=8)), ["a/b/c/d/deep.zip", "a/shallow.zip"])

    def test_empty_folder_and_mixed_content(self):
        self.assertEqual(self.scan().candidates, [])
        (self.root_dir / "empty").mkdir()
        self.make("sub/readme.txt")
        res = self.scan(recursive=True)
        self.assertEqual((res.candidates, res.truncated, res.errors), ([], False, []))

    def test_bounds_on_entries_and_results(self):
        for i in range(30):
            self.make(f"f{i:02d}.zip")
        res = self.scan(max_results=10)
        self.assertEqual((len(res.candidates), res.truncated), (10, True))
        res = self.scan(max_entries=12)
        self.assertTrue(res.truncated)
        self.assertLessEqual(res.visited, 13)

    def test_symlinks_are_never_followed_or_listed(self):
        self.make("real.zip")
        os.symlink(self.outside / "secret.zip", self.root_dir / "link-to-outside.zip")
        os.symlink(self.root_dir / "real.zip", self.root_dir / "link-inside.zip")
        os.symlink(self.outside, self.root_dir / "dirlink")
        res = self.scan(recursive=True)
        self.assertEqual(self.rels(res, eligible_only=False), ["real.zip"])
        self.assertEqual(res.skipped_links, 3)

    def test_a_symlinked_subpath_cannot_be_scanned(self):
        os.symlink(self.outside, self.root_dir / "escape")
        with self.assertRaises(LocalImportError):
            self.scan(subpath="escape")
        with self.assertRaises(LocalImportError):
            self.scan(subpath="../outside")
        with self.assertRaises(LocalImportError):
            self.scan(subpath="/etc")
        with self.assertRaises(LocalImportError):
            self.scan(subpath="missing")

    def test_a_symlinked_root_is_resolved_but_content_cannot_escape_it(self):
        link = self.tmp / "rootlink"
        os.symlink(self.root_dir, link)                                          # admin configured a mount via a symlink: that is allowed
        self.make("ok.zip")
        os.symlink(self.outside, self.root_dir / "x")
        res = localscan.scan_root(parse_roots(str(link))[0], recursive=True)
        self.assertEqual(self.rels(res), ["ok.zip"])

    def test_special_files_are_listed_as_ineligible(self):
        os.mkfifo(self.root_dir / "pipe.zip")
        self.make("empty.zip", b"")
        self.make("fine.zip")
        res = self.scan()
        by = {c.rel: c for c in res.candidates}
        self.assertEqual((by["fine.zip"].eligible, by["pipe.zip"].eligible, by["empty.zip"].eligible), (True, False, False))
        self.assertEqual(by["pipe.zip"].reason, "Not a regular file")
        self.assertEqual(by["empty.zip"].reason, "Empty file")

    def test_oversized_files_are_ineligible(self):
        self.make("big.zip", ZIPISH + b"x" * 5000)
        res = self.scan(max_file_bytes=1000)
        self.assertEqual((res.candidates[0].eligible, res.candidates[0].reason), (False, "Larger than the configured limit"))

    def test_control_characters_in_names_are_ineligible(self):
        self.make("bad\nname.zip")
        self.assertEqual((self.scan().candidates[0].eligible, "control" in self.scan().candidates[0].reason), (False, True))

    @unittest.skipUnless(NOT_ROOT, "permission checks are meaningless as root")
    def test_unreadable_subfolder_is_reported_not_fatal(self):
        self.make("ok.zip")
        self.make("locked/hidden.zip")
        self.make("fine/also.zip")
        (self.root_dir / "locked").chmod(0)
        res = self.scan(recursive=True)
        self.assertEqual(self.rels(res), ["fine/also.zip", "ok.zip"])
        self.assertEqual(res.errors, ["locked"])

    def test_missing_root_is_an_error_not_a_crash(self):
        with self.assertRaises(LocalImportError):
            localscan.scan_root(parse_roots(str(self.tmp / "gone"))[0])

    def test_listing_does_not_read_or_hash_files(self):
        data = ZIPISH + b"x" * 100_000
        p = self.make("a.zip", data)
        opened = []
        real_open = os.open

        def spy(path, flags, *a, **kw):
            opened.append(path)
            return real_open(path, flags, *a, **kw)
        os.open = spy
        try:
            self.scan()
        finally:
            os.open = real_open
        self.assertNotIn("a.zip", opened)                                          # directories are opened; the candidate file is not
        self.assertEqual(p.read_bytes(), data)

    def test_scanning_changes_nothing(self):
        self.make("a.zip")
        self.make("sub/b.zip")
        os.symlink(self.outside, self.root_dir / "l")
        before = snapshot(self.root_dir)
        self.scan(recursive=True, kinds=("zip", "pak"))
        self.assertEqual(snapshot(self.root_dir), before)


class OpenAndCopyTests(Base):
    def identity(self, rel):
        st = (self.root_dir / rel).stat()
        return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)

    def copy(self, rel, expected="auto", max_bytes=10 ** 9, progress=None):
        dest = self.tmp / "staged.bin"
        exp = self.identity(rel) if expected == "auto" else expected
        return localscan.copy_candidate(self.root, rel, dest, exp, max_bytes, progress), dest

    def test_copy_is_exact_and_leaves_the_source_untouched(self):
        data = ZIPISH + os.urandom(2_500_000)
        self.make("pkg.zip", data)
        os.utime(self.root_dir / "pkg.zip", ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        before = snapshot(self.root_dir)
        (sha, size), dest = self.copy("pkg.zip")
        self.assertEqual((sha, size, dest.read_bytes()), (hashlib.sha256(data).hexdigest(), len(data), data))
        self.assertEqual(snapshot(self.root_dir), before)                            # content, size and mtime all unchanged

    def test_a_read_only_source_tree_works(self):
        self.make("sub/pkg.zip")
        for p in (self.root_dir / "sub" / "pkg.zip",):
            p.chmod(0o444)
        (self.root_dir / "sub").chmod(0o555)
        self.root_dir.chmod(0o555)
        (sha, size), dest = self.copy("sub/pkg.zip")
        self.assertEqual(size, len(ZIPISH))

    def test_progress_reports_bytes(self):
        data = ZIPISH + b"x" * 3_000_000
        self.make("p.zip", data)
        seen = []
        self.copy("p.zip", progress=lambda s, d, t: seen.append((d, t)))
        self.assertEqual(seen[0], (0, len(data)))
        self.assertEqual(seen[-1], (len(data), len(data)))                               # a real total, so a percentage is meaningful
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_symlink_swapped_in_after_the_scan_is_refused(self):
        self.make("pkg.zip")
        expected = self.identity("pkg.zip")
        (self.root_dir / "pkg.zip").unlink()
        os.symlink(self.outside / "secret.zip", self.root_dir / "pkg.zip")
        with self.assertRaises(LocalImportError) as ctx:
            self.copy("pkg.zip", expected=expected)
        self.assertEqual(ctx.exception.kind, "symlink")
        self.assertFalse((self.tmp / "staged.bin").exists())

    def test_a_directory_swapped_for_a_symlink_cannot_redirect_the_open(self):
        self.make("sub/pkg.zip")
        expected = self.identity("sub/pkg.zip")
        shutil.rmtree(self.root_dir / "sub")
        os.symlink(self.outside, self.root_dir / "sub")
        (self.outside / "pkg.zip").write_bytes(ZIPISH + b"decoy")
        with self.assertRaises(LocalImportError):
            self.copy("sub/pkg.zip", expected=expected)

    def test_a_fifo_swapped_in_never_blocks_the_worker(self):
        self.make("pkg.zip")
        expected = self.identity("pkg.zip")
        (self.root_dir / "pkg.zip").unlink()
        os.mkfifo(self.root_dir / "pkg.zip")
        result = []
        t = threading.Thread(target=lambda: result.append(self._try(lambda: self.copy("pkg.zip", expected=expected))), daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "opening a FIFO hung the worker")
        self.assertIsInstance(result[0], LocalImportError)
        self.assertEqual(result[0].kind, "not-regular")

    @staticmethod
    def _try(fn):
        try:
            return fn()
        except Exception as exc:
            return exc

    def test_a_file_changed_after_the_scan_is_reported(self):
        p = self.make("pkg.zip")
        expected = self.identity("pkg.zip")
        time.sleep(0.01)
        p.write_bytes(ZIPISH + b"changed!")
        with self.assertRaises(LocalImportError) as ctx:
            self.copy("pkg.zip", expected=expected)
        self.assertEqual(ctx.exception.kind, "changed")
        self.assertIn("Scan the folder again", str(ctx.exception))
        self.assertFalse((self.tmp / "staged.bin").exists())

    def test_a_file_replaced_by_another_with_the_same_size_is_still_noticed(self):
        p = self.make("pkg.zip", ZIPISH + b"AAAA")
        expected = self.identity("pkg.zip")
        replacement = self.root_dir / "tmp.zip"
        replacement.write_bytes(ZIPISH + b"BBBB")
        os.replace(replacement, p)                                                   # new inode, same size
        with self.assertRaises(LocalImportError) as ctx:
            self.copy("pkg.zip", expected=expected)
        self.assertEqual(ctx.exception.kind, "changed")

    def test_a_file_still_being_written_is_not_imported_half_way(self):
        p = self.make("pkg.zip", ZIPISH + b"x" * 2_000_000)
        expected = self.identity("pkg.zip")

        def grow(stage, done, total):
            if done > 0 and not getattr(grow, "done", False):
                grow.done = True
                with open(p, "ab") as f:
                    f.write(b"more data arrived")
        with self.assertRaises(LocalImportError) as ctx:
            self.copy("pkg.zip", expected=expected, progress=grow)
        self.assertEqual(ctx.exception.kind, "changed")
        self.assertFalse((self.tmp / "staged.bin").exists())

    def test_removed_file(self):
        self.make("pkg.zip")
        expected = self.identity("pkg.zip")
        (self.root_dir / "pkg.zip").unlink()
        with self.assertRaises(LocalImportError) as ctx:
            self.copy("pkg.zip", expected=expected)
        self.assertEqual(ctx.exception.kind, "not-found")

    def test_size_limit_applies_while_copying(self):
        self.make("pkg.zip", ZIPISH + b"x" * 5000)
        with self.assertRaises(LocalImportError) as ctx:
            self.copy("pkg.zip", max_bytes=1000)
        self.assertEqual(ctx.exception.kind, "too-large")
        self.assertFalse((self.tmp / "staged.bin").exists())

    def test_traversal_in_a_forged_path_is_refused_before_any_filesystem_access(self):
        for bad in ("../outside/secret.zip", "/etc/passwd", "sub/../../outside/secret.zip", "a//b.zip"):
            with self.assertRaises(LocalImportError):
                self.copy(bad, expected=None)

    @unittest.skipUnless(NOT_ROOT, "permission checks are meaningless as root")
    def test_unreadable_file(self):
        p = self.make("pkg.zip")
        expected = self.identity("pkg.zip")
        p.chmod(0)
        with self.assertRaises(LocalImportError) as ctx:
            self.copy("pkg.zip", expected=expected)
        self.assertEqual(ctx.exception.kind, "permission")

    def test_destination_unwritable(self):
        self.make("pkg.zip")
        with self.assertRaises(LocalImportError) as ctx:
            localscan.copy_candidate(self.root, "pkg.zip", self.tmp / "nodir" / "x.bin", None, 10 ** 9)
        self.assertEqual(ctx.exception.kind, "disk")


if __name__ == "__main__":
    unittest.main()
