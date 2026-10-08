"""What this computer has: its CPU, cores, memory and GPUs. Read once; shown in Settings and used for defaults."""
import functools
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from . import config

VENDORS = {"0x10de": "NVIDIA GPU", "0x1002": "AMD GPU", "0x8086": "Intel GPU"}  # PCI vendor ids


def _run(cmd, timeout=20):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _mac():
    cores = _run(["sysctl", "-n", "hw.perflevel0.physicalcpu"]) or _run(["sysctl", "-n", "hw.physicalcpu"])
    cpu = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Mac"
    memory = _run(["sysctl", "-n", "hw.memsize"])
    apple = cpu.startswith("Apple")
    return {"cpu": cpu, "performance_cores": int(cores) if cores.isdigit() else None,
            "memory": int(memory) if memory.isdigit() else None,
            "gpus": [f"{cpu} GPU"] if apple else [], "neural_engine": apple}


def _windows():
    script = ("$p = @(Get-CimInstance Win32_Processor); $m = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory; "
              "@{cpu = $p[0].Name; cores = ($p | Measure-Object NumberOfCores -Sum).Sum; memory = $m; "
              "gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name })} | ConvertTo-Json -Compress")
    try:
        info = json.loads(_run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script]) or "{}")
    except ValueError:
        info = {}
    gpus = [g for g in info.get("gpus") or [] if g and "Basic Display" not in g]
    return {"cpu": (info.get("cpu") or "").strip() or None, "performance_cores": info.get("cores"),
            "memory": info.get("memory"), "gpus": gpus, "neural_engine": False}


def _linux():
    lscpu = {k.strip(): v.strip() for k, v in (line.split(":", 1) for line in _run(["lscpu"]).splitlines() if ":" in line)}
    cpu = lscpu.get("Model name", "")
    if cpu in ("", "-"):  # some ARM systems and VMs name no model
        cpu = f"{lscpu.get('Vendor ID', '')} {platform.machine()} processor".strip()
    cores = {(Path(d, "topology/physical_package_id").read_text().strip(), Path(d, "topology/core_id").read_text().strip())
             for d in Path("/sys/devices/system/cpu").glob("cpu[0-9]*") if Path(d, "topology/core_id").exists()}
    memory = next((int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                   if line.startswith("MemTotal:")), None)
    nvidia = [g for g in _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).splitlines() if g]
    others = []
    if shutil.which("lspci"):  # "00:02.0 VGA compatible controller: Intel Corporation ... [Iris Xe Graphics] (rev 0c)"
        for line in _run(["lspci"]).splitlines():
            kind, _, name = line.partition(": ")
            if any(k in kind for k in ("VGA", "3D", "Display")) and not (nvidia and "NVIDIA" in name):
                others.append(name.split(" (rev")[0])
    else:  # no lspci: just the makers of the GPUs in use
        vendors = sorted({p.read_text().strip() for p in Path("/sys/class/drm").glob("card*/device/vendor")})
        others = [VENDORS[v] for v in vendors if v in VENDORS and not (nvidia and v == "0x10de")]
    return {"cpu": cpu, "performance_cores": len(cores) or None, "memory": memory, "gpus": nvidia + others,
            "neural_engine": False}


@functools.lru_cache(maxsize=1)
def summary():
    """{"cpu", "cores" (logical), "performance_cores", "memory" (bytes), "gpus", "neural_engine"}."""
    try:
        info = {"mac": _mac, "windows": _windows, "linux": _linux}[config.PLATFORM]()
    except Exception:  # an odd system mustn't stop the app: Settings just shows less
        info = {"cpu": None, "performance_cores": None, "memory": None, "gpus": [], "neural_engine": False}
    return {**info, "cores": os.cpu_count()}


def auto_threads():
    """CPU threads when Settings says Automatic. On a Mac Whisper runs on the GPU or Neural Engine, so one per
    performance core (an M1 has 4; 4 beat 6 there). Elsewhere it may run on the CPU: one per core, up to 8."""
    cores = summary()["performance_cores"] or max(1, (os.cpu_count() or 8) // 2)
    return max(2, min(cores, 8))
