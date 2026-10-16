"""Inspection tests. Fixtures are built here byte by byte; expectations are written from the Ironwail rules
(host_cmd.c ExtraMaps_Init/Modlist_Add, gl_model.c Mod_LoadLighting, gl_sky.c Sky_LoadSkyBox, pr_edict.c PR_LoadProgs,
bgmusic.c), not computed by tools.pak_report."""
import hashlib
import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from tools.pak_report import analyze_pak, classify, parse_readme_fields, parse_worldspawn
from tests.test_zip_conversion import make_pak_bytes

V29 = struct.unpack("<i", struct.pack("<i", 29))[0]
BSP2 = struct.unpack("<i", b"BSP2")[0]
BIG = 40 * 1024      # above the 32 KiB menu-listing threshold


def make_bsp(version=29, worldspawn=None, lighting=0, size=BIG, extra_entities=""):
    props = "".join(f'"{k}" "{v}"\n' for k, v in (worldspawn or {}).items())
    text = ('{\n"classname" "worldspawn"\n' + props + "}\n" + extra_entities).encode() + b"\0"
    lumps = [(0, 0)] * 15
    lumps[0] = (124, len(text))
    lumps[8] = (124 + len(text), lighting)
    body = struct.pack("<i", version) + b"".join(struct.pack("<ii", *x) for x in lumps) + text + b"\x80" * lighting
    return body.ljust(size, b"\0")


def lit_for(lighting, version=1, sig=b"QLIT", size=None):
    data = sig + struct.pack("<i", version) + b"\x55" * (3 * lighting)
    return data if size is None else data[:size].ljust(size, b"\0")


class ReportBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quake-report-tests-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def report(self, files, gamedir="mod", current=None):
        p = self.tmp / "t.pak"
        p.write_bytes(make_pak_bytes(list(files.items())))
        return analyze_pak(p, gamedir, current or {})

    @staticmethod
    def codes(report, severity=None):
        return sorted(f["code"] for f in report["findings"] if severity in (None, f["severity"]))


class ClassifyTests(unittest.TestCase):
    def test_categories(self):
        expected = {
            "maps/a.bsp": "maps", "maps/a.lit": "map_lighting", "maps/a.map": "authoring", "mapsrc/a.map": "authoring",
            "src/ai.qc": "authoring", "progs/x.mdl": "models", "progs/x.dat": "other", "sound/a/b.wav": "sounds",
            "music/track02.ogg": "music", "gfx/env/sky_up.tga": "graphics", "progs.dat": "gamecode", "quake.rc": "config",
            "mapdb.json": "metadata", "pun.txt": "docs", "random.bin": "other", "Thumbs.db": "authoring",
        }
        for path, cat in expected.items():
            self.assertEqual(classify(path), cat, path)


class MapTests(ReportBase):
    def test_menu_listing_rules(self):
        r = self.report({
            "maps/big.bsp": make_bsp(), "maps/small.bsp": make_bsp(size=32 * 1024),   # exactly 32 KiB is NOT listed (needs >)
            "maps/sub/deep.bsp": make_bsp(), "maps/" + "n" * 31 + ".bsp": make_bsp(), "maps/start.bsp": make_bsp(),
            "maps/e1dm.bsp": make_bsp(), "maps/finalend.bsp": make_bsp(), "maps/mod_dm3.bsp": make_bsp(),
        })
        rows = {m["name"]: m for m in r["maps"]}
        self.assertEqual(sorted(n for n, m in rows.items() if m["listed"]), sorted(["big", "n" * 31, "start", "e1dm", "finalend", "mod_dm3"]))
        self.assertFalse(rows["small"]["listed"])
        self.assertNotIn("deep", rows)                     # subdirectories are never listed
        self.assertEqual({n: rows[n]["kind"] for n in ("start", "e1dm", "finalend", "mod_dm3", "big")},
                         {"start": "start", "e1dm": "deathmatch", "finalend": "end", "mod_dm3": "deathmatch", "big": "level"})
        self.assertEqual(self.codes(r), [])

    def test_name_longer_than_31_chars_warns(self):
        r = self.report({"maps/" + "n" * 32 + ".bsp": make_bsp()})
        self.assertEqual(self.codes(r, "warning"), ["map.name_too_long"])

    def test_title_version_and_unsupported_bsp(self):
        r = self.report({"maps/a.bsp": make_bsp(version=BSP2, worldspawn={"message": "Hello <b>World</b>"}),
                         "maps/hl.bsp": make_bsp(version=30)})
        rows = {m["name"]: m for m in r["maps"]}
        self.assertEqual((rows["a"]["bsp"], rows["a"]["title"]), ("BSP2", "Hello <b>World</b>"))   # text is data, not interpreted
        self.assertFalse(rows["hl"]["listed"])
        self.assertEqual(self.codes(r, "warning"), ["map.unsupported_bsp"])

    def test_all_supported_versions_are_recognised(self):
        versions = {"BSP29": 29, "BSP2": struct.unpack("<i", b"BSP2")[0], "2PSB": struct.unpack("<i", b"2PSB")[0],
                    "Quake64": struct.unpack("<i", b" 46Q")[0]}
        r = self.report({f"maps/m{i}.bsp": make_bsp(version=v) for i, v in enumerate(versions.values())})
        self.assertEqual(sorted(m["bsp"] for m in r["maps"]), sorted(versions))
        self.assertEqual(self.codes(r), [])

    def test_corrupt_bsp_is_reported_not_raised(self):
        bad = bytearray(make_bsp())
        struct.pack_into("<ii", bad, 4, 10**9, 10**9)         # entity lump far outside the file
        r = self.report({"maps/bad.bsp": bytes(bad), "maps/trunc.bsp": b"\x1d\x00\x00\x00" + b"\0" * 20 + b"x" * BIG})
        self.assertIn("map.corrupt", self.codes(r, "warning"))

    def test_map_limit_is_bounded(self):
        r = self.report({f"maps/m{i:03d}.bsp": make_bsp(size=BIG) for i in range(130)})
        self.assertEqual(len(r["maps"]), 128)
        self.assertIn("map.limit", self.codes(r))


class LitTests(ReportBase):
    def test_valid_lit_is_accepted(self):
        r = self.report({"maps/a.bsp": make_bsp(lighting=1000), "maps/a.lit": lit_for(1000)})
        self.assertEqual(r["maps"][0]["lit"], "ok")
        self.assertEqual(self.codes(r), [])

    def test_lit_rejected_cases_match_engine_conditions(self):
        cases = {
            "wrong size (built for another map version)": lit_for(999),
            "bad signature": lit_for(1000, sig=b"XLIT"),
            "unsupported version": lit_for(1000, version=2),
        }
        for label, lit in cases.items():
            with self.subTest(label):
                r = self.report({"maps/a.bsp": make_bsp(lighting=1000), "maps/a.lit": lit})
                self.assertEqual(r["maps"][0]["lit"], "rejected")
                self.assertEqual(self.codes(r, "warning"), ["lit.rejected"])

    def test_off_by_one_byte_is_rejected(self):
        r = self.report({"maps/a.bsp": make_bsp(lighting=1000), "maps/a.lit": lit_for(1000) + b"\0"})
        self.assertEqual(self.codes(r, "warning"), ["lit.rejected"])

    def test_orphan_lit_is_info_only(self):
        r = self.report({"maps/a.bsp": make_bsp(), "maps/zzz.lit": lit_for(10)})
        self.assertEqual(self.codes(r), ["lit.orphan"])
        self.assertEqual(self.codes(r, "warning"), [])

    def test_no_lit_is_not_a_finding(self):
        self.assertEqual(self.codes(self.report({"maps/a.bsp": make_bsp(lighting=500)})), [])


class SkyAndMusicTests(ReportBase):
    def faces(self, name, sides, ext="tga"):
        return {f"gfx/env/{name}{s}.{ext}": b"x" for s in sides}

    def test_complete_skybox_with_mixed_formats(self):
        files = {"maps/a.bsp": make_bsp(worldspawn={"sky": "sky_"})}
        files.update(self.faces("sky_", ["rt", "bk", "lf"]))
        files.update(self.faces("sky_", ["ft", "up", "dn"], ext="png"))
        r = self.report(files)
        self.assertEqual((r["maps"][0]["sky"], r["maps"][0]["sky_sides"]), ("sky_", 6))
        self.assertEqual(self.codes(r), [])

    def test_partial_skybox_warns_and_absent_is_info(self):
        files = {"maps/a.bsp": make_bsp(worldspawn={"sky": "half_"}), "maps/b.bsp": make_bsp(worldspawn={"sky": "gone_"})}
        files.update(self.faces("half_", ["rt", "bk", "lf", "ft", "up"]))
        r = self.report(files)
        self.assertEqual(self.codes(r, "warning"), ["sky.partial"])
        self.assertEqual(self.codes(r), ["sky.absent", "sky.partial"])
        self.assertIn("dn", r["findings"][0]["message"] + r["findings"][1]["message"])

    def test_alternative_sky_keys_and_underscore_prefix_and_last_wins(self):
        sides = ["rt", "bk", "lf", "ft", "up", "dn"]
        for key in ("skyname", "qlsky", "_sky", "sky"):
            with self.subTest(key=key):
                files = {"maps/a.bsp": make_bsp(worldspawn={key: "s_"})}
                files.update(self.faces("s_", sides))
                self.assertEqual(self.report(files)["maps"][0]["sky_sides"], 6)
        files = {"maps/a.bsp": make_bsp(worldspawn={"sky": "old_", "skyname": "new_"})}
        files.update(self.faces("new_", sides))
        self.assertEqual(self.report(files)["maps"][0]["sky"], "new_")

    def test_sky_in_subdirectory_name(self):
        files = {"maps/a.bsp": make_bsp(worldspawn={"sky": "ely/peaks_"})}
        files.update(self.faces("ely/peaks_", ["rt", "bk", "lf", "ft", "up", "dn"]))
        self.assertEqual(self.report(files)["maps"][0]["sky_sides"], 6)

    def test_music_tracks(self):
        r = self.report({"maps/a.bsp": make_bsp(worldspawn={"sounds": "93"}), "maps/b.bsp": make_bsp(worldspawn={"sounds": "94"}),
                         "music/track94.wav": b"x"})
        self.assertEqual(self.codes(r), ["music.absent"])
        self.assertIn("93", r["findings"][0]["message"])
        self.assertNotIn("94", r["findings"][0]["message"])

    def test_base_game_tracks_are_never_reported(self):
        r = self.report({f"maps/m{t}.bsp": make_bsp(worldspawn={"sounds": str(t)}) for t in range(1, 12)})
        self.assertEqual(self.codes(r), [])
        self.assertEqual([m["cdtrack"] for m in r["maps"] if m["name"] == "m1"], [1])

    def test_non_numeric_or_zero_sounds_ignored(self):
        r = self.report({"maps/a.bsp": make_bsp(worldspawn={"sounds": "abc"}), "maps/b.bsp": make_bsp(worldspawn={"sounds": "0"})})
        self.assertEqual([m["cdtrack"] for m in r["maps"]], [None, None])


class FileRuleTests(ReportBase):
    def progs(self, version, crc, size=100):
        return (struct.pack("<ii", version, crc) + b"\0" * size)

    def test_progs_rules(self):
        self.assertEqual(self.codes(self.report({"progs.dat": self.progs(6, 5927)})), [])
        r = self.report({"progs.dat": self.progs(7, 5927)})
        self.assertEqual(self.codes(r, "warning"), ["progs.version"])
        r = self.report({"progs.dat": self.progs(6, 22390)})
        self.assertEqual(self.codes(r, "warning"), ["progs.crc"])
        self.assertIn("CSQC", r["findings"][0]["message"])
        r = self.report({"progs.dat": self.progs(6, 12345)})
        self.assertEqual(self.codes(r, "warning"), ["progs.crc"])
        self.assertEqual(self.codes(self.report({"progs.dat": b"abc"}), "warning"), ["progs.truncated"])

    def test_descript_ion_is_inert_inside_a_pak(self):
        r = self.report({"descript.ion": b"My Mod\n"})
        self.assertEqual(self.codes(r, "warning"), ["descript.ion.inert"])

    def test_quake_rc_is_info_and_preserved(self):
        r = self.report({"quake.rc": b"exec default.cfg\n"})
        self.assertEqual(self.codes(r), ["quake.rc.present"])

    def mapdb(self, episodes):
        return json.dumps({"episodes": episodes, "maps": []}).encode()

    def test_mapdb_name_follows_engine_rules(self):
        ep = lambda name, d: {"name": name, "dir": d}
        cases = [
            ("normal", [ep("The Punishment Due", "pun")], "mod", "The Punishment Due"),
            ("dir need not match for non-copper", [ep("Other Name", "elsewhere")], "mod", "Other Name"),
            ("unmodified copper ignored", [ep("Underdark Overbright", "copper")], "qbj", None),
            ("copper ok when gamedir is copper", [ep("Underdark Overbright", "Copper")], "COPPER", "Underdark Overbright"),
            ("later episode used after skipped copper", [ep("Underdark Overbright", "copper"), ep("Real", "x")], "qbj", "Real"),
            ("episode missing dir skipped", [{"name": "No Dir"}, ep("Second", "y")], "mod", "Second"),
            ("no usable episode", [{"x": 1}, "junk", 5], "mod", None),
        ]
        for label, episodes, gamedir, expected in cases:
            with self.subTest(label):
                r = self.report({"mapdb.json": self.mapdb(episodes)}, gamedir=gamedir)
                self.assertEqual(r["effective_name"], expected)
                self.assertEqual(r["suggestions"].get("title", {}).get("value"), expected)

    def test_mapdb_invalid_variants(self):
        for label, data in {"not json": b"{nope", "binary": b"\xff\xfe\x00", "array": b"[1,2]", "no episodes": b'{"maps": []}',
                            "deeply nested (RecursionError)": b"[" * 100000 + b"]" * 100000}.items():
            with self.subTest(label):
                r = self.report({"mapdb.json": data})
                self.assertIsNone(r["effective_name"])
                self.assertIn("mapdb.invalid", self.codes(r, "warning"))

    def test_oversized_mapdb_is_skipped_not_parsed(self):
        r = self.report({"mapdb.json": b"{" + b" " * (300 * 1024) + b"}"})
        self.assertEqual(self.codes(r), ["mapdb.large"])

    def test_name_mismatch_is_info(self):
        r = self.report({"mapdb.json": self.mapdb([{"name": "Real Name", "dir": "m"}])}, current={"title": "Typed Title"})
        self.assertEqual(self.codes(r), ["name.mismatch"])
        r = self.report({"mapdb.json": self.mapdb([{"name": "Real Name", "dir": "m"}])}, current={"title": "Real Name"})
        self.assertEqual(self.codes(r), [])
        self.assertNotIn("title", r["suggestions"])

    def test_authoring_bytes_are_reported_but_kept(self):
        files = {"maps/a.map": b"x" * (2 * 1024 * 1024), "maps/a.bsp": make_bsp()}
        r = self.report(files)
        self.assertEqual(self.codes(r), ["size.extras"])
        self.assertEqual(r["entries"], 2)

    def test_duplicate_directory_entries_use_first(self):
        p = self.tmp / "d.pak"
        p.write_bytes(make_pak_bytes([("mapdb.json", self.mapdb([{"name": "First", "dir": "m"}])),
                                      ("mapdb.json", self.mapdb([{"name": "Second", "dir": "m"}]))]))
        r = analyze_pak(p, "m")
        self.assertEqual((r["effective_name"], r["duplicate_directory_entries"]), ("First", 1))
        self.assertIn("pak.duplicate_entries", self.codes(r))


PUN_TXT = b"""===========
Title                   : The Punishment Due
Filename                : pun.zip
Release date            : 2021-03-15
Author                  : Julian 'Juz' Priestley

Description             :
  A 5 map episode
  for Quake.

Maps\t\t\t:
\tstart.bsp - Start
"""


class SuggestionTests(ReportBase):
    def test_template_fields(self):
        self.assertEqual(parse_readme_fields(PUN_TXT), {
            "title": "The Punishment Due", "date": "2021/03/15", "author": "Julian 'Juz' Priestley",
            "description": "A 5 map episode for Quake."})

    def test_dates(self):
        for raw, want in {"2021-03-15": "2021/03/15", "2021/3/5": "2021/03/05", "2021.12.31": "2021/12/31"}.items():
            self.assertEqual(parse_readme_fields(f"Title: T\nDate: {raw}\n".encode()).get("date"), want, raw)
        for raw in ("15 March 2021", "2021-13-40", "March 2021", ""):
            self.assertNotIn("date", parse_readme_fields(f"Title: T\nRelease date: {raw}\n".encode()), raw)

    def test_hostile_and_odd_text_is_data_only(self):
        f = parse_readme_fields(b"Title: <script>alert(1)</script>\x00\x07 A\nAuthor: " + b"x" * 5000 + b"\n")
        self.assertEqual(f["title"], "<script>alert(1)</script> A")      # control chars removed, markup untouched (escaped when rendered)
        self.assertEqual(len(f["author"]), 200)
        self.assertEqual(parse_readme_fields(b"Title: caf\xe9\n")["title"], "caf\u00e9")   # latin-1 fallback
        self.assertEqual(parse_readme_fields(b""), {})

    def test_only_a_file_with_title_is_used(self):
        r = self.report({"a_component.txt": b"Author: Someone Else\nDate: 2001-01-01\n", "readme.txt": PUN_TXT})
        self.assertEqual({k: v["source"] for k, v in r["suggestions"].items()},
                         {k: f"readme.txt ({k})" for k in ("title", "author", "date", "description")})
        r = self.report({"a_component.txt": b"Author: Someone Else\n"})
        self.assertEqual(r["suggestions"], {})

    def test_suggestions_skip_values_already_set(self):
        r = self.report({"pun.txt": PUN_TXT}, current={"title": "The Punishment Due", "author": "Unknown"})
        self.assertEqual(sorted(r["suggestions"]), ["author", "date", "description"])

    def test_mapdb_title_beats_readme_title_but_keeps_other_fields(self):
        r = self.report({"pun.txt": PUN_TXT, "mapdb.json": json.dumps({"episodes": [{"name": "Episode Name", "dir": "m"}]}).encode()})
        self.assertEqual(r["suggestions"]["title"]["value"], "Episode Name")
        self.assertEqual(r["suggestions"]["author"]["source"], "pun.txt (author)")

    def test_nested_text_files_are_not_used(self):
        self.assertEqual(self.report({"docs/readme.txt": PUN_TXT})["suggestions"], {})


def wav(tag=1, channels=1, bits=16, data=b"\0\0" * 10, fmt_extra=b"", pre=b"", with_fmt=True, with_data=True, rate=11025):
    fmt_body = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * bits // 8, channels * bits // 8, bits) + fmt_extra
    body = b"WAVE" + pre
    if with_fmt:
        body += b"fmt " + struct.pack("<i", len(fmt_body)) + fmt_body
    if with_data:
        body += b"data" + struct.pack("<i", len(data)) + data
    return b"RIFF" + struct.pack("<i", len(body)) + body


class AssetHeaderTests(ReportBase):
    def mdl(self, ident=b"IDPO", version=6):
        return ident + struct.pack("<i", version) + b"\0" * 100

    def test_valid_models_sprites_and_brush_models_are_fine(self):
        r = self.report({"progs/a.mdl": self.mdl(), "progs/s.spr": self.mdl(b"IDSP", 1), "progs/b.mdl": make_bsp(size=300)[:300]})
        self.assertEqual(self.codes(r), [])

    def test_rejected_model_headers(self):
        r = self.report({"progs/v7.mdl": self.mdl(version=7), "progs/s2.spr": self.mdl(b"IDSP", 2), "progs/m3.mdl": self.mdl(b"IDP3", 15),
                         "progs/junk.mdl": b"JUNKJUNKJUNK", "progs/tiny.mdl": b"IDP", "progs/ok.mdl": self.mdl()})
        self.assertEqual(self.codes(r, "warning"), ["model.rejected"] * 5)
        flagged = sorted(p for f in r["findings"] for p in f["paths"])
        self.assertEqual(flagged, ["progs/junk.mdl", "progs/m3.mdl", "progs/s2.spr", "progs/tiny.mdl", "progs/v7.mdl"])
        self.assertTrue(all("fatal error" in f["message"] for f in r["findings"]))

    def test_valid_wavs(self):
        r = self.report({
            "sound/mono16.wav": wav(), "sound/stereo8.wav": wav(channels=2, bits=8), "sound/ext.wav": wav(fmt_extra=b"\0\0"),
            "sound/list_first.wav": wav(pre=b"LIST" + struct.pack("<i", 5) + b"abcde" + b"\0"),          # odd chunk is padded to even
            "sound/data_before_fmt_ignored.wav": wav(),
            "music/track02.wav": b"not looked at", "gfx/readme.wav": b"only sound/ is checked",
        })
        self.assertEqual(self.codes(r), [])

    def test_unplayable_wavs_by_reason(self):
        cases = {
            "adpcm": (wav(tag=2), "plain PCM"), "bits24": (wav(bits=24), "24-bit"), "channels3": (wav(channels=3), "3 channels"),
            "nodata": (wav(with_data=False), "missing its data chunk"), "nofmt": (wav(with_fmt=False), "missing its fmt chunk"),
            "empty": (wav(data=b""), "no samples"), "notriff": (b"RIFX" + b"\0" * 20, "RIFF/WAVE"), "short": (b"RIFF", "RIFF/WAVE"),
        }
        r = self.report({f"sound/{k}.wav": v[0] for k, v in cases.items()})
        text = " | ".join(f["message"] + str(f.get("paths")) for f in r["findings"])
        self.assertEqual(self.codes(r, "warning"), ["sound.unplayable"] * len({v[1] for v in cases.values()}))
        for key, (_, phrase) in cases.items():
            self.assertIn(phrase, text, key)
            self.assertIn(f"sound/{key}.wav", text)

    def test_inconclusive_scan_stays_silent(self):
        big_list = b"LIST" + struct.pack("<i", 200 * 1024) + b"\0" * (200 * 1024)
        r = self.report({"sound/late_fmt.wav": wav(pre=big_list)})                     # fmt lies beyond the bytes inspected
        self.assertEqual(self.codes(r), [])


class SafetyTests(ReportBase):
    def test_analysis_never_modifies_the_package(self):
        p = self.tmp / "t.pak"
        p.write_bytes(make_pak_bytes([("maps/a.bsp", make_bsp(lighting=10)), ("maps/a.lit", lit_for(9)), ("pun.txt", PUN_TXT)]))
        before = hashlib.sha256(p.read_bytes()).hexdigest()
        mtime = p.stat().st_mtime_ns
        for _ in range(3):
            analyze_pak(p, "m", {"title": "x"})
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), before)
        self.assertEqual(p.stat().st_mtime_ns, mtime)
        self.assertEqual([x.name for x in self.tmp.iterdir()], ["t.pak"])

    def test_findings_are_capped(self):
        files = {f"maps/m{i:03d}.bsp": make_bsp(version=30) for i in range(128)}
        files.update({f"maps/{'n' * 32}{i:03d}.bsp": make_bsp() for i in range(100)})
        r = self.report(files)
        self.assertLessEqual(len(r["findings"]), 200)

    def test_empty_and_invalid_paks_raise_value_error(self):
        p = self.tmp / "bad.pak"
        p.write_bytes(b"not a pak at all")
        with self.assertRaises(ValueError):
            analyze_pak(p)

    def test_worldspawn_parser_handles_quoted_braces(self):
        ws = parse_worldspawn('{\n"classname" "worldspawn"\n"message" "a } b { c"\n"_sky" "x_"\n}\n{\n"classname" "other"\n"message" "no"\n}')
        self.assertEqual(ws, {"classname": "worldspawn", "message": "a } b { c", "sky": "x_"})


if __name__ == "__main__":
    unittest.main()
