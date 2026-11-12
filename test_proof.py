"""Tests for the proof step. Run: python -m pytest -q"""
import io, os, random, tempfile

import pytest
from PIL import Image

from immich_exact_duplicates import compare, image_data_hash


def photo(seed=1, size=(320, 240)):
    rnd = random.Random(seed)
    im = Image.new("RGB", size)
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                for _ in range(size[0] * size[1])])
    return im.resize((size[0] * 2, size[1] * 2))  # smoother, more photo-like


def save(im, path, **kw):
    im.save(path, "JPEG", **kw)
    return path


@pytest.fixture
def d():
    with tempfile.TemporaryDirectory() as t:
        yield t


def with_exif(src, dst, text):
    """Same compressed picture, different metadata: rewrite only the APP1 segment."""
    data = open(src, "rb").read()
    exif = Image.Exif()
    exif[0x010E] = text  # ImageDescription
    blob = b"Exif\x00\x00" + exif.tobytes()
    seg = b"\xff\xe1" + (len(blob) + 2).to_bytes(2, "big") + blob
    open(dst, "wb").write(data[:2] + seg + data[2:])
    return dst


def test_metadata_only_difference_has_same_hash(d):
    a = save(photo(), os.path.join(d, "a.jpg"), quality=90)
    b = with_exif(a, os.path.join(d, "b.jpg"), "edited caption")
    assert open(a, "rb").read() != open(b, "rb").read()
    assert image_data_hash(a) == image_data_hash(b)
    assert compare(a, b, {})[0] == "same_image_data"


def test_reencoded_copy_is_same_picture_not_same_data(d):
    a = save(photo(), os.path.join(d, "a.jpg"), quality=95)
    b = save(Image.open(a), os.path.join(d, "b.jpg"), quality=80)
    assert image_data_hash(a) != image_data_hash(b)
    assert compare(a, b, {})[0] == "same_picture_resaved"


def test_different_photo_is_different(d):
    a = save(photo(1), os.path.join(d, "a.jpg"))
    b = save(photo(2), os.path.join(d, "b.jpg"))
    assert compare(a, b, {})[0] == "different"


def test_progressive_jpeg_hashes(d):
    a = save(photo(), os.path.join(d, "a.jpg"), progressive=True)
    b = with_exif(a, os.path.join(d, "b.jpg"), "x")
    assert image_data_hash(a) == image_data_hash(b)


def test_non_jpeg_returns_none(d):
    p = os.path.join(d, "a.png")
    photo().save(p, "PNG")
    assert image_data_hash(p) is None
