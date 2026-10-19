#!/usr/bin/env python3
"""Exercise the source extractor without apt, Docker or GNU tar."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = (ROOT / 'tools/gstreamer/build.sh').read_text().split('export DEBIAN_FRONTEND=', 1)[0]


class SourceArchives(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'extracted'
        self.output.mkdir()
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'python3').symlink_to(sys.executable)
        blocked_tar = bin_dir / 'tar'
        blocked_tar.write_text('#!/bin/sh\nexit 97\n')
        blocked_tar.chmod(0o755)
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])

    def extract(self, members):
        archive = self.root / 'source with spaces.tar.xz'
        with tarfile.open(archive, 'w:xz') as output:
            for member, content in members:
                output.addfile(member, io.BytesIO(content) if content else None)
        return subprocess.run(['bash', '-c', FUNCTIONS + '\nextract_archive "$1"',
                               'extract-test', str(archive)], cwd=self.output,
                              env=self.env, capture_output=True, text=True)

    def test_nested_sources_executable_and_relative_link(self):
        content = b'#!/bin/sh\necho source\n'
        member = tarfile.TarInfo('source/tools/nested/build.sh')
        member.mode = 0o755
        member.size = len(content)
        link = tarfile.TarInfo('source/tools/build-link.sh')
        link.type = tarfile.SYMTYPE
        link.linkname = 'nested/build.sh'
        result = self.extract([(member, content), (link, None)])
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self.output / member.name
        self.assertEqual(extracted.read_bytes(), content)
        self.assertTrue(extracted.stat().st_mode & 0o100)
        self.assertEqual((self.output / link.name).read_bytes(), content)

    def test_parent_path_is_rejected(self):
        member = tarfile.TarInfo('../escaped')
        member.size = 4
        result = self.extract([(member, b'nope')])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'escaped').exists())

    def test_link_outside_destination_is_rejected(self):
        link = tarfile.TarInfo('unsafe-link')
        link.type = tarfile.SYMTYPE
        link.linkname = '../outside'
        result = self.extract([(link, None)])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.output / link.name).is_symlink())


if __name__ == '__main__':
    unittest.main()
