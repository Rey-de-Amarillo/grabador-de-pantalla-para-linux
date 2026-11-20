"""Read real X11 backing pixmaps underneath selected recorder windows.

This is a capture backend, not desktop automation.  It never hides, moves,
redirects or changes another application's window.  Cinnamon already redirects
its top-level windows with XComposite; this module only names and reads those
existing pixmaps, compositing them in root stacking order with XRender.

The returned patches are little-endian BGRA (premultiplied alpha, always opaque
when a real desktop background covers the patch).  A caller can replace only
the recorder's rectangles in an ordinary screen frame.  No cached screenshot or
synthetic background is substituted.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import atexit
import ctypes as C
import ctypes.util
import threading


class UnderlayUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Patch:
    data: bytes
    stride: int
    width: int
    height: int


class _Attributes(C.Structure):
    _fields_ = [
        ("x", C.c_int), ("y", C.c_int), ("width", C.c_int), ("height", C.c_int),
        ("border_width", C.c_int), ("depth", C.c_int), ("visual", C.c_void_p),
        ("root", C.c_ulong), ("class_", C.c_int), ("bit_gravity", C.c_int),
        ("win_gravity", C.c_int), ("backing_store", C.c_int),
        ("backing_planes", C.c_ulong), ("backing_pixel", C.c_ulong),
        ("save_under", C.c_int), ("colormap", C.c_ulong),
        ("map_installed", C.c_int), ("map_state", C.c_int),
        ("all_event_masks", C.c_long), ("your_event_mask", C.c_long),
        ("do_not_propagate_mask", C.c_long), ("override_redirect", C.c_int),
        ("screen", C.c_void_p),
    ]


class _Direct(C.Structure):
    _fields_ = [(name, C.c_short) for name in
                ("red", "redMask", "green", "greenMask", "blue", "blueMask", "alpha", "alphaMask")]


class _Format(C.Structure):
    _fields_ = [("id", C.c_ulong), ("type", C.c_int), ("depth", C.c_int),
                ("direct", _Direct), ("colormap", C.c_ulong)]


class _Color(C.Structure):
    _fields_ = [(key, C.c_ushort) for key in ("red", "green", "blue", "alpha")]


class _Rectangle(C.Structure):
    _fields_ = [("x", C.c_short), ("y", C.c_short),
                ("width", C.c_ushort), ("height", C.c_ushort)]


class _Image(C.Structure):
    _fields_ = [
        ("width", C.c_int), ("height", C.c_int), ("xoffset", C.c_int), ("format", C.c_int),
        ("data", C.c_void_p), ("byte_order", C.c_int), ("bitmap_unit", C.c_int),
        ("bitmap_bit_order", C.c_int), ("bitmap_pad", C.c_int), ("depth", C.c_int),
        ("bytes_per_line", C.c_int), ("bits_per_pixel", C.c_int),
        ("red_mask", C.c_ulong), ("green_mask", C.c_ulong), ("blue_mask", C.c_ulong),
        ("obdata", C.c_void_p), ("functions", C.c_void_p * 6),
    ]


class _Error(C.Structure):
    _fields_ = [("type", C.c_int), ("display", C.c_void_p),
                ("resourceid", C.c_ulong), ("serial", C.c_ulong),
                ("error_code", C.c_ubyte), ("request_code", C.c_ubyte),
                ("minor_code", C.c_ubyte)]


_HANDLER = C.CFUNCTYPE(C.c_int, C.c_void_p, C.POINTER(_Error))
_ERROR_LOCK = threading.RLock()
_ERROR_DISPLAYS: dict[int, list[int]] = {}
_PREVIOUS_HANDLER = None
_PREVIOUS_HANDLER_POINTER = None
_ERROR_HANDLER = None
_ERROR_HANDLER_XLIB = None


def _shutdown_error_handler():
    """Never leave a Python C callback reachable during interpreter teardown."""
    global _PREVIOUS_HANDLER, _PREVIOUS_HANDLER_POINTER, _ERROR_HANDLER, _ERROR_HANDLER_XLIB
    with _ERROR_LOCK:
        if _ERROR_HANDLER is not None and _ERROR_HANDLER_XLIB is not None:
            setter = _bind(_ERROR_HANDLER_XLIB, "XSetErrorHandler", [C.c_void_p], C.c_void_p)
            setter(_PREVIOUS_HANDLER_POINTER)
            _ERROR_HANDLER = None
            _PREVIOUS_HANDLER = None
            _PREVIOUS_HANDLER_POINTER = None
            _ERROR_HANDLER_XLIB = None


atexit.register(_shutdown_error_handler)


def _library(name):
    path = ctypes.util.find_library(name)
    if not path:
        raise UnderlayUnavailable(f"Falta la biblioteca {name}.")
    return C.CDLL(path)


def _bind(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


def _intersection(first, second):
    x = max(first[0], second[0]); y = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    return (x, y, right - x, bottom - y) if right > x and bottom > y else None


class Underlay:
    """Compose live pixels below excluded XIDs/PIDs without changing the UI.

    Call all methods on one capture thread.  `excluded_xids` may contain GTK
    client IDs; their containing WM frames are excluded automatically.
    `excluded_pids` additionally excludes recorder-owned popups and tooltips.
    """

    def __init__(self, excluded_xids=(), excluded_pids=(), display_name=None):
        self.x = _library("X11")
        self.composite = _library("Xcomposite")
        self.render = _library("Xrender")
        self.shape = _library("Xext")
        p = C.c_void_p; u = C.c_ulong; i = C.c_int
        self._open = _bind(self.x, "XOpenDisplay", [C.c_char_p], p)
        self._close = _bind(self.x, "XCloseDisplay", [p], i)
        self._root = _bind(self.x, "XDefaultRootWindow", [p], u)
        self._screen = _bind(self.x, "XDefaultScreen", [p], i)
        self._sync = _bind(self.x, "XSync", [p, i], i)
        self._free = _bind(self.x, "XFree", [p], i)
        self._attributes = _bind(self.x, "XGetWindowAttributes", [p, u, C.POINTER(_Attributes)], i)
        self._query_tree = _bind(self.x, "XQueryTree", [p, u, C.POINTER(u), C.POINTER(u), C.POINTER(C.POINTER(u)), C.POINTER(C.c_uint)], i)
        self._translate = _bind(self.x, "XTranslateCoordinates", [p, u, u, i, i, C.POINTER(i), C.POINTER(i), C.POINTER(u)], i)
        self._atom = _bind(self.x, "XInternAtom", [p, C.c_char_p, i], u)
        self._property = _bind(self.x, "XGetWindowProperty", [p, u, u, C.c_long, C.c_long, i, u, C.POINTER(u), C.POINTER(i), C.POINTER(u), C.POINTER(u), C.POINTER(p)], i)
        self._selection_owner = _bind(self.x, "XGetSelectionOwner", [p, u], u)
        self._new_pixmap = _bind(self.x, "XCreatePixmap", [p, u, C.c_uint, C.c_uint, C.c_uint], u)
        self._free_pixmap = _bind(self.x, "XFreePixmap", [p, u], i)
        self._get_image = _bind(self.x, "XGetImage", [p, u, i, i, C.c_uint, C.c_uint, u, i], C.POINTER(_Image))
        self._destroy_image = _bind(self.x, "XDestroyImage", [C.POINTER(_Image)], i)
        self._geometry = _bind(self.x, "XGetGeometry", [p, u, C.POINTER(u), C.POINTER(i), C.POINTER(i), C.POINTER(C.c_uint), C.POINTER(C.c_uint), C.POINTER(C.c_uint), C.POINTER(C.c_uint)], i)
        self._name_pixmap = _bind(self.composite, "XCompositeNameWindowPixmap", [p, u], u)
        self._find_visual = _bind(self.render, "XRenderFindVisualFormat", [p, p], C.POINTER(_Format))
        self._find_standard = _bind(self.render, "XRenderFindStandardFormat", [p, i], C.POINTER(_Format))
        self._picture = _bind(self.render, "XRenderCreatePicture", [p, u, C.POINTER(_Format), u, p], u)
        self._free_picture = _bind(self.render, "XRenderFreePicture", [p, u], None)
        self._fill = _bind(self.render, "XRenderFillRectangle", [p, i, u, C.POINTER(_Color), i, i, C.c_uint, C.c_uint], None)
        self._over = _bind(self.render, "XRenderComposite", [p, i, u, u, u, i, i, i, i, i, i, C.c_uint, C.c_uint], None)
        self._solid = _bind(self.render, "XRenderCreateSolidFill", [p, C.POINTER(_Color)], u)
        self._shapes = _bind(self.shape, "XShapeGetRectangles", [p, u, i, C.POINTER(i), C.POINTER(i)], C.POINTER(_Rectangle))
        self.display = self._open(display_name.encode() if display_name else None)
        if not self.display:
            raise UnderlayUnavailable("No se pudo abrir la pantalla X11.")
        self._install_error_handler()
        self.root = self._root(self.display)
        self.atoms = {name: self._atom(self.display, name.encode(), False) for name in
                      ("_NET_WM_PID", "_NET_WM_WINDOW_OPACITY", "_XROOTPMAP_ID", "ESETROOT_PMAP_ID")}
        compositor = self._atom(self.display, f"_NET_WM_CM_S{self._screen(self.display)}".encode(), False)
        if not self._selection_owner(self.display, compositor):
            self.close()
            raise UnderlayUnavailable("La pantalla no tiene un compositor X11 activo.")
        self.excluded_xids = set(int(value) for value in excluded_xids)
        self.excluded_pids = set(int(value) for value in excluded_pids)
        self._target = None
        self._target_picture = None
        self._target_size = None
        self.last_windows = []

    def _install_error_handler(self):
        global _PREVIOUS_HANDLER, _PREVIOUS_HANDLER_POINTER, _ERROR_HANDLER, _ERROR_HANDLER_XLIB
        with _ERROR_LOCK:
            if _ERROR_HANDLER is None:
                @_HANDLER
                def handler(display, event):
                    address = int(display or 0)
                    errors = _ERROR_DISPLAYS.get(address)
                    if errors is not None:
                        errors.append(int(event.contents.error_code))
                        return 0
                    return _PREVIOUS_HANDLER(display, event) if _PREVIOUS_HANDLER is not None else 0
                setter = _bind(self.x, "XSetErrorHandler", [p := C.c_void_p], p)
                old = setter(C.cast(handler, C.c_void_p))
                _PREVIOUS_HANDLER_POINTER = old
                _PREVIOUS_HANDLER = _HANDLER(old) if old else None
                _ERROR_HANDLER = handler
                _ERROR_HANDLER_XLIB = self.x
            _ERROR_DISPLAYS[int(self.display)] = []

    def _restore_error_handler(self):
        """Release our process-global trap after the final private connection.

        Another library may temporarily own Xlib's handler.  In that case keep
        its current registration and keep our callback alive in case it chains
        back to us; never replace an unrelated library's active trap.
        """
        global _PREVIOUS_HANDLER, _PREVIOUS_HANDLER_POINTER, _ERROR_HANDLER, _ERROR_HANDLER_XLIB
        if _ERROR_DISPLAYS or _ERROR_HANDLER is None:
            return
        setter = _bind(self.x, "XSetErrorHandler", [C.c_void_p], C.c_void_p)
        ours = C.cast(_ERROR_HANDLER, C.c_void_p).value
        current = setter(_PREVIOUS_HANDLER_POINTER)
        if current != ours:
            setter(current)
            return
        _ERROR_HANDLER = None
        _PREVIOUS_HANDLER = None
        _PREVIOUS_HANDLER_POINTER = None
        _ERROR_HANDLER_XLIB = None

    @contextmanager
    def _errors(self):
        self._sync(self.display, False)
        errors = _ERROR_DISPLAYS[int(self.display)]
        errors.clear()
        yield errors
        self._sync(self.display, False)

    def _longs(self, window, name, count=8):
        actual = C.c_ulong(); fmt = C.c_int(); length = C.c_ulong(); after = C.c_ulong(); data = C.c_void_p()
        status = self._property(self.display, window, self.atoms[name], 0, count, False, 0,
                                C.byref(actual), C.byref(fmt), C.byref(length), C.byref(after), C.byref(data))
        try:
            if status or not data or fmt.value != 32:
                return []
            values = C.cast(data, C.POINTER(C.c_ulong))
            return [int(values[index]) for index in range(length.value)]
        finally:
            if data:
                self._free(data)

    def _tree(self, window):
        root = C.c_ulong(); parent = C.c_ulong(); children = C.POINTER(C.c_ulong)(); count = C.c_uint()
        ok = self._query_tree(self.display, window, C.byref(root), C.byref(parent), C.byref(children), C.byref(count))
        try:
            return (int(parent.value), [int(children[n]) for n in range(count.value)]) if ok else (0, [])
        finally:
            if children:
                self._free(children)

    def _top_level(self, window):
        for _ in range(32):
            parent, _children = self._tree(window)
            if not parent or parent == self.root:
                return window
            window = parent
        return window

    def _owned(self, window, depth=0):
        if window in self.excluded_xids:
            return True
        pid = self._longs(window, "_NET_WM_PID", 1)
        if pid and pid[0] in self.excluded_pids:
            return True
        if depth < 3:
            return any(self._owned(child, depth + 1) for child in self._tree(window)[1])
        return False

    def _window_opacity(self, window, depth=0):
        value = self._longs(window, "_NET_WM_WINDOW_OPACITY", 1)
        if value:
            return value[0] & 0xFFFFFFFF
        # Window managers reparent the actual client into a decoration frame;
        # EWMH opacity normally remains on that client, not on the frame.
        if depth < 2:
            for child in self._tree(window)[1]:
                opacity = self._window_opacity(child, depth + 1)
                if opacity is not None:
                    return opacity
        return None

    def set_excluded(self, xids=(), pids=()):
        self.excluded_xids = set(map(int, xids))
        self.excluded_pids = set(map(int, pids))

    def _window(self, window):
        attributes = _Attributes()
        if not self._attributes(self.display, window, C.byref(attributes)):
            return None
        if attributes.map_state != 2 or attributes.class_ != 1 or attributes.width <= 0 or attributes.height <= 0:
            return None
        x = C.c_int(); y = C.c_int(); child = C.c_ulong()
        if not self._translate(self.display, window, self.root, 0, 0, C.byref(x), C.byref(y), C.byref(child)):
            return None
        return attributes, x.value, y.value

    def excluded_rectangles(self, margin=0):
        """Return mapped recorder frame rectangles, optionally including shadow margin."""
        rectangles = []
        with self._errors():
            excluded_frames = {self._top_level(window) for window in self.excluded_xids}
            for window in self._tree(self.root)[1]:
                if window not in excluded_frames and not self._owned(window):
                    continue
                info = self._window(window)
                if info:
                    a, x, y = info; border = a.border_width
                    rectangles.append((x - border - margin, y - border - margin,
                                       a.width + border * 2 + margin * 2,
                                       a.height + border * 2 + margin * 2))
        return rectangles

    def _prepare_target(self, width, height):
        if self._target_size == (width, height):
            return
        if self._target_picture:
            self._free_picture(self.display, self._target_picture)
        if self._target:
            self._free_pixmap(self.display, self._target)
        self._target = self._new_pixmap(self.display, self.root, width, height, 32)
        self._target_picture = self._picture(self.display, self._target, self._find_standard(self.display, 0), 0, None)
        self._target_size = width, height

    def _paint_background(self, rect):
        for name in ("_XROOTPMAP_ID", "ESETROOT_PMAP_ID"):
            ids = self._longs(self.root, name, 1)
            if not ids:
                continue
            pixmap = ids[0]
            root = C.c_ulong(); x = C.c_int(); y = C.c_int()
            width = C.c_uint(); height = C.c_uint(); border = C.c_uint(); depth = C.c_uint()
            if not self._geometry(self.display, pixmap, C.byref(root), C.byref(x), C.byref(y), C.byref(width), C.byref(height), C.byref(border), C.byref(depth)):
                continue
            if depth.value not in (24, 32):
                continue
            picture = self._picture(self.display, pixmap, self._find_standard(self.display, 0 if depth.value == 32 else 1), 0, None)
            if picture:
                self._over(self.display, 1, picture, 0, self._target_picture, rect[0], rect[1], 0, 0, 0, 0, rect[2], rect[3])
                self._free_picture(self.display, picture)
                return

    def _paint_window(self, window, rect, info):
        attributes, content_x, content_y = info
        border = attributes.border_width
        origin = content_x - border, content_y - border
        outer = (*origin, attributes.width + border * 2, attributes.height + border * 2)
        if _intersection(rect, outer) is None:
            return
        # Attribute/ownership queries for a different window may have raced
        # its destruction.  Attribute only errors from this Name request to
        # the foreground window checked below.
        self._sync(self.display, False)
        _ERROR_DISPLAYS[int(self.display)].clear()
        pixmap = self._name_pixmap(self.display, window)
        self._sync(self.display, False)
        if not pixmap or _ERROR_DISPLAYS[int(self.display)]:
            _ERROR_DISPLAYS[int(self.display)].clear()
            # A window may disappear between QueryTree and NameWindowPixmap.
            # A still-mapped but unreadable foreground must never be silently
            # replaced with the wrong window's pixels.
            if self._window(window) is None:
                return
            raise UnderlayUnavailable("Una ventana visible no permite leer su contenido real con XComposite.")
        picture = self._picture(self.display, pixmap, self._find_visual(self.display, attributes.visual), 0, None)
        if not picture:
            self._free_pixmap(self.display, pixmap)
            return
        mask = 0
        try:
            opacity = self._window_opacity(window)
            if opacity is not None and opacity != 0xFFFFFFFF:
                if opacity == 0:
                    return
                color = _Color(0, 0, 0, (opacity >> 16) & 0xFFFF)
                mask = self._solid(self.display, C.byref(color))
            count = C.c_int(); ordering = C.c_int()
            shapes = self._shapes(self.display, window, 0, C.byref(count), C.byref(ordering))
            try:
                regions = [(content_x + shapes[n].x, content_y + shapes[n].y,
                            shapes[n].width, shapes[n].height) for n in range(count.value)] if shapes else [outer]
                for shape in regions:
                    overlap = _intersection(rect, shape)
                    if overlap is None:
                        continue
                    x, y, width, height = overlap
                    self._over(self.display, 3, picture, mask, self._target_picture,
                               x - origin[0], y - origin[1], 0, 0,
                               x - rect[0], y - rect[1], width, height)
            finally:
                if shapes:
                    self._free(shapes)
            self.last_windows.append(int(window))
        finally:
            if mask:
                self._free_picture(self.display, mask)
            self._free_picture(self.display, picture)
            self._free_pixmap(self.display, pixmap)

    def get_patch(self, rect):
        """Return live BGRA bytes for a root-space rectangle, excluding recorder UI."""
        rect = tuple(map(int, rect))
        if len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0 or rect[0] < 0 or rect[1] < 0:
            raise ValueError("El rectángulo debe estar dentro de la pantalla.")
        with _ERROR_LOCK, self._errors() as errors:
            self._prepare_target(rect[2], rect[3])
            clear = _Color(0, 0, 0, 0)
            self._fill(self.display, 1, self._target_picture, C.byref(clear), 0, 0, rect[2], rect[3])
            self._paint_background(rect)
            frames = {self._top_level(window) for window in self.excluded_xids}
            self.last_windows = []
            for window in self._tree(self.root)[1]:
                info = self._window(window)
                if info is None or window in frames or self._owned(window):
                    continue
                self._paint_window(window, rect, info)
            self._sync(self.display, False)
            image = self._get_image(self.display, self._target, 0, 0, rect[2], rect[3], C.c_ulong(-1).value, 2)
            self._sync(self.display, False)
            if not image:
                raise UnderlayUnavailable(f"No se pudo leer la composición X11: {errors!r}.")
            try:
                meta = image.contents
                # XGetImage leaves RGB masks unspecified for a Pixmap.  Its
                # layout is nevertheless fixed by our standard ARGB32 target.
                if meta.bits_per_pixel != 32 or meta.byte_order != 0 or meta.depth != 32:
                    raise UnderlayUnavailable("Formato de píxeles X11 no admitido.")
                raw = C.string_at(meta.data, meta.bytes_per_line * meta.height)
                stride = rect[2] * 4
                data = b"".join(raw[row * meta.bytes_per_line:row * meta.bytes_per_line + stride] for row in range(rect[3]))
                if any(data[index] != 255 for index in range(3, len(data), 4)):
                    raise UnderlayUnavailable("La composición no tiene un fondo real completamente opaco.")
                return Patch(data, stride, rect[2], rect[3])
            finally:
                self._destroy_image(image)

    def close(self):
        with _ERROR_LOCK:
            if getattr(self, "display", None):
                if getattr(self, "_target_picture", None):
                    self._free_picture(self.display, self._target_picture)
                if getattr(self, "_target", None):
                    self._free_pixmap(self.display, self._target)
                address = int(self.display)
                self._close(self.display)
                _ERROR_DISPLAYS.pop(address, None)
                self.display = None
                self._target_picture = None
                self._target = None
                self._target_size = None
                self._restore_error_handler()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
