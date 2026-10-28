#!/usr/bin/env python3
"""Platform/build contract tests. Requires Compose + PyYAML; never accesses the Docker daemon."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil
import yaml

REPO = Path(__file__).resolve().parents[1]


def clean_env(**overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('CAM_', 'RIG_', 'COMPOSE_'))
           and k not in ('BASE_IMAGE', 'WEBRTC_BASE', 'GST_RS_TAG', 'ROS_DISTRO', 'RMW_IMPLEMENTATION',
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

    def render_all(self, platform='dev', **env):
        # Include ros2-source as well as the publishing bridge; either can build their shared image.
        overlay = 'jp6' if platform == 'jp6m' else platform
        result = subprocess.run(['docker', 'compose', '-f', 'docker-compose.yml', '-f',
                                 f'docker-compose.{overlay}.yml', '--profile', '*',
                                 'config', '--format', 'json'], cwd=REPO,
                                env=clean_env(CAM_PLATFORM=platform, **env),
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)['services']

    def build_commands(self, platform='dev', **overrides):
        # Capture arguments at the Docker CLI boundary without a daemon, build, or push.
        fake = Path(self.tmp.name) / 'docker'
        fake.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                        'with open(os.environ["CAM_TEST_COMMANDS"], "a") as out:\n'
                        '    out.write(json.dumps(sys.argv[1:]) + "\\n")\n')
        fake.chmod(0o755)
        commands = Path(self.tmp.name) / 'commands.jsonl'
        commands.write_text('')
        env = clean_env(PATH=f'{self.tmp.name}:{os.environ["PATH"]}', PUSH='0',
                        CAM_TEST_COMMANDS=str(commands), **overrides)
        subprocess.run(['bash', 'tools/build-images.sh', 'registry.example:5000', f'v1-{platform}'],
                       cwd=REPO, env=env, capture_output=True, check=True)
        return [json.loads(line) for line in commands.read_text().splitlines()]

    @staticmethod
    def build_args(command):
        return dict(command[i + 1].split('=', 1) for i, arg in enumerate(command)
                    if arg == '--build-arg')

    @staticmethod
    def by_image(commands):
        return {call[call.index('-t') + 1].split('/')[-1].split(':')[0]: call for call in commands}

    def assert_dev(self, services, tag='dev', prefix=''):
        for name, image in (('core-driver', 'cam-dev'), ('webrtc-bridge', 'webrtc-bridge'),
                            ('ros2-bridge', 'ros2-bridge')):
            self.assertEqual(services[name]['image'], f'{prefix}{image}:{tag}')
            self.assertEqual(services[name]['network_mode'], 'host')
        for name in ('core-driver', 'webrtc-bridge'):
            self.assertEqual(services[name]['runtime'], 'runc')
            self.assertEqual(services[name]['build']['target'], 'modern')
        self.assertEqual(services['core-driver']['build']['args']['BASE'], 'ubuntu:26.04')
        self.assertEqual(services['webrtc-bridge']['build']['args']['BASE_IMAGE'], 'ubuntu:26.04')
        self.assertEqual(services['webrtc-bridge']['build']['args']['GST_RS_TAG'], '0.15.4')
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

    def test_dev_replay_network_defaults_and_overrides(self):
        config = json.loads(self.config.read_text())
        config['camera'] = {'type': 'replay'}
        config['replay'] = {'path': self.tmp.name}
        self.config.write_text(json.dumps(config))
        selectors = [('--dev', {}),
                     (None, {'CAM_PLATFORM': 'dev'}),
                     (None, {'RIG_TARGET_PLATFORM': 'dev'}),
                     (None, {'RIG_IMAGE_TAG': 'dev'})]
        for flag, selector in selectors:
            for network, expected in ((None, 'host'), ('', 'host'),
                                      ('host', 'host'), ('bridge', 'bridge')):
                with self.subTest(flag=flag, selector=selector, network=network):
                    env = dict(selector)
                    if network is not None:
                        env['CAM_NETWORK'] = network
                    services = self.render(flag, **env)
                    for name in ('core-driver', 'ros2-bridge', 'webrtc-bridge'):
                        self.assertEqual(services[name]['network_mode'], expected, name)

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
                self.assertEqual(svc['webrtc-bridge']['build']['args']['GST_RS_TAG'], '0.15.4')
                self.assertEqual(svc['ros2-bridge']['environment']['CAM_PLATFORM'], platform)

    def test_raw_compose_includes_ros_source(self):
        svc = self.render_all()
        self.assert_dev(svc)
        self.assertEqual(svc['ros2-source']['image'], 'ros2-bridge:dev')

    def test_compose_shared_ros_base_does_not_change_media_bases(self):
        base = 'registry.example:5000/fleet-ros@sha256:' + 'a' * 64
        for platform in ('dev', 'jp6', 'jp7', 'jp6m'):
            with self.subTest(platform=platform):
                baseline = self.render_all(platform)
                services = self.render_all(platform, RIG_BASE_IMAGE=base,
                                           RIG_ROS_RMW='rmw_cyclonedds_cpp')
                for name in ('ros2-bridge', 'ros2-source'):
                    args = services[name]['build']['args']
                    self.assertEqual(args['BASE_IMAGE'], base)
                    self.assertEqual(args['ROS_DISTRO'], 'lyrical')
                    self.assertEqual(args['ROS_RMW'], 'rmw_cyclonedds_cpp')
                for name in ('core-driver', 'webrtc-bridge', 'ros1-bridge'):
                    self.assertEqual(services[name]['build'], baseline[name]['build'], name)

    def test_compose_ros_base_fallbacks_and_middleware_precedence(self):
        cases = [({}, 'lyrical', 'rmw_zenoh_cpp'),
                 ({'RIG_BASE_IMAGE': '', 'ROS_DISTRO': 'jazzy'}, 'jazzy', 'rmw_zenoh_cpp'),
                 ({'CAM_ROS_RMW': 'rmw_cyclonedds_cpp'}, 'lyrical', 'rmw_cyclonedds_cpp'),
                 ({'RIG_ROS_RMW': 'rmw_fastrtps_cpp', 'CAM_ROS_RMW': 'rmw_cyclonedds_cpp'},
                  'lyrical', 'rmw_fastrtps_cpp'),
                 ({'RIG_ROS_RMW': '', 'CAM_ROS_RMW': 'rmw_cyclonedds_cpp'},
                  'lyrical', 'rmw_cyclonedds_cpp'),
                 ({'RIG_ROS_RMW': '', 'CAM_ROS_RMW': ''}, 'lyrical', 'rmw_zenoh_cpp')]
        for env, distro, middleware in cases:
            with self.subTest(env=env):
                services = self.render_all(RMW_IMPLEMENTATION='rmw_fastrtps_cpp', **env)
                for name in ('ros2-bridge', 'ros2-source'):
                    args = services[name]['build']['args']
                    self.assertEqual(args['BASE_IMAGE'], f'ros:{distro}-ros-base')
                    self.assertEqual(args['ROS_DISTRO'], distro)
                    self.assertEqual(args['ROS_RMW'], middleware)
                    self.assertEqual(services[name]['environment']['RMW_IMPLEMENTATION'],
                                     'rmw_fastrtps_cpp')

    def test_vendored_dev_uses_image_code(self):
        staging = Path(self.tmp.name) / 'vendored'
        descriptor = yaml.safe_load((REPO / 'rigging.yaml').read_text())
        for rel in descriptor['launch_surface'] + descriptor['examples']:
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        result = subprocess.run([str(staging / 'cam-up'), '--dev', str(self.config),
                                 'config', '--format', 'json'], env=clean_env(),
                                capture_output=True, text=True, check=True)
        core = json.loads(result.stdout)['services']['core-driver']
        self.assertEqual(core['command'], ['supervisor.py'])
        self.assertFalse(any(v['target'] == '/app' for v in core.get('volumes', [])))
        result = subprocess.run([str(staging / 'cam-up'), '--dev', '--source',
                                 str(self.config), 'config'], env=clean_env(),
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('full source checkout', result.stderr)

    def test_explicit_source_mount(self):
        result = subprocess.run([str(REPO / 'cam-up'), '--dev', '--source',
                                 str(self.config), 'config', '--format', 'json'],
                                env=clean_env(), capture_output=True, text=True, check=True)
        core = json.loads(result.stdout)['services']['core-driver']
        self.assertTrue(any(v['target'] == '/app' for v in core['volumes']))

    def test_build_matrix(self):
        for platform in ('dev', 'jp6', 'jp7', 'jp6m'):
            with self.subTest(platform=platform):
                calls = self.build_commands(platform)
                self.assertTrue(all(call[0] == 'build' for call in calls))
                by_image = self.by_image(calls)
                web = by_image['webrtc-bridge']
                self.assertEqual(web[web.index('--target') + 1], 'modern' if platform == 'dev' else 'runtime')
                if platform == 'dev':
                    self.assertEqual(set(by_image), {'cam-dev', 'ros2-bridge', 'webrtc-bridge'})
                    core = by_image['cam-dev']
                    self.assertEqual(core[core.index('--target') + 1], 'modern')
                    self.assertIn('BASE=ubuntu:26.04', core)
                    self.assertIn('BASE_IMAGE=ubuntu:26.04', web)
                    self.assertIn('GST_RS_TAG=0.15.4', web)
                else:
                    self.assertIn('cam-core', by_image)
                    self.assertNotIn('cam-dev', by_image)

    def test_shared_ros_base_build_matrix(self):
        base = 'registry.example:5000/fleet-ros@sha256:' + 'b' * 64
        for platform in ('dev', 'jp6', 'jp7', 'jp6m'):
            for buildx in (False, True):
                with self.subTest(platform=platform, buildx=buildx):
                    mode = {'RIG_BUILD_PLATFORMS': 'linux/arm64'} if buildx else {}
                    baseline = self.by_image(self.build_commands(platform, **mode))
                    calls = self.build_commands(platform, RIG_BASE_IMAGE=base,
                                                RIG_ROS_RMW='rmw_cyclonedds_cpp', **mode)
                    expected_prefix = ['buildx', 'build'] if buildx else ['build']
                    self.assertTrue(all(call[:len(expected_prefix)] == expected_prefix for call in calls))
                    by_image = self.by_image(calls)
                    args = self.build_args(by_image['ros2-bridge'])
                    self.assertEqual(args['BASE_IMAGE'], base)
                    self.assertEqual(args['ROS_DISTRO'], 'lyrical')
                    self.assertEqual(args['ROS_RMW'], 'rmw_cyclonedds_cpp')
                    for name in set(by_image) - {'ros2-bridge'}:
                        self.assertEqual(self.build_args(by_image[name]),
                                         self.build_args(baseline[name]), name)

    def test_build_ros_base_fallbacks_and_middleware_precedence(self):
        cases = [({}, 'rmw_zenoh_cpp'),
                 ({'RIG_BASE_IMAGE': ''}, 'rmw_zenoh_cpp'),
                 ({'CAM_ROS_RMW': 'rmw_cyclonedds_cpp'}, 'rmw_cyclonedds_cpp'),
                 ({'RIG_ROS_RMW': 'rmw_fastrtps_cpp', 'CAM_ROS_RMW': 'rmw_cyclonedds_cpp'},
                  'rmw_fastrtps_cpp'),
                 ({'RIG_ROS_RMW': '', 'CAM_ROS_RMW': 'rmw_cyclonedds_cpp'}, 'rmw_cyclonedds_cpp'),
                 ({'RIG_ROS_RMW': '', 'CAM_ROS_RMW': ''}, 'rmw_zenoh_cpp')]
        for env, middleware in cases:
            with self.subTest(env=env):
                calls = self.build_commands(IMAGES='ros2-bridge', ROS_DISTRO='jazzy',
                                             RMW_IMPLEMENTATION='rmw_fastrtps_cpp', **env)
                self.assertEqual(len(calls), 1)
                args = self.build_args(calls[0])
                self.assertNotIn('BASE_IMAGE', args)
                self.assertEqual(args['ROS_DISTRO'], 'jazzy')
                self.assertEqual(args['ROS_RMW'], middleware)


if __name__ == '__main__':
    unittest.main(verbosity=2)
