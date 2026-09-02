"""Per-thread CPU sampling from /proc, for the health heartbeat (Linux; pure stdlib).

Answers "where does this process's CPU go?" straight from `docker logs`, without `top -H` on the
vehicle: GStreamer names its streaming threads after the element (`videoconvert0:src`,
`nvv4l2h265enc0`, `avenc_ffv1-0`...), so a top-N-by-CPU line of thread names IS a per-element
breakdown -- convert vs encoder vs the Python feeder -- for free. Linux truncates a thread name to
15 characters (TASK_COMM_LEN), so long element names arrive clipped; still unambiguous in practice.

Usage: keep one ThreadCpu per process and call tick() once per heartbeat; it returns the busiest
threads over the interval since the previous tick (percent of ONE core, so a 4-thread videoconvert
can read 250%). The baseline is taken at construction, so the FIRST heartbeat already carries a
breakdown (of the startup interval). On a non-Linux host it returns nothing.
"""
import os
import time

_TASK_DIR = "/proc/self/task"


def parse_stat(line: str):
    """(comm, cpu_ticks) from one /proc/<pid>/task/<tid>/stat line, or None. comm sits in parens
    and may itself contain spaces and parens, so split at the LAST ')'; utime/stime are fields 14
    and 15 (1-based), i.e. the 12th/13th after the comm."""
    i = line.rfind(")")
    j = line.find("(")
    if i < 0 or j < 0 or i < j:
        return None
    comm = line[j + 1:i]
    rest = line[i + 2:].split()
    try:
        return comm, int(rest[11]) + int(rest[12])
    except (IndexError, ValueError):
        return None


def sample_threads():
    """{tid: (comm, cpu_ticks)} for every thread of this process; {} where /proc is unavailable."""
    out = {}
    try:
        tids = os.listdir(_TASK_DIR)
    except OSError:
        return out
    for tid in tids:
        try:
            with open(os.path.join(_TASK_DIR, tid, "stat")) as f:
                parsed = parse_stat(f.read())
        except OSError:
            continue                       # the thread exited between listdir and open
        if parsed is not None:
            out[int(tid)] = parsed
    return out


class ThreadCpu:
    def __init__(self, top: int = 6, sampler=sample_threads, clock_hz=None, now: float = None):
        self.top = top
        self._sample = sampler
        self._hz = clock_hz or (os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100)
        self._prev = None
        self._t = None
        self.tick(now)                     # prime: the first heartbeat spans construction -> tick

    def tick(self, now: float = None):
        """(total_percent, [(comm, percent), ...]) over the interval since the previous tick: the
        process total and its `top` busiest threads, percent of one core. None without /proc (no
        line at all); (0.0, []) when the process was simply idle."""
        now = time.monotonic() if now is None else now
        cur = self._sample()
        if not cur:
            self._prev, self._t = None, now
            return None
        total, rows = 0.0, []
        if self._prev is not None:
            dt = now - self._t
            if dt > 0:
                for tid, (comm, ticks) in cur.items():
                    prev = self._prev.get(tid)
                    if prev is None:
                        continue           # new thread: no baseline this interval
                    d = ticks - prev[1]
                    if d > 0:
                        pct = 100.0 * d / self._hz / dt
                        total += pct
                        if pct >= 1.0:             # sub-1% threads are noise, not a breakdown
                            rows.append((comm, pct))
                rows.sort(key=lambda r: -r[1])
                rows = rows[:self.top]
        self._prev, self._t = cur, now
        return total, rows


def format_top(result) -> str:
    """One log segment from tick(): `cpu=187% [videoconvert0:s 71% nvv4l2h265enc0 40% python3 22%]`,
    `cpu=0%` for an idle process, "" where there is no /proc to read."""
    if result is None:
        return ""
    total, rows = result
    if not rows:
        return "cpu=%.0f%%" % total
    return "cpu=%.0f%% [%s]" % (total, " ".join("%s %.0f%%" % (c, p) for c, p in rows))
