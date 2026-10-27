"""Atomic file-protocol fixture; no Docker, network or external coordinator required."""
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

class Coordination:
    def __init__(self, directory, *, timeout=10):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'statuses').mkdir(exist_ok=True)
        self.identity = dict(version=1, session_id='a' * 32, plan_id='b' * 64)
        self.participant = 'fixture.camera'
        self.seq = 0
        self.write('session.json', dict(self.identity, participants=[self.participant],
                                       startup_deadline_unix_s=time.time() + timeout))
        self.command('hold')
        self.env = patch.dict(os.environ, RIG_REPLAY_CONTROL_DIR=str(self.root),
                              RIG_REPLAY_SESSION_ID=self.identity['session_id'],
                              RIG_REPLAY_PLAN_ID=self.identity['plan_id'],
                              RIG_REPLAY_PARTICIPANT='fixture')

    def write(self, name, value):
        path = self.root / name
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(value))
        tmp.replace(path)

    def command(self, op, **fields):
        self.seq += 1
        self.write('command.json', dict(self.identity, seq=self.seq, op=op, **fields))

    def status(self):
        path = self.root / 'statuses' / (self.participant + '.json')
        return json.loads(path.read_text()) if path.is_file() else {}

    def wait(self, state, pump=None, timeout=4):
        start = time.monotonic()
        while self.status().get('state') != state:
            if pump:
                pump()
            if time.monotonic() - start > timeout:
                raise AssertionError((state, self.status()))
            time.sleep(.005)
        return self.status()

    def release(self, pump=None):
        self.wait('held', pump)
        self.command('arm')
        self.wait('ready', pump)
        at = time.time() + .5
        self.command('prepare_release', release_at_unix_s=at)
        self.wait('release_ack', pump)
        self.command('commit', release_at_unix_s=at)
        return at
