"""
Pure-Python SVG generators for the Kindle e-ink landscape dashboard.

Landscape viewport (viewer perspective): 1465 × 1264 px.
All sizes assume the rotated landscape layout.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING

import pytz

if TYPE_CHECKING:
    from app.store import WeatherSample

ROME_TZ = pytz.timezone("Europe/Rome")

# ---------------------------------------------------------------------------
# WMO weather code → icon key
# ---------------------------------------------------------------------------
_WMO_ICON: dict[int, str] = {
    0: "clear",
    1: "clear", 2: "partly_cloudy", 3: "cloudy",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    61: "rain", 63: "rain", 65: "rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow",
    80: "rain", 81: "rain", 82: "rain",
    85: "snow", 86: "snow",
    95: "storm", 96: "storm", 99: "storm",
}

# All icons drawn in a 40×40 coordinate space, black strokes only.
_ICON_PATHS: dict[str, str] = {
    "clear": (
        '<circle cx="20" cy="20" r="7" fill="none" stroke="#000" stroke-width="2.5"/>'
        '<line x1="20" y1="2"  x2="20" y2="9"  stroke="#000" stroke-width="2.5"/>'
        '<line x1="20" y1="31" x2="20" y2="38" stroke="#000" stroke-width="2.5"/>'
        '<line x1="2"  y1="20" x2="9"  y2="20" stroke="#000" stroke-width="2.5"/>'
        '<line x1="31" y1="20" x2="38" y2="20" stroke="#000" stroke-width="2.5"/>'
        '<line x1="6"  y1="6"  x2="11" y2="11" stroke="#000" stroke-width="2.5"/>'
        '<line x1="29" y1="29" x2="34" y2="34" stroke="#000" stroke-width="2.5"/>'
        '<line x1="34" y1="6"  x2="29" y2="11" stroke="#000" stroke-width="2.5"/>'
        '<line x1="11" y1="29" x2="6"  y2="34" stroke="#000" stroke-width="2.5"/>'
    ),
    "partly_cloudy": (
        '<circle cx="14" cy="14" r="6" fill="none" stroke="#000" stroke-width="2"/>'
        '<line x1="14" y1="3"  x2="14" y2="7"  stroke="#000" stroke-width="2"/>'
        '<line x1="3"  y1="14" x2="7"  y2="14" stroke="#000" stroke-width="2"/>'
        '<line x1="6"  y1="6"  x2="9"  y2="9"  stroke="#000" stroke-width="2"/>'
        '<line x1="22" y1="6"  x2="19" y2="9"  stroke="#000" stroke-width="2"/>'
        '<path d="M17 26 Q17 22 21 22 Q23 22 24 24 Q26 19 31 20 Q37 21 37 27 '
        'Q37 32 30 32 L20 32 Q13 32 13 27 Q13 22 17 26" '
        'fill="none" stroke="#000" stroke-width="2"/>'
    ),
    "cloudy": (
        '<path d="M7 26 Q7 20 13 20 Q15 20 17 22 Q19 15 27 16 '
        'Q35 17 35 24 Q35 31 27 31 L13 31 Q5 31 5 25 Q5 20 7 26" '
        'fill="none" stroke="#000" stroke-width="2.5"/>'
    ),
    "fog": (
        '<line x1="4"  y1="12" x2="36" y2="12" stroke="#000" stroke-width="3"/>'
        '<line x1="8"  y1="20" x2="32" y2="20" stroke="#000" stroke-width="3"/>'
        '<line x1="4"  y1="28" x2="36" y2="28" stroke="#000" stroke-width="3"/>'
    ),
    "drizzle": (
        '<path d="M7 18 Q7 12 13 12 Q15 12 17 14 Q19 8 27 9 Q34 10 34 17 '
        'Q34 22 27 23 L13 23 Q6 23 6 18" fill="none" stroke="#000" stroke-width="2.5"/>'
        '<line x1="14" y1="27" x2="12" y2="33" stroke="#000" stroke-width="2"/>'
        '<line x1="21" y1="27" x2="19" y2="33" stroke="#000" stroke-width="2"/>'
        '<line x1="28" y1="27" x2="26" y2="33" stroke="#000" stroke-width="2"/>'
    ),
    "rain": (
        '<path d="M7 17 Q7 11 13 11 Q15 11 17 13 Q19 7 27 8 Q35 9 35 16 '
        'Q35 21 28 22 L12 22 Q5 22 5 17" fill="none" stroke="#000" stroke-width="2.5"/>'
        '<line x1="12" y1="26" x2="9"  y2="36" stroke="#000" stroke-width="2.5"/>'
        '<line x1="20" y1="26" x2="17" y2="36" stroke="#000" stroke-width="2.5"/>'
        '<line x1="28" y1="26" x2="25" y2="36" stroke="#000" stroke-width="2.5"/>'
    ),
    "snow": (
        '<path d="M7 17 Q7 11 13 11 Q15 11 17 13 Q19 7 27 8 Q35 9 35 16 '
        'Q35 21 28 22 L12 22 Q5 22 5 17" fill="none" stroke="#000" stroke-width="2.5"/>'
        '<circle cx="13" cy="30" r="2.5" fill="#000"/>'
        '<circle cx="20" cy="33" r="2.5" fill="#000"/>'
        '<circle cx="27" cy="30" r="2.5" fill="#000"/>'
        '<circle cx="16" cy="37" r="2.5" fill="#000"/>'
        '<circle cx="24" cy="37" r="2.5" fill="#000"/>'
    ),
    "storm": (
        '<path d="M5 16 Q5 10 11 10 Q13 10 15 12 Q17 6 25 7 Q33 8 33 15 '
        'Q33 20 26 21 L11 21 Q4 21 4 16" fill="none" stroke="#000" stroke-width="2.5"/>'
        '<polyline points="22,22 17,31 22,31 16,39" '
        'fill="none" stroke="#000" stroke-width="3" stroke-linejoin="round"/>'
    ),
}


def wmo_icon_key(code: int) -> str:
    return _WMO_ICON.get(code, "cloudy")


def weather_icon_svg(icon_key: str, size: int = 40) -> str:
    """Standalone <svg> for use directly in HTML (not inside another SVG)."""
    paths = _ICON_PATHS.get(icon_key, _ICON_PATHS["cloudy"])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" '
        f'width="{size}" height="{size}" style="display:inline-block;vertical-align:middle">'
        f'{paths}</svg>'
    )


# ---------------------------------------------------------------------------
# Hourly bar + icon + precipitation chart
# ---------------------------------------------------------------------------

def hourly_chart(hourly_points: list, width: int, height: int) -> str:
    """
    24-column SVG: precipitation probability strip → icon → temp label → bar → hour label.
    Icons embedded as <g transform="scale+translate"> over 40×40 path data (no nested <svg>).
    Current hour is shaded with a clearly visible dark band.
    """
    if not hourly_points:
        return _empty_svg(width, height, "No hourly data")

    n = len(hourly_points)
    slot_w = width / n

    temps = [p.temperature for p in hourly_points]
    t_min, t_max = min(temps), max(temps)
    if t_max == t_min:
        t_max = t_min + 1

    # Row heights
    precip_h = 10     # precipitation probability strip at top
    icon_h   = 36     # weather icon
    temp_h   = 26     # temperature label
    label_h  = 20     # hour label at bottom
    bar_h    = height - precip_h - icon_h - temp_h - label_h  # bar chart area

    now_h = datetime.now(ROME_TZ).hour

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="{height}">'
        f'<rect width="{width}" height="{height}" fill="white" stroke="black" stroke-width="1"/>'
    ]

    for i, pt in enumerate(hourly_points):
        x_left   = slot_w * i
        x_center = x_left + slot_w / 2

        # Strong shading for the current hour
        if pt.hour == now_h:
            parts.append(
                f'<rect x="{x_left:.1f}" y="0" width="{slot_w:.1f}" height="{height}" '
                f'fill="#000" opacity="0.14"/>'
            )

        # Vertical separator
        if i > 0:
            parts.append(
                f'<line x1="{x_left:.1f}" y1="0" x2="{x_left:.1f}" y2="{height}" '
                f'stroke="#bbb" stroke-width="0.5"/>'
            )

        # Precipitation probability strip (top)
        if pt.precip_prob > 0:
            pct = pt.precip_prob / 100
            pw = max(1, slot_w - 2)
            # Fill proportional to probability, left-aligned within the strip
            pp_w = pw * pct
            parts.append(
                f'<rect x="{x_left + 1:.1f}" y="1" width="{pp_w:.1f}" height="{precip_h - 2}" '
                f'fill="#444" opacity="0.7"/>'
            )
            if pt.precip_prob >= 30:
                parts.append(
                    f'<text x="{x_center:.1f}" y="{precip_h - 1}" text-anchor="middle" '
                    f'font-family="Georgia,serif" font-size="9" fill="white">'
                    f'{pt.precip_prob}%</text>'
                )

        # Weather icon
        icon_key = wmo_icon_key(pt.weather_code)
        paths = _ICON_PATHS.get(icon_key, _ICON_PATHS["cloudy"])
        icon_size = min(30, slot_w - 4)
        scale = icon_size / 40
        tx = x_left + (slot_w - icon_size) / 2
        ty = precip_h + (icon_h - icon_size) / 2
        parts.append(
            f'<g transform="translate({tx:.2f},{ty:.2f}) scale({scale:.4f})">'
            f'{paths}</g>'
        )

        # Temperature label
        temp_y = precip_h + icon_h + temp_h - 5
        # Bold for current hour
        weight = "bold" if pt.hour == now_h else "normal"
        parts.append(
            f'<text x="{x_center:.1f}" y="{temp_y}" text-anchor="middle" '
            f'font-family="Georgia,serif" font-size="16" font-weight="{weight}" fill="#000">'
            f'{pt.temperature:.0f}°</text>'
        )

        # Temperature bar (filled from bottom of bar area upward)
        norm = (pt.temperature - t_min) / (t_max - t_min)
        bh = max(4, norm * (bar_h - 4))
        bar_top = precip_h + icon_h + temp_h + (bar_h - bh)
        bw = max(1, slot_w - 6)
        bx = x_left + 3
        # Current hour: solid black bar; others: light gray
        fill = "#000" if pt.hour == now_h else "#888"
        opacity = "0.55" if pt.hour == now_h else "0.25"
        parts.append(
            f'<rect x="{bx:.1f}" y="{bar_top:.1f}" width="{bw:.1f}" '
            f'height="{bh:.1f}" fill="{fill}" opacity="{opacity}"/>'
        )

        # Hour label
        label_y = height - 4
        parts.append(
            f'<text x="{x_center:.1f}" y="{label_y}" text-anchor="middle" '
            f'font-family="Georgia,serif" font-size="13" fill="#333">'
            f'{pt.hour:02d}h</text>'
        )

    parts.append('</svg>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# 48h line graph (pressure / wind)
# ---------------------------------------------------------------------------

def line_graph(
    samples: list,
    attr: str,
    width: int,
    height: int,
    title: str,
    y_unit: str,
    x_fmt: str = "%H:%M",
) -> str:
    """
    SVG line graph: labeled axes, dashed grid, bold data line, latest value annotated.
    Fonts are sized for clear readability on 300ppi e-ink at arm's length.
    """
    if not samples:
        return _empty_svg(width, height, "No data yet")

    values = [float(getattr(s, attr)) for s in samples]
    times  = [s.timestamp for s in samples]

    v_min, v_max = min(values), max(values)
    v_range = v_max - v_min or 1.0
    v_min -= v_range * 0.08
    v_max += v_range * 0.08

    # ml wide: room for y-axis numeric labels + rotated unit label; mr small: don't waste width
    ml, mr, mt, mb = 90, 20, 40, 52

    gw = width  - ml - mr
    gh = height - mt - mb

    def px(i: int) -> float:
        if len(samples) == 1:
            return ml + gw / 2
        return ml + i / (len(samples) - 1) * gw

    def py(v: float) -> float:
        return mt + (1.0 - (v - v_min) / (v_max - v_min)) * gh

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="{height}">'
        f'<rect width="{width}" height="{height}" fill="white" stroke="black" stroke-width="1"/>'
    ]

    # Title
    parts.append(
        f'<text x="{ml + gw // 2}" y="26" text-anchor="middle" '
        f'font-family="Georgia,serif" font-size="17" font-weight="bold" fill="#000">'
        f'{title}</text>'
    )

    # Y grid + labels
    for i in range(6):
        v = v_min + (v_max - v_min) * i / 5
        y = py(v)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + gw}" y2="{y:.1f}" '
            f'stroke="#999" stroke-width="1" stroke-dasharray="5,4"/>'
        )
        parts.append(
            f'<text x="{ml - 7}" y="{y + 5:.1f}" text-anchor="end" '
            f'font-family="Georgia,serif" font-size="14" fill="#333">'
            f'{v:.0f}</text>'
        )

    # Y-axis unit label (rotated), centred in left margin
    lx, ly = ml // 2, mt + gh // 2
    parts.append(
        f'<text transform="rotate(-90,{lx},{ly})" x="{lx}" y="{ly}" '
        f'text-anchor="middle" font-family="Georgia,serif" font-size="20" font-weight="bold" fill="#000">'
        f'{y_unit}</text>'
    )

    # X ticks + time labels (at most 8)
    n = len(samples)
    tick_step = max(1, math.ceil(n / 8))
    for i in range(0, n, tick_step):
        x = px(i)
        label = times[i].strftime(x_fmt)
        parts.append(
            f'<line x1="{x:.1f}" y1="{mt + gh}" x2="{x:.1f}" y2="{mt + gh + 5}" '
            f'stroke="#333" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{mt + gh + 22}" text-anchor="middle" '
            f'font-family="Georgia,serif" font-size="16" font-weight="bold" fill="#000">{label}</text>'
        )

    # Date label centred below x-axis
    if times:
        date_str = times[0].strftime("%d %b") + " → " + times[-1].strftime("%d %b")
        parts.append(
            f'<text x="{ml + gw // 2}" y="{mt + gh + 38}" text-anchor="middle" '
            f'font-family="Georgia,serif" font-size="12" fill="#888">{date_str}</text>'
        )

    # Axes
    parts.append(
        f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt + gh}" '
        f'stroke="#000" stroke-width="2"/>'
    )
    parts.append(
        f'<line x1="{ml}" y1="{mt + gh}" x2="{ml + gw}" y2="{mt + gh}" '
        f'stroke="#000" stroke-width="2"/>'
    )

    # Data line (thick, for e-ink legibility)
    pts_str = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(values))
    parts.append(
        f'<polyline points="{pts_str}" fill="none" '
        f'stroke="#000" stroke-width="3" stroke-linejoin="round"/>'
    )

    # Latest value dot + annotation
    lx_dot = px(n - 1)
    ly_dot = py(values[-1])
    parts.append(f'<circle cx="{lx_dot:.1f}" cy="{ly_dot:.1f}" r="5" fill="#000"/>')
    ann_y = ly_dot - 10 if ly_dot > mt + 24 else ly_dot + 22
    parts.append(
        f'<text x="{min(lx_dot, ml + gw - 30):.1f}" y="{ann_y:.1f}" text-anchor="middle" '
        f'font-family="Georgia,serif" font-size="15" font-weight="bold" fill="#000">'
        f'{values[-1]:.1f} {y_unit}</text>'
    )

    parts.append('</svg>')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Docker horizontal bar chart
# ---------------------------------------------------------------------------

def docker_bar_chart(
    items: list[tuple[str, float]],  # (name, value) already sorted descending
    max_val: float,
    width: int,
    height: int,
    unit: str = "%",
) -> str:
    """
    Horizontal bar chart for per-container CPU% or memory%.
    max_val is the denominator (pass the top value for CPU, 100 for memory%).
    """
    if not items:
        return _empty_svg(width, height, "No running containers")

    n = len(items)
    label_w = 160   # name label column — wider for bigger text
    value_w = 80    # value text at right — wider for bigger text
    bar_area = width - label_w - value_w   # narrower bar track (frees space for text)

    row_h = height // n
    bar_h = min(20, max(10, int(row_h * 0.36)))

    safe_max = max_val if max_val > 0 else 1.0

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">'
    ]

    for i, (name, val) in enumerate(items):
        cy   = i * row_h + row_h // 2
        bar_y = cy - bar_h // 2
        fill_px = int(bar_area * min(val, safe_max) / safe_max)
        fill_px = max(0, min(bar_area, fill_px))

        display = (name[:14] + "…") if len(name) > 15 else name

        # Alternating row tint
        if i % 2 == 0:
            parts.append(
                f'<rect x="0" y="{i * row_h}" width="{width}" height="{row_h}" '
                f'fill="#f7f7f7"/>'
            )

        parts += [
            # Name label — right-aligned in the label column
            f'<text x="{label_w - 8}" y="{cy + 7}" text-anchor="end" '
            f'font-family="Georgia,serif" font-size="20" fill="#000">{display}</text>',
            # Background track
            f'<rect x="{label_w}" y="{bar_y}" width="{bar_area}" height="{bar_h}" '
            f'fill="none" stroke="#ccc" stroke-width="1"/>',
            # Fill bar (solid black)
            f'<rect x="{label_w}" y="{bar_y}" width="{fill_px}" height="{bar_h}" fill="#000"/>',
            # Value label — right of the bar
            f'<text x="{label_w + bar_area + 7}" y="{cy + 7}" '
            f'font-family="Georgia,serif" font-size="20" fill="#000">'
            f'{val:.1f}{unit}</text>',
        ]

    parts.append('</svg>')
    return ''.join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_svg(width: int, height: int, msg: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
        f'<rect width="{width}" height="{height}" fill="white" stroke="black" stroke-width="1"/>'
        f'<text x="{width // 2}" y="{height // 2}" text-anchor="middle" '
        f'font-family="Georgia,serif" font-size="20" fill="#999">{msg}</text>'
        f'</svg>'
    )
