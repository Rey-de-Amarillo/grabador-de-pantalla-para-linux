"""Screen selection and placement, independent of the graphical toolkit."""
from __future__ import annotations

import math


def selection_region(start, end, screen_size):
    width, height = screen_size
    left = max(0, min(width, math.floor(min(start[0], end[0]))))
    top = max(0, min(height, math.floor(min(start[1], end[1]))))
    right = max(0, min(width, math.ceil(max(start[0], end[0]))))
    bottom = max(0, min(height, math.ceil(max(start[1], end[1]))))
    selected_width = right - left
    selected_height = bottom - top
    selected_width -= selected_width % 2
    selected_height -= selected_height % 2
    if selected_width < 16 or selected_height < 16:
        return None
    return left, top, selected_width, selected_height


def intersects(first, second):
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by


def clamp_toolbar_position(position, screen_size, toolbar_size, workarea=None):
    """Keep the whole toolbar reachable, including after a display change."""
    sw, sh = screen_size
    tw, th = toolbar_size
    area_x, area_y, width, height = workarea or (0, 0, sw, sh)
    left, top = max(0, area_x), max(0, area_y)
    right, bottom = min(sw, area_x + width), min(sh, area_y + height)
    if right <= left or bottom <= top:
        left, top, right, bottom = 0, 0, sw, sh
    left, top = min(left, max(0, sw - tw)), min(top, max(0, sh - th))
    return (int(max(left, min(position[0], max(left, right - tw)))),
            int(max(top, min(position[1], max(top, bottom - th)))))


def toolbar_position(screen_size, toolbar_size, region=None):
    sw, sh = screen_size
    tw, th = toolbar_size
    preferred = (max(0, (sw - tw) // 2), 12)
    if region is None or not intersects((*preferred, tw, th), region):
        return preferred
    x, y, width, height = region
    center_x = x + (width - tw) // 2
    center_y = y + (height - th) // 2
    candidates = [
        (center_x, y - th - 8),
        (center_x, y + height + 8),
        (x + width + 8, center_y),
        (x - tw - 8, center_y),
        (8, 12), (sw - tw - 8, 12),
    ]
    for cx, cy in candidates:
        if 0 <= cx <= sw - tw and 0 <= cy <= sh - th:
            if not intersects((cx, cy, tw, th), region):
                return cx, cy
    # A full-screen selection keeps the single stop control reachable.
    return preferred
