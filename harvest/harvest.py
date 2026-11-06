#!/usr/bin/env python3
"""Weekly sound harvester for «مكتبتي الصوتية».

Searches licensed sources (Openverse, and Freesound when an API key exists),
keeps only CC0 / public-domain / CC-BY sounds (commercial use allowed),
downloads real audio files (MP3/WAV), and updates docs/catalog.json.

Standard library only. Nothing is invented: every catalog entry comes from a
downloaded file whose license was reported by the source.
"""
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "maktabati-sawtiya-harvester/1.0 (personal media library)"

LICENSE_LABEL = {"cc0": "CC0 1.0", "pdm": "Public Domain", "by": "CC BY"}
LICENSE_DEFAULT_URL = {
    "cc0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "pdm": "https://creativecommons.org/publicdomain/mark/1.0/",
}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- network
def http_get(url, headers=None, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 502, 503):
                time.sleep(5 * (i + 1))
                continue
            raise
        except Exception as e:  # network hiccup
            last = e
            time.sleep(2 * (i + 1))
    raise last


def get_json(url, headers=None):
    return json.loads(http_get(url, headers).decode("utf-8"))


def post_form(url, form):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def download(url, max_bytes):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        cl = r.headers.get("Content-Length")
        if cl and int(cl) > max_bytes:
            raise ValueError("file too large")
        buf = bytearray()
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                raise ValueError("file too large")
        return bytes(buf)


# ---------------------------------------------------------------- helpers
def sniff(data):
    """Return 'mp3' or 'wav' from the real bytes, else None."""
    if data[:3] == b"ID3" or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    return None


def norm_license(raw, url=""):
    """Normalise a source's license value to cc0 / pdm / by / other."""
    s = (raw or "").strip().lower()
    u = (url or "").strip().lower()
    if s in ("cc0", "pdm", "by"):
        return s
    for x in (u, s):
        if not x:
            continue
        if "publicdomain/zero" in x or "creative commons 0" in x:
            return "cc0"
        if "publicdomain/mark" in x:
            return "pdm"
        m = re.search(r"/licenses/([a-z\-]+)/", x)
        if m:
            return m.group(1)
    if s == "attribution":
        return "by"
    return s or "unknown"


def clean_title(t):
    t = re.sub(r"\.(wav|mp3|ogg|flac|aiff?)$", "", (t or "").strip(), flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    return (t or "Sound")[:80]


def safe_id(s):
    return re.sub(r"[^a-zA-Z0-9_-]", "", str(s)) or hashlib.sha1(str(s).encode()).hexdigest()[:10]


# ---------------------------------------------------------------- sources
class Freesound:
    name = "Freesound"

    def __init__(self, key):
        self.key = key

    def search(self, query, cfg, page):
        lo, hi = cfg.get("min_seconds", 0.3), cfg["max_seconds"]
        base = {
            "query": query,
            "fields": "id,name,username,license,url,duration,previews",
            "page_size": 30,
            "page": page,
            "sort": "downloads_desc",
            "token": self.key,
        }
        dur = "duration:[%s TO %s]" % (lo, hi)
        lic = 'license:("Creative Commons 0" OR "Attribution")'
        try:
            data = get_json("https://freesound.org/apiv2/search/text/?" + urllib.parse.urlencode({**base, "filter": lic + " " + dur}))
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            # filter syntax rejected: retry with duration only; licenses are re-checked below anyway
            data = get_json("https://freesound.org/apiv2/search/text/?" + urllib.parse.urlencode({**base, "filter": dur}))
        out = []
        for r in data.get("results", []):
            prev = r.get("previews") or {}
            url = prev.get("preview-hq-mp3") or prev.get("preview-lq-mp3")
            if not url or r.get("id") is None:
                continue
            out.append({
                "source": "Freesound",
                "source_id": "fs-%s" % r["id"],
                "title": r.get("name"),
                "url": url,
                "license_raw": r.get("license"),
                "license_url": r.get("license"),
                "author": r.get("username"),
                "page_url": r.get("url") or "https://freesound.org/s/%s/" % r["id"],
                "duration": r.get("duration"),
            })
        return out


class Openverse:
    name = "Openverse"

    def __init__(self, client_id=None, client_secret=None):
        self.client_id, self.client_secret, self.token = client_id, client_secret, None

    def _headers(self):
        if self.client_id and self.client_secret and not self.token:
            try:
                res = post_form("https://api.openverse.org/v1/auth_tokens/token/", {
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                })
                self.token = res.get("access_token")
            except Exception as e:
                log("  openverse auth failed, continuing anonymously: %s" % e)
        return {"Authorization": "Bearer %s" % self.token} if self.token else {}

    def search(self, query, cfg, page):
        params = {"q": query, "license_type": "commercial", "page_size": 20, "page": page,
                  "category": "music" if cfg["type"] == "music" else "sound_effect"}
        data = get_json("https://api.openverse.org/v1/audio/?" + urllib.parse.urlencode(params), self._headers())
        out = []
        for r in data.get("results", []):
            url, rid = r.get("url"), r.get("id")
            if not url or not rid:
                continue
            ms = r.get("duration")
            out.append({
                "source": "Openverse",
                "source_id": "ov-%s" % safe_id(rid),
                "title": r.get("title"),
                "url": url,
                "license_raw": r.get("license"),
                "license_url": r.get("license_url"),
                "author": r.get("creator"),
                "page_url": r.get("foreign_landing_url") or r.get("detail_url"),
                "duration": (ms / 1000.0) if isinstance(ms, (int, float)) and ms else None,
                "origin": r.get("source") or r.get("provider"),
            })
        return out


# ---------------------------------------------------------------- catalog
def load_catalog(docs):
    p = os.path.join(docs, "catalog.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        c.setdefault("items", [])
        return c
    return {"updated": None, "items": []}


def save_catalog(docs, catalog, today):
    catalog["updated"] = today
    catalog["items"].sort(key=lambda i: (i.get("added", ""), i["id"]), reverse=True)
    with open(os.path.join(docs, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    lines = ["# Attribution / الإسناد", "",
             "Sounds marked **CC BY** require crediting the author when used in a published video.", "",
             "| Title | Author | License | Source |", "|---|---|---|---|"]
    for i in catalog["items"]:
        lines.append("| %s | %s | %s | %s |" % (
            i["name"].replace("|", "/"), (i.get("author") or "").replace("|", "/"),
            LICENSE_LABEL.get(i["license"], i["license"]), i.get("source_url") or i["source"]))
    with open(os.path.join(docs, "ATTRIBUTION.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def accept(c, ccfg, cfg, seen_ids, seen_hash=None):
    """License + duration + dedupe gate. Returns (ok, normalised_license)."""
    if c["source_id"] in seen_ids:
        return False, None
    lic = norm_license(c.get("license_raw"), c.get("license_url"))
    if lic not in cfg["licenses_allowed"]:
        return False, lic
    d = c.get("duration")
    if d is None:
        if ccfg["type"] == "music":      # music needs a known length
            return False, lic
    else:
        if d < ccfg.get("min_seconds", 0) or d > ccfg["max_seconds"]:
            return False, lic
    return True, lic


def make_item(c, lic, cat_id, ccfg, ext, data, today):
    title = clean_title(c.get("title"))
    author = (c.get("author") or "").strip() or "Unknown"
    lic_url = c.get("license_url") or ""
    if not lic_url.startswith("http"):
        lic_url = LICENSE_DEFAULT_URL.get(lic, "")
    attribution = '"%s" by %s via %s (%s) — %s%s' % (
        title, author, c["source"], c.get("page_url") or "", LICENSE_LABEL.get(lic, lic),
        (" (%s)" % lic_url) if lic_url else "")
    sid = safe_id(c["source_id"])
    return {
        "id": sid,
        "name": title,
        "category": cat_id,
        "type": ccfg["type"],
        "file": "audio/%s/%s.%s" % (cat_id, sid, ext),
        "ext": ext,
        "size": len(data),
        "duration": round(c["duration"], 2) if c.get("duration") else None,
        "license": lic,
        "license_url": lic_url,
        "author": author,
        "source": c["source"] + ("/" + c["origin"] if c.get("origin") else ""),
        "source_id": c["source_id"],
        "source_url": c.get("page_url") or "",
        "attribution": attribution,
        "sha1": hashlib.sha1(data).hexdigest(),
        "added": today,
    }


# ---------------------------------------------------------------- run
def run(cfg, sources, docs, today=None, week=None):
    today = today or datetime.date.today().isoformat()
    week = week if week is not None else datetime.date.fromisoformat(today).isocalendar()[1]
    os.makedirs(docs, exist_ok=True)
    catalog = load_catalog(docs)
    seen_ids = {i["source_id"] for i in catalog["items"]}
    seen_hash = {i.get("sha1") for i in catalog["items"] if i.get("sha1")}
    total = sum(i.get("size", 0) for i in catalog["items"])
    cap = cfg["max_library_mb"] * 1048576
    max_bytes = cfg["max_file_mb"] * 1048576
    added = []

    for cat_id, ccfg in cfg["categories"].items():
        want, got = ccfg["quota"], 0
        qs = ccfg["queries"]
        n = len(qs)
        page0 = 1 + (week // n) % 3
        order = sources[week % len(sources):] + sources[:week % len(sources)] if sources else []
        log("== %s (%s) want %d" % (cat_id, ccfg["label"], want))
        for qi in range(n):
            if got >= want or len(added) >= cfg["per_run"]:
                break
            query = qs[(week + qi) % n]
            for src in order:
                if got >= want or len(added) >= cfg["per_run"]:
                    break
                for page in (page0, page0 + 1):
                    if got >= want:
                        break
                    try:
                        cands = src.search(query, ccfg, page)
                    except Exception as e:
                        log("  %s search failed for '%s': %s" % (src.name, query, e))
                        break
                    log("  %s '%s' p%d: %d candidates" % (src.name, query, page, len(cands)))
                    for c in cands:
                        if got >= want:
                            break
                        ok, lic = accept(c, ccfg, cfg, seen_ids)
                        if not ok:
                            continue
                        if total >= cap:
                            log("  library size cap reached, stopping")
                            save_catalog(docs, catalog, today)
                            return added
                        try:
                            data = download(c["url"], max_bytes)
                        except Exception as e:
                            log("  download failed (%s): %s" % (c["source_id"], e))
                            continue
                        ext = sniff(data)
                        if not ext:
                            log("  not mp3/wav, skipped: %s" % c["source_id"])
                            continue
                        item = make_item(c, lic, cat_id, ccfg, ext, data, today)
                        if item["sha1"] in seen_hash:
                            continue
                        path = os.path.join(docs, item["file"])
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(data)
                        catalog["items"].append(item)
                        seen_ids.add(c["source_id"])
                        seen_hash.add(item["sha1"])
                        total += item["size"]
                        got += 1
                        added.append(item)
                        log("  + %s | %s | %s" % (item["name"], lic, c["source"]))
        if got < want:
            log("  only %d of %d found for %s" % (got, want, cat_id))
    save_catalog(docs, catalog, today)
    return added


def main():
    with open(os.path.join(ROOT, "harvest", "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    sources = []
    if os.environ.get("FREESOUND_API_KEY"):
        sources.append(Freesound(os.environ["FREESOUND_API_KEY"]))
    else:
        log("FREESOUND_API_KEY not set: Freesound skipped")
    sources.append(Openverse(os.environ.get("OPENVERSE_CLIENT_ID"), os.environ.get("OPENVERSE_CLIENT_SECRET")))
    added = run(cfg, sources, os.path.join(ROOT, "docs"))
    log("Done: %d new sounds" % len(added))
    if not added:
        log("WARNING: nothing was added. Check the log above (API key, network, or filters).")


if __name__ == "__main__":
    main()
