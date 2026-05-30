"""Connect to the patched GV fake camera, enable chunks, and read ChunkTimestamp +
ChunkFrameID off each buffer. Validates the chunk-emitter patch end-to-end over GVSP."""
import sys

import gi
gi.require_version("Aravis", "0.8")
from gi.repository import Aravis  # noqa: E402

Aravis.update_device_list()
ids = [Aravis.get_device_id(i) for i in range(Aravis.get_n_devices())]
print("devices:", ids)
if not ids:
    print("NO CAMERA FOUND")
    sys.exit(1)

cam = Aravis.Camera.new(None)
print("opened:", cam.get_vendor_name(), cam.get_model_name())
try:
    cam.set_chunk_mode(True)
    cam.set_chunks("Timestamp,FrameID")
    print("chunk mode enabled")
except Exception as e:  # noqa: BLE001
    print("chunk enable FAILED:", e)
    sys.exit(2)

parser = cam.create_chunk_parser()
payload = cam.get_payload()
print(f"payload: {payload}  (image 512x512=262144, +32 chunk bytes => expect 262176)")

stream = cam.create_stream(None, None)
for _ in range(20):
    stream.push_buffer(Aravis.Buffer.new_allocate(payload))
cam.start_acquisition()

ok = 0
for _ in range(5):
    buf = stream.timeout_pop_buffer(3_000_000)
    if buf is None:
        print("  [no buffer]")
        continue
    if buf.get_status() == Aravis.BufferStatus.SUCCESS:
        ts = parser.get_integer_value(buf, "ChunkTimestamp")
        fid = parser.get_integer_value(buf, "ChunkFrameID")
        print(f"  frame_id={buf.get_frame_id()} has_chunks={buf.has_chunks()} "
              f"ChunkTimestamp={ts} ChunkFrameID={fid} buffer_ts={buf.get_timestamp()}")
        ok += 1
    else:
        print("  status:", buf.get_status())
    stream.push_buffer(buf)
cam.stop_acquisition()

print(f"{'OK' if ok else 'FAILED'} ({ok} frames with chunks)")
sys.exit(0 if ok else 3)
