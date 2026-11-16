import shutil
import subprocess

import pytest
from conftest import DENY_TERM
from PIL import Image, PngImagePlugin

from argus_herald.media import X_LIMITS, MediaError, check_limits, clean
from argus_herald.models import Post
from argus_herald.platforms import DiscordPublisher, PatreonPublisher, TwitterPublisher
from argus_herald.store import Store
from argus_herald.workflow import ReviewError, approve, attach_media, compose, media_problems


def jpeg_with_gps(path, size=(40, 20)):
    image = Image.new("RGB", size, (200, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 on display
    exif[0x010F] = "PhoneMaker"
    exif[0x8825] = {1: "S", 2: (41.0, 17.0, 0.0)}
    image.save(path, exif=exif, comment=b"taken at home")
    return path


def test_jpeg_loses_exif_gps_and_keeps_orientation(tmp_path):
    cleaned = clean(jpeg_with_gps(tmp_path / "a.jpg"), tmp_path / "clean")
    assert cleaned.kind == "image" and "EXIF with GPS location" in cleaned.removed and "comment" in cleaned.removed
    with Image.open(cleaned.path) as out:
        assert out.size == (20, 40)
        assert dict(out.getexif()) == {} and "comment" not in out.info and "exif" not in out.info
    assert b"PhoneMaker" not in cleaned.path.read_bytes() and b"taken at home" not in cleaned.path.read_bytes()


def test_png_text_and_webp_and_wrong_extension(tmp_path):
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Author", "Someone Private")
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(tmp_path / "b.png", pnginfo=meta)
    cleaned = clean(tmp_path / "b.png", tmp_path / "clean")
    assert "text chunks (Author)" in cleaned.removed and b"Someone Private" not in cleaned.path.read_bytes()
    Image.new("RGB", (8, 8)).save(tmp_path / "c.webp")
    assert clean(tmp_path / "c.webp", tmp_path / "clean").removed == "nothing found"
    Image.new("RGB", (8, 8)).save(tmp_path / "d.jpg", "PNG")
    with pytest.raises(MediaError, match="really PNG"):
        clean(tmp_path / "d.jpg", tmp_path / "clean")
    (tmp_path / "e.bmp").write_bytes(b"x")
    with pytest.raises(MediaError, match="use JPEG"):
        clean(tmp_path / "e.bmp", tmp_path / "clean")


def test_animated_gif_keeps_frames_and_drops_comment(tmp_path):
    frames = [Image.new("RGB", (10, 10), (i * 80, 0, 0)) for i in range(3)]
    frames[0].save(
        tmp_path / "g.gif",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200, 300],
        loop=0,
        comment=b"secret note",
    )
    cleaned = clean(tmp_path / "g.gif", tmp_path / "clean")
    assert "comment" in cleaned.removed and b"secret note" not in cleaned.path.read_bytes()
    with Image.open(cleaned.path) as out:
        assert out.n_frames == 3


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_video_metadata_is_removed_with_ffmpeg(tmp_path):
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=5:duration=1",
            "-metadata",
            "location=+12.3456+045.6789/",
            "-metadata",
            "title=home",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    cleaned = clean(source, tmp_path / "clean")
    assert cleaned.kind == "video" and cleaned.removed.startswith("location")
    assert b"+12.3456" not in cleaned.path.read_bytes() and b"home" not in cleaned.path.read_bytes()


def test_without_ffmpeg_video_is_unstripped_and_blocks_approve(tmp_path, monkeypatch, gate):
    import argus_herald.media as media

    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    store = Store(tmp_path / "s.sqlite")
    draft_id, _ = store.add_draft(project="p", version="1", platform="twitter", title="", body="b", link="l")
    attach_media(store, tmp_path / "m", draft_id, tmp_path / "clip.mp4", "A clip")
    with pytest.raises(ReviewError, match="metadata wasn't stripped"):
        approve(store, TwitterPublisher(), draft_id, None, gate=gate)


def test_limits():
    image = {"path": "a.jpg", "kind": "image", "bytes": 1000}
    check_limits(X_LIMITS, "X", [image] * 4)
    with pytest.raises(MediaError, match="at most 4"):
        check_limits(X_LIMITS, "X", [image] * 5)
    with pytest.raises(MediaError, match="one GIF or video"):
        check_limits(X_LIMITS, "X", [image, {"path": "b.gif", "kind": "gif", "bytes": 10}])
    with pytest.raises(MediaError, match="allows 5 MB"):
        check_limits(X_LIMITS, "X", [{"path": "a.png", "kind": "image", "bytes": 6_000_000}])
    with pytest.raises(MediaError, match="doesn't accept .webm"):
        check_limits(X_LIMITS, "X", [{"path": "a.webm", "kind": "video", "bytes": 10}])
    with pytest.raises(MediaError, match="can't carry media"):
        check_limits(None, "Discord", [image])


def test_approve_needs_alt_text_passes_the_gate_and_fits_the_platform(tmp_path, gate):
    store = Store(tmp_path / "s.sqlite")
    ids = compose(
        store,
        [TwitterPublisher(), PatreonPublisher(), DiscordPublisher()],
        Post(kind="demo", subject="Atlas", text="Coastline", link="https://example.org/a"),
    )
    x, patreon, discord = ids
    approve(store, PatreonPublisher(), patreon, None, gate=gate)
    media_ids = attach_media(store, tmp_path / "m", x, jpeg_with_gps(tmp_path / "a.jpg"), "", all_drafts=True)
    assert len(media_ids) == 3 and store.get(patreon).status == "pending"  # new media needs a fresh look
    with pytest.raises(ReviewError, match="needs alt text"):
        approve(store, TwitterPublisher(), x, None, gate=gate)
    store.set_alt(media_ids[0], f"A map near {DENY_TERM}")
    with pytest.raises(ReviewError, match="Refused by the privacy gate"):
        approve(store, TwitterPublisher(), x, None, gate=gate)
    store.set_alt(media_ids[0], "A painted coastline with place names")
    assert approve(store, TwitterPublisher(), x, None, gate=gate).status == "approved"
    store.set_alt(media_ids[2], "A painted coastline")
    assert any("can't carry media" in p for p in media_problems(store, DiscordPublisher(), store.get(discord)))
    assert TwitterPublisher("api").media_limits is None
    store.close()
