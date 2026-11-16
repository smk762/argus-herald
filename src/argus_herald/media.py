"""Media on drafts: images, GIFs and short clips, always with alt text, never with metadata.

Every attached file gets a cleaned copy, and only the cleaned copy is ever posted or handed off:

- images (JPEG, PNG, WebP): the EXIF orientation is applied to the pixels, then the image is
  saved again with no EXIF, XMP, IPTC, comments or text chunks (GPS goes with EXIF). The colour
  profile is kept;
- GIFs: every frame is kept with its timing, and comments and extensions are dropped;
- video (MP4, MOV, WebM): ffmpeg copies the streams with all metadata dropped. Without ffmpeg
  the clip is kept but marked unstripped, and approve refuses it (fail closed).

What was removed is recorded so the reviewer sees it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

IMAGE_EXT = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
UNSTRIPPED = "unstripped"


class MediaError(ValueError):
    pass


@dataclass(frozen=True)
class MediaLimits:
    """What a platform accepts per post. `mixed` False: images, or one GIF/video, not both."""

    max_images: int
    max_bytes_image: int
    max_bytes_gif: int
    max_bytes_video: int
    formats: frozenset[str]
    one_gif_or_video: bool = True


@dataclass(frozen=True)
class Cleaned:
    path: Path
    kind: str  # image | gif | video
    bytes: int
    removed: str  # what was stripped, for the reviewer; "unstripped" when it couldn't be


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in GIF_EXT:
        return "gif"
    if ext in VIDEO_EXT:
        return "video"
    raise MediaError(f"{path.name}: use JPEG, PNG, WebP, GIF, MP4, MOV or WebM")


def clean(source: Path, out_dir: Path) -> Cleaned:
    """Write a metadata-free copy into out_dir (named by content hash) and describe it."""
    if not source.is_file():
        raise MediaError(f"No such file: {source}")
    kind = kind_of(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:20]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{digest}{source.suffix.lower()}"
    if kind == "image":
        removed = _clean_image(source, target)
    elif kind == "gif":
        removed = _clean_gif(source, target)
    else:
        removed = _clean_video(source, target)
    return Cleaned(target, kind, target.stat().st_size, removed)


def _found_metadata(image: Image.Image) -> list[str]:
    found = []
    exif = image.getexif()
    if exif:
        gps = exif.get_ifd(0x8825) if hasattr(exif, "get_ifd") else {}
        found.append("EXIF with GPS location" if gps else "EXIF")
    info = image.info
    if any(k in info for k in ("xmp", "XML:com.adobe.xmp")):
        found.append("XMP")
    if "comment" in info:
        found.append("comment")
    if "photoshop" in info or "iptc" in info:
        found.append("IPTC")
    text = [
        k
        for k in info
        if k
        not in {
            "icc_profile",
            "dpi",
            "transparency",
            "gamma",
            "duration",
            "loop",
            "background",
            "version",
            "extension",
            "exif",
            "xmp",
            "comment",
            "XML:com.adobe.xmp",
            "photoshop",
            "iptc",
            "jfif",
            "jfif_version",
            "jfif_unit",
            "jfif_density",
            "progressive",
            "progression",
            "adobe",
            "adobe_transform",
            "interlace",
            "aspect",
            "srgb",
            "chromaticity",
        }
    ]
    if text:
        found.append("text chunks (" + ", ".join(sorted(text))[:60] + ")")
    return found


def _clean_image(source: Path, target: Path) -> str:
    try:
        with Image.open(source) as image:
            found = _found_metadata(image)
            fmt = IMAGE_EXT[source.suffix.lower()]
            if image.format and image.format != fmt and not (fmt == "JPEG" and image.format == "MPO"):
                raise MediaError(f"{source.name} is really {image.format}, not {fmt}")
            icc = image.info.get("icc_profile")
            upright = ImageOps.exif_transpose(image) or image
            upright.load()
            # A new image from raw pixels carries no metadata at all.
            pixels = Image.frombytes(upright.mode, upright.size, upright.tobytes())
            if upright.mode == "P" and upright.getpalette():
                pixels.putpalette(upright.getpalette())
            options: dict = {"icc_profile": icc} if icc else {}
            if fmt == "PNG" and "transparency" in upright.info:
                options["transparency"] = upright.info["transparency"]
            if fmt == "JPEG":
                options["quality"] = 95
                if pixels.mode not in ("RGB", "L", "CMYK"):
                    pixels = pixels.convert("RGB")
            if fmt == "WEBP":
                options["quality"] = 95
            pixels.save(target, fmt, **options)
    except MediaError:
        raise
    except (OSError, ValueError) as exc:
        raise MediaError(f"{source.name} isn't a readable image: {exc}") from exc
    return ", ".join(found) or "nothing found"


def _clean_gif(source: Path, target: Path) -> str:
    try:
        with Image.open(source) as image:
            found = [f for f in _found_metadata(image) if f != "EXIF"] + (["EXIF"] if image.getexif() else [])
            frames, durations = [], []
            for frame in ImageSequence.Iterator(image):
                copy = frame.copy()
                # Pillow writes a frame's info (comments, extensions) back out; keep only timing.
                copy.info = {k: v for k, v in frame.info.items() if k in ("duration", "transparency", "background")}
                frames.append(copy)
                durations.append(frame.info.get("duration", image.info.get("duration", 100)))
            options = {
                "save_all": len(frames) > 1,
                "duration": durations if len(frames) > 1 else durations[0],
                "loop": image.info.get("loop", 0),
                "disposal": 2,
                "optimize": False,
            }
            if len(frames) > 1:
                options["append_images"] = frames[1:]
            frames[0].save(target, "GIF", **options)
    except (OSError, ValueError, IndexError) as exc:
        raise MediaError(f"{source.name} isn't a readable GIF: {exc}") from exc
    return ", ".join(found) or "nothing found"


def _clean_video(source: Path, target: Path) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        shutil.copyfile(source, target)
        return UNSTRIPPED
    tags = _video_tags(source)
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c",
            "copy",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise MediaError(f"ffmpeg couldn't rewrite {source.name}: {result.stderr.strip()[:200]}")
    return ", ".join(tags) or "nothing found"


_LOCATION_TAGS = re.compile(r"(?i)location|gps|xyz")


def _video_tags(source: Path) -> list[str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return ["container metadata"]
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(source)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return ["container metadata"]
    keys = set((data.get("format") or {}).get("tags") or {})
    for stream in data.get("streams") or []:
        keys |= set(stream.get("tags") or {})
    keys -= {"handler_name", "vendor_id", "language", "major_brand", "minor_version", "compatible_brands", "encoder"}
    if not keys:
        return []
    found = ["location"] if any(_LOCATION_TAGS.search(k) for k in keys) else []
    return found + [f"tags: {', '.join(sorted(keys))[:80]}"]


def check_limits(limits: MediaLimits | None, label: str, media: list[dict]) -> None:
    """Raise MediaError when attachments don't fit the platform."""
    if not media:
        return
    if limits is None:
        raise MediaError(f"{label} drafts can't carry media yet; remove the attachments or post by hand")
    kinds = [m["kind"] for m in media]
    heavy = [k for k in kinds if k in ("gif", "video")]
    if limits.one_gif_or_video and heavy and len(media) > 1:
        raise MediaError(f"{label} takes up to {limits.max_images} images, or one GIF or video, per post")
    if len(media) > limits.max_images:
        raise MediaError(f"{label} takes at most {limits.max_images} attachments")
    for item in media:
        ext = Path(item["path"]).suffix.lower().lstrip(".")
        if ext not in limits.formats:
            raise MediaError(f"{label} doesn't accept .{ext} files")
        cap = {"image": limits.max_bytes_image, "gif": limits.max_bytes_gif, "video": limits.max_bytes_video}[
            item["kind"]
        ]
        if item["bytes"] > cap:
            raise MediaError(
                f"{Path(item['path']).name} is {item['bytes'] // 1_000_000} MB; {label} allows {cap // 1_000_000} MB for a {item['kind']}"
            )


MB = 1_000_000
X_LIMITS = MediaLimits(4, 5 * MB, 15 * MB, 512 * MB, frozenset({"jpg", "jpeg", "png", "webp", "gif", "mp4", "mov"}))
PATREON_LIMITS = MediaLimits(
    20,
    200 * MB,
    200 * MB,
    2000 * MB,
    frozenset({"jpg", "jpeg", "png", "webp", "gif", "mp4", "mov", "webm", "m4v"}),
    one_gif_or_video=False,
)
