"""Generate a small dependency-free iOS touch icon; cached in process memory."""
import math
import struct
import zlib


def _in_triangle(x, y, points):
    (ax, ay), (bx, by), (cx, cy) = points
    d1 = (x-bx)*(ay-by) - (ax-bx)*(y-by)
    d2 = (x-cx)*(by-cy) - (bx-cx)*(y-cy)
    d3 = (x-ax)*(cy-ay) - (cx-ax)*(y-ay)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and
                (d1 > 0 or d2 > 0 or d3 > 0))


def _pixel(x, y):
    u, v = (x + .5) / 180, (y + .5) / 180
    t = (u + v) / 2
    base = (int(249-26*t), int(218-88*t), int(224-53*t))
    if ((u-.5)/.37)**2 + ((v-.52)/.37)**2 < 1:
        base = tuple(min(255, int(c*.78 + 255*.22)) for c in base)
    # Cat ears, face and rosy cheeks; all drawn over a coral-pink background.
    left = _in_triangle(u, v, ((.25,.43),(.29,.24),(.43,.43)))
    right = _in_triangle(u, v, ((.57,.43),(.72,.24),(.75,.43)))
    head = ((u-.5)/.265)**2 + ((v-.545)/.255)**2 < 1
    if left or right or head:
        base = (255, 250, 246)
        if ((u-.33)/.09)**2 + ((v-.585)/.055)**2 < 1 or \
           ((u-.67)/.09)**2 + ((v-.585)/.055)**2 < 1:
            base = (250, 213, 215)
        # Eyes: petite sideways arcs
        if ((u-.415)/.058)**2 + ((v-.515)/.021)**2 < 1 and v < .52 or \
           ((u-.585)/.058)**2 + ((v-.515)/.021)**2 < 1 and v < .52:
            base = (164, 92, 117)
        # Nose and muzzle
        if abs(u-.5) < .026 and abs(v-.586) < .015:
            base = (211, 124, 151)
        if (.604 < v < .616) and abs(u-.5) < .008:
            base = (164, 92, 117)
    # Small star above the right ear.
    if abs(u-.77) + abs(v-.19) < .055 or \
       (abs(u-.77) < .015 and abs(v-.19) < .085) or \
       (abs(v-.19) < .015 and abs(u-.77) < .085):
        base = (255, 253, 246)
    return bytes((*base, 255))


def _chunk(label, payload):
    return (struct.pack(">I", len(payload)) + label + payload +
            struct.pack(">I", zlib.crc32(label + payload) & 0xffffffff))


def build_icon_png():
    width = height = 180
    raw = b"".join(b"\\x00" + b"".join(_pixel(x,y) for x in range(width))
                   for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\\x89PNG\\r\\n\\x1a\\n" + _chunk(b"IHDR", header) +
            _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


APPLE_TOUCH_ICON = build_icon_png()
