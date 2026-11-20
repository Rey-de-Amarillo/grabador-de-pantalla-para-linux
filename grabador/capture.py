"""Small asynchronous X11 screen-and-audio recorder for the native toolbar.

The caller runs a GLib main loop. All public callbacks run on that loop.
No desktop, microphone-volume, audio-routing, or service settings are changed.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import time
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib, Gst


Gst.init(None)
LOGGER = logging.getLogger("grabador.capture")


class CaptureError(RuntimeError):
    """A capture could not be started or finalized safely."""


def _default_device(kind: str) -> str:
    result = subprocess.run(
        ["pactl", f"get-default-{kind}"], capture_output=True, text=True,
        timeout=5, check=False,
    )
    device = result.stdout.strip()
    if result.returncode or not device or "\n" in device:
        raise CaptureError(f"No se pudo identificar el dispositivo de audio: {kind}.")
    return device


def normalize_region(region: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Preserve the top-left corner and round dimensions down for H.264."""
    if len(region) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in region):
        raise CaptureError("La región debe contener cuatro coordenadas enteras.")
    x, y, width, height = region
    width -= width % 2
    height -= height % 2
    if x < 0 or y < 0 or width < 16 or height < 16:
        raise CaptureError("El área de grabación no es válida.")
    return x, y, width, height


class CaptureSession:
    """Record a region and the current desktop/microphone into one MP4.

    start() raises for synchronous validation errors. Device or encoding errors
    discovered later call on_error(str). stop() is asynchronous: on_complete
    receives the final Path only after both audio/video and the MP4 index flush.
    An incomplete recording is preserved as ``*.part.mp4`` after an error.
    """

    def __init__(
        self,
        region: tuple[int, int, int, int],
        output_path: str | Path,
        on_complete: Callable[[Path], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_started: Callable[[], None] | None = None,
        fps: int = 30,
        on_cancelled: Callable[[], None] | None = None,
        excluded_xids=(),
        excluded_pids=(),
    ) -> None:
        self.region = normalize_region(region)
        if fps not in (15, 24, 25, 30, 60):
            raise CaptureError("La frecuencia de grabación no es válida.")
        self.fps = fps
        self.final_path = Path(output_path).expanduser().absolute()
        self.partial_path = self.final_path.with_name(self.final_path.stem + ".part.mp4")
        self.on_complete = on_complete
        self.on_error = on_error
        self.on_started = on_started
        self.on_cancelled = on_cancelled
        self.excluded_xids = tuple(excluded_xids)
        self.excluded_pids = tuple(excluded_pids)
        self._exclusion = None
        self._pixel_filter = None
        self.pipeline: Gst.Pipeline | None = None
        self.audio_sources: dict[str, str] = {}
        self.audio_buffer_counts = {"desktop": 0, "microphone": 0}
        self.video_buffer_count = 0
        self._state = "new"
        self._bus = None
        self._bus_handler = None
        self._start_timeout = None
        self._stop_timeout = None
        self._stop_requested = False
        self._eos_sent = False
        self._owns_partial = False
        self._started_at = None
        self._ended_at = None
        self._paused_at = None
        self._paused_total = 0.0

    @property
    def active(self) -> bool:
        return self._state in {"starting", "recording", "paused", "stopping"}

    @property
    def paused(self) -> bool:
        return self._state == "paused"

    @property
    def stopping(self) -> bool:
        return self._state == "stopping" or (
            self._state == "starting" and self._stop_requested
        )

    @property
    def state(self) -> str:
        return self._state

    @property
    def statistics(self) -> dict:
        current = self._ended_at if self._ended_at is not None else time.monotonic()
        paused_seconds = self._paused_total
        if self._paused_at is not None:
            paused_seconds += max(0.0, current - self._paused_at)
        elapsed = (
            max(0.0, current - self._started_at - paused_seconds)
            if self._started_at is not None else 0.0
        )
        result = {
            "state": self._state,
            "region": self.region,
            "fps": self.fps,
            "video_buffers": self.video_buffer_count,
            "audio_buffers": dict(self.audio_buffer_counts),
            "audio_sources": dict(self.audio_sources),
            "elapsed_seconds": round(elapsed, 3),
            "paused_seconds": round(paused_seconds, 3),
        }
        if self._exclusion is not None:
            result["control_exclusion"] = self._exclusion.statistics
        return result

    def _make(self, factory: str, name: str, **properties):
        element = Gst.ElementFactory.make(factory, name)
        if element is None:
            raise CaptureError(f"Falta el componente de grabación {factory}.")
        for key, value in properties.items():
            element.set_property(key.replace("_", "-"), value)
        self.pipeline.add(element)
        return element

    @staticmethod
    def _link(*elements) -> None:
        for previous, following in zip(elements, elements[1:]):
            if not previous.link(following):
                raise CaptureError(
                    f"No se pudo conectar {previous.get_name()} con {following.get_name()}."
                )

    def _queue(self, name: str):
        return self._make("queue", name, max_size_time=2 * Gst.SECOND,
                          max_size_bytes=0, max_size_buffers=0)

    def _count_video(self, _pad, _info):
        self.video_buffer_count += 1
        return Gst.PadProbeReturn.OK

    def _count_audio(self, _pad, _info, label: str):
        self.audio_buffer_counts[label] += 1
        return Gst.PadProbeReturn.OK

    def _build(self) -> None:
        self.pipeline = Gst.Pipeline.new("grabador-pantalla")
        if self.pipeline is None:
            raise CaptureError("No se pudo preparar la grabación.")
        x, y, width, height = self.region
        screen = self._make(
            "ximagesrc", "pantalla", startx=x, starty=y,
            endx=x + width - 1, endy=y + height - 1,
            use_damage=False, show_pointer=True, do_timestamp=True,
        )
        screen.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._count_video)
        video_queue = self._queue("cola-pantalla")
        video_rate = self._make("videorate", "frecuencia", max_rate=self.fps)
        video_convert = self._make("videoconvert", "convertir-video")
        video_caps = self._make("capsfilter", "formato-video", caps=Gst.Caps.from_string(
            f"video/x-raw,format=I420,width={width},height={height},framerate={self.fps}/1"
        ))
        video_encoder = self._make(
            "x264enc", "h264", speed_preset="veryfast", quantizer=20,
            key_int_max=self.fps * 2, threads=max(1, min((os.cpu_count() or 2) - 1, 4)),
            tune="zerolatency",
        )
        video_encoder.set_property("pass", "qual")
        video_parser = self._make("h264parse", "indice-video")
        encoded_video = self._queue("cola-video-codificado")
        muxer = self._make("mp4mux", "mp4", faststart=True)
        file_sink = self._make("filesink", "archivo", location=str(self.partial_path))
        if self.excluded_xids or self.excluded_pids:
            from exclusion import WindowExclusion
            from pixel_filter import PixelPatchFilter
            packed_caps = self._make("capsfilter", "formato-exclusion", caps=Gst.Caps.from_string(
                f"video/x-raw,format=BGRx,width={width},height={height},framerate={self.fps}/1"
            ))
            self._exclusion = WindowExclusion(self.excluded_xids, self.excluded_pids)
            self._pixel_filter = PixelPatchFilter(self._exclusion, origin=(x, y))
            self.pipeline.add(self._pixel_filter)
            encode_convert = self._make("videoconvert", "convertir-video-codificacion")
            self._link(screen, video_queue, video_rate, video_convert, packed_caps,
                       self._pixel_filter, encode_convert, video_caps, video_encoder,
                       video_parser, encoded_video, muxer, file_sink)
        else:
            self._link(screen, video_queue, video_rate, video_convert, video_caps,
                       video_encoder, video_parser, encoded_video, muxer, file_sink)

        # Both PulseAudio branches are normalized below to identical PCM.
        # adder mixes received samples without generating clock-driven silence
        # while the surrounding live pipeline is paused.
        mixer = self._make("adder", "mezcla")
        convert = self._make("audioconvert", "convertir-mezcla")
        resample = self._make("audioresample", "frecuencia-audio")
        caps = self._make("capsfilter", "formato-mezcla", caps=Gst.Caps.from_string(
            "audio/x-raw,format=F32LE,rate=48000,channels=2"
        ))
        encoder = self._make("avenc_aac", "aac", bitrate=192000)
        parser = self._make("aacparse", "indice-audio")
        encoded_audio = self._queue("cola-audio-codificado")
        self._link(mixer, convert, resample, caps, encoder, parser, encoded_audio, muxer)

        for label, device in self.audio_sources.items():
            source = self._make(
                "pulsesrc", label, device=device, do_timestamp=True,
                provide_clock=False, buffer_time=200000, latency_time=10000,
            )
            source.get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, self._count_audio, label,
            )
            queue = self._queue(f"cola-{label}")
            convert = self._make("audioconvert", f"convertir-{label}")
            resample = self._make("audioresample", f"frecuencia-{label}")
            caps = self._make("capsfilter", f"formato-{label}", caps=Gst.Caps.from_string(
                "audio/x-raw,format=F32LE,rate=48000,channels=2"
            ))
            self._link(source, queue, convert, resample, caps, mixer)

        self._bus = self.pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect("message", self._message)

    def start(self) -> None:
        if self._state != "new":
            raise CaptureError("Esta sesión de grabación ya se inició.")
        if self.final_path.suffix.lower() != ".mp4":
            raise CaptureError("El archivo de grabación debe terminar en .mp4.")
        if not self.final_path.parent.is_dir():
            raise CaptureError("La carpeta de grabaciones no está disponible.")
        if self.final_path.exists() or self.partial_path.exists():
            raise CaptureError("Ya existe un archivo con ese nombre.")
        self.audio_sources = {
            "desktop": _default_device("sink") + ".monitor",
            "microphone": _default_device("source"),
        }
        self._state = "starting"
        try:
            self._build()
            # Reserve only a new partial file: cancelling a never-started
            # session must never trash somebody else's existing recording.
            with self.partial_path.open("xb"):
                pass
            self._owns_partial = True
            LOGGER.info("Iniciando captura: region=%s fuentes=%s archivo=%s",
                        self.region, self.audio_sources, self.partial_path)
            result = self.pipeline.set_state(Gst.State.PLAYING)
            if result == Gst.StateChangeReturn.FAILURE:
                raise CaptureError("No se pudo iniciar la captura de pantalla y audio.")
            self._start_timeout = GLib.timeout_add_seconds(15, self._startup_expired)
        except Exception:
            self._state = "error"
            self._end_clock()
            self._teardown()
            raise

    def pause(self) -> None:
        """Pause the live pipeline; its running-time clock also pauses."""
        if self._state != "recording":
            return
        self._state = "paused"
        self._paused_at = time.monotonic()
        LOGGER.info("Captura pausada: %s", self.final_path)
        if self.pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self._fail("No se pudo pausar la grabación.")

    def resume(self) -> None:
        """Resume without introducing the paused wall-time into the MP4."""
        if self._state != "paused":
            return
        self._resume_clock()
        self._state = "recording"
        LOGGER.info("Captura reanudada: %s", self.final_path)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._fail("No se pudo reanudar la grabación.")

    def stop(self) -> None:
        if self._state == "starting":
            self._stop_requested = True
            return
        if self._state not in {"recording", "paused"}:
            return
        was_paused = self._state == "paused"
        self._resume_clock()
        self._end_clock()
        self._state = "stopping"
        self._stop_requested = True
        self._stop_timeout = GLib.timeout_add_seconds(20, self._stop_expired)
        LOGGER.info("Finalizando captura: %s", self.partial_path)
        # Queue EOS while still paused, then let sinks drain in PLAYING.
        # Waiting for PLAYING first can deadlock a faststart MP4 sink's preroll.
        self._send_eos()
        if was_paused and self._state == "stopping":
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                self._fail("No se pudo finalizar la grabación pausada.")

    def _send_eos(self) -> None:
        if self._eos_sent or self._state != "stopping":
            return
        self._eos_sent = True
        if not self.pipeline.send_event(Gst.Event.new_eos()):
            self._fail("No se pudo finalizar el archivo de grabación.")

    def cancel(self) -> None:
        """Discard only this unfinished capture into the recoverable Trash."""
        if self._state in {"complete", "cancelled", "error"}:
            return
        self._state = "cancelling"
        self._stop_requested = False
        self._end_clock()
        self._teardown()
        if self._owns_partial and self.partial_path.exists():
            try:
                if not Gio.File.new_for_path(str(self.partial_path)).trash(None):
                    raise CaptureError("La Papelera no confirmó la operación.")
            except (GLib.Error, OSError, CaptureError) as error:
                LOGGER.exception("No se pudo llevar el parcial a Papelera")
                self._fail(
                    "No se pudo descartar la grabación; se conservó el archivo parcial: "
                    + str(error)
                )
                return
        self._state = "cancelled"
        LOGGER.info("Captura cancelada: %s", self.final_path)
        self._callback(self.on_cancelled)

    def _resume_clock(self) -> None:
        if self._paused_at is not None:
            self._paused_total += max(0.0, time.monotonic() - self._paused_at)
            self._paused_at = None

    def _end_clock(self) -> None:
        if self._ended_at is None:
            self._ended_at = time.monotonic()
        if self._paused_at is not None:
            self._paused_total += max(0.0, self._ended_at - self._paused_at)
            self._paused_at = None

    def _startup_expired(self):
        self._start_timeout = None
        self._fail("La captura no respondió al iniciar.")
        return GLib.SOURCE_REMOVE

    def _stop_expired(self):
        self._stop_timeout = None
        self._fail("La captura no respondió al finalizar; se conservó el archivo parcial.")
        return GLib.SOURCE_REMOVE

    def _message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            LOGGER.error("Error de captura: %s; %s", error, debug)
            self._fail(str(error))
        elif message.type == Gst.MessageType.WARNING:
            warning, debug = message.parse_warning()
            LOGGER.warning("Aviso de captura: %s; %s", warning, debug)
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            _old, new, _pending = message.parse_state_changed()
            if new == Gst.State.PLAYING and self._state == "starting":
                self._cancel_timeout("_start_timeout")
                self._state = "recording"
                self._started_at = time.monotonic()
                LOGGER.info("Captura activa: %s", self.final_path)
                self._callback(self.on_started)
                if self._stop_requested:
                    self.stop()
        elif message.type == Gst.MessageType.EOS:
            self._finish()

    def _finish(self) -> None:
        if self._state not in {"starting", "recording", "paused", "stopping"}:
            return
        self._end_clock()
        self._teardown()
        try:
            if not self.partial_path.is_file() or self.partial_path.stat().st_size == 0:
                raise CaptureError("La grabación no produjo un archivo válido.")
            if self.final_path.exists():
                raise CaptureError("El destino ya existe; se conservó el archivo parcial.")
            # Hard-link creation refuses an existing destination atomically.
            # Removing our temporary name leaves the finalized video intact.
            os.link(self.partial_path, self.final_path)
            self.partial_path.unlink()
        except OSError as error:
            self._fail(f"No se pudo guardar la grabación: {error}")
            return
        except CaptureError as error:
            self._fail(str(error))
            return
        self._state = "complete"
        LOGGER.info("Grabación guardada: %s; %s", self.final_path, self.statistics)
        self._callback(self.on_complete, self.final_path)

    def _fail(self, message: str) -> None:
        if self._state in {"error", "complete", "cancelled"}:
            return
        self._state = "error"
        self._end_clock()
        self._teardown()
        LOGGER.error("Grabación interrumpida: %s", message)
        self._callback(self.on_error, message)

    @staticmethod
    def _callback(callback, *args) -> None:
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                LOGGER.exception("Error en callback de grabación")

    def _cancel_timeout(self, attribute: str) -> None:
        source = getattr(self, attribute)
        if source is not None:
            GLib.source_remove(source)
            setattr(self, attribute, None)

    def _teardown(self) -> None:
        self._cancel_timeout("_start_timeout")
        self._cancel_timeout("_stop_timeout")
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        if self._exclusion is not None:
            self._exclusion.close()
        if self._bus is not None:
            if self._bus_handler is not None:
                self._bus.disconnect(self._bus_handler)
                self._bus_handler = None
            self._bus.remove_signal_watch()
            self._bus = None

