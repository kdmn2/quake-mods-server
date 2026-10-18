"""Conversion tests with independently specified expectations.

Precedence expectations come from Ironwail's common.c, not from the merge code under test:
  COM_AddGameDirectory pushes the game directory first, then pak0, pak1, ... each on the FRONT of
  the search list, so lookup order is pakN ... pak1, pak0, loose files.
  COM_FindFile scans a pak linearly and returns the first strcmp match.
  COM_LoadPackFile: more than 2048 entries is fatal; names are copied into 56 bytes (55 + NUL).
"""
import hashlib
import random
import shutil
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from tools.catalog import (
    build_single_pak_from_zip,
    classify_release_layout,
    inspect_zip_release,
    read_pak_directory,
    validate_package,
    validate_pak_paths,
)
from tools.convert_options import ConvertOptions


def make_pak_bytes(entries):
    """entries: list of (name, bytes). Duplicates allowed. Independent of tools.catalog."""
    body = bytearray()
    records = bytearray()
    for name, payload in entries:
        records += name.encode().ljust(56, b"\0") + struct.pack("<II", 12 + len(body), len(payload))
        body += payload
    return b"PACK" + struct.pack("<II", 12 + len(body), len(records)) + bytes(body) + bytes(records)


def parse_pak(path):
    """Independent PAK reader -> {name: bytes}; asserts the format invariants it relies on."""
    raw = Path(path).read_bytes()
    assert raw[:4] == b"PACK"
    dir_off, dir_len = struct.unpack("<II", raw[4:12])
    assert dir_len % 64 == 0 and dir_off + dir_len == len(raw)
    result = {}
    for i in range(dir_len // 64):
        rec = raw[dir_off + i * 64: dir_off + i * 64 + 64]
        name = rec[:56].split(b"\0")[0].decode()
        off, length = struct.unpack("<II", rec[56:])
        assert name not in result, f"duplicate output entry {name}"
        result[name] = raw[off:off + length]
    return result


def write_zip(path, members, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in members:
            zf.writestr(name, data)


def write_deflate64_zip(path, members):
    """Hand-assembled ZIP using method 9 (Deflate64). Data is a raw deflate stream of stored blocks,
    which is valid for both Deflate and Deflate64, so only a Deflate64-capable reader can open it."""
    out = bytearray()
    central = bytearray()
    for name, data in members:
        nb = name.encode()
        stream = b"\x01" + struct.pack("<HH", len(data), len(data) ^ 0xFFFF) + data
        crc = zlib.crc32(data) & 0xFFFFFFFF
        offset = len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 9, 0, 0x21, crc, len(stream), len(data), len(nb), 0) + nb + stream
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0, 9, 0, 0x21, crc, len(stream), len(data), len(nb), 0, 0, 0, 0, 0, offset) + nb
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(members), len(members), len(central), len(out), 0)
    Path(path).write_bytes(bytes(out) + bytes(central) + eocd)


class ZipConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-conv-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def convert(self, members, name="rel.zip", **kwargs):
        z = self.tmp / name
        write_zip(z, members)
        out = self.tmp / (name + ".pak")
        info = build_single_pak_from_zip(z, out, **kwargs)
        return parse_pak(out), info, out

    def assertRejected(self, members, fragment, **kwargs):
        z = self.tmp / "bad.zip"
        write_zip(z, members)
        out = self.tmp / "bad.pak"
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, out, **kwargs)
        self.assertIn(fragment, str(ctx.exception))
        self.assertFalse(out.exists())
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])

    # ---- precedence (engine semantics) -------------------------------------------------------
    PRECEDENCE_MEMBERS = None

    def _precedence_members(self):
        pak0 = make_pak_bytes([("a", b"p0-a"), ("b", b"p0-b"), ("c", b"p0-c")])
        pak1 = make_pak_bytes([("b", b"p1-b"), ("d", b"p1-d")])
        return [
            ("pak0.pak", pak0), ("pak1.pak", pak1),
            ("c", b"loose-c"), ("d", b"loose-d"), ("e", b"loose-e"),
        ]

    # Hand-derived from the engine rules above.
    EXPECTED_PRECEDENCE = {"a": b"p0-a", "b": b"p1-b", "c": b"p0-c", "d": b"p1-d", "e": b"loose-e"}

    def test_pak_beats_loose_and_higher_pak_beats_lower(self):
        got, info, _ = self.convert(self._precedence_members())
        self.assertEqual(got, self.EXPECTED_PRECEDENCE)
        self.assertEqual(
            sorted((o["path"], o["overridden"], o["by"]) for o in info["overrides"]),
            sorted([("b", "pak0.pak", "pak1.pak"), ("c", "loose:c", "pak0.pak"), ("d", "loose:d", "pak1.pak")]),
        )

    def test_result_is_independent_of_zip_member_order(self):
        members = self._precedence_members()
        outputs = set()
        rng = random.Random(1234)
        for i in range(8):
            order = members[:]
            rng.shuffle(order)
            if i == 0:
                order.reverse()
            got, _, out = self.convert(order, name=f"order{i}.zip")
            self.assertEqual(got, self.EXPECTED_PRECEDENCE, f"order {[n for n, _ in order]}")
            outputs.add(hashlib.sha256(out.read_bytes()).hexdigest())
        self.assertEqual(len(outputs), 1, "output bytes must not depend on ZIP enumeration order")

    def test_first_duplicate_inside_one_pak_wins(self):
        pak0 = make_pak_bytes([("maps/x.bsp", b"first"), ("maps/x.bsp", b"second"), ("y", b"y")])
        got, _, _ = self.convert([("pak0.pak", pak0)])
        self.assertEqual(got, {"maps/x.bsp": b"first", "y": b"y"})

    def test_noncontiguous_numbered_paks_rejected(self):
        p = make_pak_bytes([("a", b"a")])
        self.assertRejected([("pak0.pak", p), ("pak2.pak", p)], "contiguous")

    def test_non_numbered_pak_is_kept_as_opaque_file(self):
        inner = make_pak_bytes([("a", b"a")])
        got, _, _ = self.convert([("pak0.pak", make_pak_bytes([("m", b"m")])), ("maps/other.pak", inner)])
        self.assertEqual(got, {"m": b"m", "maps/other.pak": inner})

    def test_duplicate_zip_member_rejected(self):
        z = self.tmp / "dup.zip"
        with zipfile.ZipFile(z, "w") as zf, self.assertWarns(UserWarning):
            zf.writestr("maps/a.bsp", b"1")
            zf.writestr("maps/a.bsp", b"2")
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, self.tmp / "dup.pak")
        self.assertIn("duplicate path", str(ctx.exception))

    def test_case_only_difference_rejected(self):
        self.assertRejected([("maps/A.bsp", b"1"), ("maps/a.bsp", b"2")], "letter case")

    # ---- root inference vs. inclusion ---------------------------------------------------------
    def test_rooted_release_with_root_docs_keeps_everything(self):
        members = [
            ("pak0.pak", make_pak_bytes([("gfx/a.lmp", b"a")])), ("progs.dat", b"dat"), ("pun.txt", b"readme"),
            ("maps/m.bsp", b"m"), ("sound/s.wav", b"s"), ("music/t.ogg", b"t"), ("progs/p.mdl", b"p"),
        ]
        got, info, _ = self.convert(members)
        self.assertEqual(info["root_mapping"], "rooted")
        self.assertEqual(got, {"gfx/a.lmp": b"a", "progs.dat": b"dat", "pun.txt": b"readme", "maps/m.bsp": b"m",
                               "sound/s.wav": b"s", "music/t.ogg": b"t", "progs/p.mdl": b"p"})

    def test_aux_directory_beside_rooted_content_is_not_a_second_root(self):
        got, info, _ = self.convert([("pak0.pak", make_pak_bytes([("a", b"a")])), ("docs/readme.txt", b"r")])
        self.assertEqual((info["root_mapping"], got), ("rooted", {"a": b"a", "docs/readme.txt": b"r"}))

    def test_localization_and_map_source_folders_do_not_make_a_second_root(self):
        # Ironwail reads localization/loc_<lang>.txt (common.c); mapsrc/ holds map sources. Neither is a mod root.
        members = [("pak0.pak", make_pak_bytes([("a", b"a")])), ("localization/loc_english.txt", b"l"), ("mapsrc/a.map", b"m"), ("maps/b.bsp", b"b")]
        got, info, _, = self.convert(members)
        self.assertEqual((info["root_mapping"], sorted(got)), ("rooted", ["a", "localization/loc_english.txt", "maps/b.bsp", "mapsrc/a.map"]))

    def test_single_wrapper_is_stripped_only_when_common(self):
        members = [("qbj/pak0.pak", make_pak_bytes([("a", b"a")])), ("qbj/maps/m.bsp", b"m"), ("qbj/quake.rc", b"rc")]
        got, info, _ = self.convert(members)
        self.assertEqual((info["root_mapping"], info["wrapper"]), ("wrapper", "qbj"))
        self.assertEqual(got, {"a": b"a", "maps/m.bsp": b"m", "quake.rc": b"rc"})

    def test_wrapper_with_sibling_root_doc_keeps_the_doc(self):
        got, _, _ = self.convert([("mod/maps/m.bsp", b"m"), ("README.txt", b"doc")])
        self.assertEqual(got, {"maps/m.bsp": b"m", "README.txt": b"doc"})

    def test_collision_after_wrapper_strip_rejected(self):
        self.assertRejected([("mod/readme.txt", b"inner"), ("readme.txt", b"outer")], "collide")

    def test_nested_wrapper_rejected(self):
        self.assertRejected([("outer/inner/maps/m.bsp", b"m")], "nested wrapper")

    def test_two_mod_roots_and_mixed_roots_rejected(self):
        self.assertRejected([("moda/maps/a.bsp", b"a"), ("modb/maps/b.bsp", b"b")], "ambiguous")
        self.assertRejected([("pak0.pak", make_pak_bytes([("a", b"a")])), ("modb/maps/b.bsp", b"b")], "ambiguous")

    def test_bot_navigation_directory_is_game_content_not_a_second_root(self):
        """Layout observed in mc_retrojam3.zip (and 5 other releases in the Quaddicted catalogue): bots/navigation/*.nav
        beside maps/, gfx/, docs/ and src/. The bots/ folder is runtime data for bots, so the release is rooted."""
        lessons = b"how to install"
        members = [
            ("maps/mc_rj3_one.bsp", b"first BSP"), ("maps/mc_rj3_one.lit", b"lighting"),
            ("gfx/conback.lmp", b"conback"), ("bots/navigation/mc_rj3_one.nav", b"nav"),
            ("docs/mc_rj3_one.txt", lessons), ("src/mc_rj3_one.map", b"map source"),
            ("mapdb.json", b'{"maps": {}}'), ("mc_retrojam3_readme.txt", b"readme"),
            ("Remastered Map Center Retrojam3.lnk", b"windows shortcut"), ("descript.ion", b"descript"),
        ]
        got, info, _ = self.convert(members, name="mc_retrojam3.zip")
        self.assertEqual(info["root_mapping"], "rooted")
        self.assertEqual(info["wrapper"], None)
        # every member is kept with its exact path and bytes, root-level files included
        self.assertEqual(got, dict(members))

    def test_macosx_resource_forks_are_ignored(self):
        got, _, _ = self.convert([("mod/maps/m.bsp", b"m"), ("__MACOSX/mod/._m.bsp", b"junk")])
        self.assertEqual(got, {"maps/m.bsp": b"m"})

    def test_classifier_is_pure_function_of_paths(self):
        a = classify_release_layout(["maps/a", "progs.dat", "pun.txt"])
        b = classify_release_layout(["pun.txt", "progs.dat", "maps/a"])
        self.assertEqual(a, b)

    def test_blockers_name_the_unrecognised_directories(self):
        """A rejection must say why: which top-level folders were unmatched, and what was matched."""
        layout = classify_release_layout(["maps/a.bsp", "botdir/x.nav", "docs/readme.txt", "readme.txt"])
        self.assertEqual(layout["root_mapping"], "ambiguous")
        self.assertEqual(layout["unknown_dirs"], ["botdir"])
        z = self.tmp / "amb.zip"
        write_zip(z, [("maps/a.bsp", b"a"), ("botdir/x.nav", b"n"), ("progs.dat", b"p")])
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, self.tmp / "amb.pak")
        message = str(ctx.exception)
        self.assertIn("botdir", message)
        self.assertIn("maps", message)
        self.assertIn("Treat the archive root as the mod", message)

    def test_inspect_reports_stripped_paths_without_decompressing(self):
        z = self.tmp / "i.zip"
        write_deflate64_zip(z, [("w/pak0.pak", make_pak_bytes([("a", b"a")])), ("w/maps/m.bsp", b"m")])
        layout = inspect_zip_release(z)
        self.assertEqual((layout["root_mapping"], layout["wrapper_dirs"]), ("wrapper", ["w"]))
        self.assertEqual([p["path"] for p in layout["embedded_paks"]], ["pak0.pak"])
        self.assertEqual([p["path"] for p in layout["loose"]], ["maps/m.bsp"])

    # ---- engine limits -------------------------------------------------------------------------
    def test_engine_entry_and_name_limits(self):
        ok_name = "maps/" + "x" * 50                       # 55 bytes
        got, _, _ = self.convert([(ok_name, b"1")])
        self.assertIn(ok_name, got)
        # Strict mode (the historical default) rejects an overlong name outright.
        self.assertRejected([("maps/" + "x" * 51, b"1")], "too long",
                             options=ConvertOptions(skip_overlong_names=False))
        # Default mode (skip on): every entry overlong → build refused with a clear message.
        long_name = "maps/" + "x" * 51
        self.assertRejected([(long_name, b"1")], "Refusing to write an empty package")
        # Mixed payload: keepable entries survive, the overlong one is recorded in the report.
        z2 = self.tmp / "mixed.zip"
        keepable_path = "maps/a.bsp"
        write_zip(z2, [(keepable_path, b"A"), (long_name, b"X")])
        out2 = self.tmp / "mixed.pak"
        info2 = build_single_pak_from_zip(z2, out2)
        self.assertTrue(out2.is_file())
        self.assertEqual(parse_pak(out2), {keepable_path: b"A"})            # only the keepable entry survives
        self.assertIsNotNone(info2["excluded"])                            # the overlong one is in the report
        self.assertEqual(info2["excluded"]["total"], 1)
        # The exclusion reason is preserved verbatim; the rule bucket identifies it.
        by_rule = info2["excluded"]["by_rule"]
        self.assertEqual(sum(by_rule.values()), 1)
        self.assertIn("too long", next(iter(by_rule)))
        many = [(f"maps/m{i}.bsp", b"") for i in range(2049)]
        self.assertRejected(many, "2048")
        got, _, _ = self.convert(many[:2048], name="max.zip")
        self.assertEqual(len(got), 2048)

    # ---- archive safety ------------------------------------------------------------------------
    def test_unsafe_member_names_rejected(self):
        for bad in ("../evil", "maps/../../evil", "/abs/evil", "C:/evil", "maps\\..\\evil"):
            with self.subTest(bad=bad):
                self.assertRejected([(bad, b"x")], "Unsafe")

    def test_symlink_member_rejected(self):
        z = self.tmp / "sym.zip"
        with zipfile.ZipFile(z, "w") as zf:
            info = zipfile.ZipInfo("maps/link")
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, self.tmp / "o.pak")
        self.assertIn("symlink", str(ctx.exception))

    def test_uncompressed_size_limit_enforced(self):
        self.assertRejected([("maps/a", b"a" * 600), ("maps/b", b"b" * 600)], "exceeds limit", max_uncompressed_size=1000)

    def test_file_count_limit_enforced(self):
        self.assertRejected([(f"maps/{i}", b"") for i in range(10)], "too many", max_files=5)

    def test_failed_conversion_leaves_existing_target_untouched(self):
        z = self.tmp / "ok.zip"
        write_zip(z, [("moda/maps/a", b"a"), ("modb/maps/b", b"b")])
        out = self.tmp / "existing.pak"
        out.write_bytes(b"previous good bytes")
        with self.assertRaises(ValueError):
            build_single_pak_from_zip(z, out)
        self.assertEqual(out.read_bytes(), b"previous good bytes")
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])

    # ---- Deflate64 (Python's zipfile cannot decode it; system unzip can) --------------------------
    @unittest.skipUnless(shutil.which("unzip"), "system unzip required for Deflate64 fallback")
    def test_deflate64_members_converted_via_unzip(self):
        z = self.tmp / "d64.zip"
        payload = bytes(range(256)) * 40
        write_deflate64_zip(z, [("qbj/pak0.pak", make_pak_bytes([("gfx/p", b"pp"), ("gfx/q", b"qq")])),
                                ("qbj/gfx/q", b"loose-q"), ("qbj/maps/[odd]*.bsp", payload)])
        with zipfile.ZipFile(z) as zf, self.assertRaises(NotImplementedError):
            zf.read("qbj/gfx/q")
        out = self.tmp / "d64.pak"
        info = build_single_pak_from_zip(z, out)
        self.assertEqual(parse_pak(out), {"gfx/p": b"pp", "gfx/q": b"qq", "maps/[odd]*.bsp": payload})
        self.assertEqual(info["wrapper"], "qbj")

    @unittest.skipUnless(shutil.which("unzip"), "system unzip required for Deflate64 fallback")
    def test_deflate64_truncated_or_lying_size_rejected(self):
        z = self.tmp / "lie.zip"
        write_deflate64_zip(z, [("maps/a", b"abcdef")])
        raw = bytearray(z.read_bytes())
        # declare 3 bytes uncompressed in the central directory only
        cd = raw.index(b"PK\x01\x02")
        struct.pack_into("<I", raw, cd + 24, 3)
        z.write_bytes(bytes(raw))
        with self.assertRaises(ValueError):
            build_single_pak_from_zip(z, self.tmp / "lie.pak")


class OverlongEntryNameTests(unittest.TestCase):
    """Skip-with-warning vs strict rejection for entries whose final PAK path exceeds
    Ironwail's 55-byte dpackfile_t.name[56] limit. Covers the user's example
    (AlbumArt_...{UUID}_Large.jpg) and the related edge cases."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-overlong-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # 63-byte path used in the user's report; the B5020207-… GUID is 36 chars,
        # AlbumArt_{…}_Large.jpg = 6 + 36 + 11 = 53, plus music/ prefix = 59 bytes.
        # Use a path that is exactly the byte count reported: 63 bytes total.
        self.long_name = "music/AlbumArt_{B5020207-474E-4720-D903-8139E4180600}_Large.jpg"
        self.keepables = [("maps/start.bsp", b"BSP-DATA-START"),
                          ("maps/end.bsp", b"BSP-DATA-END"),
                          ("sound/shot.wav", b"RIFF-WAVE-DATA")]

    def _zip_with(self, *entries):
        z = self.tmp / "src.zip"
        write_zip(z, entries)
        return z

    def test_default_skip_omits_overlong_entry_and_keeps_everything_else(self):
        """New jobs default to skip-overlong-names. The overlong entry is dropped with a
        clear exclusion record; all kept entries keep their exact bytes; no name truncation."""
        z = self._zip_with(*self.keepables, (self.long_name, b"ALBUM-ART-BYTES"))
        out = self.tmp / "out.pak"
        info = build_single_pak_from_zip(z, out)                  # default = skip on
        self.assertTrue(out.is_file())
        got = parse_pak(out)
        self.assertEqual(set(got.keys()), {p for p, _ in self.keepables})
        # Each kept entry's bytes are byte-for-byte preserved.
        for name, payload in self.keepables:
            self.assertEqual(got[name], payload, f"bytes of {name} changed")
        # The overlong entry is in the exclusion record with the documented wording.
        excl = info["excluded"]
        self.assertIsNotNone(excl)
        self.assertEqual(excl["total"], 1)
        self.assertEqual(excl["bytes"], len(b"ALBUM-ART-BYTES"))
        # The reason names the entry, the actual byte length, and the limit.
        rule_text = next(iter(excl["by_rule"]))
        self.assertIn("too long", rule_text)
        self.assertIn(str(len(self.long_name.encode("utf-8"))), rule_text)
        self.assertIn(str(55), rule_text)
        self.assertEqual(excl["sample"][0]["path"], self.long_name)

    def test_explicit_skip_off_strictly_rejects_overlong_entry(self):
        """Per-item or settings explicit False → strict rejection. Nothing is dropped."""
        z = self._zip_with(*self.keepables, (self.long_name, b"X"))
        out = self.tmp / "out.pak"
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, out, options=ConvertOptions(skip_overlong_names=False))
        self.assertIn("Entry name too long for PAK", str(ctx.exception))
        self.assertIn("63 > 55", str(ctx.exception))               # actual byte counts
        self.assertFalse(out.is_file())                             # no partial output

    def test_exact_byte_boundary_55_passes_through(self):
        """A 55-byte path is exactly at the limit and must be kept."""
        ok_path = "maps/" + "x" * 50                                  # 55 bytes total
        z = self._zip_with((ok_path, b"OK"))
        out = self.tmp / "out.pak"
        info = build_single_pak_from_zip(z, out)
        self.assertIn(ok_path, parse_pak(out))
        # The entry is kept, so no exclusion should be recorded for it.
        if info["excluded"]:
            for sample in info["excluded"]["sample"]:
                self.assertNotEqual(sample["path"], ok_path)

    def test_one_byte_over_the_boundary_56_is_omitted_in_default_mode(self):
        """A 56-byte path is over the limit; default skip mode drops it with a clear record."""
        over_by_one = "maps/" + "x" * 51                              # 56 bytes total
        z = self._zip_with(("maps/keep.bsp", b"K"), (over_by_one, b"X"))
        out = self.tmp / "out.pak"
        info = build_single_pak_from_zip(z, out)
        self.assertIn("maps/keep.bsp", parse_pak(out))
        self.assertNotIn(over_by_one, parse_pak(out))
        rule_text = next(iter(info["excluded"]["by_rule"]))
        self.assertIn("56 > 55", rule_text)

    def test_multibyte_name_uses_utf8_byte_length_not_char_count(self):
        """A name with multi-byte characters is counted by UTF-8 bytes (the format's actual encoding)."""
        # 10 ASCII chars + one 2-byte UTF-8 char (é = 2 bytes) = 12 bytes
        # A 53-byte path with several é characters at the end:
        multibyte_name = "maps/αβγδεζηθικλμνξοπ"  # 14 Greek chars * 2 bytes = 28 bytes + 5 ascii = 33 bytes; still under limit
        # Make it overlong: prefix + many multibyte chars
        multibyte_overlong = "maps/" + "α" * 30      # 5 + 60 = 65 bytes (over 55)
        z = self.tmp / "src.zip"
        write_zip(z, [("maps/αβ.bsp", b"B"), (multibyte_overlong, b"X")])
        out = self.tmp / "out.pak"
        info = build_single_pak_from_zip(z, out)
        self.assertIn("maps/αβ.bsp", parse_pak(out))
        self.assertNotIn(multibyte_overlong, parse_pak(out))
        rule_text = next(iter(info["excluded"]["by_rule"]))
        # 65 bytes (5 ascii + 30 * 2-byte UTF-8) > 55.
        self.assertIn(f"{len(multibyte_overlong.encode('utf-8'))} > 55", rule_text)

    def test_outer_wrapper_removal_makes_a_path_that_would_have_fit_now_fit(self):
        """A name that's overlong ONLY because of an outer wrapper directory is still
        overlong after wrapper removal — this test pins the principle that validation
        happens after root-mapping/wrapper-removal."""
        # Without the wrapper: 56 bytes ("maps/" + "x"*51) — overlong.
        overlong_path = "maps/" + "x" * 51
        # ZIP wraps it in 'release/'; after wrapper removal the final path is the same
        # 56 bytes, so skip mode still drops it. (The point is to document behaviour.)
        z = self.tmp / "src.zip"
        write_zip(z, [(f"release/{overlong_path}", b"X"), ("release/maps/keep.bsp", b"K")])
        out = self.tmp / "out.pak"
        info = build_single_pak_from_zip(z, out)
        # The release/ wrapper is stripped; the overlong final path is dropped, the keepable one survives.
        self.assertIn("maps/keep.bsp", parse_pak(out))
        self.assertNotIn(overlong_path, parse_pak(out))
        self.assertEqual(info["excluded"]["total"], 1)

    def test_outer_wrapper_removal_can_rescue_a_path_that_otherwise_fits(self):
        """A path that is overlong in the raw archive but fits after wrapper removal is kept."""
        # Raw: release/maps/<51 x's> = 7 + 51 = 58 bytes (overlong)
        # After wrapper removal: maps/<51 x's> = 56 bytes (still overlong by 1) -- pick a case that fits.
        # Use a wrapper that consumes exactly 5 bytes of headroom:
        #   raw: w/maps/<49 x's> = 2 + 5 + 49 = 56 (overlong)
        #   after: maps/<49 x's> = 55 (fits)
        raw_path = "w/maps/" + "x" * 49                  # 56 bytes raw
        fits_after = "maps/" + "x" * 49                  # 55 bytes after wrapper removal
        z = self.tmp / "src.zip"
        write_zip(z, [(raw_path, b"X"), ("w/maps/keep.bsp", b"K")])
        out = self.tmp / "out.pak"
        info = build_single_pak_from_zip(z, out)
        got = parse_pak(out)
        self.assertIn(fits_after, got)                      # wrapper-stripped path fits
        self.assertNotIn(raw_path, got)
        self.assertIsNone(info["excluded"])                # nothing else was dropped

    def test_skip_default_in_convert_options(self):
        """ConvertOptions() defaults to skip=True; from_dict({}) defaults to True for the same reason."""
        self.assertTrue(ConvertOptions().skip_overlong_names)
        self.assertTrue(ConvertOptions.from_dict({}).skip_overlong_names)
        # Explicit False round-trips.
        self.assertFalse(ConvertOptions.from_dict({"skip_overlong_names": "0"}).skip_overlong_names)
        self.assertFalse(ConvertOptions.from_dict({"skip_overlong_names": False}).skip_overlong_names)
        # Stored JSON missing the key defaults to True (preserves "skip on" for older records).
        self.assertTrue(ConvertOptions.from_dict({"reencode_textures": True}).skip_overlong_names)

    def test_no_usable_entries_remaining_is_rejected_not_silently_packed(self):
        """Every entry overlong → refuse to write an empty package with a clear message."""
        z = self.tmp / "src.zip"
        write_zip(z, [(self.long_name, b"X"), ("sound/" + "x" * 49 + ".wav", b"X")])  # both overlong
        out = self.tmp / "out.pak"
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, out)
        self.assertIn("Refusing to write an empty package", str(ctx.exception))
        self.assertIn("too long", str(ctx.exception))
        self.assertFalse(out.is_file())                     # no partial output

    def test_existing_traversal_and_collision_checks_still_reject_unsafe_input(self):
        """The overlong-name policy does NOT become a general 'ignore invalid files' switch:
        traversal, collision, and other archive safety checks still raise."""
        for bad in ("../evil", "/abs/evil", "C:/evil"):
            with self.subTest(bad=bad):
                z = self.tmp / "bad.zip"
                write_zip(z, [(bad, b"x")])
                with self.assertRaises(ValueError):
                    build_single_pak_from_zip(z, self.tmp / "bad.pak")

    def test_report_keeps_full_path_text_for_overlong_exclusion(self):
        """The conversion report preserves the full path text of the excluded entry
        (the GUI may shorten it visually but the stored record does not)."""
        z = self.tmp / "src.zip"
        write_zip(z, [("maps/keep.bsp", b"K"), (self.long_name, b"X")])
        out = self.tmp / "out.pak"
        info = build_single_pak_from_zip(z, out)
        # The full path is in the sample (not truncated):
        self.assertEqual(info["excluded"]["sample"][0]["path"], self.long_name)
        self.assertEqual(len(info["excluded"]["sample"][0]["path"]), 63)


class PakValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-pakval-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def pak(self, entries=None, raw=None):
        p = self.tmp / "t.pak"
        p.write_bytes(raw if raw is not None else make_pak_bytes(entries))
        return p

    def test_valid_pak_lists_entries(self):
        p = self.pak([("a", b"123"), ("b/c", b"45")])
        self.assertEqual([(e["path"], e["size"]) for e in read_pak_directory(p)], [("a", 3), ("b/c", 2)])
        self.assertEqual(validate_package(p)["size"], p.stat().st_size)

    def test_unsafe_name_in_later_entry_is_detected(self):
        # the previous validator walked the table with a 72-byte stride and never reached later records
        for bad in ("../x", "/abs", "a/../b", "a\\b", "a//b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_pak_paths(self.pak([("ok1", b"1"), ("ok2", b"2"), ("ok3", b"3"), (bad, b"4")]))

    def test_structural_problems_rejected(self):
        good = make_pak_bytes([("a", b"abc")])
        cases = {
            "bad magic": b"XXXX" + good[4:],
            "dir past end": good[:4] + struct.pack("<II", 12, 9999) + good[12:],
            "dir len not multiple of 64": good[:8] + struct.pack("<I", 65) + good[12:],
            "empty directory": b"PACK" + struct.pack("<II", 12, 0),
            "truncated header": b"PACK",
        }
        for label, raw in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                validate_pak_paths(self.pak(raw=raw))

    def test_entry_out_of_bounds_and_unterminated_name_rejected(self):
        good = bytearray(make_pak_bytes([("a", b"abc")]))
        dir_off = struct.unpack("<I", good[4:8])[0]
        oob = bytearray(good)
        struct.pack_into("<II", oob, dir_off + 56, 12, 99999)
        with self.assertRaises(ValueError):
            validate_pak_paths(self.pak(raw=bytes(oob)))
        unterminated = bytearray(good)
        unterminated[dir_off:dir_off + 56] = b"n" * 56
        with self.assertRaises(ValueError):
            validate_pak_paths(self.pak(raw=bytes(unterminated)))

    def test_more_than_engine_limit_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_pak_paths(self.pak([(f"f{i}", b"") for i in range(2049)]))
        self.assertIn("2048", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
