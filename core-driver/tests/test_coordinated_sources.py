"""Real decoder/PCAP readiness: no preview before commit, no first-frame loss afterward."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from coordination_fixture import Coordination
from test_replay_source import _source, _write_session, _pump, _px, T0, IV, Gst, GLib
from cam_driver.config import parse_config
from cam_driver.sources.pcapsrc import PcapSource
from uvcpcap_fixture import build_y16_capture, build_mjpeg_capture, make_jpeg


def pump():
    while GLib.MainContext.default().iteration(False):pass

class CoordinatedSources(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.control=Coordination(self.root/'control')
        self.control.env.start();self.addCleanup(self.control.env.stop)

    def exercise(self, source, *, expected_count, expected_raw=None, expected_encoded=None, abort=False,
                 expected_ids=None, minimum_lead_s=0):
        self.addCleanup(source.stop)
        raw=[];encoded=[]
        source.start(lambda st,data:raw.append((st,bytes(data),time.time())),
                     lambda st,data,caps=None:encoded.append((st,bytes(data),time.time())))
        self.control.wait('held',pump)
        self.assertEqual(raw,[]);self.assertEqual(encoded,[])
        self.assertFalse(source.playback.request('resume')['ok'])
        time.sleep(.15)
        self.assertEqual(raw,[]);self.assertEqual(encoded,[])
        if abort:
            self.control.command('abort');self.control.wait('aborted',pump)
            _pump(lambda:source.finished,timeout_s=3)
            self.assertTrue(source.finished_error)
            self.assertEqual(raw,[]);self.assertEqual(encoded,[])
            return
        at=self.control.release(pump)
        self.assertEqual(raw,[]);self.assertEqual(encoded,[])
        _pump(lambda:source.finished,timeout_s=5)
        self.assertFalse(source.finished_error,self.control.status())
        if isinstance(source,PcapSource) and source._mjpeg:
            self.assertEqual(self.control.status()['state'],'completed')
        self.control.wait('completed',pump)
        self.assertEqual([st.frame_id for st,_,_ in raw],
                         list(range(expected_count)) if expected_ids is None else expected_ids)
        self.assertTrue(all(wall>=at for _,_,wall in raw+encoded))
        self.assertGreaterEqual(raw[0][2]-at,minimum_lead_s)
        if expected_raw is not None:self.assertEqual([data for _,data,_ in raw],expected_raw)
        if expected_encoded is not None:
            self.assertEqual([data for _,data,_ in encoded],expected_encoded)
        self.assertEqual(source.playback.descriptor()['frames'],expected_count)

    def test_ffv1_decoder_ready_and_first_frame_retained(self):
        run=self.root/'run';run.mkdir();_write_session(run,'cam',1,T0,6)
        self.exercise(_source(run,speed=1,epoch_unix_ns=T0),expected_count=6,
                      expected_raw=[_px(1,i) for i in range(6)])

    def test_ffv1_abort_held_never_publishes_preview(self):
        run=self.root/'run';run.mkdir();_write_session(run,'cam',1,T0,6)
        self.exercise(_source(run,speed=1),expected_count=6,abort=True)

    def test_window_preserves_first_selected_frame_and_remaining_lead(self):
        run=self.root/'run';run.mkdir();_write_session(run,'cam',1,T0,6)
        self.exercise(_source(run,speed=1,epoch_unix_ns=T0,from_s=.05,to_s=.16),
                      expected_count=2,expected_ids=[2,3],minimum_lead_s=.025,
                      expected_raw=[_px(1,i) for i in (2,3)])

    def pcap(self,mjpeg):
        path=self.root/'fixture.pcap'
        payload,expected=(build_mjpeg_capture(frames=6) if mjpeg else
                          build_y16_capture(frames=6,width=64,height=8))
        path.write_bytes(payload)
        cfg=parse_config({'camera':{'type':'pcap'},'pcap':{'path':str(path),
            'pixel_format':'MJPEG' if mjpeg else 'GRAY16_LE','width':32 if mjpeg else 64,
            'height':24 if mjpeg else 8,'speed':1,'loop':False},'control':{'enabled':False}})
        source=PcapSource(cfg.pcap,cfg.playback);source.open();source.configure()
        self.exercise(source,expected_count=len(expected),expected_raw=None if mjpeg else [data for _,data in expected],
                      expected_encoded=[data for _,data in expected] if mjpeg else None)

    def test_raw_pcap_first_frame_retained(self):self.pcap(False)
    def test_mjpeg_pcap_decoder_ready_before_release(self):self.pcap(True)

    def slow_pcap(self,frame_period_ns=40_000_000):
        path=self.root/'slow.pcap'
        payload,expected=build_mjpeg_capture(frames=3,frame_period_ns=frame_period_ns)
        path.write_bytes(payload)
        cfg=parse_config({'camera':{'type':'pcap'},'pcap':{'path':str(path),
            'pixel_format':'MJPEG','width':32,'height':24,'speed':.1,'loop':False},
            'control':{'enabled':False}})
        source=PcapSource(cfg.pcap,cfg.playback);source.open()
        return source,expected

    def test_slow_mjpeg_drain_keeps_scheduled_frames_after_inactivity_budget(self):
        source,expected=self.slow_pcap();source.configure()
        # Each real decoder callback waits .4s for its next frame, exceeding this
        # decoder inactivity budget. All three frames must still drain in order.
        with patch('cam_driver.sources.pcapsrc.DRAIN_STALL_TIMEOUT_S',.15):
            self.exercise(source,expected_count=3,
                          expected_encoded=[data for _,data in expected])

    def test_mjpeg_drain_still_fails_when_no_eos_or_output_progress_remains(self):
        source,_=self.slow_pcap()
        source._on_mini_eos=lambda *_:None  # A decoder/pipeline that never confirms drain.
        source.configure();self.addCleanup(source.stop)
        raw=[]
        with patch('cam_driver.sources.pcapsrc.DRAIN_STALL_TIMEOUT_S',.15):
            source.start(lambda st,data:raw.append(st.frame_id))
            self.control.wait('held',pump);self.control.release(pump)
            _pump(lambda:source.finished,timeout_s=3)
        self.assertEqual(raw,[0,1,2])
        self.assertTrue(source.finished_error)
        self.assertEqual(self.control.wait('failed')['state'],'failed')

    def test_abort_interrupts_long_paced_mjpeg_drain(self):
        source,_=self.slow_pcap(frame_period_ns=1_000_000_000)
        source.configure();self.addCleanup(source.stop)
        raw=[]
        source.start(lambda st,data:raw.append(st.frame_id))
        self.control.wait('held',pump);self.control.release(pump)
        _pump(lambda:bool(raw),timeout_s=1)
        self.control.command('abort');self.control.wait('aborted',pump)
        started=time.monotonic();source.stop()
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(raw,[0])
        self.assertTrue(source.finished_error)

    def test_encoded_replay_decoder_ready_before_release(self):
        run=self.root/'run';run.mkdir()
        base=_write_session(run,'cam',1,T0,6,width=32,height=24)
        path=Path(str(base)+'-00000.mkv')
        pipeline=Gst.parse_launch(f'appsrc name=src format=time caps="image/jpeg,framerate=25/1" ! '
                                 f'jpegparse ! splitmuxsink muxer=matroskamux max-size-time=0 '
                                 f'location="{base}-%05d.mkv"')
        pipeline.set_state(Gst.State.PLAYING)
        frames=[make_jpeg(i) for i in range(6)]
        for i,data in enumerate(frames):
            buffer=Gst.Buffer.new_wrapped(data);buffer.pts=i*IV;buffer.duration=IV
            self.assertEqual(pipeline.get_by_name('src').emit('push-buffer',buffer),Gst.FlowReturn.OK)
        pipeline.get_by_name('src').emit('end-of-stream')
        msg=pipeline.get_bus().timed_pop_filtered(5*Gst.SECOND,Gst.MessageType.EOS|Gst.MessageType.ERROR)
        pipeline.set_state(Gst.State.NULL)
        self.assertEqual(msg.type,Gst.MessageType.EOS)
        header=json.loads(Path(str(base)+'.json').read_text());header['recording_encoder']='stream-copy'
        header['pixel_format']='I420'
        Path(str(base)+'.json').write_text(json.dumps(header))
        self.exercise(_source(run,speed=1,epoch_unix_ns=T0),expected_count=6,expected_encoded=frames)

if __name__=='__main__':unittest.main()
