"""Opt-in conversion options. Expected values are written by hand from the rules in tools/convert_options.py's docstrings and
from Ironwail's image.c (png -> tga -> jpg, always RGBA); fixtures (TGA/PAK/ZIP writers) are independent of the code under test."""
import hashlib
import io
import json
import random
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.catalog import MAX_PAK_ENTRIES, build_single_pak_from_zip, plan_zip_release
from tools.convert_options import ConvertOptions, PRESETS, matches, options_from_form
from tools.pak_report import parse_mapdb_name
from tests.test_zip_conversion import make_pak_bytes, parse_pak, write_zip

STB_H = Path(__file__).resolve().parent.parent / "ironwail-src" / "Quake" / "stb_image.h"


def tga_bytes(width, height, rows, channels=4, rle=False, origin_top=False, id_field=b"", desc_extra=0):
    """rows: top-down list of rows of pixel tuples (RGB or RGBA). Written the way the TGA spec describes it."""
    bpp = channels * 8
    desc = (0x20 if origin_top else 0) | (8 if channels == 4 else 0) | desc_extra
    out = bytearray([len(id_field), 0, 10 if rle else 2]) + bytearray(5) + struct.pack("<HHHH", 0, 0, width, height) + bytes([bpp, desc]) + id_field
    ordered = rows if origin_top else list(reversed(rows))
    pixels = [bytes([p[2], p[1], p[0]] + ([p[3]] if channels == 4 else [])) for row in ordered for p in row]
    if not rle:
        for px in pixels:
            out += px
        return bytes(out)
    i = 0
    while i < len(pixels):                 # one packet stream over the whole image (packets may cross rows)
        run = 1
        while i + run < len(pixels) and pixels[i + run] == pixels[i] and run < 128:
            run += 1
        if run >= 2:
            out.append(0x80 | (run - 1))
            out += pixels[i]
            i += run
        else:
            j = i + 1
            while j < len(pixels) and j - i < 128 and not (j + 1 < len(pixels) and pixels[j] == pixels[j + 1]):
                j += 1
            out.append(j - i - 1)
            for px in pixels[i:j]:
                out += px
            i = j
    return bytes(out)


def gradient(width, height, channels=4, seed=1):
    rng = random.Random(seed)
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            base = ((x * 255) // max(1, width - 1), (y * 255) // max(1, height - 1), ((x + y) * 7) % 256)
            row.append(base + ((255 - (x * 3) % 256,) if channels == 4 else ()))
        rows.append(row)
    rows[0][0] = (0, 0, 0) + ((0,) if channels == 4 else ())             # asymmetric corner so flips are detectable
    return rows


def flatten(rows):
    return b"".join(bytes(p) for row in rows for p in row)


def png_pixels(png):
    from PIL import Image
    with Image.open(io.BytesIO(png)) as im:
        im.load()
        return im.mode, im.size, im.tobytes()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-opt-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.n = 0

    def build(self, members, options=None, progress=None, **kw):
        self.n += 1
        z = self.tmp / f"r{self.n}.zip"
        write_zip(z, members)
        out = self.tmp / f"o{self.n}.pak"
        info = build_single_pak_from_zip(z, out, options=options, progress=progress, **kw)
        return parse_pak(out), info, out, z

    def zip(self, members):
        self.n += 1
        z = self.tmp / f"r{self.n}.zip"
        write_zip(z, members)
        return z


class OptionParsingTests(unittest.TestCase):
    def test_defaults_and_roundtrip(self):
        self.assertTrue(ConvertOptions().is_default)
        o = ConvertOptions.from_dict({"exclude_presets": ["authoring", "authoring"], "exclude_patterns": "a*.bak\n\n /x/ ", "root": "qbj",
                                      "reencode_textures": "on", "mapdb": ["Name", "dir"]})
        self.assertEqual((o.exclude_presets, o.exclude_patterns, o.root, o.reencode_textures, o.reencode_skyboxes, o.mapdb),
                         (("authoring",), ("a*.bak", "/x/"), "qbj", True, False, ("Name", "dir")))
        self.assertEqual(ConvertOptions.from_dict(o.to_dict()), o)
        self.assertEqual(ConvertOptions.from_dict(None), ConvertOptions())

    def test_rejects_bad_input(self):
        bad = [{"exclude_presets": ["nope"]}, {"exclude_patterns": ["*"]}, {"exclude_patterns": ["../x"]}, {"exclude_patterns": ["a" * 200]},
               {"exclude_patterns": [f"p{i}" for i in range(61)]}, {"exclude_patterns": ["/"]}, {"root": "a/b"}, {"root": ".."}, {"mapdb": ["only-one"]},
               {"mapdb": ["", "dir"]}, {"exclude_presets": [5]}, {"exclude_patterns": 7}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ConvertOptions.from_dict(raw)

    def test_form_parsing(self):
        from werkzeug.datastructures import MultiDict
        form = MultiDict([("exclude_presets", "junk"), ("exclude_presets", "docs"), ("exclude_patterns", "*.bak"), ("reencode_skyboxes", "1"),
                          ("generate_mapdb", "1"), ("mapdb_name", "My Mod"), ("mapdb_dir", "mymod")])
        o = options_from_form(form)
        self.assertEqual((o.exclude_presets, o.exclude_patterns, o.reencode_skyboxes, o.mapdb), (("junk", "docs"), ("*.bak",), True, ("My Mod", "mymod")))
        self.assertTrue(options_from_form(MultiDict()).is_default)

    def test_pattern_matching_table(self):
        table = [
            ("*.map", "maps/a.map", True), ("*.map", "a.MAP", True), ("*.map", "maps/a.bsp", False),
            ("mapsrc/", "mapsrc/a.map", True), ("mapsrc/", "x/MapSrc/deep/a.map", True), ("mapsrc/", "mapsrc", False), ("mapsrc/", "maps/a.map", False),
            ("/*.txt", "pun.txt", True), ("/*.txt", "localization/loc_en.txt", False), ("/*.txt", "a/b.txt", False),
            ("sound/*.wav", "sound/a.wav", True), ("sound/*.wav", "x/sound/a.wav", True), ("sound/*.wav", "sound/sub/a.wav", False),
            ("thumbs.db", "models/Thumbs.db", True), ("progs/*.mdl", "progs/a.mdl", True), ("a?c.txt", "abc.txt", True),
            ("/maps/*.lit", "maps/a.lit", True), ("/maps/*.lit", "x/maps/a.lit", False),
        ]
        for pattern, path, want in table:
            self.assertEqual(matches(pattern, path), want, (pattern, path))

    def test_presets_are_what_the_ui_says(self):
        self.assertEqual(sorted(PRESETS), ["authoring", "docs", "junk"])
        for path in ("mapsrc/a.map", "src/ai.qc", "maps/x.map", "copper.def", "copper.fgd", "a.psd", "src/progs.src"):
            self.assertTrue(any(matches(p, path) for p in PRESETS["authoring"]["patterns"]), path)
        for path in ("maps/a.bsp", "progs.dat", "progs/a.mdl", "quake.rc", "sound/a.wav", "gfx/a.tga", "maps/a.lit", "localization/loc_en.txt"):
            for name, preset in PRESETS.items():
                self.assertFalse(any(matches(p, path) for p in preset["patterns"]), (name, path))


class ExclusionTests(Base):
    MEMBERS = lambda self: [
        ("pak0.pak", make_pak_bytes([("maps/a.bsp", b"bsp"), ("maps/a.map", b"src-in-pak"), ("src/x.qc", b"qc-in-pak")])),
        ("maps/b.bsp", b"b"), ("maps/b.map", b"src"), ("mapsrc/c.map", b"c"), ("Thumbs.db", b"db"), ("pun.txt", b"doc"),
        ("localization/loc_en.txt", b"loc"), ("progs.dat", b"dat"),
    ]

    def test_default_options_do_not_change_the_output(self):
        a = self.build(self.MEMBERS())[2].read_bytes()
        b = self.build(self.MEMBERS(), ConvertOptions())[2].read_bytes()
        self.assertEqual(a, b)

    def test_presets_remove_exactly_their_matches_from_loose_files_and_embedded_paks(self):
        got, info, _, _ = self.build(self.MEMBERS(), ConvertOptions(exclude_presets=("authoring", "junk", "docs")))
        self.assertEqual(sorted(got), ["localization/loc_en.txt", "maps/a.bsp", "maps/b.bsp", "progs.dat"])
        self.assertEqual(got["maps/a.bsp"], b"bsp")
        ex = info["excluded"]
        self.assertEqual(ex["total"], 6)
        self.assertEqual(sorted(e["path"] for e in ex["sample"]), sorted(["maps/a.map", "src/x.qc", "maps/b.map", "mapsrc/c.map", "Thumbs.db", "pun.txt"]))
        self.assertEqual(ex["by_rule"].get("preset:docs (/*.txt)"), 1)          # only the root-level readme, not localization/loc_en.txt

    def test_custom_patterns_and_record(self):
        got, info, _, _ = self.build(self.MEMBERS(), ConvertOptions(exclude_patterns=("*.dat", "localization/")))
        self.assertNotIn("progs.dat", got)
        self.assertNotIn("localization/loc_en.txt", got)
        self.assertIn("pun.txt", got)
        self.assertEqual(info["excluded"]["by_rule"], {"custom (*.dat)": 1, "custom (localization/)": 1})
        self.assertEqual(info["options"]["exclude_patterns"], ["*.dat", "localization/"])

    def test_excluded_copies_do_not_count_as_overrides(self):
        members = [("pak0.pak", make_pak_bytes([("src/q.qc", b"in-pak"), ("maps/a.bsp", b"b")])), ("src/q.qc", b"loose")]
        _, info, _, _ = self.build(members)
        self.assertEqual([o["path"] for o in info["overrides"]], ["src/q.qc"])        # without options the PAK copy wins over the loose one
        got, info, _, _ = self.build(members, ConvertOptions(exclude_presets=("authoring",)))
        self.assertEqual((info["overrides"], sorted(got), info["excluded"]["total"]), ([], ["maps/a.bsp"], 2))

    def test_excluding_everything_is_rejected(self):
        z = self.zip([("maps/a.map", b"x")])
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, self.tmp / "none.pak", options=ConvertOptions(exclude_patterns=("*.map",)))
        self.assertIn("does not contain any representable files", str(ctx.exception))
        self.assertFalse((self.tmp / "none.pak").exists())

    def test_exclusion_can_bring_a_release_under_the_entry_limit(self):
        members = [(f"maps/m{i}.bsp", b"x") for i in range(MAX_PAK_ENTRIES - 10)] + [(f"maps/s{i}.map", b"s") for i in range(50)]
        z = self.zip(members)
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, self.tmp / "big.pak")
        self.assertIn(str(MAX_PAK_ENTRIES), str(ctx.exception))
        plan = plan_zip_release(z)
        self.assertTrue(any("2048" in b for b in plan["blockers"]))
        self.assertEqual(plan["entries"], MAX_PAK_ENTRIES + 40)
        plan = plan_zip_release(z, ConvertOptions(exclude_presets=("authoring",)))
        self.assertEqual((plan["blockers"], plan["entries"]), ([], MAX_PAK_ENTRIES - 10))
        got, _, _, _ = self.build(members, ConvertOptions(exclude_presets=("authoring",)))
        self.assertEqual(len(got), MAX_PAK_ENTRIES - 10)


class RootSelectionTests(Base):
    AMBIG = lambda self: [("moda/maps/a.bsp", b"a"), ("moda/readme.txt", b"ra"), ("modb/maps/b.bsp", b"b"), ("notes.txt", b"n")]

    def test_ambiguous_without_selection_still_rejected(self):
        z = self.zip(self.AMBIG())
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, self.tmp / "x.pak")
        self.assertIn("unrecognised top-level directories: moda, modb", str(ctx.exception))
        self.assertIn("Treat the archive root as the mod", str(ctx.exception))

    def test_selected_root_is_stripped_and_the_rest_recorded_as_excluded(self):
        got, info, _, _ = self.build(self.AMBIG(), ConvertOptions(root="moda"))
        self.assertEqual(got, {"maps/a.bsp": b"a", "readme.txt": b"ra"})
        self.assertEqual((info["root_mapping"], info["wrapper"]), ("selected", "moda"))
        self.assertEqual(sorted(e["path"] for e in info["excluded"]["sample"]), ["modb/maps/b.bsp", "notes.txt"])
        self.assertEqual({e["reason"] for e in info["excluded"]["sample"]}, {"outside the selected root directory"})

    def test_selection_overrides_automatic_rooting(self):
        members = [("maps/a.bsp", b"a"), ("extra/x.cfg", b"x")]
        got, info, _, _ = self.build(members, ConvertOptions(root="extra"))
        self.assertEqual(got, {"x.cfg": b"x"})

    def test_unknown_root_is_rejected(self):
        z = self.zip(self.AMBIG())
        with self.assertRaises(ValueError) as ctx:
            build_single_pak_from_zip(z, self.tmp / "x.pak", options=ConvertOptions(root="nope"))
        self.assertIn("not in the archive", str(ctx.exception))

    def test_plan_for_an_ambiguous_release_offers_the_choices(self):
        plan = plan_zip_release(self.zip(self.AMBIG()))
        self.assertEqual(plan["top_level_dirs"], ["moda", "modb"])
        self.assertTrue(any("ambiguous" in b for b in plan["blockers"]))
        plan = plan_zip_release(self.zip(self.AMBIG()), ConvertOptions(root="modb"))
        self.assertEqual((plan["blockers"], plan["entries"], plan["wrapper"]), ([], 1, "modb"))

    def test_rooted_choice_keeps_every_top_level_folder_as_it_is(self):
        """The administrator's explicit fallback when the unrecognised folders belong to the release."""
        got, info, _, _ = self.build(self.AMBIG(), ConvertOptions(rooted=True))
        self.assertEqual(got, {"moda/maps/a.bsp": b"a", "moda/readme.txt": b"ra", "modb/maps/b.bsp": b"b", "notes.txt": b"n"})
        self.assertEqual((info["root_mapping"], info["wrapper"]), ("selected-rooted", None))
        self.assertFalse(info["excluded"] and info["excluded"]["total"])

    def test_rooted_choice_builds_after_the_plan_preview_clears_the_blocker(self):
        z = self.zip(self.AMBIG())
        plan = plan_zip_release(z, ConvertOptions(rooted=True))
        self.assertEqual((plan["root_mapping"], plan["blockers"], plan["entries"]), ("selected-rooted", [], 4))

    def test_root_and_rooted_choice_are_mutually_exclusive(self):
        with self.assertRaises(ValueError) as ctx:
            ConvertOptions.from_dict({"root": "moda", "rooted": True})
        self.assertIn("not both", str(ctx.exception))
        got, info, _, _ = self.build(self.AMBIG(), ConvertOptions(root="moda", rooted=False))
        self.assertEqual(info["root_mapping"], "selected")

    def test_rooted_choice_is_recorded_in_the_options_and_not_implicit(self):
        _, info, _, _ = self.build(self.AMBIG(), ConvertOptions(rooted=True))
        self.assertTrue(info["options"]["rooted"])
        self.assertEqual(info["root_mapping"], "selected-rooted")


class PlanTests(Base):
    def test_plan_matches_what_the_build_produces(self):
        pak0 = make_pak_bytes([("maps/a.bsp", b"pak"), ("sound/s.wav", b"w"), ("maps/dup.lit", b"pak-lit")])
        members = [("w/pak0.pak", pak0), ("w/maps/dup.lit", b"loose-lit"), ("w/maps/z.map", b"zz"), ("w/gfx/a.tga", b"t"), ("w/pun.txt", b"d")]
        opts = ConvertOptions(exclude_presets=("authoring",))
        z = self.zip(members)
        plan = plan_zip_release(z, opts)
        got, info, _, _ = self.build(members, opts)
        self.assertEqual(plan["entries"], len(got))
        self.assertEqual(plan["payload_bytes"], sum(len(v) for v in got.values()))
        self.assertEqual((plan["overrides_total"], plan["overrides"]), (len(info["overrides"]), info["overrides"]))
        self.assertEqual(plan["excluded"], info["excluded"])
        self.assertEqual((plan["root_mapping"], plan["wrapper"]), (info["root_mapping"], info["wrapper"]))
        self.assertEqual(plan["embedded_paks"], [{"name": "pak0.pak", "bytes": len(pak0), "entries": 3}])
        self.assertEqual(plan["loose_files"], info["loose_files"])

    def test_plan_reports_preset_sizes_and_image_candidates(self):
        members = [("maps/a.map", b"x" * 10), ("src/b.qc", b"y" * 5), ("gfx/env/s_up.tga", b"t" * 7), ("gfx/a.tga", b"u" * 3), ("gfx/a.png", b"p"),
                   ("gfx/b.tga", b"v" * 4), ("maps/a.bsp", b"b")]
        plan = plan_zip_release(self.zip(members))
        self.assertEqual(plan["preset_stats"]["authoring"]["files"], 2)
        self.assertEqual(plan["preset_stats"]["authoring"]["bytes"], 15)
        # gfx/a.tga is shadowed by gfx/a.png (png is tried first), so it is not a candidate
        self.assertEqual(plan["image_candidates"], {"textures": {"files": 1, "bytes": 4}, "skyboxes": {"files": 1, "bytes": 7}})

    def test_plan_warns_when_engine_read_files_would_be_excluded(self):
        plan = plan_zip_release(self.zip([("maps/a.bsp", b"b"), ("progs.dat", b"d")]), ConvertOptions(exclude_patterns=("*.dat",)))
        self.assertEqual(plan["excluded_engine_read_types"], ["gamecode"])

    def test_plan_writes_nothing_next_to_the_source(self):
        z = self.zip([("pak0.pak", make_pak_bytes([("a", b"a")])), ("b", b"b")])
        before = sorted(p.name for p in self.tmp.iterdir())
        plan_zip_release(z)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before)

    def test_plan_still_rejects_unsafe_archives(self):
        for members in ([("../evil", b"x")], [("a", b"x")] * 0 + [("/abs", b"x")]):
            with self.assertRaises(ValueError):
                plan_zip_release(self.zip(members))


class MapdbTests(Base):
    def test_generated_when_absent(self):
        got, info, _, _ = self.build([("maps/a.bsp", b"b")], ConvertOptions(mapdb=("My Episode", "mymod")))
        self.assertEqual(json.loads(got["mapdb.json"]), {"episodes": [{"dir": "mymod", "name": "My Episode"}], "maps": []})
        self.assertEqual(got["maps/a.bsp"], b"b")
        self.assertEqual(info["generated"][0]["path"], "mapdb.json")
        self.assertEqual(parse_mapdb_name(got["mapdb.json"], "mymod"), ("My Episode", None))   # Ironwail would use this name

    def test_existing_mapdb_is_never_replaced(self):
        original = json.dumps({"episodes": [{"name": "Shipped", "dir": "x"}]}).encode()
        got, info, _, _ = self.build([("maps/a.bsp", b"b"), ("mapdb.json", original)], ConvertOptions(mapdb=("Mine", "mymod")))
        self.assertEqual(got["mapdb.json"], original)
        self.assertIsNone(info["generated"])

    def test_existing_mapdb_inside_an_embedded_pak_counts(self):
        original = b'{"episodes": []}'
        got, _, _, _ = self.build([("pak0.pak", make_pak_bytes([("mapdb.json", original)]))], ConvertOptions(mapdb=("Mine", "m")))
        self.assertEqual(got["mapdb.json"], original)

    def test_unicode_name(self):
        got, _, _, _ = self.build([("maps/a.bsp", b"b")], ConvertOptions(mapdb=("Caf\u00e9 \u201cX\u201d", "m")))
        self.assertEqual(json.loads(got["mapdb.json"].decode("utf-8"))["episodes"][0]["name"], "Caf\u00e9 \u201cX\u201d")


class ReencodeTests(Base):
    def setUp(self):
        super().setUp()
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow is not installed")

    def test_pixels_survive_for_every_supported_variant(self):
        variants = [("raw bottom-left rgba", dict(channels=4)), ("raw top-left rgba", dict(channels=4, origin_top=True)),
                    ("raw bottom-left rgb", dict(channels=3)), ("raw top-left rgb", dict(channels=3, origin_top=True)),
                    ("rle bottom-left rgba", dict(channels=4, rle=True)), ("rle top-left rgb", dict(channels=3, rle=True, origin_top=True)),
                    ("id field", dict(channels=4, id_field=b"made by test")), ("rgba without alpha bits in descriptor", dict(channels=4)),
                    ]
        for label, kw in variants:
            with self.subTest(label):
                ch = kw["channels"]
                rows = gradient(48, 32, ch, seed=len(label))
                tga = tga_bytes(48, 32, rows, **kw)
                got, info, _, _ = self.build([("gfx/a.tga", tga), ("maps/a.bsp", b"keep")], ConvertOptions(reencode_textures=True))
                self.assertNotIn("gfx/a.tga", got)
                mode, size, pixels = png_pixels(got["gfx/a.png"])
                self.assertEqual((mode, size, pixels), ("RGBA" if ch == 4 else "RGB", (48, 32), flatten(rows)))
                self.assertEqual(got["maps/a.bsp"], b"keep")
                self.assertEqual(info["reencoded"]["converted"], 1)
                self.assertLess(info["reencoded"]["bytes_after"], info["reencoded"]["bytes_before"])

    def test_rle_packets_that_cross_rows(self):
        rows = [[(10, 20, 30, 255)] * 200 for _ in range(200)]             # 128-pixel run packets cross the 200-pixel rows
        rows[77][3] = (1, 2, 3, 4)
        tga = tga_bytes(200, 200, rows, rle=True)
        got, _, _, _ = self.build([("gfx/a.tga", tga)], ConvertOptions(reencode_textures=True))
        self.assertEqual(png_pixels(got["gfx/a.png"])[2], flatten(rows))

    def test_unsupported_variants_stay_tga(self):
        rows = gradient(16, 16, 4)
        good = tga_bytes(16, 16, rows)
        sixteen = bytearray(good[:18]) + bytes(16 * 16 * 2)
        sixteen[16] = 16
        colormapped = bytearray(good)
        colormapped[1] = 1
        greyscale = bytearray(good)
        greyscale[2] = 3
        greyscale[16] = 8
        right_to_left = bytearray(good)
        right_to_left[17] |= 0x10
        truncated = good[:-5]
        # 128 + 127 pixels fill 255 of 256; a third 128-pixel packet then overruns the image
        rle_over = bytearray(tga_bytes(16, 16, rows, rle=True)[:18]) + bytes([0xFF, 0, 0, 0, 0, 0xFE, 0, 0, 0, 0, 0xFF, 0, 0, 0, 0])
        members = [("gfx/16.tga", bytes(sixteen)), ("gfx/cmap.tga", bytes(colormapped)), ("gfx/grey.tga", bytes(greyscale)),
                   ("gfx/rtl.tga", bytes(right_to_left)), ("gfx/trunc.tga", truncated), ("gfx/over.tga", bytes(rle_over)), ("gfx/ok.tga", good)]
        got, info, _, _ = self.build(members, ConvertOptions(reencode_textures=True))
        for name in ("16", "cmap", "grey", "rtl", "trunc", "over"):
            self.assertIn(f"gfx/{name}.tga", got, name)
            self.assertNotIn(f"gfx/{name}.png", got, name)
        self.assertIn("gfx/ok.png", got)
        self.assertEqual(info["reencoded"]["converted"], 1)
        self.assertEqual(sum(info["reencoded"]["skipped"].values()), 6)
        self.assertEqual(got["gfx/16.tga"], bytes(sixteen))                    # untouched bytes

    def test_not_smaller_stays_tga(self):
        rng = random.Random(5)
        rows = [[(rng.randrange(256), rng.randrange(256), rng.randrange(256), 255) for _ in range(3)] for _ in range(3)]
        tga = tga_bytes(3, 3, rows)
        got, info, _, _ = self.build([("gfx/tiny.tga", tga)], ConvertOptions(reencode_textures=True))
        self.assertEqual(got["gfx/tiny.tga"], tga)
        self.assertEqual(info["reencoded"]["skipped"], {"not-smaller": 1})

    def test_existing_png_shadows_the_tga_and_is_never_overwritten(self):
        rows = gradient(32, 32, 3)
        members = [("gfx/a.tga", tga_bytes(32, 32, rows, channels=3)), ("gfx/a.png", b"existing-png-bytes")]
        got, info, _, _ = self.build(members, ConvertOptions(reencode_textures=True))
        self.assertEqual(got["gfx/a.png"], b"existing-png-bytes")
        self.assertIn("gfx/a.tga", got)
        self.assertEqual(info["reencoded"]["converted"], 0)

    def test_textures_and_skyboxes_are_independent_choices(self):
        tga = tga_bytes(32, 32, gradient(32, 32, 3), channels=3)
        members = [("gfx/env/sky_up.tga", tga), ("gfx/tex.tga", tga), ("progs/skin.tga", tga)]
        got, _, _, _ = self.build(members, ConvertOptions(reencode_textures=True))
        self.assertEqual(sorted(got), ["gfx/env/sky_up.tga", "gfx/tex.png", "progs/skin.png"])
        got, _, _, _ = self.build(members, ConvertOptions(reencode_skyboxes=True))
        self.assertEqual(sorted(got), ["gfx/env/sky_up.png", "gfx/tex.tga", "progs/skin.tga"])
        got, _, _, _ = self.build(members)
        self.assertEqual(sorted(got), ["gfx/env/sky_up.tga", "gfx/tex.tga", "progs/skin.tga"])

    def test_images_inside_embedded_paks_and_non_images_are_handled(self):
        tga = tga_bytes(32, 32, gradient(32, 32, 4))
        pak0 = make_pak_bytes([("gfx/p.tga", tga), ("sound/a.wav", b"wav-bytes"), ("gfx/not-really.tga", b"short")])
        got, info, _, _ = self.build([("pak0.pak", pak0)], ConvertOptions(reencode_textures=True))
        self.assertIn("gfx/p.png", got)
        self.assertEqual((got["sound/a.wav"], got["gfx/not-really.tga"]), (b"wav-bytes", b"short"))
        self.assertEqual(info["reencoded"]["skipped"], {"truncated": 1})

    def test_oversized_images_are_left_alone(self):
        header = bytes([0, 0, 2]) + bytes(5) + struct.pack("<HHHH", 0, 0, 5000, 5000) + bytes([24, 0x20])
        got, info, _, _ = self.build([("gfx/huge.tga", header + b"\0" * 100)], ConvertOptions(reencode_textures=True))
        self.assertEqual(info["reencoded"]["skipped"], {"too-large": 1})

    def test_record_lists_libraries_and_totals(self):
        tga = tga_bytes(64, 64, gradient(64, 64, 4))
        _, info, _, _ = self.build([("gfx/a.tga", tga), ("gfx/b.tga", tga)], ConvertOptions(reencode_textures=True))
        r = info["reencoded"]
        self.assertEqual((r["converted"], r["files_total"], r["bytes_before"]), (2, 2, 2 * len(tga)))
        self.assertEqual([f["to"] for f in r["files"]], ["gfx/a.png", "gfx/b.png"])
        self.assertTrue(r["libraries"]["pillow"] and r["libraries"]["zlib"])

    def test_reencoding_is_deterministic_for_fixed_libraries(self):
        tga = tga_bytes(64, 64, gradient(64, 64, 4))
        a = self.build([("gfx/a.tga", tga)], ConvertOptions(reencode_textures=True))[2].read_bytes()
        b = self.build([("gfx/a.tga", tga)], ConvertOptions(reencode_textures=True))[2].read_bytes()
        self.assertEqual(a, b)


@unittest.skipUnless(shutil.which("gcc") and STB_H.exists(), "needs gcc and ironwail-src/Quake/stb_image.h")
class EngineDecoderEquivalenceTests(Base):
    """Decode original and re-encoded images with Ironwail's own stb_image (req_comp = 4, as image.c does) and compare."""

    @classmethod
    def setUpClass(cls):
        try:
            import PIL  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("Pillow is not installed")
        cls.dir = Path(tempfile.mkdtemp(prefix="quake-stb-"))
        (cls.dir / "d.c").write_text('#define STB_IMAGE_IMPLEMENTATION\n#include "stb_image.h"\n#include <stdio.h>\n'
                                     'int main(int c,char**v){int w,h,n;unsigned char*d=stbi_load(v[1],&w,&h,&n,4);if(!d)return 1;'
                                     'printf("%d %d\\n",w,h);fwrite(d,1,(size_t)w*h*4,stdout);return 0;}\n')
        subprocess.run(["gcc", "-O1", f"-I{STB_H.parent}", "-o", str(cls.dir / "d"), str(cls.dir / "d.c"), "-lm"], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def decode(self, name, data):
        f = self.dir / name
        f.write_bytes(data)
        r = subprocess.run([str(self.dir / "d"), str(f)], capture_output=True)
        self.assertEqual(r.returncode, 0, name)
        return r.stdout

    def test_every_variant_decodes_identically_in_the_engines_decoder(self):
        for ch in (3, 4):
            for rle in (False, True):
                for top in (False, True):
                    with self.subTest(channels=ch, rle=rle, top=top):
                        tga = tga_bytes(37, 29, gradient(37, 29, ch, seed=ch * 7), channels=ch, rle=rle, origin_top=top)
                        got, _, _, _ = self.build([("gfx/a.tga", tga)], ConvertOptions(reencode_textures=True))
                        self.assertEqual(self.decode("a.png", got["gfx/a.png"]), self.decode("a.tga", tga))

    def test_engine_decoder_agrees_with_the_hand_built_pixels(self):
        rows = gradient(16, 16, 4)
        out = self.decode("x.tga", tga_bytes(16, 16, rows))
        self.assertEqual(out.split(b"\n", 1)[1], flatten(rows))


if __name__ == "__main__":
    unittest.main()
