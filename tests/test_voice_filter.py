"""Noise reduction checks on deterministic synthetic speech-like waveforms."""
from __future__ import annotations

import math
from pathlib import Path
import random
import sys
import tempfile
import threading
import unittest
from unittest import mock
import wave

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "grabador"))
import voice_filter as vf


@unittest.skipUnless(vf.available(), "optional SpeexDSP is not installed")
class VoiceFilterTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="voice-filter-", dir=PROJECT / "tests")
        self.directory = Path(self.scratch.name)
        self.source = self.directory / "source.wav"
        self.destination = self.directory / "cleaned.wav"

    def tearDown(self):
        self.scratch.cleanup()

    def write(self, samples):
        with wave.open(str(self.source), "wb") as recording:
            recording.setparams((1, 3, vf.RATE, 0, "NONE", "not compressed"))
            recording.writeframes(b"".join(max(-vf.SCALE, min(vf.SCALE - 1, round(value * vf.SCALE))).to_bytes(3, "little", signed=True)
                                           for value in samples))

    def read(self):
        with wave.open(str(self.destination), "rb") as recording:
            params = recording.getparams()
            samples = [value / vf.SCALE for value in vf._samples(recording.readframes(recording.getnframes()))]
        return params, samples

    @staticmethod
    def rms(samples, start, end):
        region = samples[int(start * vf.RATE):int(end * vf.RATE)]
        return math.sqrt(sum(value * value for value in region) / max(1, len(region)))

    def fixture(self, voice=True, clicks=True, seconds=3):
        rng = random.Random(407)
        samples = []
        for i in range(round(seconds * vf.RATE)):
            t = i / vf.RATE
            value = rng.gauss(0, .008)
            if voice and .95 <= t < 2.0:
                envelope = (.10 + .035 * math.sin(2 * math.pi * 3.1 * t))
                envelope *= min(1, (t - .95) / .015, (2 - t) / .04)
                value += envelope * (math.sin(2 * math.pi * 155 * t) + .35 * math.sin(2 * math.pi * 310 * t) + .2 * math.sin(2 * math.pi * 465 * t))
            samples.append(value)
        if clicks and seconds > .56:
            # Isolated mouse-style impulse in a quiet interval.
            for j in range(20):
                samples[round(.55 * vf.RATE) + j] += .65 * math.exp(-j / 5) * (-1 if j % 2 else 1)
            # The same abrupt transient immediately before voice is protected.
            if voice:
                for j in range(20):
                    samples[round(.95 * vf.RATE) + j] += .45 * math.exp(-j / 5) * (-1 if j % 2 else 1)
        return samples

    def test_reduces_stationary_noise_and_isolated_click_without_cutting_voice(self):
        original = self.fixture()
        self.write(original)
        source_bytes = self.source.read_bytes()
        stats = vf.process_wave(self.source, self.destination, threading.Event())
        params, cleaned = self.read()
        self.assertEqual((params.nchannels, params.sampwidth, params.framerate, params.nframes), (1, 3, vf.RATE, len(original)))
        self.assertEqual(source_bytes, self.source.read_bytes())
        quiet_db = 20 * math.log10(self.rms(cleaned, .10, .40) / self.rms(original, .10, .40))
        late_quiet_db = 20 * math.log10(self.rms(cleaned, 2.3, 2.8) / self.rms(original, 2.3, 2.8))
        voice_db = 20 * math.log10(self.rms(cleaned, 1.10, 1.75) / self.rms(original, 1.10, 1.75))
        click_db = 20 * math.log10(self.rms(cleaned, .54, .57) / self.rms(original, .54, .57))
        self.assertLess(quiet_db, -7, (quiet_db, late_quiet_db))
        self.assertLess(late_quiet_db, -7)
        self.assertGreater(voice_db, -3)
        self.assertLess(voice_db, 1)
        self.assertLess(click_db, -7)
        self.assertTrue(stats["speech_detected"])
        self.assertEqual(stats["clicks_attenuated"], 1)
        self.assertTrue(stats["speex"])
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)

    def test_noise_only_and_clicks_do_not_grant_automatic_voice_gain(self):
        self.write(self.fixture(voice=False))
        stats = vf.process_wave(self.source, self.destination, threading.Event())
        self.assertFalse(stats["speech_detected"])

    def test_silence_and_partial_final_frame_keep_exact_duration(self):
        for count in (1, 959, 960, 1001):
            with self.subTest(frames=count):
                self.destination.unlink(missing_ok=True)
                self.write([0.0] * count)
                stats = vf.process_wave(self.source, self.destination, threading.Event())
                params, samples = self.read()
                self.assertEqual(params.nframes, count)
                self.assertEqual(samples, [0.0] * count)
                self.assertFalse(stats["speech_detected"])

    def test_continuous_quiet_voiced_audio_is_not_learned_as_room_noise(self):
        samples = []
        for i in range(vf.RATE * 2):
            t = i / vf.RATE
            level = .012 * (.65 + .35 * math.sin(2 * math.pi * 3.1 * t))
            samples.append(level * (math.sin(2 * math.pi * 160 * t) + .3 * math.sin(2 * math.pi * 320 * t)))
        self.write(samples)
        stats = vf.process_wave(self.source, self.destination, threading.Event())
        _params, cleaned = self.read()
        ratio = self.rms(cleaned, .1, 1.9) / self.rms(samples, .1, 1.9)
        self.assertGreater(ratio, .65)
        self.assertTrue(stats["speech_detected"])

    def test_flush_preserves_audio_in_last_frame_without_time_shift(self):
        samples = [0.0] * (vf.FRAME * 5 + 123)
        start = len(samples) - vf.FRAME
        for i in range(start, len(samples)):
            samples[i] = .25 * math.sin(2 * math.pi * 440 * (i - start) / vf.RATE)
        self.write(samples)
        vf.process_wave(self.source, self.destination, threading.Event())
        _params, cleaned = self.read()
        self.assertEqual(len(cleaned), len(samples))
        self.assertGreater(max(abs(value) for value in cleaned[-480:]), .10)
        # One complete leading frame must not gain the tail voice.
        self.assertLess(max(abs(value) for value in cleaned[:vf.FRAME]), .001)

    def test_existing_destination_and_source_are_never_overwritten(self):
        self.write(self.fixture(seconds=.2))
        before = self.source.read_bytes()
        self.destination.write_bytes(b"existing recording")
        with self.assertRaises(FileExistsError):
            vf.process_wave(self.source, self.destination, threading.Event())
        self.assertEqual(self.destination.read_bytes(), b"existing recording")
        self.assertEqual(self.source.read_bytes(), before)

    def test_cancel_during_native_processing_cleans_only_own_destination(self):
        self.write(self.fixture())
        original = self.source.read_bytes()
        cancelled = threading.Event()
        original_run = vf._Highpass.run
        count = 0
        def run(instance, sample):
            nonlocal count
            count += 1
            if count >= 2000:
                cancelled.set()
            return original_run(instance, sample)
        with mock.patch.object(vf._Highpass, "run", run):
            with self.assertRaises(vf.FilterCancelled):
                vf.process_wave(self.source, self.destination, cancelled)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.source.read_bytes(), original)

    def test_missing_library_fails_before_creating_output(self):
        self.write([0.0] * 1001)
        with mock.patch.object(vf.ctypes.util, "find_library", return_value=None):
            with self.assertRaises(vf.FilterUnavailable):
                vf.process_wave(self.source, self.destination, threading.Event())
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
