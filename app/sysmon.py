"""Memory readings for a small NAS (a DS925+ with 6 GB fills its swap easily).

* The NAS as a whole: RAM and swap from /proc/meminfo. Inside a container this file still shows the host,
  which is what we want: Claude should not start while DSM itself is short of memory.
* This container: usage, limit (MEM_LIMIT in docker-compose), peak and OOM kills, from cgroup v2
  (memory.current / memory.max / memory.peak / memory.events) or cgroup v1 (memory.usage_in_bytes ...).
* One Claude run: the CLI's own peak (VmHWM, the kernel's high-water mark) plus the current memory of
  everything it started (/proc/<pid>/status), sampled while it runs.

Every reader returns None when its files are missing (a laptop, an unusual kernel), and nothing here
raises: memory readings must never break a job.
"""
from __future__ import annotations

from pathlib import Path

MB = 1024 * 1024
UNLIMITED = 1 << 60  # cgroup v1 reports "no limit" as a huge number such as 9223372036854771712

PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _number(path: Path) -> int | None:
    text = _read(path)
    if not text or text == "max":
        return None
    try:
        return int(text.split()[0])
    except ValueError:
        return None


def _pairs(path: Path) -> dict[str, int]:
    """`key value` lines: memory.stat, memory.events, memory.oom_control."""
    found = {}
    for line in (_read(path) or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                found[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return found


def host_memory(meminfo: Path = PROC / "meminfo") -> dict | None:
    values = {}
    for line in (_read(meminfo) or "").splitlines():
        key, _, rest = line.partition(":")
        try:
            values[key] = int(rest.split()[0]) * 1024  # the file is in kB
        except (ValueError, IndexError):
            continue
    total = values.get("MemTotal")
    if not total:
        return None
    available = values.get("MemAvailable")
    if available is None:  # kernels before 3.14
        available = values.get("MemFree", 0) + values.get("Cached", 0) + values.get("Buffers", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_used = max(0, swap_total - values.get("SwapFree", 0))
    return {
        "total_mb": total // MB,
        "available_mb": available // MB,
        "used_pct": round(100 * (total - available) / total),
        "swap_total_mb": swap_total // MB,
        "swap_used_mb": swap_used // MB,
        "swap_used_pct": round(100 * swap_used / swap_total) if swap_total else None,
    }


def container_memory(root: Path = CGROUP, host_total_mb: int | None = None) -> dict | None:
    """This container's own memory. `used_mb` leaves out file cache the kernel can drop at any time,
    the same way `docker stats` counts it."""
    if (root / "memory.current").exists():  # cgroup v2
        version, usage, limit = 2, _number(root / "memory.current"), _number(root / "memory.max")
        peak = _number(root / "memory.peak")  # kernel 5.19+
        inactive = _pairs(root / "memory.stat").get("inactive_file", 0)
        oom_kills = _pairs(root / "memory.events").get("oom_kill")
        swap = _number(root / "memory.swap.current")
    elif (root / "memory" / "memory.usage_in_bytes").exists():  # cgroup v1 (DSM 7.2 uses this)
        base = root / "memory"
        version, usage, limit = 1, _number(base / "memory.usage_in_bytes"), _number(base / "memory.limit_in_bytes")
        peak = _number(base / "memory.max_usage_in_bytes")
        stat = _pairs(base / "memory.stat")
        inactive = stat.get("total_inactive_file", stat.get("inactive_file", 0))
        oom_kills = _pairs(base / "memory.oom_control").get("oom_kill")
        with_swap = _number(base / "memory.memsw.usage_in_bytes")
        swap = max(0, with_swap - usage) if with_swap is not None and usage is not None else None
    else:
        return None
    if usage is None:
        return None
    if limit is not None and (limit >= UNLIMITED or (host_total_mb and limit // MB >= host_total_mb)):
        limit = None  # no MEM_LIMIT set: the container may use all of the NAS
    return {
        "cgroup": version,
        "used_mb": max(0, usage - inactive) // MB,
        "limit_mb": limit // MB if limit else None,
        "peak_mb": peak // MB if peak else None,
        "oom_kills": oom_kills,
        "swap_mb": swap // MB if swap is not None else None,
    }


def pressure(path: Path = PROC / "pressure" / "memory") -> dict | None:
    """Share of time tasks waited for memory (Linux PSI), when the kernel provides it."""
    found = {}
    for line in (_read(path) or "").splitlines():
        kind, *fields = line.split()
        for field in fields:
            key, _, value = field.partition("=")
            if key == "avg60":
                try:
                    found[f"{kind}_avg60"] = float(value)
                except ValueError:
                    pass
    return found or None


def snapshot() -> dict:
    host = host_memory()
    return {
        "host": host,
        "container": container_memory(host_total_mb=host["total_mb"] if host else None),
        "pressure": pressure(),
    }


def _status_kb(pid: int, proc: Path, fields: tuple[str, ...] = ("VmRSS",)) -> int | None:
    """The largest of the given /proc/<pid>/status fields, in kB (None if the process is gone)."""
    found = None
    for line in (_read(proc / str(pid) / "status") or "").splitlines():
        key, _, rest = line.partition(":")
        if key in fields:
            try:
                found = max(found or 0, int(rest.split()[0]))
            except (ValueError, IndexError):
                continue
    return found


def process_tree_mb(pid: int, proc: Path = PROC, root_peak: bool = False) -> int | None:
    """Resident memory of a process and all its descendants, in MB (None once the process is gone).
    With `root_peak`, the process itself counts at its high-water mark (VmHWM), so its true peak shows
    even if it happened between two samples. Walking /proc is cheap inside a container, where only a
    handful of processes exist."""
    children: dict[int, list[int]] = {}
    try:
        entries = [e for e in proc.iterdir() if e.name.isdigit()]
    except OSError:
        return None
    for entry in entries:
        stat = _read(entry / "stat")
        if not stat:
            continue
        try:
            parent = int(stat.rsplit(")", 1)[1].split()[1])  # field 4; the name in (...) may contain spaces
        except (IndexError, ValueError):
            continue
        children.setdefault(parent, []).append(int(entry.name))
    root = _status_kb(pid, proc, ("VmRSS", "VmHWM") if root_peak else ("VmRSS",))
    if root is None:
        return None
    total, todo, seen = root, list(children.get(pid, [])), {pid}
    while todo:
        child = todo.pop()
        if child in seen:
            continue
        seen.add(child)
        total += _status_kb(child, proc) or 0
        todo += children.get(child, [])
    return total // 1024
