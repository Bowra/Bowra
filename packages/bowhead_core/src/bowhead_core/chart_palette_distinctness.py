"""Perceptual distinctness of categorical chart colors.

Contrast against the surface answers "can I see this mark?"; it says nothing about
"can I tell these two marks apart?". This module answers the second question with
CIEDE2000 under normal vision and under simulated protan / deutan vision (the two
common dichromacies), taking the worst of the three. Simulated colors are quantized
back to 8-bit sRGB before measuring, so the number describes the swatch that is
actually painted rather than an unrounded intermediate.

Pure Python on purpose: the gateway runs it while projecting palettes, and the
registry checker runs it over every built-in preset.
"""

from __future__ import annotations

import math
from functools import lru_cache
from itertools import combinations
from typing import Final

# 100% severity matrices from the Machado model, applied to linear RGB.
CVD_MATRICES: Final = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.01182, 0.04294, 0.968881),
    ),
    "tritan": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.3039),
    ),
}

#: Visions a categorical palette must stay distinguishable under. Tritan is rare
#: (about 0.01%) and is reported by the existing OKLab warning instead.
DISTINCTNESS_VISIONS: Final = ("normal", "protan", "deutan")

#: Reviewed base colors: every pair must keep at least this CIEDE2000 distance under
#: normal vision ...
BASE_NORMAL_TARGET: Final = 10.0
#: ... and at least this under simulated protan / deutan vision.
BASE_CVD_TARGET: Final = 7.0

_SRGB_TO_XYZ: Final = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
_WHITE: Final = (0.95047, 1.0, 1.08883)


def _srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return 12.92 * value if value <= 0.0031308 else 1.055 * (value ** (1 / 2.4)) - 0.055


def _linear_rgb(value: str) -> tuple[float, float, float]:
    return (
        _srgb_to_linear(int(value[1:3], 16) / 255),
        _srgb_to_linear(int(value[3:5], 16) / 255),
        _srgb_to_linear(int(value[5:7], 16) / 255),
    )


def simulate_vision(value: str, vision: str) -> str:
    """Return the 8-bit color a viewer with ``vision`` perceives for ``value``."""

    value = value.upper()
    if vision == "normal":
        return value
    matrix = CVD_MATRICES[vision]
    rgb = _linear_rgb(value)
    transformed = tuple(sum(row[index] * rgb[index] for index in range(3)) for row in matrix)
    return "#" + "".join(f"{round(_linear_to_srgb(channel) * 255):02X}" for channel in transformed)


@lru_cache(maxsize=8192)
def _lab(value: str) -> tuple[float, float, float]:
    rgb = _linear_rgb(value)
    xyz = [sum(row[index] * rgb[index] for index in range(3)) / _WHITE[axis] for axis, row in enumerate(_SRGB_TO_XYZ)]

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = (f(component) for component in xyz)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _ciede2000(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    l1, a1, b1 = first
    l2, a2, b2 = second
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7))) if c_bar > 0 else 0.5
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0
    delta_l = l2 - l1
    delta_c = c2p - c1p
    if c1p * c2p == 0:
        delta_h = 0.0
    else:
        difference = h2p - h1p
        delta_h = difference - 360 if difference > 180 else difference + 360 if difference < -180 else difference
    delta_big_h = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(delta_h / 2))
    l_bar = (l1 + l2) / 2
    cp_bar = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hp_bar = h1p + h2p
    else:
        span = abs(h1p - h2p)
        total = h1p + h2p
        hp_bar = total / 2 if span <= 180 else (total + 360) / 2 if total < 360 else (total - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hp_bar - 30))
        + 0.24 * math.cos(math.radians(2 * hp_bar))
        + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
        - 0.20 * math.cos(math.radians(4 * hp_bar - 63))
    )
    delta_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    r_c = 2 * math.sqrt(cp_bar**7 / (cp_bar**7 + 25**7)) if cp_bar > 0 else 0.0
    s_l = 1 + (0.015 * (l_bar - 50) ** 2) / math.sqrt(20 + (l_bar - 50) ** 2)
    s_c = 1 + 0.045 * cp_bar
    s_h = 1 + 0.015 * cp_bar * t
    r_t = -math.sin(math.radians(2 * delta_theta)) * r_c
    return math.sqrt((delta_l / s_l) ** 2 + (delta_c / s_c) ** 2 + (delta_big_h / s_h) ** 2 + r_t * (delta_c / s_c) * (delta_big_h / s_h))


def delta_e_2000(first: str, second: str) -> float:
    """CIEDE2000 between two opaque ``#RRGGBB`` colors."""

    return _ciede2000(_lab(first.upper()), _lab(second.upper()))


def vision_labs(value: str, visions: tuple[str, ...] = DISTINCTNESS_VISIONS) -> tuple[tuple[float, float, float], ...]:
    """Lab coordinates of ``value`` as seen under each of ``visions``."""

    return tuple(_lab(simulate_vision(value, vision)) for vision in visions)


def labs_distinctness(
    first: tuple[tuple[float, float, float], ...],
    second: tuple[tuple[float, float, float], ...],
) -> float:
    return min(_ciede2000(left, right) for left, right in zip(first, second, strict=True))


def distinctness(first: str, second: str, visions: tuple[str, ...] = DISTINCTNESS_VISIONS) -> float:
    """Worst CIEDE2000 between two colors across ``visions``."""

    return labs_distinctness(vision_labs(first, visions), vision_labs(second, visions))


def worst_pair(palette: list[str], visions: tuple[str, ...] = DISTINCTNESS_VISIONS) -> tuple[int, int, float] | None:
    """``(i, j, ΔE)`` of the least distinguishable pair, or ``None`` below two colors."""

    labs = [vision_labs(color, visions) for color in palette]
    worst: tuple[int, int, float] | None = None
    for (first_index, first), (second_index, second) in combinations(enumerate(labs), 2):
        value = labs_distinctness(first, second)
        if worst is None or value < worst[2]:
            worst = (first_index, second_index, value)
    return worst


def distinct_prefix_length(palette: list[str], *, normal_target: float, cvd_target: float) -> int:
    """Largest ``k`` whose first ``k`` colors keep every pair above both targets."""

    normal = [vision_labs(color, ("normal",)) for color in palette]
    cvd = [vision_labs(color, ("protan", "deutan")) for color in palette]
    for count in range(1, len(palette)):
        for earlier in range(count):
            if labs_distinctness(normal[earlier], normal[count]) < normal_target or labs_distinctness(cvd[earlier], cvd[count]) < cvd_target:
                return count
    return len(palette)


__all__ = [
    "BASE_CVD_TARGET",
    "BASE_NORMAL_TARGET",
    "CVD_MATRICES",
    "DISTINCTNESS_VISIONS",
    "delta_e_2000",
    "distinct_prefix_length",
    "distinctness",
    "labs_distinctness",
    "simulate_vision",
    "vision_labs",
    "worst_pair",
]
