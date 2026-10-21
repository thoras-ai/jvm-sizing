"""Byte formatting and quantity parsing utilities.

No imports from other project modules — safe to import anywhere.
"""
import argparse
import math
import re

_MiB: int = 1024 ** 2
_GiB: int = 1024 ** 3
_300MiB: int = 300 * _MiB


def fmt_bytes(b: int) -> str:
    """Human-readable byte count: GiB, MiB, KiB, or B."""
    if b >= _GiB:
        return f"{b / _GiB:.2f} GiB"
    if b >= _MiB:
        return f"{b / _MiB:.0f} MiB"
    if b >= 1024:
        return f"{b / 1024:.0f} KiB"
    return f"{int(b)} B"


def fmt_jvm_flag_mem(b: int) -> str:
    """Format bytes as a JVM flag value: whole GiB as Ng, otherwise Nm."""
    gib = b / _GiB
    if gib == int(gib) and gib >= 1:
        return f"{int(gib)}g"
    return f"{int(b / _MiB)}m"


def fmt_k8s_mem(b: int) -> str:
    """Format bytes as a Kubernetes memory quantity (e.g. 4Gi, 512Mi)."""
    gib = b / _GiB
    if gib == int(gib) and gib >= 1:
        return f"{int(gib)}Gi"
    return f"{math.ceil(b / _MiB)}Mi"


def parse_jvm_mem_size(value_str: str, unit_str: str) -> int:
    """Convert a JVM -Xmx/-Xms value+unit pair to bytes (K/M/G = 1024-base)."""
    v = int(value_str)
    u = unit_str.lower()
    return v * {'g': _GiB, 'm': _MiB, 'k': 1024}.get(u, 1)


def parse_k8s_quantity(s: str) -> float:
    """Convert a Kubernetes quantity string to bytes."""
    s = s.strip().strip('"').strip("'")
    m = re.match(r'^([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)$', s)
    if not m:
        raise ValueError(f"Cannot parse Kubernetes quantity: {s!r}")
    value, unit = float(m.group(1)), m.group(2)
    suffixes: dict[str, float] = {
        'Ki': 1024, 'Mi': _MiB, 'Gi': _GiB, 'Ti': 1024 ** 4,
        'K':  1000, 'M':  1000 ** 2, 'G':  1000 ** 3, 'T':  1000 ** 4,
        'm':  0.001, '': 1,
    }
    if unit not in suffixes:
        raise ValueError(f"Unknown Kubernetes quantity suffix: {unit!r}")
    return value * suffixes[unit]


def parse_jvm_bytes(s: str) -> float:
    """Convert jcmd output quantities or raw bytes to bytes.

    K/M/G use 1024-base (JVM convention, matching jcmd GC.heap_info).
    """
    s = s.strip().strip('"').strip("'")
    m = re.match(r'^([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)$', s)
    if not m:
        raise ValueError(f"Cannot parse JVM quantity: {s!r}")
    value, unit = float(m.group(1)), m.group(2)
    suffixes: dict[str, float] = {
        'K':  1024, 'M':  1024 ** 2, 'G':  1024 ** 3, 'T':  1024 ** 4,
        'Ki': 1024, 'Mi': _MiB,      'Gi': _GiB,       'Ti': 1024 ** 4,
        '': 1,
    }
    if unit not in suffixes:
        raise ValueError(f"Unknown JVM quantity suffix: {unit!r}")
    return value * suffixes[unit]


def k8s_bytes(s: str) -> int:
    """argparse type: Kubernetes quantity string → bytes."""
    try:
        return int(parse_k8s_quantity(s))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def jvm_bytes(s: str) -> int:
    """argparse type: jcmd quantity string or raw bytes → bytes."""
    try:
        return int(parse_jvm_bytes(s))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
