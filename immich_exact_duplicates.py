#!/usr/bin/env python3
"""immich-exact-duplicates: find photos stored twice in Immich, and prove it.

Immich's built-in duplicate detection groups photos that *look* alike. This tool
finds copies of the *same* photo and proves each one:

1. Candidates: images with the same camera (EXIF make + model) and the same capture
   time (EXIF DateTimeOriginal, to the millisecond when the camera records it), but
   different files. One query against the Immich database, read-only.
2. Proof, without decoding: for JPEGs, a SHA-256 of only the picture-defining
   segments (quantisation and Huffman tables, frame header, compressed scans), with
   the metadata segments (EXIF, XMP, ICC, embedded thumbnails, comments) left out.
   Equal hash = the files decode to exactly the same pixels.
3. Fallback: when the hashes differ (re-encoded, or not a JPEG), both images are
   decoded at 1/8 size and compared as 64x64 grayscale. A mean difference under 2/255
   means the same picture saved again; anything else is reported as different.

The tool only reads. It moves copies to Immich's trash only with --trash, through the
Immich API, and only for the user who owns the API key.
"""
import argparse, csv, hashlib, json, os, sys, urllib.request
from concurrent.futures import ProcessPoolExecutor

__version__ = "0.1.0"

META_MARKERS = set(range(0xE0, 0xF0)) | {0xFE}       # APP0-APP15, COM
STANDALONE = set(range(0xD0, 0xD8)) | {0x01}         # RSTn, TEM
RESAVED_THRESHOLD = 2.0                              # mean abs diff on 0-255 scale

CANDIDATES_SQL = """
with x as (
  select a.id, a.checksum, a."originalPath" path, a."ownerId" owner,
         a."libraryId" is not null as external, a."createdAt" created,
         e.make, e.model, e."dateTimeOriginal" t,
         coalesce(e."exifImageWidth", 0) * coalesce(e."exifImageHeight", 0) px,
         coalesce(e."fileSizeInByte", 0) size
  from asset a join asset_exif e on e."assetId" = a.id
  where a."deletedAt" is null and a.type = 'IMAGE'
    and e."dateTimeOriginal" is not null and e.make is not null
), g as (
  select owner, make, model, t from x
  group by 1, 2, 3, 4 having count(*) > 1 and count(distinct checksum) > 1
)
select md5(x.owner::text || x.make || coalesce(x.model, '') || x.t::text) gid,
       x.id, x.owner, x.external, x.px, x.size, x.path
from x join g using (owner, make, model, t)
order by gid, %s
"""

PREFER = {
    "external": 'x.external desc, x.px desc, x.size desc, x.created, x.id',
    "upload": 'x.external asc, x.px desc, x.size desc, x.created, x.id',
    "largest": 'x.px desc, x.size desc, x.external desc, x.created, x.id',
}


# ---------------------------------------------------------------- proof

def image_data_hash(path):
    """SHA-256 of a JPEG's picture data, ignoring metadata. None if not a JPEG."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        return None
    h = hashlib.sha256()
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:          # fill byte
            i += 1
            continue
        if marker in STANDALONE:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker not in META_MARKERS:
            h.update(data[i:i + 2 + length])
        i += 2 + length
        if marker == 0xDA:          # start of scan: compressed data up to EOI
            end = data.find(b"\xff\xd9", i)   # 0xFF is byte-stuffed inside scans
            h.update(data[i:end if end >= 0 else n])
            return h.hexdigest()
    return None


def small_gray(path):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = None   # panoramas are real photos, not decompression bombs
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    with Image.open(path) as im:
        im.draft("L", (max(1, im.width // 8), max(1, im.height // 8)))
        im = ImageOps.exif_transpose(im).convert("L").resize((64, 64))
        return im.tobytes()


def compare(keeper_path, other_path, cache):
    kh = cache.get(("h", keeper_path))
    if kh is None:
        kh = cache[("h", keeper_path)] = image_data_hash(keeper_path) or ""
    oh = image_data_hash(other_path)
    if kh and oh and kh == oh:
        return "same_image_data", ""
    ks = cache.get(("s", keeper_path))
    if ks is None:
        ks = cache[("s", keeper_path)] = small_gray(keeper_path)
    os_ = small_gray(other_path)
    diff = sum(abs(a - b) for a, b in zip(ks, os_)) / len(ks)
    return ("same_picture_resaved" if diff < RESAVED_THRESHOLD else "different"), "%.2f" % diff


def check_group(args):
    group, path_map = args
    keeper, others, cache, out = group[0], group[1:], {}, []
    for o in others:
        try:
            verdict, detail = compare(map_path(keeper["path"], path_map), map_path(o["path"], path_map), cache)
        except Exception as e:  # unreadable, missing, unsupported format
            verdict, detail = "unreadable", "%s: %s" % (type(e).__name__, e)
        out.append({"group": keeper["gid"], "owner": keeper["owner"], "keep": keeper["id"],
                    "copy": o["id"], "verdict": verdict, "detail": detail,
                    "keep_path": keeper["path"], "copy_path": o["path"]})
    return out


def map_path(p, path_map):
    for src, dst in path_map:
        if p.startswith(src):
            return dst + p[len(src):]
    return p


# ---------------------------------------------------------------- database

def load_candidates(dsn, prefer, limit=None):
    try:
        import psycopg
    except ImportError:
        sys.exit("needs psycopg:  pip install 'psycopg[binary]'")
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        rows = conn.execute(CANDIDATES_SQL % PREFER[prefer]).fetchall()
    groups, cur = [], []
    for gid, aid, owner, external, px, size, path in rows:
        if cur and cur[0]["gid"] != gid:
            groups.append(cur)
            cur = []
        cur.append({"gid": gid, "id": str(aid), "owner": str(owner), "path": path})
    if cur:
        groups.append(cur)
    return groups[:limit] if limit else groups


# ---------------------------------------------------------------- trash via API

def api(base, key, method, path, body=None):
    req = urllib.request.Request(base.rstrip("/") + "/api" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"x-api-key": key, "content-type": "application/json",
                                          "accept": "application/json"})
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def trash(report, base, key, include_resaved):
    me = api(base, key, "GET", "/users/me")["id"]
    ok = {"same_image_data"} | ({"same_picture_resaved"} if include_resaved else set())
    ids = sorted({r["copy"] for r in report if r["verdict"] in ok and r["owner"] == me})
    skipped = sum(1 for r in report if r["verdict"] in ok and r["owner"] != me)
    for i in range(0, len(ids), 500):
        api(base, key, "DELETE", "/assets", {"ids": ids[i:i + 500], "force": False})
    print("moved %d copies to the trash for this user (%d belong to other users, untouched)"
          % (len(ids), skipped))


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Read-only unless --trash is given.")
    p.add_argument("--db", default=os.environ.get("IMMICH_DB_URL"),
                   help="Postgres URL of the Immich database (env IMMICH_DB_URL)")
    p.add_argument("--path-map", action="append", default=[], metavar="IMMICH=LOCAL",
                   help="rewrite path prefixes, e.g. /usr/src/app/upload=/srv/immich/upload")
    p.add_argument("--prefer", choices=PREFER, default="external",
                   help="which copy to keep: external = file from an external library (default), "
                        "upload = uploaded file, largest = most pixels")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--limit", type=int, help="check only the first N groups (for a quick try)")
    p.add_argument("--report", default="duplicates.csv", help="CSV report to write")
    p.add_argument("--trash", action="store_true",
                   help="move proven copies to Immich's trash (needs --api-url and --api-key)")
    p.add_argument("--include-resaved", action="store_true",
                   help="with --trash, also trash 'same_picture_resaved' copies")
    p.add_argument("--api-url", default=os.environ.get("IMMICH_URL"))
    p.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"))
    p.add_argument("--version", action="version", version=__version__)
    a = p.parse_args()
    if not a.db:
        p.error("--db or IMMICH_DB_URL is required")
    path_map = [tuple(m.split("=", 1)) for m in a.path_map]

    groups = load_candidates(a.db, a.prefer, a.limit)
    print("%d candidate groups" % len(groups), file=sys.stderr)
    report, counts = [], {}
    with ProcessPoolExecutor(a.workers) as ex:
        for i, rows in enumerate(ex.map(check_group, ((g, path_map) for g in groups), chunksize=16), 1):
            report.extend(rows)
            for r in rows:
                counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
            if i % 1000 == 0:
                print("  %d groups checked" % i, file=sys.stderr)
    with open(a.report, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(report[0]) if report else ["group"])
        w.writeheader()
        w.writerows(report)
    for k in ("same_image_data", "same_picture_resaved", "different", "unreadable"):
        print("%-22s %d" % (k, counts.get(k, 0)))
    print("report: %s" % a.report)
    if a.trash:
        if not (a.api_url and a.api_key):
            p.error("--trash needs --api-url and --api-key")
        trash(report, a.api_url, a.api_key, a.include_resaved)


if __name__ == "__main__":
    main()
