"""Image helpers for point-and-speak: change detection, trail annotation, crops.

The idea is to send as few pixels as possible. A tiny grey thumbnail tells whether the
screen changed while the person spoke. One downscaled screenshot carries the whole mouse
trail and the numbered points drawn on it. Small native-resolution crops cover the points.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from lookcore import Loop, Marker, Sample

THUMB = (64, 36)


def thumbnail(img: Image.Image) -> np.ndarray:
    """A 64x36 greyscale thumbnail as float32, for cheap frame comparison."""
    return np.asarray(img.convert("L").resize(THUMB, Image.BILINEAR), dtype=np.float32)


def frame_change(a: np.ndarray, b: np.ndarray, pixel_delta: float = 12.0) -> tuple[float, float]:
    """(mean absolute difference, fraction of thumbnail pixels that changed by > pixel_delta)."""
    diff = np.abs(a - b)
    return float(diff.mean()), float((diff > pixel_delta).mean())


def frame_changed(a: np.ndarray, b: np.ndarray, mean_thresh: float = 2.5, frac_thresh: float = 0.04) -> bool:
    """True when the screen changed enough to be worth another frame.

    A cursor, a caret blink or a clock changes a few thumbnail pixels at most, so it stays
    under both limits. Scrolling, a new page or a dialog changes far more.
    """
    mean, frac = frame_change(a, b)
    return mean >= mean_thresh or frac >= frac_thresh


class KeyframeKeeper:
    """Keeps the first frame, then a frame each time the screen changes, up to `limit` frames."""

    def __init__(self, limit: int = 3):
        self.limit = limit
        self.frames: list[tuple[float, Image.Image]] = []
        self._last: np.ndarray | None = None

    def offer(self, t: float, img: Image.Image) -> bool:
        thumb = thumbnail(img)
        if self._last is None or frame_changed(self._last, thumb):
            self._last = thumb
            if len(self.frames) < self.limit:
                self.frames.append((t, img))
                return True
            # over the limit: keep the newest change in place of the previous keyframe
            self.frames[-1] = (t, img)
            return True
        return False


def _scale(img: Image.Image, display: tuple[float, float]) -> tuple[float, float]:
    """Pixels per screen point, horizontally and vertically."""
    return img.width / display[0], img.height / display[1]


def downscale(img: Image.Image, max_width: int = 1280) -> Image.Image:
    if img.width <= max_width:
        return img.copy()
    h = round(img.height * max_width / img.width)
    return img.resize((max_width, h), Image.LANCZOS)


def _font(px: int) -> ImageFont.ImageFont:
    for path, idx in (("/System/Library/Fonts/Helvetica.ttc", 1), ("/Library/Fonts/Arial Bold.ttf", 0)):
        try:
            return ImageFont.truetype(path, px, index=idx)
        except Exception:
            continue
    return ImageFont.load_default()


def annotate_overview(
    img: Image.Image,
    samples: list[Sample],
    markers: list[Marker],
    loops: list[Loop],
    display: tuple[float, float],
    max_width: int = 1280,
) -> Image.Image:
    """The screenshot with the mouse trail, loops and numbered markers drawn on it, downscaled.

    The trail fades from faint (old) to strong (new), so the direction of travel reads at a glance.
    """
    out = downscale(img.convert("RGB"), max_width)
    sx, sy = _scale(out, display)
    over = Image.new("RGBA", out.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    n = len(samples)
    step = max(1, n // 240)
    pts = samples[::step] + ([samples[-1]] if n and (n - 1) % step else [])
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        alpha = 50 + int(170 * (i + 1) / max(1, len(pts) - 1))
        d.line((a.x * sx, a.y * sy, b.x * sx, b.y * sy), fill=(255, 64, 64, alpha), width=3)
    for lp in loops:
        poly = [(x * sx, y * sy) for x, y in lp.points]
        d.line(poly + poly[:1], fill=(255, 190, 0, 230), width=4)
    font = _font(max(14, out.width // 70))
    r = max(11, out.width // 110)
    for m in markers:
        cx, cy = m.x * sx, m.y * sy
        if m.bbox:
            l, t, rr, b = m.bbox
            d.rectangle((l * sx, t * sy, rr * sx, b * sy), outline=(255, 190, 0, 255), width=3)
            cx, cy = l * sx, t * sy
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(20, 110, 255, 235), outline=(255, 255, 255, 255), width=2)
        label = str(m.n)
        l0, t0, l1, t1 = d.textbbox((0, 0), label, font=font)
        d.text((cx - (l1 - l0) / 2 - l0, cy - (t1 - t0) / 2 - t0), label, font=font, fill=(255, 255, 255, 255))
    if samples:
        e = samples[-1]
        d.ellipse((e.x * sx - 5, e.y * sy - 5, e.x * sx + 5, e.y * sy + 5), fill=(255, 64, 64, 255))
    return Image.alpha_composite(out.convert("RGBA"), over).convert("RGB")


def crop_around(
    img: Image.Image,
    x: float,
    y: float,
    display: tuple[float, float],
    size: tuple[int, int] = (480, 320),
) -> Image.Image:
    """A native-resolution crop centred on a screen point, kept inside the image."""
    sx, sy = _scale(img, display)
    w, h = min(size[0], img.width), min(size[1], img.height)
    cx, cy = x * sx, y * sy
    left = int(min(max(cx - w / 2, 0), img.width - w))
    top = int(min(max(cy - h / 2, 0), img.height - h))
    return img.crop((left, top, left + w, top + h))


def crop_region(
    img: Image.Image,
    bbox: tuple[float, float, float, float],
    display: tuple[float, float],
    pad: float = 24.0,
    max_side: int = 900,
) -> Image.Image:
    """A crop of a circled region (with a little padding), shrunk only if it is very large."""
    sx, sy = _scale(img, display)
    l, t, r, b = bbox
    box = (
        int(max(0, (l - pad) * sx)),
        int(max(0, (t - pad) * sy)),
        int(min(img.width, (r + pad) * sx)),
        int(min(img.height, (b + pad) * sy)),
    )
    out = img.crop(box)
    longest = max(out.size)
    if longest > max_side:
        k = max_side / longest
        out = out.resize((round(out.width * k), round(out.height * k)), Image.LANCZOS)
    return out
