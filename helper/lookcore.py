"""Pure analysis core for point-and-speak: no macOS calls, so it unit-tests anywhere.

Given the mouse trace and the spoken words (each with a start and end time), it finds
where the pointer paused (dwells), where it drew a loop (a "circled region"), and which
spoken words point at something ("this", "that", "here", "my circled region"). Those
become numbered markers, so the model gets text coordinates, not a pile of screenshots.

Coordinates are screen points with the origin at the top left, as macOS reports the pointer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

DEICTIC = {"this", "that", "these", "those", "here", "there"}
REGION_WORDS = {"circled", "circle", "highlighted", "selected", "outlined", "boxed"}


@dataclass(frozen=True)
class Sample:
    t: float  # seconds from the start of the recording
    x: float
    y: float
    down: bool = False  # a mouse button is held


@dataclass(frozen=True)
class Word:
    text: str
    t0: float
    t1: float


@dataclass(frozen=True)
class Dwell:
    t0: float
    t1: float
    x: float
    y: float


@dataclass(frozen=True)
class Loop:
    t0: float
    t1: float
    bbox: tuple[float, float, float, float]  # left, top, right, bottom
    center: tuple[float, float]
    points: tuple[tuple[float, float], ...]


@dataclass
class Marker:
    n: int
    word: str
    t: float
    x: float
    y: float
    kind: str  # "point", "dwell" or "loop"
    bbox: tuple[float, float, float, float] | None = None  # loop region, else None
    word_indexes: list[int] = field(default_factory=list)


def _dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def position_at(samples: list[Sample], t: float) -> tuple[float, float]:
    """The pointer position at time `t`, interpolated between the two nearest samples."""
    if not samples:
        return (0.0, 0.0)
    if t <= samples[0].t:
        return (samples[0].x, samples[0].y)
    if t >= samples[-1].t:
        return (samples[-1].x, samples[-1].y)
    lo, hi = 0, len(samples) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if samples[mid].t <= t:
            lo = mid
        else:
            hi = mid
    a, b = samples[lo], samples[hi]
    span = b.t - a.t
    f = 0.0 if span <= 0 else (t - a.t) / span
    return (a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f)


def detect_dwells(samples: list[Sample], min_s: float = 0.35, radius: float = 12.0) -> list[Dwell]:
    """Stretches where the pointer stayed within `radius` points for at least `min_s` seconds."""
    out: list[Dwell] = []
    n = len(samples)
    i = 0
    while i < n:
        anchor = samples[i]
        j = i
        while j + 1 < n and _dist(samples[j + 1].x, samples[j + 1].y, anchor.x, anchor.y) <= radius:
            j += 1
        if samples[j].t - anchor.t >= min_s:
            run = samples[i : j + 1]
            out.append(
                Dwell(
                    t0=anchor.t,
                    t1=samples[j].t,
                    x=sum(s.x for s in run) / len(run),
                    y=sum(s.y for s in run) / len(run),
                )
            )
            i = j + 1
        else:
            i += 1
    return out


def _resample(samples: list[Sample], hz: float) -> list[Sample]:
    """Thin the trace to about `hz` samples a second, so loop search stays cheap."""
    if not samples:
        return []
    step = 1.0 / hz
    out = [samples[0]]
    for s in samples[1:]:
        if s.t - out[-1].t >= step:
            out.append(s)
    return out


def _path_length(pts: list[Sample]) -> float:
    return sum(_dist(a.x, a.y, b.x, b.y) for a, b in zip(pts, pts[1:]))


def _shoelace_area(pts: list[Sample]) -> float:
    a = 0.0
    for p, q in zip(pts, pts[1:] + pts[:1]):
        a += p.x * q.y - q.x * p.y
    return abs(a) / 2.0


def _total_turning(pts: list[Sample]) -> float:
    """Sum of the absolute heading changes, in radians, over moves long enough to have a heading."""
    heads = []
    for a, b in zip(pts, pts[1:]):
        if _dist(a.x, a.y, b.x, b.y) >= 3.0:
            heads.append(math.atan2(b.y - a.y, b.x - a.x))
    total = 0.0
    for h0, h1 in zip(heads, heads[1:]):
        d = (h1 - h0 + math.pi) % (2 * math.pi) - math.pi
        total += abs(d)
    return total


def detect_loops(
    samples: list[Sample],
    min_path: float = 120.0,
    min_area: float = 2500.0,
    max_s: float = 6.0,
    closure: float = 0.22,
    roundness: float = 0.30,
) -> list[Loop]:
    """Closed loops the pointer drew: a "circled region".

    A loop is a stretch of the trace that ends near where it began (within `closure` of its
    own length), encloses at least `min_area`, is round enough (not a thin back-and-forth),
    and turns through about three quarters of a full circle or more.
    """
    pts = _resample(samples, 20.0)
    out: list[Loop] = []
    n = len(pts)
    i = 0
    while i < n - 3:
        best: Loop | None = None
        best_j = i
        for j in range(i + 4, n):
            if pts[j].t - pts[i].t > max_s:
                break
            run = pts[i : j + 1]
            length = _path_length(run)
            if length < min_path:
                continue
            gap = _dist(run[0].x, run[0].y, run[-1].x, run[-1].y)
            if gap > closure * length:
                continue
            area = _shoelace_area(run)
            if area < min_area:
                continue
            perimeter = length + gap
            if 4 * math.pi * area / (perimeter**2) < roundness:
                continue
            if _total_turning(run) < 1.5 * math.pi:
                continue
            xs = [p.x for p in run]
            ys = [p.y for p in run]
            best = Loop(
                t0=run[0].t,
                t1=run[-1].t,
                bbox=(min(xs), min(ys), max(xs), max(ys)),
                center=(sum(xs) / len(xs), sum(ys) / len(ys)),
                points=tuple((p.x, p.y) for p in run),
            )
            best_j = j
        if best is not None:
            out.append(best)
            i = best_j + 1
        else:
            i += 1
    return out


def _clean(word: str) -> str:
    return "".join(ch for ch in word.lower() if ch.isalpha() or ch == "'")


def _snap_to_dwell(mid: float, dwells: list[Dwell], lead: float = 0.9, lag: float = 0.3) -> Dwell | None:
    """The rest point a pointing word belongs to.

    A person says "here" while the pointer is arriving, and word times are estimated, so the
    pointer may still be moving at that instant. A rest point whose time covers the word wins;
    otherwise the next one that starts within `lead` seconds after the word.
    """
    inside = [d for d in dwells if d.t0 - 0.2 <= mid <= d.t1 + lag]
    if inside:
        return min(inside, key=lambda d: abs(mid - (d.t0 + d.t1) / 2))
    ahead = [d for d in dwells if 0 < d.t0 - mid <= lead]
    return min(ahead, key=lambda d: d.t0 - mid) if ahead else None


def resolve_markers(
    words: list[Word],
    samples: list[Sample],
    dwells: list[Dwell],
    loops: list[Loop],
    merge_px: float = 24.0,
    merge_s: float = 1.2,
    loop_window_s: float = 3.0,
) -> list[Marker]:
    """Numbered markers for the words that point at something.

    "this", "that", "here" and the like point at the pointer position when the word was said.
    "circled", "highlighted" and the like point at the loop drawn nearest in time. Markers
    close in space and time are merged, so a sentence with three "this" in a row costs one crop.
    """
    markers: list[Marker] = []
    for idx, w in enumerate(words):
        key = _clean(w.text)
        mid = (w.t0 + w.t1) / 2.0
        if key in REGION_WORDS and loops:
            loop = min(loops, key=lambda lp: min(abs(mid - lp.t0), abs(mid - lp.t1), 0 if lp.t0 <= mid <= lp.t1 else 1e9))
            gap = min(abs(mid - loop.t0), abs(mid - loop.t1), 0 if loop.t0 <= mid <= loop.t1 else 1e9)
            if gap <= loop_window_s:
                prev = next((m for m in markers if m.kind == "loop" and m.bbox == loop.bbox), None)
                if prev:
                    prev.word_indexes.append(idx)
                else:
                    markers.append(
                        Marker(len(markers) + 1, w.text, mid, loop.center[0], loop.center[1], "loop", loop.bbox, [idx])
                    )
                continue
        if key in DEICTIC:
            x, y = position_at(samples, mid)
            snap = _snap_to_dwell(mid, dwells)
            if snap is not None:
                x, y = snap.x, snap.y
            near = next(
                (
                    m
                    for m in reversed(markers)
                    if m.kind != "loop"
                    and _dist(m.x, m.y, x, y) <= merge_px
                    and (abs(mid - m.t) <= merge_s or (snap is not None and m.kind == "dwell"))
                ),
                None,
            )
            if near:
                near.word_indexes.append(idx)
                continue
            markers.append(
                Marker(len(markers) + 1, w.text, mid, x, y, "dwell" if snap is not None else "point", None, [idx])
            )
    return markers


def annotate_transcript(words: list[Word], markers: list[Marker]) -> str:
    """The spoken text with `[@n]` after each word that has a marker."""
    tag: dict[int, int] = {}
    for m in markers:
        for i in m.word_indexes:
            tag[i] = m.n
    parts = []
    for i, w in enumerate(words):
        parts.append(f"{w.text}[@{tag[i]}]" if i in tag else w.text)
    return " ".join(parts)


def analyze(words: list[Word], samples: list[Sample]) -> tuple[list[Dwell], list[Loop], list[Marker]]:
    dwells = detect_dwells(samples)
    loops = detect_loops(samples)
    return dwells, loops, resolve_markers(words, samples, dwells, loops)
