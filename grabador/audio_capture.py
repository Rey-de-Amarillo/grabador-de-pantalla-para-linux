"""Asynchronous microphone capture using native GStreamer and PulseAudio.

The caller runs a GLib main loop. No capture starts during construction: start()
opens the selected input. stop() sends EOS, then on_ready receives a complete
WAV after its header has been finalized. save() publishes it atomically without
replacing an existing file. Only this session's private temporary files are
removed by cancel(), errors, or a successful save().
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Callable
import warnings
import wave

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

if __package__:
    from .voice_filter import process_wave
else:
    from voice_filter import process_wave

# CPython up to 3.12 provides fast, exact 24-bit PCM helpers. Python 3.13+
# uses the bounded-memory fallback below and needs no extra dependency.
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop as _audioop
except ImportError:
    _audioop = None

Gst.init(None)
LOGGER = logging.getLogger("grabador.capture")
SAMPLE_RATE = 48_000
SAMPLE_WIDTH = 3
CHANNELS = 1
NORMALIZATION_TARGET = 10 ** (-1 / 20)
NORMALIZATION_MAX_GAIN = 10 ** (18 / 20)
NORMALIZATION_MIN_PEAK = 10 ** (-60 / 20)
VOICE_NORMALIZATION_TARGET = 10 ** (-3 / 20)
PCM24_SCALE = 1 << 23
REQUIRED_PLUGINS = (
    "pulsesrc", "audioconvert", "audioresample", "capsfilter", "level",
    "wavenc", "filesink",
)


class CaptureError(RuntimeError):
    """A recording could not be started, finalized, or saved safely."""


def missing_plugins() -> list[str]:
    """Return names of unavailable components without opening an audio input."""
    return [name for name in REQUIRED_PLUGINS if Gst.ElementFactory.find(name) is None]


def _pcm24_peak(data: bytes) -> int:
    if _audioop is not None:
        return _audioop.max(data, SAMPLE_WIDTH)
    peak = 0
    for offset in range(0, len(data), SAMPLE_WIDTH):
        sample = int.from_bytes(data[offset:offset + SAMPLE_WIDTH], "little", signed=True)
        peak = max(peak, abs(sample))
    return peak


def _pcm24_amplify(data: bytes, gain: float) -> bytes:
    if _audioop is not None:
        return _audioop.mul(data, SAMPLE_WIDTH, gain)
    result = bytearray(len(data))
    for offset in range(0, len(data), SAMPLE_WIDTH):
        sample = int.from_bytes(data[offset:offset + SAMPLE_WIDTH], "little", signed=True)
        sample = min(PCM24_SCALE - 1, max(-PCM24_SCALE, round(sample * gain)))
        result[offset:offset + SAMPLE_WIDTH] = sample.to_bytes(SAMPLE_WIDTH, "little", signed=True)
    return bytes(result)


class AudioSession:
    """One 48 kHz, mono, 24-bit PCM WAV recording.

    Construct a new session for each recording. Call start/pause/resume/stop/cancel/save from
    the GLib main thread. Callback signatures are on_started(), on_ready(Path),
    on_error(str), on_level(float) with a 0..1 peak, and on_cancelled().
    Set normalize_peak=True to raise low levels toward -1 dBFS after EOS,
    limited to +18 dB. Already loud audio and near-silence below -60 dBFS stay
    unchanged. Processing runs off the GLib thread; on_ready receives its final
    result. A processing failure preserves the valid original WAV.

    Set clean_voice=True to suppress background noise and isolated mouse
    transients before normalizing. Cleaned voice targets -3 dBFS; gain requires
    evidence of sustained speech. Noise-only audio and unavailable cleanup
    remain unamplified. Global device volume and routing are never changed.

    A synchronous start/save validation error raises CaptureError; device and
    encoding errors discovered later invoke on_error. An existing destination
    is never overwritten. A failed save leaves the ready recording available.

    source_factory='audiotestsrc' is an explicit test facility; production code
    should leave it at its default 'pulsesrc'. It never silently substitutes a
    test source for a missing microphone. Temporary files use a private directory under the system's temporary
    directory; callers can supply a writable temp_dir instead.
    """

    def __init__(
        self,
        device: str | None = None,
        *,
        on_started: Callable[[], None] | None = None,
        on_ready: Callable[[Path], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_level: Callable[[float], None] | None = None,
        on_cancelled: Callable[[], None] | None = None,
        temp_dir: str | Path | None = None,
        source_factory: str = "pulsesrc",
        normalize_peak: bool = False,
        clean_voice: bool = False,
    ) -> None:
        if source_factory not in {"pulsesrc", "audiotestsrc"}:
            raise CaptureError("La fuente de audio no es compatible.")
        if device is not None and (not isinstance(device, str) or not device.strip()):
            raise CaptureError("El micrófono seleccionado no es válido.")
        self.device = device
        self.on_started = on_started
        self.on_ready = on_ready
        self.on_error = on_error
        self.on_level = on_level
        self.on_cancelled = on_cancelled
        self.source_factory = source_factory
        self.normalize_peak = bool(normalize_peak)
        self.clean_voice = bool(clean_voice)
        self.temp_dir = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())
        self.pipeline: Gst.Pipeline | None = None
        self._bus = None
        self._bus_handler = None
        self._state = "new"
        self._work_dir: Path | None = None
        self._temporary_path: Path | None = None
        self._saved_path: Path | None = None
        self._start_timeout: int | None = None
        self._stop_timeout: int | None = None
        self._first_buffer_idle: int | None = None
        self._first_buffer_queued = False
        self._playing = False
        self._stop_requested = False
        self._pcm_frames = 0
        self._audio_buffers = 0
        self._normalization_cancelled = threading.Event()
        self._normalization_path: Path | None = None
        self._filter_path: Path | None = None
        self._normalization_gain = 1.0
        self._voice_cleanup = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._state in {"starting", "recording", "paused", "stopping", "processing"}

    @property
    def paused(self) -> bool:
        return self._state == "paused"

    @property
    def stopping(self) -> bool:
        return self._state in {"stopping", "processing"} or self._stop_requested

    @property
    def elapsed(self) -> float:
        """Duration of received PCM, excluding startup latency and pauses."""
        return self._pcm_frames / SAMPLE_RATE

    @property
    def temporary_path(self) -> Path | None:
        """Own WAV while starting/recording/ready; None after save or cleanup."""
        return self._temporary_path

    @property
    def saved_path(self) -> Path | None:
        return self._saved_path

    @property
    def statistics(self) -> dict:
        return {
            "state": self.state, "elapsed_seconds": self.elapsed,
            "audio_buffers": self._audio_buffers, "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS, "sample_width": SAMPLE_WIDTH,
            "device": self.device, "source_factory": self.source_factory,
            "normalization_gain": self._normalization_gain,
            "voice_cleanup": dict(self._voice_cleanup),
        }

    def _callback(self, callback, *args) -> None:
        if callback is not None:
            try:
                callback(*args)
            except Exception:
                LOGGER.exception("Falló un callback de la grabación")

    def _make(self, factory: str, name: str, **properties):
        element = Gst.ElementFactory.make(factory, name)
        if element is None:
            raise CaptureError(f"Falta el componente de audio {factory}.")
        for key, value in properties.items():
            element.set_property(key.replace("_", "-"), value)
        self.pipeline.add(element)
        return element

    def _build(self) -> None:
        self.pipeline = Gst.Pipeline.new("grabador-voz")
        if self.pipeline is None:
            raise CaptureError("No se pudo preparar la grabación.")
        if self.source_factory == "pulsesrc":
            source = self._make("pulsesrc", "microfono", do_timestamp=True,
                                provide_clock=False, buffer_time=200000, latency_time=10000)
            if self.device is not None:
                source.set_property("device", self.device)
        else:
            source = self._make("audiotestsrc", "tono-prueba", is_live=True,
                                wave=0, freq=440.0, volume=0.25)
        convert = self._make("audioconvert", "convertir-audio")
        resample = self._make("audioresample", "frecuencia-audio")
        caps = self._make("capsfilter", "formato-audio", caps=Gst.Caps.from_string(
            f"audio/x-raw,format=S24LE,layout=interleaved,rate={SAMPLE_RATE},channels={CHANNELS}"
        ))
        meter = self._make("level", "nivel", interval=100 * Gst.MSECOND,
                           post_messages=True)
        pcm_convert = self._make("audioconvert", "convertir-pcm")
        encoder = self._make("wavenc", "wav")
        sink = self._make("filesink", "archivo", location=str(self._temporary_path), sync=False)
        elements = (source, convert, resample, meter, pcm_convert, caps, encoder, sink)
        for previous, following in zip(elements, elements[1:]):
            if not previous.link(following):
                raise CaptureError(f"No se pudo conectar {previous.get_name()} con {following.get_name()}.")
        caps.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._count_audio)
        self._bus = self.pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus_handler = self._bus.connect("message", self._message)

    def _count_audio(self, _pad, info):
        buffer = info.get_buffer()
        if buffer is not None:
            self._audio_buffers += 1
            self._pcm_frames += buffer.get_size() // (SAMPLE_WIDTH * CHANNELS)
            if not self._first_buffer_queued:
                self._first_buffer_queued = True
                self._first_buffer_idle = GLib.idle_add(self._first_buffer_available)
        return Gst.PadProbeReturn.OK

    def start(self) -> None:
        if self._state != "new":
            raise CaptureError("Esta sesión de grabación ya se inició.")
        try:
            plugins = missing_plugins()
            if self.source_factory == "audiotestsrc":
                plugins = [name for name in plugins if name != "pulsesrc"]
                if Gst.ElementFactory.find("audiotestsrc") is None:
                    plugins.append("audiotestsrc")
            if plugins:
                raise CaptureError("Faltan componentes de GStreamer: " + ", ".join(plugins) + ".")
            self.temp_dir.mkdir(parents=True, exist_ok=True)
            self._work_dir = Path(tempfile.mkdtemp(prefix="voz-", dir=self.temp_dir))
            self._temporary_path = self._work_dir / "grabacion.wav"
            # Reserve the file before filesink opens it, in our private directory.
            descriptor = os.open(self._temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            self._build()
            self._state = "starting"
            self._start_timeout = GLib.timeout_add_seconds(8, self._startup_expired)
            result = self.pipeline.set_state(Gst.State.PLAYING)
            if result == Gst.StateChangeReturn.FAILURE:
                raise CaptureError("No se pudo abrir el micrófono. Verificá que esté conectado y disponible.")
        except Exception as exc:
            self._state = "error"
            self._release_pipeline()
            self._remove_owned_files()
            if isinstance(exc, CaptureError):
                raise
            raise CaptureError(f"No se pudo iniciar la grabación: {exc}") from exc

    def _first_buffer_available(self) -> bool:
        self._first_buffer_idle = None
        return self._mark_started()

    def _mark_started(self) -> bool:
        if self._state != "starting" or not self._playing or not self._first_buffer_queued:
            return GLib.SOURCE_REMOVE
        self._remove_source("_start_timeout")
        self._state = "recording"
        self._callback(self.on_started)
        if self._stop_requested and self._state == "recording":
            self._send_eos()
        return GLib.SOURCE_REMOVE

    def pause(self) -> None:
        """Pause acquisition; no silence is inserted into the WAV."""
        if self._state != "recording":
            return
        self._state = "paused"
        if self.pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            self._fail("No se pudo pausar la grabación de voz.")
        else:
            self._callback(self.on_level, 0.0)

    def resume(self) -> None:
        """Resume the same WAV without including paused wall-clock time."""
        if self._state != "paused":
            return
        self._state = "recording"
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._fail("No se pudo reanudar la grabación de voz.")

    def stop(self) -> None:
        """Request EOS; on_ready runs later, after the WAV header is flushed."""
        if self._state == "starting":
            self._stop_requested = True
        elif self._state in {"recording", "paused"}:
            was_paused = self.paused
            self._stop_requested = True
            self._send_eos()
            # Queue EOS first, then let paused sinks drain without opening a
            # second input or starting another recording.
            if was_paused and self._state == "stopping":
                if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                    self._fail("No se pudo finalizar la grabación de voz pausada.")
        elif self._state not in {"stopping", "processing", "ready", "saved", "cancelled", "error"}:
            raise CaptureError("Todavía no se inició una grabación.")

    def _send_eos(self) -> None:
        if self.pipeline is None or self._state == "stopping":
            return
        self._state = "stopping"
        self._stop_timeout = GLib.timeout_add_seconds(8, self._finalization_expired)
        if not self.pipeline.send_event(Gst.Event.new_eos()):
            self._fail("No se pudo finalizar el archivo de audio.")

    def cancel(self) -> None:
        """Discard this session, releasing the input and its own temporary WAV."""
        if self._state in {"cancelled", "saved"}:
            return
        self._state = "cancelled"
        self._normalization_cancelled.set()
        self._stop_requested = False
        self._release_pipeline()
        self._remove_owned_files()
        self._callback(self.on_level, 0.0)
        self._callback(self.on_cancelled)

    def save(self, destination: str | Path) -> Path:
        """Atomically publish a ready WAV, refusing to overwrite any destination.

        The staged file is copied into the destination directory first so this
        also works across filesystems. Linking it to its final name is atomic
        and fails if another file already has that name. On failure the ready
        recording remains available for retry or cancel.
        """
        if self._state != "ready" or self._temporary_path is None:
            raise CaptureError("La grabación todavía no está lista para guardarse.")
        destination = Path(destination).expanduser().absolute()
        if destination.suffix.lower() != ".wav":
            raise CaptureError("El archivo debe tener la extensión .wav.")
        if not destination.parent.is_dir():
            raise CaptureError("La carpeta de destino no está disponible.")
        if self._work_dir is not None and destination.resolve().is_relative_to(self._work_dir.resolve()):
            raise CaptureError("Elegí una ubicación fuera de los archivos temporales.")
        staged: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".voz-", suffix=".tmp", dir=destination.parent)
            staged = Path(name)
            with os.fdopen(descriptor, "wb") as output, self._temporary_path.open("rb") as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            os.link(staged, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise CaptureError("Ya existe un archivo con ese nombre. Elegí otro nombre para conservar ambos.") from exc
        except OSError as exc:
            raise CaptureError(f"No se pudo guardar la grabación: {exc.strerror or exc}") from exc
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception("No se pudo retirar un archivo temporal de guardado")
        self._saved_path = destination
        self._state = "saved"
        self._stop_requested = False
        self._remove_owned_files()
        return destination

    def _message(self, _bus, message) -> None:
        if not self.active:
            return
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            LOGGER.error("Error de GStreamer: %s (%s)", error.message, debug)
            self._fail("No se pudo grabar el audio. Verificá que el micrófono esté conectado y disponible."
                       if message.src.get_factory() and message.src.get_factory().get_name() == "pulsesrc"
                       else "No se pudo completar la grabación: " + error.message)
        elif message.type == Gst.MessageType.STATE_CHANGED and message.src == self.pipeline:
            _old, current, _pending = message.parse_state_changed()
            if current == Gst.State.PLAYING:
                self._playing = True
                self._mark_started()
        elif message.type == Gst.MessageType.EOS:
            self._finish()
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is not None and structure.get_name() == "level":
                peaks = structure.get_value("peak")
                peak_db = max(peaks) if peaks else -float("inf")
                amplitude = min(1.0, max(0.0, 10.0 ** (peak_db / 20.0))) if math.isfinite(peak_db) else 0.0
                self._callback(self.on_level, amplitude)

    def _finish(self) -> None:
        self._release_pipeline()
        try:
            if self._temporary_path is None:
                raise CaptureError("No se generó un archivo de audio.")
            # Python <3.12's wave reader cannot read WAVE_FORMAT_EXTENSIBLE.
            # wavenc emits standard PCM for the mono caps configured above.
            with wave.open(str(self._temporary_path), "rb") as recording:
                if (recording.getnframes() <= 0 or recording.getnchannels() != CHANNELS
                        or recording.getframerate() != SAMPLE_RATE
                        or recording.getsampwidth() != SAMPLE_WIDTH):
                    raise CaptureError("El archivo de audio está vacío o incompleto.")
                self._pcm_frames = recording.getnframes()
        except (OSError, EOFError, wave.Error, CaptureError) as exc:
            self._fail(f"No se pudo finalizar la grabación: {exc}")
            return
        if self.normalize_peak or self.clean_voice:
            self._state = "processing"
            self._normalization_path = self._temporary_path.with_name("normalizada.wav")
            if self.clean_voice:
                self._filter_path = self._temporary_path.with_name("filtrada.wav")
            threading.Thread(target=self._normalize_background,
                             args=(self._temporary_path, self._normalization_path),
                             name="grabador-normalizar-voz", daemon=True).start()
        else:
            self._ready()

    def _ready(self) -> None:
        self._state = "ready"
        self._stop_requested = False
        self._callback(self.on_level, 0.0)
        self._callback(self.on_ready, self._temporary_path)

    def _normalize_background(self, source: Path, destination: Path) -> None:
        """Clean before raising levels, keeping the original intact until done."""
        gain = 1.0
        modified = False
        error = None
        filtered = source.with_name("filtrada.wav")
        work_source = source
        try:
            if self.clean_voice:
                try:
                    self._voice_cleanup = process_wave(source, filtered, self._normalization_cancelled)
                    self._voice_cleanup["applied"] = True
                    work_source = filtered
                except Exception as exc:
                    # A missing processor must not lose the recording or raise
                    # the untreated background noise through normalization.
                    self._voice_cleanup = {"applied": False, "reason": str(exc)}
                    LOGGER.warning("No se pudo limpiar la voz; se conserva el audio sin amplificar: %s", exc)
            should_raise_level = self.normalize_peak and (
                not self.clean_voice or self._voice_cleanup.get("speech_detected", False)
            )
            peak = 0
            with wave.open(str(work_source), "rb") as recording:
                while not self._normalization_cancelled.is_set():
                    data = recording.readframes(32_768)
                    if not data:
                        break
                    peak = max(peak, _pcm24_peak(data))
                if should_raise_level and peak >= NORMALIZATION_MIN_PEAK * PCM24_SCALE:
                    target = VOICE_NORMALIZATION_TARGET if self.clean_voice else NORMALIZATION_TARGET
                    gain = min(NORMALIZATION_MAX_GAIN, target * PCM24_SCALE / peak)
                # Preserve silence, loud recordings and already suitable levels.
                if gain > 10 ** (1 / 20) and not self._normalization_cancelled.is_set():
                    recording.rewind()
                    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        with wave.open(output, "wb") as normalized:
                            normalized.setparams(recording.getparams())
                            while not self._normalization_cancelled.is_set():
                                data = recording.readframes(32_768)
                                if not data:
                                    break
                                normalized.writeframesraw(_pcm24_amplify(data, gain))
                        output.flush()
                        os.fsync(output.fileno())
                    modified = True
                else:
                    gain = 1.0
            if not modified and work_source == filtered and not self._normalization_cancelled.is_set():
                filtered.replace(destination)
                modified = True
        except Exception as exc:
            error = str(exc)
        finally:
            if self.clean_voice:
                try:
                    filtered.unlink(missing_ok=True)
                except OSError:
                    LOGGER.exception("No se pudo retirar la copia temporal del filtro")
        if self._normalization_cancelled.is_set():
            try:
                destination.unlink(missing_ok=True)
                source.parent.rmdir()
            except OSError:
                pass
            return
        GLib.idle_add(self._normalized, source, destination, gain, modified, error)

    def _normalized(self, source: Path, destination: Path, gain: float,
                    modified: bool, error: str | None) -> bool:
        if self._state != "processing" or self._normalization_cancelled.is_set():
            return GLib.SOURCE_REMOVE
        if error:
            LOGGER.warning("No se pudo normalizar el audio; se conserva el WAV original: %s", error)
            self._normalization_fallback(destination)
            return GLib.SOURCE_REMOVE
        try:
            if modified:
                destination.replace(source)
            self._normalization_path = None
            self._normalization_gain = gain
            if self.clean_voice:
                LOGGER.info("Procesamiento de voz: aumento %.2f dB; limpieza %s",
                            20 * math.log10(max(gain, 1e-10)), self._voice_cleanup)
            self._ready()
        except OSError as exc:
            LOGGER.warning("No se pudo publicar el audio normalizado; se conserva el WAV original: %s", exc)
            self._normalization_fallback(destination)
        return GLib.SOURCE_REMOVE

    def _normalization_fallback(self, destination: Path) -> None:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("No se pudo retirar la copia normalizada incompleta")
        self._normalization_path = None
        self._normalization_gain = 1.0
        if self.clean_voice:
            self._voice_cleanup["applied"] = False
        self._ready()

    def _fail(self, message: str) -> None:
        if self._state in {"error", "cancelled", "saved"}:
            return
        self._state = "error"
        self._normalization_cancelled.set()
        self._stop_requested = False
        self._release_pipeline()
        self._remove_owned_files()
        self._callback(self.on_level, 0.0)
        self._callback(self.on_error, message)

    def _startup_expired(self) -> bool:
        self._start_timeout = None
        if self._state == "starting":
            self._fail("El micrófono no entregó audio. Verificá el dispositivo seleccionado.")
        return GLib.SOURCE_REMOVE

    def _finalization_expired(self) -> bool:
        self._stop_timeout = None
        if self._state == "stopping":
            self._fail("La grabación tardó demasiado en finalizar. El micrófono fue liberado.")
        return GLib.SOURCE_REMOVE

    def _remove_source(self, attribute: str) -> None:
        source_id = getattr(self, attribute)
        if source_id is not None:
            # A buffer probe runs on a streaming thread: its first idle can
            # already have dispatched before idle_add returns the source ID.
            if GLib.MainContext.default().find_source_by_id(source_id) is not None:
                GLib.source_remove(source_id)
            setattr(self, attribute, None)

    def _release_pipeline(self) -> None:
        self._remove_source("_start_timeout")
        self._remove_source("_stop_timeout")
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        # NULL waits until streaming tasks have stopped, so no new idle can be
        # enqueued by our buffer probe after this source is removed.
        self._remove_source("_first_buffer_idle")
        if self._bus is not None:
            if self._bus_handler is not None:
                self._bus.disconnect(self._bus_handler)
            self._bus.remove_signal_watch()
        self._bus = None
        self._bus_handler = None
        self.pipeline = None

    def _remove_owned_files(self) -> None:
        if self._filter_path is not None:
            try:
                self._filter_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("No se pudo retirar el WAV filtrado temporal propio")
            self._filter_path = None
        if self._normalization_path is not None:
            try:
                self._normalization_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("No se pudo retirar el WAV normalizado temporal propio")
            self._normalization_path = None
        if self._temporary_path is not None:
            try:
                self._temporary_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("No se pudo retirar el WAV temporal propio")
            self._temporary_path = None
        if self._work_dir is not None:
            try:
                self._work_dir.rmdir()
            except OSError:
                LOGGER.exception("No se pudo retirar la carpeta temporal propia")
            self._work_dir = None
