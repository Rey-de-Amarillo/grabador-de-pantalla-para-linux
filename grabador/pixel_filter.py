"""Replace recorder-control pixels with live X11 underlay patches.

This is a regular Gst.Element with explicit pads. It works with the installed
GI bindings and does not need the optional gst-python plug-in overrides.
Buffers are passed through when no patch intersects the frame. Otherwise a
native deep copy preserves timestamps/metadata, and Gst.Buffer.fill() changes
only the affected rows. No appsink/appsrc bridge or timestamp reconstruction
is involved.

The provider runs on GStreamer's streaming thread: use its own X11 connection,
not GTK/GDK window methods. PixelPatch coordinates are frame-relative; the
FrameLayout origin identifies the corresponding root-window position.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable, Iterable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo


Gst.init(None)
LOGGER = logging.getLogger("grabador.pixel_filter")
SUPPORTED_FORMATS = frozenset({"BGRx", "BGRA"})


@dataclass(frozen=True)
class FrameLayout:
    width: int
    height: int
    stride: int
    format: str
    origin_x: int = 0
    origin_y: int = 0
    offset: int = 0


@dataclass(frozen=True)
class PixelPatch:
    x: int
    y: int
    width: int
    height: int
    data: bytes
    stride: int | None = None

    @property
    def row_stride(self) -> int:
        return self.width * 4 if self.stride is None else self.stride

    def validate(self) -> None:
        coordinates = (self.x, self.y, self.width, self.height)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in coordinates):
            raise ValueError("El parche requiere coordenadas enteras.")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("El parche no tiene dimensiones válidas.")
        if self.row_stride < self.width * 4:
            raise ValueError("La fila del parche es más pequeña que su ancho.")
        required = (self.height - 1) * self.row_stride + self.width * 4
        if len(self.data) < required:
            raise ValueError("El parche no contiene todos sus píxeles.")


PatchProvider = Callable[[FrameLayout], Iterable[PixelPatch]]


class PixelPatchFilter(Gst.Element):
    """Same-format filter for packed BGRx/BGRA frames.

    Add this object directly to the pipeline, between packed-BGRx caps and the
    existing encoder's videoconvert/I420 caps. The downstream/upstream events
    and queries remain in the same pipeline, including EOS, flush and segment.
    A provider failure returns ERROR rather than leaking the original overlay.
    """

    __gtype_name__ = "GrabadorPixelPatchFilter"

    def __init__(self, provider: PatchProvider, origin=(0, 0), name="ocultar-controles"):
        super().__init__()
        self.set_name(name)
        self.provider = provider
        self.origin = tuple(origin)
        if len(self.origin) != 2:
            raise ValueError("El origen de captura requiere dos coordenadas.")
        self.layout: FrameLayout | None = None
        self.last_error: str | None = None
        self.frame_count = 0
        self.patched_frames = 0
        self.patched_pixels = 0
        self.sinkpad = Gst.Pad.new("sink", Gst.PadDirection.SINK)
        self.srcpad = Gst.Pad.new("src", Gst.PadDirection.SRC)
        self.sinkpad.set_chain_function_full(self._chain)
        self.sinkpad.set_event_function_full(self._downstream_event)
        self.srcpad.set_event_function_full(self._upstream_event)
        self.sinkpad.set_query_function_full(self._downstream_query)
        self.srcpad.set_query_function_full(self._upstream_query)
        self.add_pad(self.sinkpad)
        self.add_pad(self.srcpad)

    def _downstream_event(self, _pad, _parent, event):
        if event.type == Gst.EventType.CAPS:
            try:
                info = GstVideo.VideoInfo.new_from_caps(event.parse_caps())
                if info is None or info.finfo.name not in SUPPORTED_FORMATS:
                    raise ValueError("El filtro necesita video BGRx o BGRA antes de codificar.")
                self.layout = FrameLayout(info.width, info.height, info.stride[0],
                                          info.finfo.name, *self.origin, info.offset[0])
            except Exception as error:
                self.last_error = str(error)
                LOGGER.exception("No se pudo preparar el filtro de controles")
                return False
        return self.srcpad.push_event(event)

    def _upstream_event(self, _pad, _parent, event):
        return self.sinkpad.push_event(event)

    def _downstream_query(self, _pad, _parent, query):
        return self.srcpad.peer_query(query)

    def _upstream_query(self, _pad, _parent, query):
        return self.sinkpad.peer_query(query)

    def _buffer_layout(self, buffer) -> FrameLayout:
        if self.layout is None:
            raise ValueError("El fotograma llegó sin un formato negociado.")
        layout = self.layout
        metadata = GstVideo.buffer_get_video_meta(buffer)
        if metadata is not None:
            layout = FrameLayout(layout.width, layout.height, metadata.stride[0],
                                 layout.format, *self.origin, metadata.offset[0])
        if layout.stride < layout.width * 4 or layout.stride <= 0:
            raise ValueError("El fotograma tiene una disposición de píxeles no compatible.")
        required = layout.offset + (layout.height - 1) * layout.stride + layout.width * 4
        if buffer.get_size() < required:
            raise ValueError("El fotograma está incompleto.")
        return layout

    def _chain(self, _pad, _parent, buffer):
        self.frame_count += 1
        try:
            layout = self._buffer_layout(buffer)
            clipped = []
            for patch in self.provider(layout):
                if not isinstance(patch, PixelPatch):
                    raise TypeError("El proveedor devolvió un parche no válido.")
                patch.validate()
                left, top = max(0, patch.x), max(0, patch.y)
                right = min(layout.width, patch.x + patch.width)
                bottom = min(layout.height, patch.y + patch.height)
                if right > left and bottom > top:
                    clipped.append((patch, left, top, right, bottom))
            if not clipped:
                return self.srcpad.push(buffer)
            output = buffer.copy_deep()
            if output is None:
                raise RuntimeError("No se pudo copiar el fotograma para excluir los controles.")
            changed = 0
            for patch, left, top, right, bottom in clipped:
                row_size = (right - left) * 4
                source_x = (left - patch.x) * 4
                for y in range(top, bottom):
                    source_offset = (y - patch.y) * patch.row_stride + source_x
                    target_offset = layout.offset + y * layout.stride + left * 4
                    row = patch.data[source_offset:source_offset + row_size]
                    if output.fill(target_offset, row) != row_size:
                        raise RuntimeError("No se pudo sustituir una fila del control.")
                changed += (right - left) * (bottom - top)
            self.patched_frames += 1
            self.patched_pixels += changed
            return self.srcpad.push(output)
        except Exception as error:
            self.last_error = str(error)
            LOGGER.exception("No se pudo excluir el control del fotograma")
            return Gst.FlowReturn.ERROR


def self_test():
    """Real non-desktop pipeline: verify pixels, clipping, PTS and EOS."""
    import json
    import time
    from unittest.mock import patch as mock_patch

    results = {}

    def run(provider, frames=3):
        pipeline = Gst.Pipeline.new(None)
        source = Gst.ElementFactory.make("videotestsrc")
        source.set_property("num-buffers", frames)
        source.set_property("pattern", "black")
        caps = Gst.ElementFactory.make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(
            "video/x-raw,format=BGRx,width=32,height=18,framerate=30/1"))
        filter_element = PixelPatchFilter(provider, origin=(100, 200))
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("sync", False)
        for element in (source, caps, filter_element, sink):
            pipeline.add(element)
        assert source.link(caps) and caps.link(filter_element) and filter_element.link(sink)
        assert pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        samples = []
        for _ in range(frames):
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            assert sample is not None, filter_element.last_error
            data = sample.get_buffer()
            samples.append((data.extract_dup(0, data.get_size()), data.pts, data.duration))
        bus_event = pipeline.get_bus().timed_pop_filtered(
            Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        assert bus_event is not None and bus_event.type == Gst.MessageType.EOS
        pipeline.set_state(Gst.State.NULL)
        return samples, filter_element

    observed_layouts = []
    red = b"\x00\x00\xff\xff"

    def patches(layout):
        observed_layouts.append(layout)
        return [PixelPatch(-2, -1, 5, 4, red * 20)]

    samples, filtered = run(patches)
    for frame, pts, duration in samples:
        for y in range(18):
            for x in range(32):
                pixel = frame[(y * 32 + x) * 4:(y * 32 + x + 1) * 4]
                assert pixel == (red if x < 3 and y < 3 else b"\x00\x00\x00\xff")
    assert [item[1] for item in samples] == [0, Gst.SECOND // 30, 2 * Gst.SECOND // 30]
    assert all(abs(item[2] - Gst.SECOND / 30) < 2 for item in samples)
    assert filtered.patched_frames == 3 and filtered.patched_pixels == 27
    assert observed_layouts[0].origin_x == 100 and observed_layouts[0].origin_y == 200
    results["clipping_and_outside_pixels"] = True
    results["timestamps_duration_and_eos_preserved"] = True
    results["global_origin_available_to_provider"] = True

    unpatched, passed = run(lambda layout: [PixelPatch(100, 100, 1, 1, red)])
    assert passed.patched_frames == 0
    assert all(frame == b"\x00\x00\x00\xff" * (32 * 18) for frame, _pts, _duration in unpatched)
    results["passthrough_when_no_intersection"] = True

    def make_live(provider):
        pipeline = Gst.Pipeline.new(None)
        source = Gst.ElementFactory.make("videotestsrc")
        source.set_property("is-live", True)
        source.set_property("pattern", "black")
        caps = Gst.ElementFactory.make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(
            "video/x-raw,format=BGRx,width=32,height=18,framerate=30/1"))
        transform = PixelPatchFilter(provider)
        sink = Gst.ElementFactory.make("appsink")
        sink.set_property("sync", False)
        for element in (source, caps, transform, sink):
            pipeline.add(element)
        assert source.link(caps) and caps.link(transform) and transform.link(sink)
        return pipeline, transform, sink

    pipeline, transform, sink = make_live(lambda layout: [PixelPatch(0, 0, 1, 1, red)])
    pipeline.set_state(Gst.State.PLAYING)
    first = sink.emit("try-pull-sample", Gst.SECOND)
    second = sink.emit("try-pull-sample", Gst.SECOND)
    assert first is not None and second is not None
    pipeline.set_state(Gst.State.PAUSED)
    pipeline.get_state(Gst.SECOND)
    previous = second.get_buffer().pts
    while True:
        sample = sink.emit("try-pull-sample", 0)
        if sample is None:
            break
        previous = sample.get_buffer().pts
    count = transform.frame_count
    time.sleep(0.12)
    assert transform.frame_count == count
    pipeline.set_state(Gst.State.PLAYING)
    resumed = sink.emit("try-pull-sample", Gst.SECOND)
    assert resumed is not None and resumed.get_buffer().pts > previous
    assert resumed.get_buffer().pts - previous < 100 * Gst.MSECOND
    assert pipeline.send_event(Gst.Event.new_eos())
    while sink.emit("try-pull-sample", 10 * Gst.MSECOND) is not None:
        pass
    event = pipeline.get_bus().timed_pop_filtered(
        Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    assert event is not None and event.type == Gst.MessageType.EOS
    pipeline.set_state(Gst.State.NULL)
    results["live_pause_resume_and_eos_preserved"] = True

    def fail_provider(_layout):
        raise RuntimeError("underlay unavailable in deliberate failure test")

    pipeline, transform, sink = make_live(fail_provider)
    with mock_patch.object(LOGGER, "exception") as reported:
        pipeline.set_state(Gst.State.PLAYING)
        event = pipeline.get_bus().timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.ERROR)
        assert event is not None
        assert sink.emit("try-pull-sample", 0) is None
        assert transform.last_error == "underlay unavailable in deliberate failure test"
        assert reported.called
        pipeline.set_state(Gst.State.NULL)
    results["provider_failure_does_not_leak_original_frame"] = True
    results["ok"] = True
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args()
    if arguments.self_test:
        self_test()
