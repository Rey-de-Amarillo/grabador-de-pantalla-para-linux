"""Supply current window pixels underneath this recorder's own controls."""
from __future__ import annotations

from pixel_filter import PixelPatch
from x11_underlay import Underlay


class WindowExclusion:
    def __init__(self, excluded_xids=(), excluded_pids=(), margin=32):
        self.excluded_xids = tuple(excluded_xids)
        self.excluded_pids = tuple(excluded_pids)
        self.margin = margin
        self.underlay = None
        self.frames = 0
        self.patches = 0
        self.pixels = 0

    def __call__(self, layout):
        # This connection is created and used by the video streaming thread.
        if self.underlay is None:
            self.underlay = Underlay(self.excluded_xids, self.excluded_pids)
        self.frames += 1
        output = []
        for x, y, width, height in self.underlay.excluded_rectangles(self.margin):
            left = max(x, layout.origin_x, 0)
            top = max(y, layout.origin_y, 0)
            right = min(x + width, layout.origin_x + layout.width)
            bottom = min(y + height, layout.origin_y + layout.height)
            if right <= left or bottom <= top:
                continue
            patch = self.underlay.get_patch((left, top, right - left, bottom - top))
            output.append(PixelPatch(left - layout.origin_x, top - layout.origin_y,
                                     patch.width, patch.height, patch.data, patch.stride))
            self.patches += 1
            self.pixels += patch.width * patch.height
        return output

    @property
    def statistics(self):
        return {"frames_checked": self.frames, "patches": self.patches, "pixels": self.pixels}

    def close(self):
        if self.underlay is not None:
            self.underlay.close()
            self.underlay = None
