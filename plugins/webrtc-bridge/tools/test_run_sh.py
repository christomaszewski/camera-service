"""run.sh decides what the bridge runs: the transport's source chain, the bitrate floor, the queue the idle
gate holds, where a shared encoder goes. CAM_DRY_RUN=1 makes it print the pipeline it assembled and stop
before waiting for, starting or opening anything, so those decisions can be read here.

Run in cam-dev (the shared-encoder cases ask this container's GStreamer which encoder it has).
"""
import os
from pathlib import Path
import subprocess

PLUGIN = Path(__file__).resolve().parents[1]
_OURS = ("CAM_", "VIDEO_CAPS", "SIGNALLING_PORT", "RUN_SIGNALLING", "GST_PLUGIN_FEATURE_RANK", "GST_DEBUG")


def _dry(**env):
    """run.sh with only `env` of the bridge's own settings: (summary line, pipeline, stderr)."""
    base = {k: v for k, v in os.environ.items() if not k.startswith(_OURS)}
    result = subprocess.run(["bash", "run.sh"], cwd=PLUGIN, env={**base, "CAM_DRY_RUN": "1", **env},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    summary = [line for line in lines if "-> webrtcsink (signalling" in line]
    pipeline = [line.split("pipeline: ", 1)[1] for line in lines if line.startswith("webrtc-bridge: pipeline: ")]
    assert len(summary) == 1 and len(pipeline) == 1, result.stdout
    return summary[0], pipeline[0], result.stderr


_TRANSPORTS = ({"CAM_PLATFORM": "jp7"},                 # unixfd
               {"CAM_PLATFORM": "jp6"},                 # headered shm
               {"CAM_TRANSPORT": "shm-raw"})            # the legacy raw endpoint


def test_a_dry_run_starts_nothing_and_describes_each_transport():
    sources = ("unixfdsrc name=cam_src socket-path=/tmp/cam/unixfd", "shmsrc name=cam_src socket-path=/tmp/cam/frames",
               "shmsrc name=cam_src socket-path=/tmp/cam/raw")
    for env, source in zip(_TRANSPORTS, sources):
        summary, pipeline, err = _dry(**env)            # no socket exists, and it did not wait for one
        assert pipeline.startswith(source) and pipeline.rstrip().endswith("min-bitrate=500000")
        assert "webrtcsink name=cam_webrtcsink signaller::uri=ws://127.0.0.1:8443" in pipeline
        assert "no socket at" not in err


def test_the_bitrate_floor_is_500_kbit_unless_configured_otherwise():
    for env in _TRANSPORTS:
        summary, pipeline, _ = _dry(**env)
        assert " min-bitrate=500000" in pipeline and summary.endswith("min-bitrate 500000)")
        assert "max-bitrate" not in pipeline and "start-bitrate" not in pipeline       # the element's own


def test_the_floor_never_sits_above_a_configured_start_or_max_bitrate():
    floor = lambda **env: _dry(CAM_PLATFORM="jp7", **env)[1].split(" min-bitrate=")[1].split()[0]    # noqa: E731
    assert floor(CAM_WEBRTC_START_BITRATE="300000") == "300000"
    assert floor(CAM_WEBRTC_MAX_BITRATE="400000") == "400000"
    assert floor(CAM_WEBRTC_MAX_BITRATE="200000", CAM_WEBRTC_START_BITRATE="300000") == "200000"
    assert floor(CAM_WEBRTC_START_BITRATE="200000", CAM_WEBRTC_MAX_BITRATE="300000") == "200000"
    assert floor(CAM_WEBRTC_START_BITRATE="6000000", CAM_WEBRTC_MAX_BITRATE="20000000") == "500000"
    assert floor(CAM_WEBRTC_START_BITRATE="500000") == "500000"
    assert floor(CAM_WEBRTC_START_BITRATE="not-a-number") == "500000"                  # not this script's to judge
    _, pipeline, _ = _dry(CAM_PLATFORM="jp7", CAM_WEBRTC_START_BITRATE="300000", CAM_WEBRTC_MAX_BITRATE="2000000")
    assert pipeline.rstrip().endswith("min-bitrate=300000 max-bitrate=2000000 start-bitrate=300000")


def test_an_explicit_floor_is_taken_as_given():
    for value in ("1000", "250000", "800000"):
        summary, pipeline, _ = _dry(CAM_PLATFORM="jp7", CAM_WEBRTC_MIN_BITRATE=value, CAM_WEBRTC_START_BITRATE="300000")
        assert f" min-bitrate={value} " in pipeline and summary.endswith(f"min-bitrate {value})")


def test_every_pipeline_has_the_one_queue_the_idle_gate_holds():
    queue = "queue name=ingress_q leaky=downstream max-size-buffers=2 ! "
    for env in (*_TRANSPORTS, {"CAM_PLATFORM": "jp7", "CAM_WEBRTC_NORMALIZE": "auto"},
                {"CAM_TRANSPORT": "shm-raw", "CAM_BAYER": "rggb"}, {"CAM_PLATFORM": "jp7", "CAM_WEBRTC_MAX_SIZE": "1280"},
                {"CAM_TRANSPORT": "shm-raw", "CAM_LAUNCHER": "gst-launch"}):
        _, pipeline, _ = _dry(**env)
        assert pipeline.count("name=ingress_q") == 1 and queue in pipeline, env
        # it is the head of the chain: nothing that costs CPU per frame sits in front of it
        head = pipeline.split(queue)[0]
        assert not any(el in head for el in ("videoconvert", "bayer2rgb", "videoscale", "textoverlay", "x264enc")), env


def test_encoding_is_per_viewer_unless_shared_is_asked_for():
    for env in ({}, {"CAM_WEBRTC_ENCODE": "per-viewer"}, {"CAM_WEBRTC_ENCODE": ""}):
        summary, pipeline, _ = _dry(CAM_TRANSPORT="shm-raw", **env)
        assert "shared_enc" not in pipeline and "(shared encoder)" not in summary
        assert "video/x-raw,format=I420 ! webrtcsink name=cam_webrtcsink" in pipeline


def test_a_shared_encoder_goes_between_the_last_raw_stage_and_the_sink():
    summary, pipeline, err = _dry(CAM_TRANSPORT="shm-raw", CAM_WEBRTC_ENCODE="Shared", CAM_WEBRTC_BITRATE="1500000")
    assert "(shared encoder)" in summary and err == ""
    assert "video/x-raw,format=I420 ! queue name=shared_enc_q leaky=downstream max-size-buffers=2 ! " in pipeline
    assert " name=shared_enc bitrate=1500" in pipeline                  # kbit/s on x264enc, bit/s on the hardware
    tail = pipeline.split(" name=shared_enc ")[1]
    assert ("capsfilter name=shared_enc_caps" in tail and "h264parse config-interval=-1 ! capssetter join=true" in tail
            and tail.split(" ! ")[-1].startswith("webrtcsink name=cam_webrtcsink"))
    # the overlay burns into the raw frame, so it comes first; the fragment follows the start bitrate
    _, pipeline, _ = _dry(CAM_PLATFORM="jp7", CAM_WEBRTC_ENCODE="shared", CAM_WEBRTC_LATENCY_OVERLAY="true",
                          CAM_WEBRTC_START_BITRATE="2500000")
    assert pipeline.index("textoverlay") < pipeline.index("name=shared_enc_q") < pipeline.index("webrtcsink")
    assert " name=shared_enc bitrate=2500" in pipeline
    # and the normalize pipeline's second half is where it goes there
    _, pipeline, _ = _dry(CAM_PLATFORM="jp7", CAM_WEBRTC_ENCODE="shared", CAM_WEBRTC_NORMALIZE="auto")
    assert pipeline.index("appsrc name=norm_out") < pipeline.index("name=shared_enc_q") < pipeline.index("webrtcsink")


def test_a_shared_encoder_that_cannot_be_had_falls_back_to_per_viewer_and_says_so():
    for env, why in (({"VIDEO_CAPS": "video/x-vp8"}, "is not H.264 or H.265"),
                     ({"VIDEO_CAPS": "video/x-h265"}, "no h265 encoder to share"),
                     ({"CAM_LAUNCHER": "gst-launch"}, "needs the python launcher")):
        summary, pipeline, err = _dry(CAM_TRANSPORT="shm-raw", CAM_WEBRTC_ENCODE="shared", **env)
        assert "shared_enc" not in pipeline and "(shared encoder)" not in summary, env
        assert why in err and "encoding per viewer" in err, (env, err)
        assert "video/x-raw,format=I420 ! webrtcsink name=cam_webrtcsink" in pipeline
