"""Small QR SVG renderer for Feishu setup URLs.

This intentionally implements only the QR subset Bowhead needs for admin setup:
byte-mode payloads that fit in version 10-L. The setup URL is short-lived and
contains no secrets; the SVG endpoint remains admin-authenticated.
"""

from __future__ import annotations

from html import escape

from bowhead_gateway.product_identity import product_name

_VERSION = 10
_SIZE = 17 + 4 * _VERSION
_DATA_CODEWORDS = 274
_ECC_CODEWORDS_PER_BLOCK = 18
_BLOCK_DATA_LENGTHS = (68, 68, 69, 69)
_FORMAT_ECL_LOW = 0b01
_MASK = 0


class QrPayloadTooLongError(ValueError):
    """Raised when the setup URL does not fit the supported QR version."""


def render_setup_qr_svg(payload: str, *, scale: int = 4, border: int = 4) -> str:
    """Render a setup URL as a QR SVG string."""

    modules = _encode_version_10_low(payload)
    dim = len(modules) + border * 2
    path_parts: list[str] = []
    for y, row in enumerate(modules):
        start: int | None = None
        for x, dark in enumerate([*row, False]):
            if dark and start is None:
                start = x
            elif not dark and start is not None:
                path_parts.append(f"M{start + border},{y + border}h{x - start}v1h-{x - start}z")
                start = None
    width = dim * scale
    path = "".join(path_parts)
    title = escape(f"{product_name()} Feishu setup QR")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{width}" '
        f'viewBox="0 0 {dim} {dim}" role="img" aria-label="{title}" '
        f'shape-rendering="crispEdges">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        f'<path d="{path}" fill="#111"/>'
        "</svg>"
    )


def _encode_version_10_low(payload: str) -> list[list[bool]]:
    data = payload.encode("utf-8")
    max_bytes = _DATA_CODEWORDS - 3
    if len(data) > max_bytes:
        raise QrPayloadTooLongError(f"QR setup payload is {len(data)} bytes; maximum supported is {max_bytes}")

    function = [[False] * _SIZE for _ in range(_SIZE)]
    modules = [[False] * _SIZE for _ in range(_SIZE)]
    _draw_function_patterns(modules, function)

    codewords = _final_codewords(_data_codewords(data))
    _draw_codewords(modules, function, codewords)
    _draw_format_bits(modules, function)
    _draw_version_bits(modules, function)
    return modules


def _data_codewords(data: bytes) -> list[int]:
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)
    _append_bits(bits, len(data), 16)
    for byte in data:
        _append_bits(bits, byte, 8)

    capacity = _DATA_CODEWORDS * 8
    _append_bits(bits, 0, min(4, capacity - len(bits)))
    while len(bits) % 8:
        bits.append(0)

    result = [int("".join(str(bit) for bit in bits[i : i + 8]), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(result) < _DATA_CODEWORDS:
        result.append(pad)
        pad ^= 0xEC ^ 0x11
    return result


def _append_bits(bits: list[int], value: int, count: int) -> None:
    for i in range(count - 1, -1, -1):
        bits.append((value >> i) & 1)


def _final_codewords(data: list[int]) -> list[int]:
    blocks: list[list[int]] = []
    ecc_blocks: list[list[int]] = []
    offset = 0
    for length in _BLOCK_DATA_LENGTHS:
        block = data[offset : offset + length]
        offset += length
        blocks.append(block)
        ecc_blocks.append(_reed_solomon_remainder(block, _ECC_CODEWORDS_PER_BLOCK))
    if offset != len(data):
        raise AssertionError("QR data block split mismatch")

    result: list[int] = []
    for i in range(max(len(block) for block in blocks)):
        for block in blocks:
            if i < len(block):
                result.append(block[i])
    for i in range(_ECC_CODEWORDS_PER_BLOCK):
        for block in ecc_blocks:
            result.append(block[i])
    return result


def _draw_function_patterns(modules: list[list[bool]], function: list[list[bool]]) -> None:
    _draw_finder(modules, function, 3, 3)
    _draw_finder(modules, function, _SIZE - 4, 3)
    _draw_finder(modules, function, 3, _SIZE - 4)

    for i in range(8, _SIZE - 8):
        dark = i % 2 == 0
        _set_function(modules, function, i, 6, dark)
        _set_function(modules, function, 6, i, dark)

    for y in (6, 28, 50):
        for x in (6, 28, 50):
            if (x, y) in {(6, 6), (50, 6), (6, 50)}:
                continue
            _draw_alignment(modules, function, x, y)

    _reserve_format_areas(function)
    _reserve_version_areas(function)


def _draw_finder(
    modules: list[list[bool]],
    function: list[list[bool]],
    cx: int,
    cy: int,
) -> None:
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            x = cx + dx
            y = cy + dy
            if 0 <= x < _SIZE and 0 <= y < _SIZE:
                dist = max(abs(dx), abs(dy))
                dark = dist not in {2, 4}
                _set_function(modules, function, x, y, dark)


def _draw_alignment(
    modules: list[list[bool]],
    function: list[list[bool]],
    cx: int,
    cy: int,
) -> None:
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            dist = max(abs(dx), abs(dy))
            _set_function(modules, function, cx + dx, cy + dy, dist != 1)


def _reserve_format_areas(function: list[list[bool]]) -> None:
    positions: set[tuple[int, int]] = set()
    for i in range(6):
        positions.add((8, i))
        positions.add((i, 8))
    positions.update({(8, 7), (8, 8), (7, 8)})
    for i in range(8):
        positions.add((_SIZE - 1 - i, 8))
    for i in range(8, 15):
        positions.add((8, _SIZE - 15 + i))
    positions.add((8, _SIZE - 8))
    for x, y in positions:
        function[y][x] = True


def _reserve_version_areas(function: list[list[bool]]) -> None:
    for i in range(18):
        a = _SIZE - 11 + i % 3
        b = i // 3
        function[b][a] = True
        function[a][b] = True


def _draw_codewords(
    modules: list[list[bool]],
    function: list[list[bool]],
    codewords: list[int],
) -> None:
    bits = [((byte >> i) & 1) == 1 for byte in codewords for i in range(7, -1, -1)]
    bit_index = 0
    upward = True
    x = _SIZE - 1
    while x > 0:
        if x == 6:
            x -= 1
        rows = range(_SIZE - 1, -1, -1) if upward else range(_SIZE)
        for y in rows:
            for dx in (0, 1):
                xx = x - dx
                if function[y][xx]:
                    continue
                bit = bits[bit_index] if bit_index < len(bits) else False
                modules[y][xx] = bit ^ _mask_bit(xx, y)
                bit_index += 1
        upward = not upward
        x -= 2
    if bit_index != len(bits):
        raise AssertionError("QR codeword placement mismatch")


def _mask_bit(x: int, y: int) -> bool:
    return (x + y) % 2 == 0


def _draw_format_bits(modules: list[list[bool]], function: list[list[bool]]) -> None:
    bits = _format_bits()
    for i in range(6):
        _set_function(modules, function, 8, i, _bit(bits, i))
    _set_function(modules, function, 8, 7, _bit(bits, 6))
    _set_function(modules, function, 8, 8, _bit(bits, 7))
    _set_function(modules, function, 7, 8, _bit(bits, 8))
    for i in range(9, 15):
        _set_function(modules, function, 14 - i, 8, _bit(bits, i))

    for i in range(8):
        _set_function(modules, function, _SIZE - 1 - i, 8, _bit(bits, i))
    for i in range(8, 15):
        _set_function(modules, function, 8, _SIZE - 15 + i, _bit(bits, i))
    _set_function(modules, function, 8, _SIZE - 8, True)


def _format_bits() -> int:
    data = (_FORMAT_ECL_LOW << 3) | _MASK
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ (0x537 if (rem >> 9) & 1 else 0)
    return ((data << 10) | rem) ^ 0x5412


def _draw_version_bits(modules: list[list[bool]], function: list[list[bool]]) -> None:
    rem = _VERSION
    for _ in range(12):
        rem = (rem << 1) ^ (0x1F25 if (rem >> 11) & 1 else 0)
    bits = (_VERSION << 12) | rem
    for i in range(18):
        bit = _bit(bits, i)
        a = _SIZE - 11 + i % 3
        b = i // 3
        _set_function(modules, function, a, b, bit)
        _set_function(modules, function, b, a, bit)


def _bit(value: int, index: int) -> bool:
    return ((value >> index) & 1) == 1


def _set_function(
    modules: list[list[bool]],
    function: list[list[bool]],
    x: int,
    y: int,
    dark: bool,
) -> None:
    modules[y][x] = dark
    function[y][x] = True


def _reed_solomon_remainder(data: list[int], degree: int) -> list[int]:
    generator = _reed_solomon_generator(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result[0]
        result = [*result[1:], 0]
        for i in range(degree):
            result[i] ^= _gf_multiply(generator[i + 1], factor)
    return result


def _reed_solomon_generator(degree: int) -> list[int]:
    result = [1]
    for i in range(degree):
        result = _poly_multiply(result, [1, _gf_pow(i)])
    return result


def _poly_multiply(left: list[int], right: list[int]) -> list[int]:
    result = [0] * (len(left) + len(right) - 1)
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            result[i + j] ^= _gf_multiply(a, b)
    return result


def _gf_pow(power: int) -> int:
    result = 1
    for _ in range(power):
        result = _gf_multiply(result, 2)
    return result


def _gf_multiply(left: int, right: int) -> int:
    result = 0
    while right:
        if right & 1:
            result ^= left
        left <<= 1
        if left & 0x100:
            left ^= 0x11D
        right >>= 1
    return result
