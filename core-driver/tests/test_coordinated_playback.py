"""Coordinated holds are protocol-owned and never leak the ordinary paused preview."""
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cam_driver.playback import FINISHED, PAUSED, PLAYING, STOP, Pacer, PlaybackState
from coordination_fixture import Coordination

class CoordinatedPlayback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.control = Coordination(self.tmp.name)
        self.control.env.start()
        self.addCleanup(self.control.env.stop)
        self.pb = PlaybackState('replay', pacer=Pacer(1, 100), initial_state=PLAYING,
                                start_at_unix_s=time.time() - 10)
        self.addCleanup(self.pb.stop_coordination)

    def test_hold_heartbeat_and_first_frame_wait_for_committed_release(self):
        self.control.wait('preparing')
        self.assertEqual(self.pb.state, PAUSED)
        self.assertFalse(self.pb.take_preroll())
        self.pb.prepared()
        first = self.control.wait('held')
        status_path = self.control.root / 'statuses' / (self.control.participant + '.json')
        self.assertEqual(status_path.stat().st_mode & 0o777, 0o644)
        time.sleep(.22)
        second = self.control.status()
        self.assertGreater(second['updated_at_unix_s'], first['updated_at_unix_s'])
        self.assertEqual(second['held_at_unix_s'], first['held_at_unix_s'])
        for op in ('resume', 'restart', 'set_speed', 'set_loop', 'pause'):
            self.assertFalse(self.pb.request(op)['ok'])
        output=[]
        worker=threading.Thread(target=lambda: output.append((self.pb.wait_if_paused(), time.time())))
        worker.start()
        self.addCleanup(worker.join, 2)
        at=self.control.release()
        self.assertEqual(output, [])
        worker.join(2)
        self.assertEqual(self.pb.state, PLAYING)
        self.assertEqual(len(output), 1)
        self.assertGreaterEqual(output[0][1], at)
        self.pb.mark_finished()
        completed = self.control.wait('completed')
        self.assertEqual(completed['held_at_unix_s'], first['held_at_unix_s'])
        self.assertLessEqual(completed['held_at_unix_s'], completed['ready_at_unix_s'])

    def test_missing_commit_fails_without_release(self):
        self.pb.prepared(); self.control.wait('held')
        self.control.command('arm'); self.control.wait('ready')
        self.control.command('prepare_release',release_at_unix_s=time.time()+.4)
        self.control.wait('release_ack'); self.control.wait('failed')
        self.assertEqual(self.pb.wait_if_paused(),STOP)
        self.assertIsNotNone(self.pb.coordination_error)

    def test_abort_while_held_and_stop_are_terminal(self):
        self.pb.prepared(); self.control.wait('held')
        self.control.command('abort')
        self.control.wait('aborted')
        deadline=time.monotonic()+2
        while self.pb.coordination_error is None and time.monotonic()<deadline:time.sleep(.005)
        self.assertEqual(self.pb.wait_if_paused(),STOP)
        self.pb.stop_coordination()
        self.assertEqual(self.control.status()['state'],'aborted')

    def test_source_error_reports_failed(self):
        self.pb.mark_finished('decoder unavailable')
        self.assertIn('decoder unavailable',str(self.control.wait('failed')))
        self.assertEqual(self.pb.state, FINISHED)

    def test_packaged_client_matches_canonical_protocol_hash(self):
        root=Path(__file__).resolve().parents[2]
        expected=(root/'tools/replay_coordination.sha256').read_text().split()[0]
        actual=hashlib.sha256((root/'core-driver/cam_driver/replay_coordination.py').read_bytes()).hexdigest()
        self.assertEqual(actual,expected,'Update the shared infra client and its packaged copy together')

if __name__=='__main__':unittest.main()
