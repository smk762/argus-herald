"""Inline SVG charts for the dashboard: no library, no CDN, themed by the page's CSS variables.

Marks follow one spec: 2px lines, a 10% area wash, hairline grid, round clean ticks. Colour
comes from fixed slots (--series-1 .. --series-8, see dashboard.PLATFORMS), so a platform has
the same colour everywhere. Text never wears a series colour. Every chart has a table view on
the page and a CSV export, so a tooltip is never the only way to read a value.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime

from markupsafe import Markup, escape

W = 760
LEFT, RIGHT, TOP, BOTTOM = 52, 14, 10, 22


def compact(value: float) -> str:
    value = float(value)
    if not math.isfinite(value):
        return "–"  # a missing or overflowed point gets a placeholder, not a crash
    # Each unit starts where the rounded label would reach it, so 999,999 reads 1M, not 1000K.
    if abs(value) >= 999_500:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(value) >= 9_999.5:
        return f"{value / 1000:.0f}K"
    if abs(value) >= 999.95:
        return f"{value:,.0f}"
    return f"{value:.0f}" if value.is_integer() else f"{value:.1f}"


def _step(raw: float) -> float:
    """The smallest clean step (1, 2, 2.5, 5 or 10 times a power of ten) that covers raw, or 0."""
    if not math.isfinite(raw) or raw <= 0:
        return 0.0
    power = 10.0 ** math.floor(math.log10(raw))
    step = next((m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw), 0.0)
    return step if math.isfinite(step) else 0.0


def _ticks(start: float, end: float, step: float) -> list[float]:
    """start, start+step, ... up to the first tick at or past end. Each is a multiple, not a running sum."""
    count = max(1, math.ceil((end - start) / step - 1e-9))
    digits = max(0, 2 - math.floor(math.log10(step)))
    return [round(start + i * step, digits) for i in range(count + 1)]


def nice_ticks(top: float, count: int = 3) -> list[float]:
    if not math.isfinite(top) or top <= 0 or count <= 0:
        return [0.0, 1.0]
    step = _step(top / count)
    if not step:
        return [0.0, 1.0]
    return _ticks(0.0, top, step)


def _log_ticks(top: float) -> list[float]:
    if not math.isfinite(top) or top <= 0:
        return [0.0, 1.0]
    ticks, value = [0.0], 1.0
    while math.isfinite(value) and value <= top * 1.0001:
        ticks.append(value)
        value *= 10
    if ticks[-1] < top:
        ticks.append(value if math.isfinite(value) else top)  # near the float maximum the next power overflows
    return ticks


def range_ticks(lo: float, hi: float, count: int = 3) -> list[float]:
    """Clean ticks spanning lo..hi, for a line that doesn't start at zero."""
    if not math.isfinite(lo) or not math.isfinite(hi) or count <= 0:
        return [0.0, 1.0]
    if hi <= lo:
        hi = lo + max(1.0, abs(lo) * 0.1)  # a flat line still gets a span, however large its value
    step = _step((hi - lo) / count)
    if not step:
        return [lo, hi] if math.isfinite(hi) and hi > lo else [0.0, 1.0]
    return _ticks(math.floor(lo / step) * step, hi, step)


class Scale:
    def __init__(self, n: int, top: float, height: int, log: bool, lo: float | None = None):
        self.n, self.height, self.log = n, height, log
        if lo is not None and not log:
            self.ticks = range_ticks(lo, top)
        else:
            self.ticks = _log_ticks(top) if log else nice_ticks(top)
        self.bottom = self.ticks[0]
        self.top = self.ticks[-1]
        if self.top == self.bottom:  # only a zero span needs a fallback; a top tick of 0 is a real top
            self.top = self.bottom + 1

    def x(self, i: float) -> float:
        span = W - LEFT - RIGHT
        return LEFT + (span * i / (self.n - 1) if self.n > 1 else span / 2)

    def y(self, v: float) -> float:
        inner = self.height - TOP - BOTTOM
        if self.log:
            frac = math.log10(1 + max(v, 0)) / math.log10(1 + self.top)
        else:
            frac = (v - self.bottom) / ((self.top - self.bottom) or 1)
        return TOP + inner * (1 - frac)


def _path(points: list[tuple[float, float] | None]) -> str:
    """An SVG path that breaks at missing values instead of drawing through them."""
    out, pen = [], False
    for point in points:
        if point is None:
            pen = False
            continue
        out.append(f"{'L' if pen else 'M'}{point[0]:.1f},{point[1]:.1f}")
        pen = True
    return "".join(out)


def _x_ticks(dates: list[date]) -> list[int]:
    if len(dates) <= 1:
        return [0]
    step = max(1, round(len(dates) / 6))
    return list(range(0, len(dates), step))


def _axes(scale: Scale, dates: list[date]) -> list[str]:
    parts = []
    for t in scale.ticks:
        y = scale.y(t)
        parts.append(f'<line class="grid" x1="{LEFT}" x2="{W - RIGHT}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{LEFT - 6}" y="{y + 4:.1f}" text-anchor="end">{compact(t)}</text>')
    base = scale.y(scale.bottom)
    parts.append(f'<line class="baseline" x1="{LEFT}" x2="{W - RIGHT}" y1="{base:.1f}" y2="{base:.1f}"/>')
    for i in _x_ticks(dates):
        parts.append(
            f'<text class="tick" x="{scale.x(i):.1f}" y="{scale.height - 6}" text-anchor="middle">'
            f"{dates[i].day} {dates[i]:%b}</text>"
        )
    return parts


def _markers(scale: Scale, dates: list[date], releases: list[dict], broadcasts: list) -> list[str]:
    if not dates:
        return []
    first, parts = dates[0], []
    top, bottom = TOP, scale.y(scale.bottom)

    def x_of(when: datetime) -> float | None:
        offset = (when.date() - first).days + (when.hour * 60 + when.minute) / 1440
        return scale.x(offset) if 0 <= offset <= len(dates) - 1 + 0.999 else None

    for r in releases:
        x = x_of(r["at"])
        if x is None:
            continue
        label = escape(f"Release {r['project']} {r['version']} · {r['at']:%d %b %H:%M} UTC")
        parts.append(
            f'<g class="marker release"><title>{label}</title>'
            f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{bottom:.1f}"/>'
            f'<rect class="hit" x="{x - 5:.1f}" y="{top}" width="10" height="{bottom - top:.1f}"/></g>'
        )
    from argus_herald.dashboard import platform_label, platform_slot

    for b in broadcasts:
        x = x_of(b.at)
        if x is None:
            continue
        what = b.subject or b.format
        note = " (logged by hand)" if b.logged else ""
        label = escape(f"{platform_label(b.platform)}{note} · {b.at:%d %b %H:%M} UTC · {what}\n{b.text[:280]}")
        mark = (
            f'<title>{label}</title><line class="s{platform_slot(b.platform)}" x1="{x:.1f}" x2="{x:.1f}" '
            f'y1="{top}" y2="{bottom:.1f}"/><rect class="hit" x="{x - 6:.1f}" y="{top}" width="12" '
            f'height="{bottom - top:.1f}"/>'
        )
        if b.link and b.link.startswith("https://"):  # never javascript: or any other scheme
            parts.append(
                f'<a class="marker broadcast" href="{escape(b.link)}" target="_blank" '
                f'rel="noopener noreferrer">{mark}</a>'
            )
        else:
            parts.append(f'<g class="marker broadcast">{mark}</g>')
    return parts


def series_panel(
    key: str,
    title: str,
    dates: list[date],
    values: list[float | None],
    mean: list[float | None] | None,
    releases: list[dict],
    broadcasts: list,
    *,
    log: bool = False,
    slot: int = 1,
    height: int = 130,
    zero: bool = True,
) -> Markup:
    """One series on the shared time axis: daily values (thin), 7-day mean (2px), event markers.

    `zero=False` fits the axis to the data, for a running total (followers) whose change is the story.
    """
    present = [v for v in values if v is not None]
    lo = None if zero or not present else min(present)
    scale = Scale(len(dates), max(present) if present else 0, height, log, lo=lo)
    parts = [
        f'<svg class="chart" viewBox="0 0 {W} {height}" role="img" aria-labelledby="{escape(key)}-title" '
        f'data-series="{escape(key)}" data-label="{escape(title)}" '
        f'data-values="{escape(json.dumps(values))}" data-start="{dates[0].isoformat() if dates else ""}">',
        f'<title id="{escape(key)}-title">{escape(title)}</title>',
    ]
    parts += _axes(scale, dates)
    if not present:
        parts.append(f'<text class="empty" x="{W / 2}" y="{height / 2}" text-anchor="middle">Not collected yet</text>')
    else:
        pts = [(scale.x(i), scale.y(v)) if v is not None else None for i, v in enumerate(values)]
        line = _path(pts)
        # Area wash under the daily values, closed down to the baseline per unbroken run.
        runs, run = [], []
        for p in pts + [None]:
            if p is None:
                if len(run) > 1:
                    runs.append(run)
                run = []
            else:
                run.append(p)
        base = scale.y(scale.bottom)
        if zero:  # a wash down to a baseline that isn't zero would overstate the change
            for r in runs:
                d = _path(r) + f"L{r[-1][0]:.1f},{base:.1f}L{r[0][0]:.1f},{base:.1f}Z"
                parts.append(f'<path class="wash s{slot}" d="{d}"/>')
        parts.append(f'<path class="{"daily" if mean else "mean"} s{slot}" d="{line}"/>')
        if mean:
            parts.append(
                f'<path class="mean s{slot}" d="'
                + _path([(scale.x(i), scale.y(v)) if v is not None else None for i, v in enumerate(mean)])
                + '"/>'
            )
    parts += _markers(scale, dates, releases, broadcasts)
    parts.append(
        f'<line class="crosshair" x1="0" x2="0" y1="{TOP}" y2="{scale.y(scale.bottom):.1f}" visibility="hidden"/>'
    )
    parts.append("</svg>")
    return Markup("".join(parts))


def stacked_area(dates: list[str], groups: list[tuple[str, str, int, list[float]]], height: int = 220) -> Markup:
    """Stacked areas, one per group, in fixed slot order; a 2px surface gap between layers."""
    if not dates or not groups:
        return Markup('<p class="quiet">No referrer snapshots in this range yet.</p>')
    totals = [sum(g[3][i] for g in groups) for i in range(len(dates))]
    scale = Scale(len(dates), max(totals) or 1, height, False)
    days = [date.fromisoformat(d) for d in dates]
    parts = [f'<svg class="chart" viewBox="0 0 {W} {height}" role="img" aria-label="Referrer uniques by source">']
    parts += _axes(scale, days)
    lower = [0.0] * len(dates)
    for _key, label, slot, values in groups:
        upper = [lo + v for lo, v in zip(lower, values, strict=False)]
        top_pts = [(scale.x(i), scale.y(v)) for i, v in enumerate(upper)]
        bottom_pts = [(scale.x(i), scale.y(v)) for i, v in reversed(list(enumerate(lower)))]
        if len(dates) == 1:
            x = scale.x(0)
            top_pts = [(x - 8, top_pts[0][1]), (x + 8, top_pts[0][1])]
            bottom_pts = [(x + 8, bottom_pts[0][1]), (x - 8, bottom_pts[0][1])]
        d = _path(top_pts) + "".join(f"L{x:.1f},{y:.1f}" for x, y in bottom_pts) + "Z"
        tip = escape(
            f"{label}: " + ", ".join(f"{d_} {compact(v)}" for d_, v in zip(dates[-3:], values[-3:], strict=False))
        )
        parts.append(f'<path class="layer s{slot}" d="{d}"><title>{tip}</title></path>')
        lower = upper
    parts.append("</svg>")
    return Markup("".join(parts))
