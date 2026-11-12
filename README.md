# immich-exact-duplicates

Find photos that are stored twice in [Immich](https://immich.app), and **prove** that each pair
really is the same photo before you remove anything.

Immich's built-in duplicate detection groups photos that *look* alike. That catches a lot, but it
also misses real copies, and you can't tell from the review screen whether two pictures are the
same file or just similar. This tool answers a narrower question with certainty: *is this the
same photo, stored again?*

## Why you'd have exact copies

The common cause: the same photos reach Immich twice. For example, the phone app backs up
the camera roll, **and** an external library points at a folder (NAS, Synology Photos, Nextcloud)
that also receives the camera roll. Immich skips byte-identical uploads, but the two copies are
rarely byte-identical: one side rewrites a metadata tag and the checksum changes, while the
picture itself is untouched.

## How it works

1. **Candidates (fast, one database query).** Same owner, same camera (EXIF make and model) and the
   same capture time (EXIF `DateTimeOriginal`, to the millisecond when the camera records it), but
   different files.
2. **Proof without decoding.** A JPEG keeps the picture (quantisation tables, Huffman tables,
   frame header and compressed scans) separate from the metadata (EXIF, XMP, ICC profile,
   embedded thumbnail, comments). The tool hashes only the picture segments. **Same hash means
   the two files decode to exactly the same pixels**, and it costs one file read, no decoding.
3. **Fallback for the rest.** When the hashes differ (the copy was re-encoded, or the file is not a
   JPEG), both images are decoded at 1/8 size, which JPEG decoders do almost for free, and compared
   as 64×64 grayscale. A mean difference under 2/255 is reported as `same_picture_resaved`;
   anything else is `different`.

Which copy is kept: by default the file from an **external library** (your original), then the
one with the most pixels, then the larger file. Change it with `--prefer upload|largest`.

## Verdicts

| Verdict | Meaning | Safe to remove? |
|---|---|---|
| `same_image_data` | Identical picture data, only metadata differs | Yes |
| `same_picture_resaved` | Same picture, saved again (re-compressed) | Usually; check a few first |
| `different` | Same moment and camera, but a different picture (burst shot, edit) | No |
| `unreadable` | File missing or format not supported | Check by hand |

## Results on a real library

Run on a family Immich server, about 123,000 assets across 9 users:

| | Copies | Share |
|---|---|---|
| Candidate groups (same owner, camera and capture time, different files) | 17,553 groups | |
| Copies checked | 19,278 | 100% |
| `same_image_data`: identical picture data | **18,311** | **95.0%** |
| `same_picture_resaved` | 143 | 0.7% |
| `different` (bursts, edits) | 744 | 3.9% |
| `unreadable` (HEIC/RAW without the optional decoder, missing files) | 80 | 0.4% |

The 18,311 proven copies take **62 GB**. About half came from the phone app backup next to a
NAS library; the rest were the same photo sitting in two folders of one external library.
The full check took 24 minutes on 3 CPU cores, nearly all of it reading files over NFS.
The picture-data hash agreed with a full pixel-by-pixel comparison on every sampled pair.

## Install

```sh
pip install -r requirements.txt     # pillow, psycopg; add pillow-heif for HEIC
```

The tool reads the Immich database and the original files, so run it where it can reach both.
The easiest way is a throwaway container that borrows the Immich server's volumes (read-only)
and joins its network:

```sh
docker run --rm -it \
  --network <immich-network> \
  --volumes-from immich_server:ro \
  -v "$PWD":/w -w /w \
  -e IMMICH_DB_URL="postgresql://postgres:<password>@database:5432/immich" \
  python:3.12-slim sh -c 'pip -q install -r requirements.txt && python immich_exact_duplicates.py'
```

Running on the host instead? Map Immich's paths to yours with
`--path-map /usr/src/app/upload=/srv/immich/upload` (repeat for each external library mount).

## Use

```sh
python immich_exact_duplicates.py --limit 50          # quick try on 50 groups
python immich_exact_duplicates.py                     # everything, writes duplicates.csv
```

Read the CSV (or spot-check some rows in Immich) before going further. Then, **per user**:

```sh
python immich_exact_duplicates.py --trash \
  --api-url https://photos.example.com --api-key <that user's API key>
```

`--trash` moves only `same_image_data` copies (add `--include-resaved` for the second verdict) to
**Immich's trash**, so they can be restored until the trash is emptied. It only touches assets owned
by the user whose API key you pass; everyone else's photos are left alone. It never deletes
files directly.

## Limits

- Needs EXIF make, model and capture time. Copies whose metadata was stripped (for example
  re-shared through a messaging app) are not candidates. Immich's own duplicate detection is the
  right tool for those.
- The no-decode proof is JPEG-only. HEIC, PNG and RAW go through the reduced-size comparison.
- Burst shots taken within the same second, on a camera that doesn't record milliseconds, become
  candidates. The proof step reports them as `different`, so they are never trashed.
- Copies **inside an external library** are files on your disk. Immich's trash hides them, but the
  file stays, and a library rescan can bring it back. Remove those files at the source instead (the
  CSV gives both paths).
- Tested against Immich v3.4. The database schema can change between Immich releases.

## Tests

```sh
pip install pytest && python -m pytest -q
```

## License

MIT
