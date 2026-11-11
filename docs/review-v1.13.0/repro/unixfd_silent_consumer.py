"""Reproduces DP-5 (docs/review-v1.13.0/02-core-data-path.md): the core's unixfd transport chain (the same
launch fragment and memfd publish as cam_driver/pipeline.py:353-356 and 676-694) against ONE client that
connects and then never reads -- a hung or SIGSTOPped bridge.

    python3 docs/review-v1.13.0/repro/unixfd_silent_consumer.py [frame_bytes=1000000] [frames=3000]

Needs GStreamer >= 1.24 (unixfdsink) + PyGObject. Expected today: ~295 frames are accepted and their
memfds stay pinned by the sink before the appsrc's 8-frame bound produces the first drop.
"""
import sys, os, socket, time, tempfile
sys.dont_write_bytecode = True
import gi
gi.require_version("Gst", "1.0"); gi.require_version("GstAllocators", "1.0")
from gi.repository import Gst, GstAllocators
Gst.init(None)
FRAME = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000          # bytes per frame
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
d = tempfile.mkdtemp()
path = os.path.join(d, "unixfd")
caps = "video/x-raw,format=GRAY8,width=1000,height=%d,framerate=30/1" % (FRAME // 1000)
p = Gst.parse_launch(f'appsrc name=unixfd_src is-live=true do-timestamp=false format=time max-bytes={8 * FRAME} '
                     f'caps="{caps}" ! queue max-size-buffers=8 ! unixfdsink name=unixfd_sink socket-path={path} sync=false')
src = p.get_by_name("unixfd_src")
p.set_state(Gst.State.PLAYING)
alloc = GstAllocators.FdAllocator.new()
time.sleep(0.3)
c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); c.connect(path)     # the stuck consumer: never recv()s
time.sleep(0.3)

def shmem_kb():
    for line in open("/proc/self/status"):
        if line.startswith("RssShmem"):
            return int(line.split()[1])
def nfds(): return len(os.listdir("/proc/self/fd"))

def shm_now():
    return [int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("Shmem:")][0]
SH0 = shm_now()
payload = bytes(FRAME)
accepted = dropped = 0
fd0, sh0 = nfds(), shmem_kb()
first_drop_at = None
for i in range(N):
    # pipeline._queue_full
    if src.get_property("current-level-bytes") + FRAME > src.get_property("max-bytes"):
        dropped += 1
        if first_drop_at is None:
            first_drop_at = i
            print(f"first publish drop at frame {i}: accepted={accepted} open fds +{nfds()-fd0} RssShmem +{(shmem_kb()-sh0)/1024:.0f} MiB")
        time.sleep(0.002)
        continue
    fd = os.memfd_create("cam", 0); os.ftruncate(fd, FRAME); os.pwrite(fd, payload, 0)
    mem = GstAllocators.FdAllocator.alloc(alloc, fd, FRAME, GstAllocators.FdMemoryFlags.NONE)
    b = Gst.Buffer.new(); b.insert_memory(-1, mem); b.pts = i * 33_000_000; b.offset = i; b.offset_end = 1
    if src.emit("push-buffer", b) == Gst.FlowReturn.OK:
        accepted += 1
    del b, mem
    time.sleep(0.001)
print(f"pushed {N}: accepted={accepted} dropped={dropped}; frames pinned by the sink for the silent client ~= open fds +{nfds()-fd0}; "
      f"RssShmem +{(shmem_kb()-sh0)/1024:.0f} MiB (frame = {FRAME/1e6:.1f} MB)")
links = [os.readlink(f"/proc/self/fd/{f}") for f in os.listdir("/proc/self/fd") if os.path.exists(f"/proc/self/fd/{f}")]
m = [l for l in links if "memfd:cam" in l]
print(f"memfd:cam fds still open in the core: {len(m)} -> {len(m)*FRAME/1e6:.0f} MB of tmpfs pinned; meminfo Shmem delta = {(shm_now()-SH0)/1024:.0f} MiB")
os._exit(0)
