"""Capture lifecycle tests using a synthetic tone; no microphone is opened."""
from __future__ import annotations

import math
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import wave

from grabador.audio_capture import (AudioSession, CaptureError, GLib, Gst, SAMPLE_RATE,
                                    SAMPLE_WIDTH, NORMALIZATION_MAX_GAIN, PCM24_SCALE,
                                    _pcm24_peak, _pcm24_amplify)


class AudioSessionTests(unittest.TestCase):
    def setUp(self):
        # Keep test mutations inside this new project, including scratch files.
        self.scratch = tempfile.TemporaryDirectory(prefix="capture-test-", dir=Path(__file__).parent)
        self.directory = Path(self.scratch.name)
        self.sessions = []
        self.scheduled = []

    def tearDown(self):
        for session in self.sessions:
            session.cancel()
        for source in self.scheduled:
            if GLib.MainContext.default().find_source_by_id(source):
                GLib.source_remove(source)
        self.scratch.cleanup()

    def session(self, **kwargs):
        session = AudioSession(temp_dir=self.directory, source_factory="audiotestsrc", **kwargs)
        self.sessions.append(session)
        return session

    def run_loop(self, loop):
        expired = []
        def timeout():
            expired.append(True)
            loop.quit()
            return GLib.SOURCE_REMOVE
        source = GLib.timeout_add_seconds(4, timeout)
        self.scheduled.append(source)
        loop.run()
        if GLib.MainContext.default().find_source_by_id(source):
            GLib.source_remove(source)
        self.assertFalse(expired, "The asynchronous capture failed to finish")

    def record(self, seconds=0.20, immediate_stop=False, volume=0.25, **kwargs):
        loop = GLib.MainLoop()
        events = []
        levels = []
        errors = []
        def started():
            events.append("started")
            if not immediate_stop:
                def stop():
                    session.stop()
                    events.append("stop-returned")
                    return GLib.SOURCE_REMOVE
                self.scheduled.append(GLib.timeout_add(round(seconds * 1000), stop))
        def ready(path):
            events.append("ready")
            loop.quit()
        def error(message):
            errors.append(message)
            loop.quit()
        session = self.session(on_started=started, on_ready=ready, on_error=error, on_level=levels.append, **kwargs)
        build = session._build
        def configured_build():
            build()
            session.pipeline.get_by_name("tono-prueba").set_property("volume", volume)
        session._build = configured_build
        session.start()
        if immediate_stop:
            session.stop()
            events.append("stop-returned")
        self.run_loop(loop)
        self.assertEqual(errors, [])
        self.assertEqual(session.state, "ready")
        return session, events, levels

    def peak(self, session):
        with wave.open(str(session.temporary_path), "rb") as recording:
            return _pcm24_peak(recording.readframes(recording.getnframes())) / PCM24_SCALE

    def test_constructor_never_opens_audio_or_creates_files(self):
        with mock.patch.object(Gst.ElementFactory, "make", side_effect=AssertionError("opened an input")):
            session = AudioSession(temp_dir=self.directory)
        self.assertEqual(session.state, "new")
        self.assertEqual(session.elapsed, 0)
        self.assertEqual(list(self.directory.iterdir()), [])
        session.cancel()

    def test_eos_flushes_real_pcm_and_reports_duration_and_level(self):
        session, events, levels = self.record(0.26)
        self.assertEqual(events, ["started", "stop-returned", "ready"])
        self.assertIsNone(session.pipeline)
        self.assertFalse(session.active)
        self.assertGreaterEqual(session.elapsed, 0.24)
        self.assertLess(session.elapsed, 0.50)
        self.assertTrue(any(0.20 < value < 0.30 for value in levels), levels)
        self.assertEqual(levels[-1], 0)
        with wave.open(str(session.temporary_path), "rb") as recording:
            self.assertEqual(recording.getnchannels(), 1)
            self.assertEqual(recording.getsampwidth(), SAMPLE_WIDTH)
            self.assertEqual(recording.getframerate(), SAMPLE_RATE)
            self.assertAlmostEqual(recording.getnframes() / SAMPLE_RATE, session.elapsed)
            raw = recording.readframes(recording.getnframes())
        samples = [int.from_bytes(raw[i:i + 3], "little", signed=True) / 8388608
                   for i in range(0, len(raw), 3)]
        rms = math.sqrt(sum(value * value for value in samples) / len(samples))
        self.assertGreater(rms, 0.15)
        self.assertLess(rms, 0.20)
        # The test source is specifically a 440 Hz tone, not fabricated silence.
        crossings = sum(left <= 0 < right for left, right in zip(samples, samples[1:]))
        self.assertAlmostEqual(crossings / session.elapsed, 440, delta=8)

    def test_stop_requested_during_startup_produces_valid_wav(self):
        session, events, _ = self.record(immediate_stop=True)
        self.assertEqual(events, ["stop-returned", "started", "ready"])
        self.assertGreater(session.elapsed, 0)
        with wave.open(str(session.temporary_path), "rb") as recording:
            self.assertGreater(recording.getnframes(), 0)
        session.stop()  # Repeated stop on a ready session is harmless.

    def test_pause_excludes_wall_time_then_resume_continues_same_wav(self):
        loop = GLib.MainLoop()
        elapsed = []
        errors = []
        def schedule(delay, callback):
            def once():
                callback()
                return GLib.SOURCE_REMOVE
            self.scheduled.append(GLib.timeout_add(delay, once))
        def started():
            schedule(80, pause)
        def pause():
            session.pause()
            self.assertTrue(session.active)
            self.assertTrue(session.paused)
            schedule(50, paused_sample)
        def paused_sample():
            elapsed.append(session.elapsed)
            schedule(170, resume)
        def resume():
            elapsed.append(session.elapsed)
            session.resume()
            self.assertFalse(session.paused)
            schedule(80, session.stop)
        def error(message):
            errors.append(message)
            loop.quit()
        session = self.session(on_started=started, on_ready=lambda _: loop.quit(), on_error=error)
        session.start()
        self.run_loop(loop)
        self.assertEqual(errors, [])
        self.assertEqual(session.state, "ready")
        self.assertAlmostEqual(elapsed[0], elapsed[1], delta=0.001)
        self.assertGreater(session.elapsed, 0.12)
        self.assertLess(session.elapsed, 0.30)
        with wave.open(str(session.temporary_path), "rb") as recording:
            self.assertAlmostEqual(recording.getnframes() / SAMPLE_RATE, session.elapsed)

    def test_stop_while_paused_finishes_valid_wav(self):
        loop = GLib.MainLoop()
        errors = []
        def started():
            session.pause()
            self.assertTrue(session.paused)
            session.stop()
        def error(message):
            errors.append(message)
            loop.quit()
        session = self.session(on_started=started, on_ready=lambda _: loop.quit(), on_error=error)
        session.start()
        self.run_loop(loop)
        self.assertEqual(errors, [])
        self.assertEqual(session.state, "ready")
        self.assertFalse(session.active)
        self.assertIsNone(session.pipeline)
        with wave.open(str(session.temporary_path), "rb") as recording:
            self.assertGreater(recording.getnframes(), 0)

    def test_normalization_raises_quiet_pcm_to_minus_one_db_without_clipping(self):
        session, events, _ = self.record(0.15, normalize_peak=True)
        self.assertEqual(events, ["started", "stop-returned", "ready"])
        self.assertAlmostEqual(self.peak(session), 10 ** (-1 / 20), delta=0.0001)
        self.assertGreater(session.statistics["normalization_gain"], 3.5)
        self.assertIsNone(session._normalization_path)
        destination = self.directory / "normalizada.wav"
        session.save(destination)
        with wave.open(str(destination), "rb") as recording:
            self.assertEqual(recording.getnframes(), round(session.elapsed * SAMPLE_RATE))

    def test_normalization_caps_gain_and_preserves_silence_and_loud_pcm(self):
        quiet, _, _ = self.record(0.06, volume=0.002, normalize_peak=True)
        self.assertAlmostEqual(quiet.statistics["normalization_gain"], NORMALIZATION_MAX_GAIN)
        self.assertAlmostEqual(self.peak(quiet), 0.002 * NORMALIZATION_MAX_GAIN, delta=0.00001)
        room_noise, _, _ = self.record(0.06, volume=0.0001, normalize_peak=True)
        self.assertAlmostEqual(self.peak(room_noise), 0.0001, delta=0.00001)
        self.assertEqual(room_noise.statistics["normalization_gain"], 1)
        silence, _, _ = self.record(0.06, volume=0, normalize_peak=True)
        self.assertEqual(self.peak(silence), 0)
        self.assertEqual(silence.statistics["normalization_gain"], 1)
        loud, _, _ = self.record(0.06, volume=0.95, normalize_peak=True)
        self.assertAlmostEqual(self.peak(loud), 0.95, delta=0.00001)
        self.assertEqual(loud.statistics["normalization_gain"], 1)

    def test_normalization_failure_keeps_original_wav_ready_to_save(self):
        with mock.patch("grabador.audio_capture._pcm24_peak", side_effect=OSError("Sin espacio")):
            session, _, _ = self.record(0.06, normalize_peak=True)
        self.assertEqual(session.state, "ready")
        self.assertAlmostEqual(self.peak(session), 0.25, delta=0.00001)
        self.assertEqual(session.statistics["normalization_gain"], 1)
        self.assertFalse(session.temporary_path.with_name("normalizada.wav").exists())
        self.assertTrue(session.save(self.directory / "original.wav").is_file())

    def test_pcm24_fallback_preserves_signed_samples_and_caps_output(self):
        samples = (-PCM24_SCALE, -12345, 0, 12345, PCM24_SCALE - 1)
        data = b"".join(sample.to_bytes(3, "little", signed=True) for sample in samples)
        with mock.patch("grabador.audio_capture._audioop", None):
            self.assertEqual(_pcm24_peak(data), PCM24_SCALE)
            amplified = _pcm24_amplify(data, 2)
        result = [int.from_bytes(amplified[i:i + 3], "little", signed=True)
                  for i in range(0, len(amplified), 3)]
        self.assertEqual(result, [-PCM24_SCALE, -24690, 0, 24690, PCM24_SCALE - 1])

    def test_cancel_while_normalizing_does_not_publish_or_notify_ready(self):
        loop = GLib.MainLoop()
        entered = threading.Event()
        release = threading.Event()
        worker_finished = threading.Event()
        ready = []
        original_peak = _pcm24_peak
        def slow_peak(data):
            entered.set()
            release.wait(2)
            result = original_peak(data)
            worker_finished.set()
            return result
        def started():
            self.scheduled.append(GLib.timeout_add(30, stop))
        def stop():
            session.stop()
            return GLib.SOURCE_REMOVE
        def cancel_when_processing():
            if not entered.is_set():
                return GLib.SOURCE_CONTINUE
            self.assertEqual(session.state, "processing")
            session.stop()  # Closing while normalization runs can safely wait.
            self.assertTrue(session.active)
            session.cancel()
            release.set()
            return GLib.SOURCE_REMOVE
        def done():
            if not worker_finished.is_set():
                return GLib.SOURCE_CONTINUE
            loop.quit()
            return GLib.SOURCE_REMOVE
        session = self.session(on_started=started, on_ready=ready.append, normalize_peak=True)
        with mock.patch("grabador.audio_capture._pcm24_peak", side_effect=slow_peak):
            session.start()
            self.scheduled.append(GLib.timeout_add(10, cancel_when_processing))
            self.scheduled.append(GLib.timeout_add(10, done))
            self.run_loop(loop)
        self.assertEqual(session.state, "cancelled")
        self.assertEqual(ready, [])
        self.assertIsNone(session.pipeline)
        self.assertIsNone(session.temporary_path)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_cancel_releases_pipeline_and_only_removes_owned_files(self):
        unrelated = self.directory / "conservar.wav"
        unrelated.write_bytes(b"an existing recording")
        loop = GLib.MainLoop()
        ready = []
        cancelled = []
        temporary_paths = []
        def started():
            temporary_paths.append(session.temporary_path)
            session.cancel()
            loop.quit()
        session = self.session(on_started=started, on_ready=ready.append,
                               on_cancelled=lambda: cancelled.append(True))
        session.start()
        self.run_loop(loop)
        self.assertEqual(session.state, "cancelled")
        self.assertIsNone(session.pipeline)
        self.assertIsNone(session.temporary_path)
        self.assertFalse(temporary_paths[0].exists())
        self.assertEqual(ready, [])
        self.assertEqual(cancelled, [True])
        self.assertEqual(unrelated.read_bytes(), b"an existing recording")
        session.cancel()
        self.assertEqual(cancelled, [True])

    def test_save_is_atomic_and_existing_file_survives_retry(self):
        session, _, _ = self.record()
        original = session.temporary_path.read_bytes()
        existing = self.directory / "existente.wav"
        existing.write_bytes(b"do not overwrite")
        with self.assertRaisesRegex(CaptureError, "Ya existe"):
            session.save(existing)
        self.assertEqual(existing.read_bytes(), b"do not overwrite")
        self.assertEqual(session.state, "ready")
        self.assertEqual(session.temporary_path.read_bytes(), original)
        destination = self.directory / "nueva.wav"
        with mock.patch("grabador.audio_capture.os.link", wraps=__import__("os").link) as publish:
            saved = session.save(destination)
            staged, final = publish.call_args.args
            self.assertEqual(staged.parent, final.parent)
            self.assertEqual(final, destination)
        self.assertEqual(saved, destination)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(session.state, "saved")
        self.assertIsNone(session.temporary_path)
        self.assertFalse(any(path.name.startswith(".voz-") for path in self.directory.iterdir()))
        session.cancel()
        self.assertEqual(destination.read_bytes(), original)

    def test_failed_publication_keeps_ready_recording_and_cleans_staging(self):
        session, _, _ = self.record()
        destination = self.directory / "retry.wav"
        with mock.patch("grabador.audio_capture.os.link", side_effect=PermissionError(13, "Permiso denegado")):
            with self.assertRaisesRegex(CaptureError, "No se pudo guardar"):
                session.save(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(session.state, "ready")
        self.assertTrue(session.temporary_path.is_file())
        self.assertFalse(any(path.name.startswith(".voz-") for path in self.directory.iterdir()))
        self.assertEqual(session.save(destination), destination)

    def test_existing_symlink_destination_is_not_followed_or_replaced(self):
        session, _, _ = self.record()
        original = self.directory / "original.wav"
        original.write_bytes(b"keep original")
        destination = self.directory / "enlace.wav"
        destination.symlink_to(original)
        with self.assertRaisesRegex(CaptureError, "Ya existe"):
            session.save(destination)
        self.assertTrue(destination.is_symlink())
        self.assertEqual(original.read_bytes(), b"keep original")
        self.assertEqual(session.state, "ready")

    def test_missing_plugin_fails_cleanly_before_opening_source(self):
        session = self.session()
        with mock.patch("grabador.audio_capture.missing_plugins", return_value=["wavenc"]):
            with self.assertRaisesRegex(CaptureError, "wavenc"):
                session.start()
        self.assertEqual(session.state, "error")
        self.assertIsNone(session.pipeline)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_async_device_error_releases_input_and_removes_incomplete_file(self):
        loop = GLib.MainLoop()
        errors = []
        ready = []
        def error(message):
            errors.append(message)
            loop.quit()
        def started():
            failure = GLib.Error.new_literal(Gst.ResourceError.quark(), "Dispositivo no disponible", Gst.ResourceError.NOT_FOUND)
            session.pipeline.get_bus().post(Gst.Message.new_error(session.pipeline, failure, "synthetic failure"))
        session = self.session(on_started=started, on_error=error, on_ready=ready.append)
        session.start()
        self.run_loop(loop)
        self.assertEqual(session.state, "error")
        self.assertEqual(len(errors), 1)
        self.assertIn("Dispositivo no disponible", errors[0])
        self.assertEqual(ready, [])
        self.assertIsNone(session.pipeline)
        self.assertIsNone(session.temporary_path)
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
