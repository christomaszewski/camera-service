#!/usr/bin/env python3
"""Per-thread CPU of a running process over an interval, read from /proc (run on the host).

    thread_cpu.py <pid> <seconds> [frames per second, for a ms/frame column]

For the real core or bridge: `thread_cpu.py $(docker inspect -f '{{.State.Pid}}' <container>) 20 24` when
the container's PID 1 is the process itself, else the PID from `pgrep -f main.py` / `bridge_stream.py`."""
import os, sys, time
pid, secs = int(sys.argv[1]), float(sys.argv[2])
fps = float(sys.argv[3]) if len(sys.argv) > 3 else 0
CLK = os.sysconf("SC_CLK_TCK")
def snap():
    out = {}
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            s = open(f"/proc/{pid}/task/{tid}/stat").read()
            comm = s[s.index("(") + 1:s.rindex(")")]
            f = s[s.rindex(")") + 2:].split()
            out[int(tid)] = (comm, int(f[11]), int(f[12]))
        except (OSError, ValueError):
            pass
    return out
a = snap(); t0 = time.monotonic(); time.sleep(secs); b = snap(); dt = time.monotonic() - t0
agg = {}
for tid, (comm, u, s) in b.items():
    pu, ps = a.get(tid, (comm, 0, 0))[1:]
    x = agg.setdefault(comm, [0, 0, 0]); x[0] += u - pu; x[1] += s - ps; x[2] += 1
tot = sum(v[0] + v[1] for v in agg.values())
print(f"pid {pid}: {tot / CLK / dt * 100:.1f}% of one core over {dt:.1f}s" + (f" = {tot / CLK / dt * 1000 / fps:.2f} ms/frame at {fps:g} fps" if fps else ""))
for comm, (u, s, n) in sorted(agg.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
    if u + s == 0:
        continue
    pct = (u + s) / CLK / dt * 100
    line = f"  {comm:18s} x{n:<2d} {pct:6.1f}%  (user {u / CLK / dt * 100:5.1f} sys {s / CLK / dt * 100:5.1f})"
    if fps:
        line += f"  {pct * 10 / fps:6.2f} ms/frame"
    print(line)
