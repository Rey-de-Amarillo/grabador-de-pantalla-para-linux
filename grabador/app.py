#!/usr/bin/python3
"""Screen recorder with a visible clock, pause, cancel and video playback."""
from __future__ import annotations

from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import signal
import sys

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

from capture import CaptureSession
from geometry import clamp_toolbar_position, selection_region, toolbar_position


LOGGER = logging.getLogger("grabador")
APP_NAME = "Grabador de pantalla de Linux"


def format_duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


class CameraIcon(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self.set_size_request(30, 24)
        self.recording = False
        self.connect("draw", self.draw_icon)

    def draw_icon(self, _widget, context):
        x = (self.get_allocated_width() - 30) / 2
        y = (self.get_allocated_height() - 24) / 2
        context.translate(x, y)
        if self.recording:
            context.set_source_rgb(0.89, 0.14, 0.18)
            context.arc(15, 12, 11, 0, 6.283185307)
            context.fill()
            context.set_source_rgb(1, 1, 1)
            context.rectangle(10.5, 7.5, 9, 9)
            context.fill()
        else:
            context.set_source_rgb(1, 1, 1)
            context.set_line_width(1.8)
            context.set_line_join(cairo.LINE_JOIN_ROUND)
            context.rectangle(2.5, 5, 17, 14)
            context.stroke()
            context.move_to(20, 9)
            context.line_to(27.5, 5.5)
            context.line_to(27.5, 18.5)
            context.line_to(20, 15)
            context.close_path()
            context.stroke()
        return False


class PlaybackIcon(Gtk.DrawingArea):
    """Keep one stable button child while switching pause to resume."""
    def __init__(self):
        super().__init__()
        self.set_size_request(24, 24)
        self.paused = False
        self.connect("draw", self.draw_icon)

    def draw_icon(self, _widget, context):
        context.translate((self.get_allocated_width() - 24) / 2,
                          (self.get_allocated_height() - 24) / 2)
        context.set_source_rgb(1, 1, 1)
        if self.paused:
            context.move_to(6, 3)
            context.line_to(21, 12)
            context.line_to(6, 21)
            context.close_path()
        else:
            context.rectangle(4, 3, 6, 18)
            context.rectangle(14, 3, 6, 18)
        context.fill()
        return False


class RegionSelector(Gtk.Window):
    def __init__(self, screen_size, selected, cancelled):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.screen_size = screen_size
        self.selected = selected
        self.cancelled = cancelled
        self.start_point = None
        self.end_point = None
        self.finished = False
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_app_paintable(True)
        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        self.set_accept_focus(True)
        self.set_can_focus(True)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK
        )
        self.connect("draw", self.draw_selection)
        self.connect("button-press-event", self.press)
        self.connect("motion-notify-event", self.motion)
        self.connect("button-release-event", self.release)
        self.connect("key-press-event", self.key)
        self.connect("grab-broken-event", lambda *_: self.cancel())
        self.connect("delete-event", lambda *_: self.cancel())
        self.connect("realize", self.realized)
        self.move(0, 0)
        self.set_default_size(*screen_size)
        self.fullscreen()

    def realized(self, _widget):
        self.get_window().set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "crosshair"))

    def draw_selection(self, _widget, context):
        context.set_operator(cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0, 0, 0, 0.25)
        context.paint()
        if self.start_point is not None and self.end_point is not None:
            region = selection_region(self.start_point, self.end_point, self.screen_size)
            if region:
                x, y, width, height = region
                context.set_source_rgba(0, 0, 0, 0)
                context.rectangle(x, y, width, height)
                context.fill()
                context.set_operator(cairo.OPERATOR_OVER)
                context.set_source_rgb(0.20, 0.58, 1)
                context.set_line_width(2)
                context.rectangle(x + 1, y + 1, width - 2, height - 2)
                context.stroke()
        return True

    def press(self, _widget, event):
        if event.button == 1:
            self.start_point = (event.x_root, event.y_root)
            self.end_point = self.start_point
            self.queue_draw()
        elif event.button == 3:
            self.cancel()
        return True

    def motion(self, _widget, event):
        if self.start_point is not None:
            self.end_point = (event.x_root, event.y_root)
            self.queue_draw()
        return True

    def release(self, _widget, event):
        if self.finished:
            return True
        if event.button != 1 or self.start_point is None:
            return self.cancel()
        region = selection_region(self.start_point, (event.x_root, event.y_root), self.screen_size)
        if region is None:
            return self.cancel()
        self.finished = True
        self.hide()
        self.destroy()
        Gdk.Display.get_default().sync()
        self.selected(region)
        return True

    def key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            return self.cancel()
        return False

    def cancel(self):
        if not self.finished:
            self.finished = True
            self.hide()
            self.destroy()
            self.cancelled()
        return True


class RecorderApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="ar.workraken.GrabadorPantalla", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.toolbar = None
        self.selector = None
        self.session = None
        self.state = "idle"
        self.region = None
        self.last_saved = None
        self.last_elapsed = 0.0
        self.preview_open_requested = False
        self.timer_label = None
        self.pause_button = None
        self.pause_icon = None
        self.cancel_button = None
        self.close_button = None
        self.timer_source = None
        self.closing = False
        self.start_source = None
        self.keybinder = None
        self.manual_toolbar_position = None
        self.toolbar_drag = None
        self.toolbar_drag_origin = None
        self.toolbar_drag_pointer = None
        self.toolbar_drag_started = False
        self.suppress_toolbar_clicks = False
        self.drag_clear_source = None
        self.state_dir = Path(os.environ.get("GRABADOR_STATE_DIR", str(Path.home() / ".local/state/grabador-pantalla")))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = Path(os.environ.get("GRABADOR_OUTPUT_DIR", str(Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS) or Path.home() / "Vídeos") / "Grabaciones de pantalla")))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        previous_status = self.state_dir / "estado.json"
        if previous_status.exists():
            try:
                previous = json.loads(previous_status.read_text())
                if not isinstance(previous, dict):
                    raise ValueError("Estado anterior inválido")
                saved = Path(previous["last_saved"]) if previous.get("last_saved") else None
                if saved is not None and saved.is_file():
                    self.last_saved = saved
                position = previous.get("toolbar_manual_position")
                if (isinstance(position, (list, tuple)) and len(position) == 2
                        and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                and math.isfinite(value) for value in position)):
                    self.manual_toolbar_position = tuple(int(value) for value in position)
            except (OSError, ValueError, TypeError):
                pass
        handler = RotatingFileHandler(self.state_dir / "grabador.log", maxBytes=1024 * 1024, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        icon = Path(__file__).with_name("icon.svg")
        if icon.is_file():
            Gtk.Window.set_default_icon_from_file(str(icon))
        self.timer_source = GLib.timeout_add(200, self.update_timer)
        for signum in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self.request_quit)
        try:
            gi.require_version("Keybinder", "3.0")
            from gi.repository import Keybinder
            Keybinder.init()
            Keybinder.bind("<Control><Shift>F10", lambda *_: self.stop_recording())
            self.keybinder = Keybinder
        except (ValueError, ImportError) as error:
            LOGGER.info("Atajo alternativo no disponible: %s", error)

    def do_activate(self):
        if self.selector is not None:
            self.selector.present()
            return
        if self.toolbar is not None:
            self.toolbar.show_all()
            self.update_controls()
            self.toolbar.deiconify()
            self.toolbar.present()
            self.place_toolbar()
            return
        root = Gdk.get_default_root_window()
        self.screen_size = (root.get_width(), root.get_height())
        self.toolbar = Gtk.ApplicationWindow(application=self)
        self.toolbar.set_title(APP_NAME)
        self.toolbar.set_decorated(False)
        self.toolbar.set_resizable(False)
        self.toolbar.set_skip_taskbar_hint(False)
        self.toolbar.set_skip_pager_hint(False)
        self.toolbar.set_keep_above(True)
        self.toolbar.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.toolbar.set_accept_focus(True)
        self.toolbar.set_focus_on_map(False)
        self.toolbar.get_style_context().add_class("camera-bar")
        self.toolbar.connect("delete-event", lambda *_: self.request_quit())
        self.toolbar.connect("configure-event", self.configured)
        self.button = Gtk.Button()
        self.button.set_size_request(56, 42)
        self.button.set_relief(Gtk.ReliefStyle.NONE)
        self.button.get_style_context().add_class("camera-button")
        self.button.connect("clicked", self.clicked)
        self.button.connect("button-press-event", self.secondary_click)
        self.button.get_accessible().set_name("Grabar pantalla")
        self.icon = CameraIcon()
        self.button.add(self.icon)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        controls.set_border_width(3)
        controls.pack_start(self.button, False, False, 0)
        self.timer_label = Gtk.Label(label=format_duration(self.last_elapsed))
        self.timer_label.get_style_context().add_class("elapsed-time")
        self.timer_label.set_size_request(68, -1)
        self.timer_label.get_accessible().set_name("Tiempo de grabación")
        controls.pack_start(self.timer_label, False, False, 2)
        self.pause_button = self.icon_button("media-playback-pause-symbolic", "Pausar grabación", self.pause_or_resume)
        self.pause_icon = PlaybackIcon()
        self.pause_button.set_image(self.pause_icon)
        self.pause_icon.show()
        self.pause_button.set_no_show_all(True)
        controls.pack_start(self.pause_button, False, False, 0)
        self.cancel_button = self.icon_button("media-playback-stop-symbolic", "Descartar grabación", self.cancel_recording)
        self.cancel_button.set_no_show_all(True)
        controls.pack_start(self.cancel_button, False, False, 0)
        self.folder_button = self.icon_button("folder-open-symbolic", "Abrir carpeta de grabaciones", self.open_folder)
        controls.pack_start(self.folder_button, False, False, 0)
        self.close_button = self.icon_button("process-stop-symbolic", "Cerrar grabador", self.request_quit)
        controls.pack_start(self.close_button, False, False, 0)
        self.toolbar.add(controls)
        self.toolbar.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                                | Gdk.EventMask.POINTER_MOTION_MASK)
        # Capture sees presses on every child. Claim only after the GTK drag
        # threshold, so buttons keep their ordinary clicks and cancel on drag.
        self.toolbar_drag = Gtk.GestureDrag.new(self.toolbar)
        self.toolbar_drag.set_button(1)
        self.toolbar_drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.toolbar_drag.connect("drag-begin", self.toolbar_drag_begin)
        self.toolbar_drag.connect("drag-update", self.toolbar_drag_update)
        self.toolbar_drag.connect("drag-end", self.toolbar_drag_end)
        self.toolbar_drag.connect("cancel", self.toolbar_drag_cancelled)
        css = Gtk.CssProvider()
        css.load_from_data(b".camera-bar { background: #202124; border: 1px solid #505050; border-radius: 8px; } .camera-button { color: white; background: transparent; background-image: none; border: 0; border-radius: 7px; padding: 5px 8px; box-shadow: none; } .camera-button:hover { background: #383a3d; } .camera-button:disabled { opacity: 0.6; } .elapsed-time { color: white; font: bold 16px monospace; } .elapsed-time.paused { color: #ffcc66; }")
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.toolbar.show_all()
        self.update_controls()
        GLib.idle_add(self.place_toolbar)

    def configured(self, _widget, _event):
        GLib.idle_add(self.place_toolbar)
        return False

    def icon_button(self, icon_name, accessible_name, callback):
        button = Gtk.Button()
        button.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR))
        button.set_always_show_image(True)
        button.get_image().show()
        button.set_size_request(40, 40)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("camera-button")
        button.get_accessible().set_name(accessible_name)
        button.set_tooltip_text(accessible_name)
        button.connect("clicked", lambda *_: self.toolbar_action(callback))
        return button

    def toolbar_action(self, callback):
        if not self.suppress_toolbar_clicks:
            return callback()
        return False

    def toolbar_pointer(self, gesture):
        event = gesture.get_last_event(gesture.get_last_updated_sequence())
        if event is not None:
            valid, root_x, root_y = event.get_root_coords()
            if valid:
                return root_x, root_y
        return None

    def toolbar_drag_begin(self, gesture, _x, _y):
        if self.drag_clear_source is not None:
            GLib.source_remove(self.drag_clear_source)
            self.drag_clear_source = None
        self.suppress_toolbar_clicks = False
        self.toolbar_drag_started = False
        self.toolbar_drag_pointer = self.toolbar_pointer(gesture)
        self.toolbar_drag_origin = tuple(self.toolbar.get_position())

    def toolbar_drag_update(self, gesture, _offset_x, _offset_y):
        pointer = self.toolbar_pointer(gesture)
        start = self.toolbar_drag_pointer
        if pointer is None or start is None or self.toolbar_drag_origin is None:
            return
        if not self.toolbar_drag_started:
            if not self.toolbar.drag_check_threshold(int(start[0]), int(start[1]),
                                                     int(pointer[0]), int(pointer[1])):
                return
            self.toolbar_drag_started = True
            self.suppress_toolbar_clicks = True
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        # Use root coordinates: local gesture offsets change when the toolbar
        # follows the pointer and would otherwise make movement oscillate.
        position = (self.toolbar_drag_origin[0] + pointer[0] - start[0],
                    self.toolbar_drag_origin[1] + pointer[1] - start[1])
        self.manual_toolbar_position = self.clamp_toolbar(position)
        self.toolbar.move(*self.manual_toolbar_position)

    def toolbar_drag_end(self, _gesture, _offset_x, _offset_y):
        if self.toolbar_drag_started:
            self.write_status()
            # Keep release-triggered GtkButton.clicked callbacks suppressed
            # until the complete event dispatch has finished.
            if self.drag_clear_source is None:
                self.drag_clear_source = GLib.idle_add(self.clear_toolbar_drag)
        else:
            self.clear_toolbar_drag()

    def toolbar_drag_cancelled(self, _gesture, _sequence):
        self.toolbar_drag_end(_gesture, 0, 0)

    def clear_toolbar_drag(self):
        self.drag_clear_source = None
        self.toolbar_drag_pointer = None
        self.toolbar_drag_origin = None
        self.toolbar_drag_started = False
        self.suppress_toolbar_clicks = False
        return False

    def clamp_toolbar(self, position):
        # The work area keeps controls above a panel, and screen bounds cover
        # disconnected monitors or a remembered position from another layout.
        monitor = self.toolbar.get_display().get_monitor_at_point(int(position[0]), int(position[1]))
        area = monitor.get_workarea() if monitor is not None else None
        bounds = (area.x, area.y, area.width, area.height) if area is not None else None
        return clamp_toolbar_position(position, self.screen_size, self.toolbar.get_size(), bounds)

    def update_controls(self):
        if self.toolbar is None:
            return
        capturing = self.state in {"starting", "recording", "paused", "stopping"}
        # Recorder-only setting: application tooltips are never captured.
        Gtk.Settings.get_default().set_property("gtk-enable-tooltips", not capturing)
        if capturing:
            for popup in Gtk.Window.list_toplevels():
                if popup.get_type_hint() == Gdk.WindowTypeHint.TOOLTIP:
                    popup.hide()
        self.icon.recording = capturing
        self.icon.queue_draw()
        self.button.set_sensitive(self.state != "stopping")
        self.button.get_accessible().set_name("Detener y guardar grabación" if capturing else "Grabar pantalla")
        self.button.set_tooltip_text("Detener y guardar grabación" if capturing else "Grabar pantalla")
        self.pause_button.set_visible(True)
        self.pause_button.set_sensitive(self.state in {"recording", "paused"})
        paused = self.state == "paused"
        self.pause_icon.paused = paused
        self.pause_icon.queue_draw()
        self.pause_button.get_accessible().set_name("Reanudar grabación" if paused else "Pausar grabación")
        self.pause_button.set_tooltip_text("Reanudar grabación" if paused else "Pausar grabación")
        self.cancel_button.set_visible(True)
        can_discard_saved = self.state == "idle" and self.last_saved is not None and self.last_saved.is_file()
        self.cancel_button.set_sensitive(self.state in {"starting", "recording", "paused"} or can_discard_saved)
        self.close_button.set_sensitive(True)
        style = self.timer_label.get_style_context()
        (style.add_class if paused else style.remove_class)("paused")
        self.update_timer()

    def update_timer(self):
        if self.session is not None:
            self.last_elapsed = self.session.statistics["elapsed_seconds"]
        if self.timer_label is not None:
            value = format_duration(self.last_elapsed)
            if self.timer_label.get_text() != value:
                self.timer_label.set_text(value)
                self.write_status()
        return True

    def open_folder(self):
        try:
            return Gio.AppInfo.launch_default_for_uri(self.output_dir.resolve().as_uri(), None)
        except GLib.Error:
            LOGGER.exception("No se pudo abrir la carpeta de grabaciones")
            return False

    def open_video(self, path):
        try:
            self.preview_open_requested = Gio.AppInfo.launch_default_for_uri(Path(path).resolve().as_uri(), None)
        except GLib.Error:
            self.preview_open_requested = False
            LOGGER.exception("No se pudo abrir el video guardado")
        self.write_status()

    def secondary_click(self, _button, event):
        if event.button == 3:
            self.request_quit()
            return True
        return False

    def place_toolbar(self):
        if self.toolbar is not None:
            if self.manual_toolbar_position is not None:
                position = self.clamp_toolbar(self.manual_toolbar_position)
                self.manual_toolbar_position = position
            else:
                position = toolbar_position(self.screen_size, self.toolbar.get_size(), self.region)
            if tuple(self.toolbar.get_position()) != position:
                self.toolbar.move(*position)
            self.write_status()
        return False

    def write_status(self):
        data = {"pid": os.getpid(), "state": self.state, "region": self.region,
                "last_saved": str(self.last_saved) if self.last_saved else None,
                "output_directory": str(self.output_dir), "elapsed_seconds": self.last_elapsed,
                "elapsed_text": format_duration(self.last_elapsed),
                "preview_open_requested": self.preview_open_requested,
                "toolbar_manual_position": self.manual_toolbar_position}
        if self.toolbar is not None:
            data["toolbar"] = {"visible": self.toolbar.get_visible(), "position": list(self.toolbar.get_position()), "size": list(self.toolbar.get_size()),
                "taskbar_visible": not self.toolbar.get_skip_taskbar_hint(),
                "window_type": self.toolbar.get_type_hint().value_nick,
                "pause_visible": bool(self.pause_button and self.pause_button.get_visible()),
                "pause_enabled": bool(self.pause_button and self.pause_button.get_sensitive()),
                "pause_action": "resume" if self.state == "paused" else "pause",
                "cancel_visible": bool(self.cancel_button and self.cancel_button.get_visible()),
                "cancel_enabled": bool(self.cancel_button and self.cancel_button.get_sensitive()),
                "close_visible": bool(self.close_button and self.close_button.get_visible())}
        temporary = self.state_dir / "estado.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.state_dir / "estado.json")

    def change_state(self, state):
        self.state = state
        self.update_controls()
        self.write_status()

    def clicked(self, _button):
        if self.suppress_toolbar_clicks:
            return
        if self.state == "idle":
            self.begin_selection()
        elif self.state in {"starting", "recording", "paused"}:
            self.stop_recording()

    def begin_selection(self):
        self.change_state("selecting")
        self.toolbar.hide()
        self.selector = RegionSelector(self.screen_size, self.selected_region, self.cancel_selection)
        self.selector.show_all()
        self.selector.present()
        self.selector.grab_focus()
        self.write_status()

    def cancel_selection(self):
        self.selector = None
        self.region = None
        if self.closing:
            self.quit()
        else:
            self.change_state("idle")
            self.toolbar.show_all()
            self.update_controls()
            self.place_toolbar()

    def selected_region(self, region):
        self.selector = None
        self.region = region
        self.last_elapsed = 0.0
        self.preview_open_requested = False
        self.change_state("starting")
        self.toolbar.show_all()
        self.place_toolbar()
        Gdk.Display.get_default().sync()
        self.start_source = GLib.timeout_add(120, self.start_capture)

    def start_capture(self):
        self.start_source = None
        if self.closing or self.state != "starting":
            return False
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]
        destination = self.output_dir / f"Grabación {stamp}.mp4"
        try:
            self.session = CaptureSession(self.region, destination,
                on_complete=self.completed, on_error=self.failed, on_started=self.started,
                on_cancelled=self.cancelled, excluded_pids=(os.getpid(),))
            self.session.start()
        except Exception as error:
            LOGGER.exception("No se pudo iniciar la grabación")
            self.failed(str(error))
        return False

    def started(self):
        if self.state != "stopping":
            self.change_state("recording")

    def stop_recording(self):
        if self.start_source is not None:
            GLib.source_remove(self.start_source)
            self.start_source = None
            self.completed(None)
        elif self.session is not None and self.session.active:
            self.change_state("stopping")
            self.session.stop()
        return False

    def pause_or_resume(self):
        if self.session is None:
            return
        session = self.session
        try:
            if self.state == "recording":
                session.pause()
            elif self.state == "paused":
                session.resume()
            else:
                return
        except Exception as error:
            LOGGER.exception("No se pudo cambiar la pausa")
            if self.session is session:
                self.failed(str(error))
            return
        if self.session is session and session.active:
            self.change_state("paused" if session.paused else "recording")

    def cancel_recording(self):
        if self.start_source is not None:
            GLib.source_remove(self.start_source)
            self.start_source = None
            self.cancelled()
        elif self.session is not None and self.session.active:
            self.change_state("stopping")
            self.session.cancel()
        elif self.state == "idle" and self.last_saved is not None:
            # The square also discards the completed video currently available
            # from this recorder. Never enumerate or remove other recordings.
            path = self.last_saved
            if path.is_file():
                try:
                    if not Gio.File.new_for_path(str(path)).trash(None):
                        raise OSError("La Papelera no confirmó la operación.")
                except (GLib.Error, OSError):
                    LOGGER.exception("No se pudo descartar la última grabación: %s", path)
                    return False
            self.last_saved = None
            self.cancelled()
        return False

    def cancelled(self):
        self.session = None
        self.region = None
        self.last_elapsed = 0.0
        self.preview_open_requested = False
        if self.closing:
            self.quit()
        else:
            self.change_state("idle")
            self.place_toolbar()

    def completed(self, path):
        self.update_timer()
        if path is not None:
            self.last_saved = Path(path)
        self.session = None
        self.region = None
        if self.closing:
            self.quit()
        else:
            self.change_state("idle")
            self.place_toolbar()
            if path is not None:
                self.open_video(path)

    def failed(self, message):
        LOGGER.error("Grabación interrumpida: %s", message)
        self.completed(None)

    def request_quit(self):
        self.closing = True
        if self.toolbar is not None:
            self.toolbar.hide()
        if self.selector is not None:
            self.selector.cancel()
        elif self.start_source is not None or (self.session is not None and self.session.active):
            self.stop_recording()
        else:
            self.quit()
        return True

    def do_shutdown(self):
        if self.drag_clear_source is not None:
            GLib.source_remove(self.drag_clear_source)
            self.drag_clear_source = None
        if self.timer_source is not None:
            GLib.source_remove(self.timer_source)
            self.timer_source = None
        if self.keybinder is not None:
            self.keybinder.unbind("<Control><Shift>F10")
        self.state = "closed"
        self.write_status()
        Gtk.Application.do_shutdown(self)


if __name__ == "__main__":
    GLib.set_prgname("grabador-pantalla")
    Gdk.set_program_class("GrabadorPantalla")
    GLib.set_application_name(APP_NAME)
    app = RecorderApp()
    raise SystemExit(app.run(sys.argv))
