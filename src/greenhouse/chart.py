"""Server-rendered SVG temperature chart, so the admin pages need no JavaScript and keep a strict CSP.

One series (temperature) on one axis. Thresholds are labeled reference lines, and periods when the
fans were commanded on are a neutral band behind the line. Each time bucket has an invisible hit
rectangle carrying a native ``<title>`` tooltip, and ``buckets`` also feeds the table view.
Colors come from CSS classes (``app.css``), so light and dark themes each use their own steps.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

from greenhouse.models import Reading

WIDTH, HEIGHT = 720, 260
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 44, 64, 12, 28
MAX_BUCKETS = 288


@dataclass(frozen=True)
class Bucket:
    start: datetime
    end: datetime
    avg_f: float
    min_f: float
    max_f: float
    count: int


@dataclass(frozen=True)
class Chart:
    svg: Markup
    buckets: list[Bucket]


def bucketize(readings: list[Reading], start: datetime, end: datetime, n: int = MAX_BUCKETS) -> list[Bucket]:
    width = (end - start) / n
    groups: dict[int, list[float]] = {}
    for r in readings:
        if start <= r.observed_at < end:
            groups.setdefault(int((r.observed_at - start) / width), []).append(r.temperature_f)
    return [
        Bucket(start + width * i, start + width * (i + 1), sum(v) / len(v), min(v), max(v), len(v))
        for i, v in sorted(groups.items())
    ]


def on_periods(events: list[dict], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Spans when the last accepted command was ON, from command_accepted events."""
    periods, on_since = [], None
    for e in sorted(events, key=lambda e: e["at"]):
        if e.get("kind") != "command_accepted":
            continue
        on = bool((e.get("detail") or {}).get("on"))
        if on and on_since is None:
            on_since = max(e["at"], start)
        elif not on and on_since is not None:
            periods.append((on_since, e["at"]))
            on_since = None
    if on_since is not None:
        periods.append((on_since, end))
    return [(a, b) for a, b in periods if b > start and a < end]


LABEL_GAP = 14  # px between label baselines in the right margin


def _end_label_y(preferred: float, threshold_ys: list[float]) -> float:
    """Keep the end-value label from overprinting a threshold label in the right margin."""
    y = preferred
    for _ in range(4):
        clash = [t for t in threshold_ys if abs((t + 4) - y) < LABEL_GAP]
        if not clash:
            return y
        t = clash[0] + 4
        y = t - LABEL_GAP if preferred <= t else t + LABEL_GAP
    return y


def _nice_range(lo: float, hi: float) -> tuple[float, float, float]:
    lo, hi = min(lo, hi), max(lo, hi)
    span = max(hi - lo, 10.0)
    step = next(s for s in (2, 5, 10, 20, 25, 50) if span / s <= 6)
    return (lo // step) * step, -(-hi // step) * step, step


def render(
    readings: list[Reading],
    events: list[dict],
    start: datetime,
    end: datetime,
    high_f: float,
    low_f: float,
    tz: str,
) -> Chart:
    zone = ZoneInfo(tz)
    buckets = bucketize(readings, start, end)
    temps = [b.min_f for b in buckets] + [b.max_f for b in buckets] + [high_f, low_f]
    y_lo, y_hi, y_step = _nice_range(min(temps), max(temps))
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    total = (end - start).total_seconds()

    def x(t: datetime) -> float:
        return PAD_LEFT + plot_w * (t - start).total_seconds() / total

    def y(f: float) -> float:
        return PAD_TOP + plot_h * (y_hi - f) / (y_hi - y_lo)

    parts = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="Greenhouse temperature from {escape(start.astimezone(zone).strftime("%b %d %H:%M"))} '
        f'to {escape(end.astimezone(zone).strftime("%b %d %H:%M"))}">'
    ]

    for a, b in on_periods(events, start, end):
        parts.append(
            f'<rect class="fan-on" x="{x(a):.1f}" y="{PAD_TOP}" width="{max(x(b) - x(a), 1):.1f}" height="{plot_h}">'
            f"<title>Fans commanded on {escape(a.astimezone(zone).strftime('%b %d %H:%M'))}"
            f"–{escape(b.astimezone(zone).strftime('%H:%M'))}</title></rect>"
        )

    f = y_lo
    while f <= y_hi + 1e-9:
        parts.append(f'<line class="grid" x1="{PAD_LEFT}" x2="{PAD_LEFT + plot_w}" y1="{y(f):.1f}" y2="{y(f):.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD_LEFT - 6}" y="{y(f) + 4:.1f}" text-anchor="end">{f:g}°</text>')
        f += y_step

    span_hours = total / 3600
    tick_every = timedelta(hours=4 if span_hours <= 36 else 24)
    t = start.astimezone(zone).replace(minute=0, second=0, microsecond=0)
    if tick_every >= timedelta(hours=24):
        t = t.replace(hour=0)
    while t <= end:
        if t >= start:
            label = t.strftime("%H:%M") if tick_every < timedelta(hours=24) else t.strftime("%a %d")
            parts.append(f'<text class="tick" x="{x(t):.1f}" y="{HEIGHT - 8}" text-anchor="middle">{escape(label)}</text>')
        t = (t + tick_every).astimezone(zone)

    for value, name in ((high_f, "High"), (low_f, "Low")):
        parts.append(f'<line class="threshold" x1="{PAD_LEFT}" x2="{PAD_LEFT + plot_w}" y1="{y(value):.1f}" y2="{y(value):.1f}"/>')
        parts.append(f'<text class="threshold-label" x="{PAD_LEFT + plot_w + 6}" y="{y(value) + 4:.1f}">{name} {value:g}°</text>')

    if buckets:
        points = " ".join(f"{x(b.start + (b.end - b.start) / 2):.1f},{y(b.avg_f):.1f}" for b in buckets)
        parts.append(f'<polyline class="temp-line" points="{points}"/>')
        last = buckets[-1]
        lx, ly = x(last.start + (last.end - last.start) / 2), y(last.avg_f)
        parts.append(f'<circle class="temp-end" cx="{lx:.1f}" cy="{ly:.1f}" r="4"/>')
        parts.append(
            f'<text class="end-label" x="{lx + 8:.1f}" y="{_end_label_y(ly - 8, [y(high_f), y(low_f)]):.1f}">'
            f"{last.avg_f:.1f}°</text>"
        )
        for b in buckets:
            bx0, bx1 = x(b.start), x(b.end)
            when = b.start.astimezone(zone).strftime("%b %d %H:%M")
            parts.append(
                f'<rect class="hit" x="{bx0:.1f}" y="{PAD_TOP}" width="{max(bx1 - bx0, 1):.1f}" height="{plot_h}">'
                f"<title>{escape(when)}: {b.avg_f:.1f}°F (low {b.min_f:.1f}, high {b.max_f:.1f}, {b.count} samples)</title></rect>"
            )
    else:
        parts.append(f'<text class="tick" x="{PAD_LEFT + plot_w / 2}" y="{PAD_TOP + plot_h / 2}" text-anchor="middle">No samples in this range</text>')

    parts.append("</svg>")
    return Chart(Markup("".join(parts)), buckets)
