"""Offline tests with a fake network. Run from inside the harvest folder: python3 -m unittest test_harvest -v"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harvest as H  # noqa: E402

MP3 = b"ID3" + b"\x00" * 200
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
NOT_AUDIO = b"<html>blocked</html>"

CFG = {
    "per_run": 6, "max_library_mb": 1, "max_file_mb": 1, "licenses_allowed": ["cc0", "pdm", "by"],
    "categories": {
        "transitions": {"label": "t", "type": "sfx", "quota": 2, "min_seconds": 0.3, "max_seconds": 6, "queries": ["whoosh"]},
        "adsmusic": {"label": "m", "type": "music", "quota": 1, "min_seconds": 20, "max_seconds": 150, "queries": ["upbeat"]},
    },
}


class Fake:
    name = "Fake"

    def __init__(self, results_by_type):
        self.r = results_by_type

    def search(self, q, cfg, page):
        return list(self.r[cfg["type"]])


def cand(i, lic="cc0", url=None, dur=2.0, src="Fake"):
    return {"source": src, "source_id": "x-%s" % i, "title": "Sound %s.wav" % i, "url": url or "http://f/%s" % i,
            "license_raw": lic, "license_url": "", "author": "someone", "page_url": "http://page/%s" % i, "duration": dur}


class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.files = {}
        H.download = lambda url, mb: self.files[url] if url in self.files else (_ for _ in ()).throw(ValueError("404"))

    def test_license_normalisation(self):
        n = H.norm_license
        self.assertEqual(n("cc0"), "cc0")
        self.assertEqual(n("by"), "by")
        self.assertEqual(n("by-nc"), "by-nc")
        self.assertEqual(n("by-sa"), "by-sa")
        self.assertEqual(n("http://creativecommons.org/publicdomain/zero/1.0/"), "cc0")
        self.assertEqual(n("https://creativecommons.org/licenses/by/4.0/"), "by")
        self.assertEqual(n("https://creativecommons.org/licenses/by-nc/3.0/"), "by-nc")
        self.assertEqual(n("Attribution"), "by")
        self.assertEqual(n(None), "unknown")

    def test_sniff(self):
        self.assertEqual(H.sniff(MP3), "mp3")
        self.assertEqual(H.sniff(WAV), "wav")
        self.assertEqual(H.sniff(b"\xff\xfb\x90\x00rest"), "mp3")
        self.assertIsNone(H.sniff(NOT_AUDIO))

    def test_filters_and_catalog(self):
        sfx = [
            cand(1, "by-nc", url="http://f/1"),       # NC: rejected
            cand(2, "by-sa", url="http://f/2"),       # SA: rejected
            cand(3, "cc0", url="http://f/3", dur=60), # too long
            cand(4, "cc0", url="http://f/4"),         # html, not audio
            cand(5, "by", url="http://f/5"),          # ok (attribution)
            cand(6, "cc0", url="http://f/6", dur=None),  # ok (sfx, unknown duration)
            cand(7, "cc0", url="http://f/7"),         # ok but quota already full
        ]
        music = [cand(8, "cc0", url="http://f/8", dur=None),   # music with unknown duration: rejected
                 cand(9, "cc0", url="http://f/9", dur=90)]     # ok
        self.files.update({"http://f/4": NOT_AUDIO, "http://f/5": MP3, "http://f/6": WAV + b"1",
                           "http://f/7": WAV + b"2", "http://f/9": MP3 + b"m"})
        added = H.run(CFG, [Fake({"sfx": sfx, "music": music})], self.tmp, today="2026-11-08", week=45)
        ids = sorted(i["source_id"] for i in added)
        self.assertEqual(ids, ["x-5", "x-6", "x-9"])
        cat = json.load(open(os.path.join(self.tmp, "catalog.json"), encoding="utf-8"))
        self.assertEqual(cat["updated"], "2026-11-08")
        self.assertEqual(len(cat["items"]), 3)
        for it in cat["items"]:
            self.assertTrue(os.path.exists(os.path.join(self.tmp, it["file"])), it["file"])
            self.assertIn(it["license"], ("cc0", "by"))
        by = [i for i in cat["items"] if i["license"] == "by"][0]
        self.assertIn("someone", by["attribution"])
        attr = open(os.path.join(self.tmp, "ATTRIBUTION.md"), encoding="utf-8").read()
        self.assertIn("Sound 5", attr)
        self.assertIn("CC BY", attr)

    def test_second_run_does_not_duplicate(self):
        sfx = [cand(1, "cc0", url="http://f/1"), cand(2, "cc0", url="http://f/2")]
        self.files.update({"http://f/1": MP3, "http://f/2": WAV})
        fake = Fake({"sfx": sfx, "music": []})
        a = H.run(CFG, [fake], self.tmp, today="2026-11-08", week=1)
        b = H.run(CFG, [fake], self.tmp, today="2026-11-15", week=2)
        self.assertEqual(len(a), 2)
        self.assertEqual(len(b), 0)
        cat = json.load(open(os.path.join(self.tmp, "catalog.json"), encoding="utf-8"))
        self.assertEqual(len(cat["items"]), 2)

    def test_size_cap_stops(self):
        cfg = json.loads(json.dumps(CFG))
        cfg["max_library_mb"] = 0  # cap of zero bytes
        self.files["http://f/1"] = MP3
        added = H.run(cfg, [Fake({"sfx": [cand(1, "cc0", url="http://f/1")], "music": []})], self.tmp, today="2026-11-08", week=1)
        self.assertEqual(added, [])

    def test_search_failure_is_survived(self):
        class Boom:
            name = "Boom"

            def search(self, *a):
                raise RuntimeError("network down")
        added = H.run(CFG, [Boom()], self.tmp, today="2026-11-08", week=1)
        self.assertEqual(added, [])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "catalog.json")))


if __name__ == "__main__":
    unittest.main()
