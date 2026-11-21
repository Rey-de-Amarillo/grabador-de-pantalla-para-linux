"""Conservative offline voice cleanup; no device or global audio settings.

SpeexDSP's documented C preprocessor operates on signed 16-bit frames. Capture
and files remain mono 24-bit PCM/48 kHz. We compensate its one-frame overlap
delay, so cleanup neither shifts the voice nor removes the final consonant.

API reference: https://www.speex.org/docs/api/speex-api-reference/
Headers: https://github.com/xiph/speexdsp/blob/master/include/speex/speex_preprocess.h
"""
from __future__ import annotations

import ctypes
import ctypes.util
import math
import os
from pathlib import Path
import threading
import wave


RATE = 48_000
WIDTH = 3
FRAME = 960                    # 20 ms: documented Speex frame length.
ANALYSIS = 240                 # 5 ms: short isolated mechanical transients.
SCALE = 1 << 23


class FilterUnavailable(RuntimeError):
    """The optional native denoiser is unavailable; keep the original WAV."""


class FilterCancelled(RuntimeError):
    """Cleanup was cancelled; the source is always retained."""


def available() -> bool:
    return ctypes.util.find_library("speexdsp") is not None


def _library():
    name = ctypes.util.find_library("speexdsp")
    if not name:
        raise FilterUnavailable("No está disponible libspeexdsp1.")
    try:
        library = ctypes.CDLL(name)
        library.speex_preprocess_state_init.argtypes = (ctypes.c_int, ctypes.c_int)
        library.speex_preprocess_state_init.restype = ctypes.c_void_p
        library.speex_preprocess_state_destroy.argtypes = (ctypes.c_void_p,)
        library.speex_preprocess_state_destroy.restype = None
        library.speex_preprocess_run.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16))
        library.speex_preprocess_run.restype = ctypes.c_int
        library.speex_preprocess_ctl.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
        library.speex_preprocess_ctl.restype = ctypes.c_int
        return library
    except (OSError, AttributeError) as error:
        raise FilterUnavailable("No se pudo cargar el filtro de voz SpeexDSP.") from error


def _check(cancelled: threading.Event) -> None:
    if cancelled.is_set():
        raise FilterCancelled("Limpieza de voz cancelada.")


def _samples(data: bytes) -> list[int]:
    return [int.from_bytes(data[i:i + WIDTH], "little", signed=True)
            for i in range(0, len(data), WIDTH)]


def _db(value: float) -> float:
    return 20 * math.log10(max(value, 1 / SCALE))


def _periodic(samples: list[int]) -> bool:
    """Do not train the noise model with a clearly voiced/pitched fragment."""
    downsampled = samples[::3]
    if len(downsampled) < 160:
        return False
    for lag in range(32, min(201, len(downsampled) - 80), 4):
        left, right = downsampled[lag:], downsampled[:-lag]
        energy = sum(value * value for value in left) * sum(value * value for value in right)
        if energy and sum(a * b for a, b in zip(left, right)) > .65 * math.sqrt(energy):
            return True
    return False


def _analyse(recording, cancelled):
    """Only bounded metadata is retained: four energy cells per 20-ms frame."""
    cells = []
    while True:
        _check(cancelled)
        data = recording.readframes(ANALYSIS)
        if not data:
            break
        samples = _samples(data)
        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / SCALE
        peak = max((abs(value) for value in samples), default=0) / SCALE
        cells.append((rms, peak))
    if not cells:
        raise ValueError("El audio está vacío.")
    ordered = sorted(cell[0] for cell in cells)
    # Noise is estimated from quiet portions throughout the recording, never
    # from an unconditional leading silence that might actually contain words.
    floor = ordered[max(0, int((len(ordered) - 1) * .15))]
    floor = max(floor, 1 / 32768)
    p90 = ordered[int((len(ordered) - 1) * .90)]
    threshold = max(floor * 3.0, p90 * .13, 3 / 32768)
    active = [rms >= threshold for rms, _peak in cells]
    spans = []
    start = None
    for i, yes in enumerate(active + [False]):
        if yes and start is None:
            start = i
        elif not yes and start is not None:
            spans.append((start, i))
            start = None
    clicks = []
    # A mouse transient can be reduced safely only when it is isolated. A
    # short plosive adjacent to speech is intentionally left alone.
    for start, end in spans:
        if end - start > 3:
            continue
        left = cells[max(0, start - 15):start]
        right = cells[end:min(len(cells), end + 15)]
        if len(left) < 10 or len(right) < 10:
            continue
        context = left + right
        if any(rms > threshold for rms, _peak in context):
            continue
        peak = max(cells[i][1] for i in range(start, end))
        rms = math.sqrt(sum(cells[i][0] ** 2 for i in range(start, end)) / (end - start))
        # High crest factor distinguishes an impulse from a short voiced tone.
        if peak / max(rms, 1 / SCALE) < 3.5 or peak < floor * 8:
            continue
        clicks.append((max(0, start * ANALYSIS - 96), end * ANALYSIS + 96))
    # Require at least 80 ms of sustained energy, rather than a click or a
    # single abrupt noise event, before granting later automatic makeup gain.
    sustained_cells = sum(end - start for start, end in spans if end - start >= 16)
    return cells, floor, clicks, sustained_cells


class _Highpass:
    """65-Hz Butterworth: remove rumble without a hard speech/noise gate."""
    def __init__(self):
        w = 2 * math.pi * 65 / RATE
        cosine = math.cos(w)
        alpha = math.sin(w) / math.sqrt(2)
        a0 = 1 + alpha
        self.b0 = (1 + cosine) / (2 * a0)
        self.b1 = -(1 + cosine) / a0
        self.b2 = self.b0
        self.a1 = -2 * cosine / a0
        self.a2 = (1 - alpha) / a0
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def run(self, sample: float) -> float:
        value = self.b0 * sample + self.b1 * self.x1 + self.b2 * self.x2 - self.a1 * self.y1 - self.a2 * self.y2
        self.x2, self.x1 = self.x1, sample
        self.y2, self.y1 = self.y1, value
        return value


def process_wave(source: Path, destination: Path,
                 cancel_event: threading.Event) -> dict:
    """Create a cleaned copy, preserving exact frames and the untouched source.

    Raises FilterUnavailable when SpeexDSP is absent. Raises FilterCancelled
    cooperatively. A pre-existing destination (including a link) is refused;
    only a destination created by this call is removed on failure.
    """
    source, destination = Path(source), Path(destination)
    _check(cancel_event)
    library = _library()
    state = None
    created = False
    try:
        with wave.open(str(source), "rb") as recording:
            if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate(), recording.getcomptype()) != (1, WIDTH, RATE, "NONE"):
                raise ValueError("El filtro requiere WAV mono PCM de 24 bits y 48 kHz.")
            count = recording.getnframes()
            cells, floor, clicks, sustained = _analyse(recording, cancel_event)
            state = library.speex_preprocess_state_init(FRAME, RATE)
            if not state:
                raise FilterUnavailable("No se pudo preparar el filtro de voz.")
            for request, number in ((0, 1), (2, 0), (8, 0), (18, -18)):
                value = ctypes.c_int(number)
                if library.speex_preprocess_ctl(state, request, ctypes.byref(value)):
                    raise FilterUnavailable("La biblioteca no admite la configuración de voz.")
            buffer = (ctypes.c_int16 * FRAME)()
            # Train with up to 2 s of low-energy input sampled throughout the
            # file. Do not learn words as noise when no quiet region is found.
            candidates = []
            for first in range(0, len(cells) - 3, 4):
                segment = cells[first:first + 4]
                if max(rms for rms, _peak in segment) <= floor * 1.4:
                    candidates.append(first * ANALYSIS)
            if candidates:
                candidates = (candidates * math.ceil(100 / len(candidates)))[:100]
            voiced_seed_frames = 0
            periodic_cache = {}
            for position in candidates:
                _check(cancel_event)
                recording.setpos(position)
                samples = _samples(recording.readframes(FRAME))
                if position not in periodic_cache:
                    periodic_cache[position] = _periodic(samples)
                if periodic_cache[position]:
                    voiced_seed_frames += 1
                    continue
                for i in range(FRAME):
                    buffer[i] = max(-32768, min(32767, round(samples[i] / 256))) if i < len(samples) else 0
                library.speex_preprocess_run(state, buffer)
            recording.rewind()
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            processed = 0
            speech_frames = 0
            written = 0
            offset = 0
            click_index = 0
            highpass = _Highpass()
            with os.fdopen(descriptor, "wb") as stream:
                with wave.open(stream, "wb") as cleaned:
                    cleaned.setnchannels(1)
                    cleaned.setsampwidth(WIDTH)
                    cleaned.setframerate(RATE)
                    while offset < count + FRAME:
                        _check(cancel_event)
                        samples = _samples(recording.readframes(FRAME)) if offset < count else []
                        for i in range(FRAME):
                            value = samples[i] / SCALE if i < len(samples) else 0.0
                            position = offset + i
                            while click_index < len(clicks) and position >= clicks[click_index][1]:
                                click_index += 1
                            if click_index < len(clicks):
                                start, end = clicks[click_index]
                                if start <= position < end:
                                    fade = min(1.0, (position - start) / 96, (end - position) / 96)
                                    value *= 1 - fade * (1 - 10 ** (-18 / 20))
                            value = highpass.run(value)
                            buffer[i] = max(-32768, min(32767, round(value * 32768)))
                        library.speex_preprocess_run(state, buffer)
                        probability = ctypes.c_int()
                        library.speex_preprocess_ctl(state, 45, ctypes.byref(probability))
                        if offset < count and probability.value >= 70:
                            speech_frames += 1
                        # Discard Speex's initial overlap frame and flush one
                        # zero frame at the end, preserving exact audio timing.
                        if processed:
                            output_count = min(FRAME, count - written)
                            output = bytearray(output_count * WIDTH)
                            for i in range(output_count):
                                output[i * WIDTH:i * WIDTH + WIDTH] = (int(buffer[i]) * 256).to_bytes(WIDTH, "little", signed=True)
                            cleaned.writeframesraw(output)
                            written += output_count
                        processed += 1
                        offset += FRAME
                stream.flush()
                os.fsync(stream.fileno())
            _check(cancel_event)
            if written != count:
                raise ValueError("El filtro no conservó la duración del audio.")
            # Continuous speech need not contain a quiet pause. When quiet
            # candidates were clearly pitched, require speech-like amplitude
            # variation and native speech probability instead of assuming that
            # the whole clip's 15th percentile is a true background floor.
            ordered_rms = sorted(rms for rms, _peak in cells)
            p90_rms = ordered_rms[int((len(ordered_rms) - 1) * .90)]
            pitched_voice = voiced_seed_frames >= 4 and p90_rms >= floor * 1.5
            return {
                "speex": True, "frame_count": count,
                "processed_frames": max(0, processed - 1),
                "speech_frames": speech_frames,
                "speech_detected": (sustained >= 16 or pitched_voice) and speech_frames >= 3,
                "clicks_attenuated": len(clicks), "noise_floor_dbfs": _db(floor),
                "noise_suppression_db": -18, "highpass_hz": 65,
            }
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if state:
            library.speex_preprocess_state_destroy(state)
