#!/usr/bin/env python3
"""Platform/build contract tests. Requires Compose + PyYAML; never accesses the Docker daemon."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def clean_env(**overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('CAM_', 'RIG_', 'COMPOSE_'))
           and k not in ('BASE_IMAGE', 'WEBRTC_BASE', 'GST_RS_TAG', 'ROS_DISTRO',
                         'IMAGES', 'PUSH', 'PLATFORM_FLAG', 'L4T_MULTIMEDIA', 'L4T_VERSION')}
    return env | overrides


class DevDefaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cam_defaults_')
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'sensor.yaml'
        self.config.write_text(json.dumps({
            'service': 'camera-service', 'name': 'defaults_test',
            'camera': {'type': 'gige'}, 'gige': {'fake': True},
            'plugins': [{'name': name, 'enabled': True, 'isolation': 'container'}
                        for name in ('ros2-bridge', 'webrtc-bridge')],
        }))

    def render(self, flag=None, **env):
        cmd = [str(REPO / 'cam-up')]
        if flag:
            cmd.append(flag)
        cmd += [str(self.config), 'config', '--format', 'json']
        result = subprocess.run(cmd, cwd=REPO, env=clean_env(**env),
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)['services']

    def assert_dev(self, services, tag='dev', prefix=''):
        for name, image in (('core-driver', 'cam-dev'), ('webrtc-bridge', 'webrtc-bridge'),
                            ('ros2-bridge', 'ros2-bridge')):
            self.assertEqual(services[name]['image'], f'{prefix}{image}:{tag}')
        for name in ('core-driver', 'webrtc-bridge'):
            self.assertEqual(services[name]['runtime'], 'runc')
            self.assertEqual(services[name]['build']['target'], 'modern')
        self.assertEqual(services['core-driver']['build']['args']['BASE'], 'ubuntu:26.04')
        self.assertEqual(services['webrtc-bridge']['build']['args']['BASE_IMAGE'], 'ubuntu:26.04')
        self.assertEqual(services['webrtc-bridge']['build']['args']['GST_RS_TAG'], '0.15.3')
        for name in ('webrtc-bridge', 'ros2-bridge'):
            self.assertEqual(services[name]['environment']['CAM_PLATFORM'], 'dev')
            self.assertEqual(services[name]['environment']['CAM_TRANSPORT'], 'unixfd')

    def test_dev_entry_points_and_tags(self):
        cases = [('--dev', {}, 'dev', ''),
                 (None, {'CAM_PLATFORM': 'dev'}, 'dev', ''),
                 (None, {'RIG_TARGET_PLATFORM': 'dev'}, 'dev', ''),
                 (None, {'RIG_IMAGE_TAG': 'dev'}, 'dev', ''),
                 (None, {'RIG_TARGET_PLATFORM': 'dev', 'RIG_IMAGE_TAG': 'v1.4.0-dev'}, 'v1.4.0-dev', ''),
                 ('--dev', {'RIG_IMAGE_TAG': 'v1.4.0'}, 'v1.4.0-dev', ''),
                 ('--dev', {'RIG_IMAGE_REGISTRY': 'registry.example:5000',
                            'RIG_IMAGE_TAG': 'v1.4.0'}, 'v1.4.0-dev', 'registry.example:5000/'),
                 ('--dev', {'CAM_IMAGE_TAG': 'pinned'}, 'pinned', '')]
        for flag, env, tag, prefix in cases:
            with self.subTest(flag=flag, env=env):
                self.assert_dev(self.render(flag, **env), tag, prefix)

    def test_explicit_compatibility_overrides(self):
        svc = self.render('--dev', CAM_DEV_IMAGE='cam-dev:legacy',
                          CAM_WEBRTC_IMAGE='webrtc-bridge:legacy', CAM_ROS2_IMAGE='ros2-bridge:legacy',
                          CAM_TRANSPORT='shm', CAM_NETWORK='host', CAM_DEV_TARGET='distro',
                          CAM_DEV_BASE='ubuntu:22.04', CAM_WEBRTC_TARGET='runtime',
                          CAM_WEBRTC_BASE='ubuntu:24.04', CAM_GST_RS_TAG='0.13.7')
        for name, image in (('core-driver', 'cam-dev'), ('webrtc-bridge', 'webrtc-bridge'),
                            ('ros2-bridge', 'ros2-bridge')):
            self.assertEqual(svc[name]['image'], f'{image}:legacy')
            self.assertEqual(svc[name]['network_mode'], 'host')
        self.assertEqual(svc['core-driver']['build']['target'], 'distro')
        self.assertEqual(svc['core-driver']['build']['args']['BASE'], 'ubuntu:22.04')
        self.assertEqual(svc['webrtc-bridge']['build']['target'], 'runtime')
        self.assertEqual(svc['webrtc-bridge']['build']['args']['GST_RS_TAG'], '0.13.7')
        for name in ('ros2-bridge', 'webrtc-bridge'):
            self.assertEqual(svc[name]['environment']['CAM_TRANSPORT'], 'shm')

    def test_vehicle_platforms(self):
        for platform, base, runtime in (('jp6', 'nvcr.io/nvidia/l4t-base:r36.2.0', 'nvidia'),
                                        ('jp7', 'ubuntu:24.04', 'runc'),
                                        ('jp6m', 'ubuntu:26.04', 'nvidia')):
            with self.subTest(platform=platform):
                svc = self.render(f'--{platform}', RIG_IMAGE_REGISTRY='registry.example:5000')
                self.assertEqual(svc['core-driver']['image'], f'registry.example:5000/cam-core:{platform}')
                self.assertEqual(svc['core-driver']['runtime'], runtime)
                self.assertEqual(svc['core-driver']['build']['args']['BASE_IMAGE'], base)
                self.assertNotEqual(svc['webrtc-bridge']['build'].get('target'), 'modern')
                self.assertEqual(svc['ros2-bridge']['environment']['CAM_PLATFORM'], platform)

    def test_raw_compose_includes_ros_source(self):
        result = subprocess.run(['docker', 'compose', '-f', 'docker-compose.yml', '-f',
                                 'docker-compose.dev.yml', '--profile', '*', 'config', '--format', 'json'],
                                cwd=REPO, env=clean_env(), text=True, capture_output=True, check=True)
        svc = json.loads(result.stdout)['services']
        self.assert_dev(svc)
        self.assertEqual(svc['ros2-source']['image'], 'ros2-bridge:dev')

    def test_build_matrix(self):
        # Capture build commands at the CLI boundary; no builds, pushes, or registry access.
        fake = Path(self.tmp.name) / 'docker'
        fake.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                        'with open(os.environ["CAM_TEST_COMMANDS"], "a") as out:\n'
                        '    out.write(json.dumps(sys.argv[1:]) + "\\n")\n')
        fake.chmod(0o755)
        commands = Path(self.tmp.name) / 'commands.jsonl'
        for platform in ('dev', 'jp6', 'jp7', 'jp6m'):
            with self.subTest(platform=platform):
                commands.write_text('')
                env = clean_env(PATH=f'{self.tmp.name}:{os.environ["PATH"]}', PUSH='0',
                                CAM_TEST_COMMANDS=str(commands))
                subprocess.run(['bash', 'tools/build-images.sh', 'registry.example:5000', f'v1-{platform}'],
                               cwd=REPO, env=env, capture_output=True, check=True)
                calls = [json.loads(line) for line in commands.read_text().splitlines()]
                self.assertTrue(all(call[0] == 'build' for call in calls))
                by_image = {call[call.index('-t') + 1].split('/')[-1].split(':')[0]: call for call in calls}
                web = by_image['webrtc-bridge']
                self.assertEqual(web[web.index('--target') + 1], 'modern' if platform == 'dev' else 'runtime')
                if platform == 'dev':
                    self.assertEqual(set(by_image), {'cam-dev', 'ros2-bridge', 'webrtc-bridge'})
                    core = by_image['cam-dev']
                    self.assertEqual(core[core.index('--target') + 1], 'modern')
                    self.assertIn('BASE=ubuntu:26.04', core)
                    self.assertIn('BASE_IMAGE=ubuntu:26.04', web)
                    self.assertIn('GST_RS_TAG=0.15.3', web)
                else:
                    self.assertIn('cam-core', by_image)
                    self.assertNotIn('cam-dev', by_image)


if __name__ == '__main__':
    unittest.main(verbosity=2)
