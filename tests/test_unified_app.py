"""Native toolbar/controller regression checks with synthetic audio only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import wave


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "grabador"))
import app
from audio_capture import AudioSession


class UnifiedToolbarTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="toolbar-test-", dir=PROJECT / "tests")
        self.directory = Path(self.scratch.name)
        self.handlers_before = list(app.LOGGER.handlers)
        settings = {
            "GRABADOR_STATE_DIR": str(self.directory / "state"),
            "GRABADOR_OUTPUT_DIR": str(self.directory / "video"),
            "GRABADOR_AUDIO_OUTPUT_DIR": str(self.directory / "audio"),
        }
        with mock.patch.dict(os.environ, settings):
            self.recorder = app.RecorderApp()
        # These are the production GTK widgets, rendered offscreen. There is no
        # GApplication registration, hotkey binding, desktop capture or UI input.
        with mock.patch.object(app.Gtk, "ApplicationWindow",
                               side_effect=lambda **_kwargs: app.Gtk.OffscreenWindow()):
            self.recorder.do_activate()
        self.recorder.open_video = mock.Mock()
        self.recorder.show_error = mock.Mock()
        self.recorder.quit = mock.Mock()
        self.synthetic = mock.patch.object(app, "AudioSession", side_effect=self.make_session)
        self.synthetic.start()
        self.sessions = []
        self.pump()

    def make_session(self, **kwargs):
        kwargs["source_factory"] = "audiotestsrc"
        kwargs["temp_dir"] = self.directory / "pending"
        session = AudioSession(**kwargs)
        self.sessions.append(session)
        return session

    def tearDown(self):
        self.recorder._generation += 1
        self.recorder.clear_audio_start()
        for session in self.sessions:
            session.cancel()
        self.synthetic.stop()
        self.pump()
        self.recorder.toolbar.destroy()
        for handler in tuple(app.LOGGER.handlers):
            if handler not in self.handlers_before:
                app.LOGGER.removeHandler(handler)
                handler.close()
        self.scratch.cleanup()

    def pump(self):
        context = app.GLib.MainContext.default()
        for _ in range(100):
            if not context.pending():
                break
            context.iteration(False)

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if predicate():
                return
            time.sleep(0.005)
        self.fail(f"Controller did not finish: {self.recorder.state}; {self.recorder.show_error.call_args_list}")

    def start_audio(self):
        recorder = self.recorder
        self.assertTrue(recorder.set_mode("audio"))
        recorder._generation += 1
        recorder.change_state("starting")
        recorder.start_audio(recorder._generation, "synthetic-input-not-hardware", None)
        self.wait_for(lambda: recorder.state == "recording")
        return recorder.session

    def test_original_dimensions_and_existing_controls_survive_both_modes(self):
        recorder = self.recorder
        self.assertEqual(tuple(recorder.toolbar.get_size()), (299, 48))
        for mode in ("audio", "video", "audio", "video"):
            self.assertTrue(recorder.set_mode(mode))
            self.pump()
            self.assertEqual(tuple(recorder.toolbar.get_size()), (299, 48))
            self.assertFalse(recorder.icon.audio)
            self.assertTrue(recorder.mode_icon.audio)
            self.assertTrue(recorder.button.get_sensitive())
            self.assertTrue(recorder.mode_button.get_sensitive())
            for button in (recorder.pause_button, recorder.cancel_button,
                           recorder.folder_button, recorder.close_button):
                self.assertTrue(button.get_visible())
                self.assertEqual(button.get_size_request(), (40, 40))
            self.assertTrue(recorder.pause_button.get_sensitive())
            self.assertTrue(recorder.pause_icon.paused)
        self.assertEqual(recorder.timer_label.get_text(), "00:00")

    def test_selectors_only_choose_mode_and_repeated_selection_never_starts(self):
        recorder = self.recorder
        with mock.patch.object(recorder, "begin_selection") as screen, \
                mock.patch.object(recorder, "begin_audio") as audio, \
                mock.patch.object(app, "CaptureSession", side_effect=AssertionError("screen opened")), \
                mock.patch.object(app, "AudioSession", side_effect=AssertionError("microphone opened")):
            recorder.button.clicked()
            recorder.button.clicked()
            screen.assert_not_called()
            audio.assert_not_called()
            recorder.mode_button.clicked()
            self.assertEqual(recorder.mode, "audio")
            recorder.mode_button.clicked()
            self.assertEqual(recorder.mode, "audio")
            screen.assert_not_called()
            audio.assert_not_called()
            recorder.button.clicked()
            self.assertEqual(recorder.mode, "video")
            screen.assert_not_called()
            audio.assert_not_called()
            self.assertEqual(recorder.state, "idle")

    def test_microphone_then_camera_then_play_starts_only_video(self):
        recorder = self.recorder
        with mock.patch.object(recorder, "begin_selection") as screen, \
                mock.patch.object(recorder, "begin_audio") as audio:
            recorder.mode_button.clicked()
            recorder.button.clicked()
            self.assertEqual(recorder.mode, "video")
            screen.assert_not_called()
            audio.assert_not_called()
            recorder.pause_button.clicked()
            screen.assert_called_once_with()
            audio.assert_not_called()

    def test_camera_then_microphone_then_play_starts_only_audio(self):
        recorder = self.recorder
        with mock.patch.object(recorder, "begin_selection") as screen, \
                mock.patch.object(recorder, "begin_audio") as audio:
            recorder.button.clicked()
            recorder.mode_button.clicked()
            self.assertEqual(recorder.mode, "audio")
            screen.assert_not_called()
            audio.assert_not_called()
            recorder.pause_button.clicked()
            audio.assert_called_once_with()
            screen.assert_not_called()

    def test_active_recording_cannot_change_modes(self):
        recorder = self.recorder
        recorder.set_mode("audio")
        for state in ("starting", "recording", "paused", "stopping", "saving", "ready"):
            recorder.change_state(state)
            self.assertFalse(recorder.button.get_sensitive())
            self.assertEqual(recorder.mode_button.get_sensitive(), state not in {"stopping", "saving"})
            self.assertFalse(recorder.set_mode("video"))
            self.assertEqual(recorder.mode, "audio")

    def test_only_selected_camera_stops_and_saves_video(self):
        recorder = self.recorder
        video = recorder.video_output_dir / "synthetic-controller-video.mp4"
        video.write_bytes(b"not a real recording; controller fixture")
        session = mock.Mock(active=True, paused=False)
        session.statistics = {"elapsed_seconds": 1.2}
        session.stop.side_effect = lambda: recorder.completed(video)
        recorder.session = session
        recorder.change_state("recording")
        self.assertTrue(recorder.icon.recording)
        self.assertFalse(recorder.mode_icon.recording)
        self.assertTrue(recorder.button.get_sensitive())
        self.assertFalse(recorder.mode_button.get_sensitive())
        recorder.mode_button.clicked()
        session.stop.assert_not_called()
        self.assertEqual(recorder.state, "recording")
        recorder.button.clicked()
        session.stop.assert_called_once_with()
        self.assertEqual(recorder.state, "idle")
        self.assertEqual(recorder.last_saved, video)
        recorder.open_video.assert_called_once_with(video)

    def test_stop_finalizes_and_saves_only_wav_then_reenables_controls(self):
        recorder = self.recorder
        with mock.patch.object(app, "CaptureSession", side_effect=AssertionError("screen opened")):
            session = self.start_audio()
            self.assertTrue(session.clean_voice)
            self.wait_for(lambda: session.elapsed >= 0.12)
            self.assertFalse(recorder.icon.recording)
            self.assertTrue(recorder.mode_icon.recording)
            self.assertFalse(recorder.button.get_sensitive())
            recorder.button.clicked()
            self.assertEqual(recorder.state, "recording")
            self.assertTrue(session.active)
            recorder.mode_button.clicked()
            self.wait_for(lambda: recorder.state == "idle" and recorder.last_saved is not None)
        saved = recorder.last_saved
        self.assertEqual(saved.parent, recorder.audio_output_dir)
        self.assertEqual(saved.suffix, ".wav")
        self.assertEqual(recorder.saved_by_mode["audio"], saved)
        self.assertIsNone(recorder.saved_by_mode["video"])
        self.assertEqual(list(recorder.video_output_dir.iterdir()), [])
        with wave.open(str(saved), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 3, 48000))
            self.assertGreater(audio.getnframes(), 4000)
        self.assertIsNone(session.pipeline)
        self.assertIsNone(recorder.session)
        self.assertTrue(recorder.mode_button.get_sensitive())
        self.assertTrue(recorder.pause_button.get_sensitive())
        self.assertFalse(recorder.mode_icon.recording)
        recorder.open_video.assert_called_once_with(saved)
        self.assertGreater(recorder.last_elapsed, 0.1)

    def test_pause_resume_button_uses_same_session_and_stop_works_paused(self):
        recorder = self.recorder
        session = self.start_audio()
        self.wait_for(lambda: session.elapsed >= 0.1)
        recorder.pause_button.clicked()
        self.assertEqual(recorder.state, "paused")
        self.assertTrue(recorder.pause_icon.paused)
        self.assertEqual(recorder.pause_button.get_accessible().get_name(), "Reanudar grabación")
        self.assertIs(recorder.session, session)
        recorder.pause_button.clicked()
        self.assertEqual(recorder.state, "recording")
        self.assertFalse(recorder.pause_icon.paused)
        recorder.pause_button.clicked()
        recorder.stop_recording()
        self.wait_for(lambda: recorder.state == "idle")
        self.assertTrue(recorder.last_saved.is_file())

    def test_cancel_only_discards_pending_audio_and_preserves_previous_file(self):
        recorder = self.recorder
        recorder.set_mode("audio")
        previous = self.directory / "previous.wav"
        previous.write_bytes(b"an existing saved recording")
        recorder.last_saved = previous
        recorder.saved_by_mode["audio"] = previous
        session = self.start_audio()
        pending = session.temporary_path
        recorder.cancel_button.clicked()
        self.assertEqual(recorder.state, "idle")
        self.assertIsNone(recorder.session)
        self.assertFalse(pending.exists())
        self.assertEqual(previous.read_bytes(), b"an existing saved recording")
        self.assertEqual(recorder.last_saved, previous)
        recorder.open_video.assert_not_called()
        self.assertEqual(recorder.last_elapsed, 0)

    def test_failed_save_keeps_ready_audio_and_selected_microphone_retries(self):
        recorder = self.recorder
        session = self.start_audio()
        self.wait_for(lambda: session.elapsed >= 0.1)
        with mock.patch.object(session, "save", side_effect=OSError("destination unavailable")):
            recorder.stop_recording()
            self.wait_for(lambda: recorder.state == "ready")
        self.assertEqual(session.state, "ready")
        self.assertTrue(session.temporary_path.is_file())
        self.assertFalse(recorder.button.get_sensitive())
        self.assertTrue(recorder.mode_button.get_sensitive())
        recorder.show_error.assert_called_once()
        recorder.button.clicked()
        self.assertEqual(recorder.state, "ready")
        self.assertTrue(session.temporary_path.is_file())
        recorder.mode_button.clicked()
        self.assertEqual(recorder.state, "idle")
        self.assertTrue(recorder.last_saved.is_file())

    def test_cancelled_discovery_cannot_start_audio_or_replace_new_session(self):
        recorder = self.recorder
        recorder.set_mode("audio")
        recorder._generation += 1
        old = recorder._generation
        recorder.change_state("starting")
        recorder.cancel_recording()
        recorder.start_audio(old, "synthetic-input", None)
        self.assertEqual(recorder.state, "idle")
        self.assertIsNone(recorder.session)
        self.assertEqual(self.sessions, [])
        session = self.start_audio()
        recorder.audio_event(old, recorder.failed, "stale error")
        self.assertIs(recorder.session, session)
        self.assertEqual(recorder.state, "recording")
        recorder.show_error.assert_not_called()

    def test_discovery_rejects_monitor_and_disconnected_inputs(self):
        recorder = self.recorder
        recorder.set_mode("audio")
        sources = [
            {"name": "speakers.monitor", "properties": {"device.class": "monitor"}},
            {"name": "disconnected-headset", "ports": [{"availability": "not available"}]},
            {"name": "muted-input", "mute": True},
            {"name": "digital-microphone", "mute": False,
             "ports": [{"availability": "availability unknown"}]},
        ]
        responses = [subprocess.CompletedProcess([], 0, stdout=json.dumps(sources)),
                     subprocess.CompletedProcess([], 0, stdout="speakers.monitor\n")]
        with mock.patch.object(app.subprocess, "run", side_effect=responses) as run, \
                mock.patch.object(app, "CaptureSession", side_effect=AssertionError("screen opened")):
            recorder.pause_button.clicked()
            self.wait_for(lambda: recorder.state == "recording")
        self.assertEqual(recorder.session.device, "digital-microphone")
        self.assertEqual(run.call_count, 2)

    def test_play_waits_750ms_before_opening_microphone_and_starts_clock_after_wait(self):
        recorder = self.recorder
        recorder.set_mode("audio")
        sources = [{"name": "digital-microphone", "mute": False}]
        responses = [subprocess.CompletedProcess([], 0, stdout=json.dumps(sources)),
                     subprocess.CompletedProcess([], 0, stdout="digital-microphone\n")]
        opened_at = []
        def open_session(**kwargs):
            opened_at.append(app.GLib.get_monotonic_time())
            return self.make_session(**kwargs)
        before = app.GLib.get_monotonic_time()
        with mock.patch.object(app, "AudioSession", side_effect=open_session):
            with mock.patch.object(app.subprocess, "run", side_effect=responses):
                recorder.pause_button.clicked()
                self.wait_for(lambda: recorder.audio_start_source is not None)
            deadline = recorder.audio_start_deadline
            self.assertGreaterEqual(deadline - before, 750_000)
            self.assertEqual(recorder.timer_label.get_text(), "…")
            self.assertEqual(tuple(recorder.toolbar.get_size()), (299, 48))
            self.assertEqual(recorder.state, "starting")
            self.assertIsNone(recorder.session)
            self.assertEqual(self.sessions, [])
            self.assertFalse(recorder.mode_icon.recording)
            self.assertTrue(recorder.cancel_button.get_sensitive())
            # Even a repeated discovery callback cannot bypass the deadline
            # or start a second timer/input.
            source_id = recorder.audio_start_source
            recorder.start_audio(recorder._generation, "digital-microphone", None)
            self.assertEqual(recorder.audio_start_source, source_id)
            self.assertEqual(opened_at, [])
            self.wait_for(lambda: recorder.state == "recording")
        self.assertEqual(len(opened_at), 1)
        self.assertGreaterEqual(opened_at[0], deadline)
        self.assertLess(recorder.last_elapsed, .1)
        self.assertEqual(recorder.timer_label.get_text(), "00:00")
        self.assertIsNone(recorder.audio_start_source)
        self.assertIsNone(recorder.audio_start_deadline)

    def test_cancel_stop_or_close_during_start_delay_never_opens_input(self):
        recorder = self.recorder
        for action in ("cancel_recording", "stop_recording", "request_quit"):
            with self.subTest(action=action):
                recorder.closing = False
                recorder.quit.reset_mock()
                recorder.state = "idle"
                recorder.toolbar.show_all()
                recorder.set_mode("audio")
                sources = [{"name": "digital-microphone", "mute": False}]
                responses = [subprocess.CompletedProcess([], 0, stdout=json.dumps(sources)),
                             subprocess.CompletedProcess([], 0, stdout="digital-microphone\n")]
                with mock.patch.object(app, "VOICE_START_DELAY_MS", 80), \
                        mock.patch.object(app.subprocess, "run", side_effect=responses):
                    recorder.pause_button.clicked()
                    self.wait_for(lambda: recorder.audio_start_source is not None)
                deadline = recorder.audio_start_deadline
                old_generation = recorder._generation
                with mock.patch.object(recorder.toolbar, "hide"):
                    getattr(recorder, action)()
                self.assertIsNone(recorder.audio_start_source)
                self.assertIsNone(recorder.audio_start_deadline)
                self.wait_for(lambda: app.GLib.get_monotonic_time() > deadline + 15_000)
                recorder.start_audio(old_generation, "stale-microphone", None)
                self.assertIsNone(recorder.session)
                self.assertEqual(self.sessions, [])
                if action == "request_quit":
                    recorder.quit.assert_called_once_with()
                else:
                    self.assertEqual(recorder.state, "idle")
                    self.assertEqual(recorder.timer_label.get_text(), "00:00")

    def test_stale_delayed_callback_cannot_erase_current_timer_handle(self):
        recorder = self.recorder
        recorder.set_mode("audio")
        recorder._generation += 1
        recorder.change_state("starting")
        recorder.audio_start_deadline = app.GLib.get_monotonic_time() + 750_000
        recorder.start_audio(recorder._generation, "current-input", None)
        source_id = recorder.audio_start_source
        self.assertIsNotNone(source_id)
        recorder.delayed_audio_start(recorder._generation - 1, "stale-input")
        self.assertEqual(recorder.audio_start_source, source_id)
        self.assertIsNotNone(app.GLib.MainContext.default().find_source_by_id(source_id))
        recorder.cancel_recording()
        self.assertIsNone(app.GLib.MainContext.default().find_source_by_id(source_id))
        self.assertEqual(self.sessions, [])

    def test_waiting_cue_remains_until_input_reports_started(self):
        recorder = self.recorder
        recorder.set_mode("audio")
        recorder.session = mock.Mock(statistics={"elapsed_seconds": 0})
        recorder.change_state("starting")
        self.assertEqual(recorder.timer_label.get_text(), "…")
        self.assertFalse(recorder.mode_icon.recording)
        self.assertFalse(recorder.pause_button.get_sensitive())
        recorder.started()
        self.assertEqual(recorder.timer_label.get_text(), "00:00")
        self.assertTrue(recorder.mode_icon.recording)
        self.assertTrue(recorder.pause_button.get_sensitive())
        recorder.session = None

    def test_close_stops_and_saves_pending_audio_without_opening_player(self):
        recorder = self.recorder
        session = self.start_audio()
        self.wait_for(lambda: session.elapsed >= 0.1)
        # GtkOffscreenWindow has no desktop origin after unmapping. Verify the
        # close request hides it, while keeping the offscreen surface mapped
        # until EOS/controller status updates complete.
        with mock.patch.object(recorder.toolbar, "hide") as hide:
            self.assertTrue(recorder.request_quit())
            self.wait_for(lambda: recorder.quit.called)
            hide.assert_called_once_with()
        self.assertTrue(recorder.last_saved.is_file())
        self.assertTrue(recorder.closing)
        recorder.open_video.assert_not_called()

    def test_close_during_discovery_cancels_late_start(self):
        recorder = self.recorder
        recorder.set_mode("audio")
        recorder._generation += 1
        generation = recorder._generation
        recorder.change_state("starting")
        recorder.request_quit()
        recorder.start_audio(generation, "synthetic-input", None)
        recorder.quit.assert_called_once_with()
        self.assertIsNone(recorder.session)
        self.assertEqual(self.sessions, [])

    def test_saving_and_switching_keep_separate_last_audio_and_video(self):
        recorder = self.recorder
        video = self.directory / "original.mp4"
        video.write_bytes(b"existing original video")
        recorder.last_saved = video
        recorder.saved_by_mode["video"] = video
        session = self.start_audio()
        self.wait_for(lambda: session.elapsed >= 0.1)
        recorder.stop_recording()
        self.wait_for(lambda: recorder.state == "idle")
        audio = recorder.last_saved
        recorder.set_mode("video")
        self.assertEqual(recorder.last_saved, video)
        self.assertEqual(recorder.output_dir, recorder.video_output_dir)
        recorder.set_mode("audio")
        self.assertEqual(recorder.last_saved, audio)
        recorder.write_status()
        status = json.loads((recorder.state_dir / "estado.json").read_text())
        self.assertEqual(status["last_audio"], str(audio))
        self.assertEqual(status["last_video"], str(video))
        self.assertEqual(status["mode"], "audio")


if __name__ == "__main__":
    unittest.main()
