"""Diagnostics package loading, container discovery, and metrics parsing."""
import json
import os
import zipfile
from pathlib import Path
from typing import Any


class DiagnosticsPackage:
    """Read files from a diagnostics zip or extracted directory.

    Handles both single-AST exports (files at root) and multi-AST exports
    (files in namespace-name/ subdirectories).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._is_zip = os.path.isfile(self._path) and zipfile.is_zipfile(self._path)
        self._index: list[str] | None = None

    def _zip_index(self) -> list[str]:
        if self._index is None:
            with zipfile.ZipFile(self._path) as zf:
                self._index = zf.namelist()
        return self._index

    def _find_in_zip(self, filename: str) -> list[str]:
        matches = [
            n for n in self._zip_index()
            if n.endswith(f"/{filename}") or n == filename
        ]
        return sorted(matches, key=len)

    def read(self, filename: str) -> bytes | None:
        """Return file contents by name, or None if not found."""
        if self._is_zip:
            matches = self._find_in_zip(filename)
            if not matches:
                return None
            with zipfile.ZipFile(self._path) as zf:
                with zf.open(matches[0]) as f:
                    return f.read()
        for root, _, files in os.walk(self._path):
            if filename in files:
                with open(os.path.join(root, filename), 'rb') as f:
                    return f.read()
        return None

    def find_all(self, filename: str) -> list[str]:
        """Return all paths matching filename, shortest first."""
        if self._is_zip:
            return sorted(
                [n for n in self._zip_index() if n.endswith(f"/{filename}") or n == filename],
                key=len,
            )
        results = []
        for root, _, files in os.walk(self._path):
            if filename in files:
                results.append(os.path.join(root, filename))
        return sorted(results, key=len)


def discover_containers(data: bytes) -> list[str]:
    """Return sorted list of container names present in metrics.json."""
    hits = json.loads(data).get('hits', [])
    containers: set[str] = set()
    for hit in hits:
        for c in (hit.get('containers') or []):
            name = c.get('name', '')
            if name:
                containers.add(name)
    return sorted(containers)


def parse_metrics(
    data: bytes,
    container_name: str | None = None,
) -> tuple[list[str], list[float], dict[str, Any]]:
    """Parse metrics.json and return (timestamps, mem_values, meta).

    timestamps  : ISO-8601 strings, one per eligible snapshot
    mem_values  : per-pod average memory bytes (adjustedAverageValue)
    meta        : spec_memory, containers, total_hits, total_replicas
    """
    hits = json.loads(data).get('hits', [])
    timestamps: list[str] = []
    mem_values: list[float] = []
    spec_mem: float | None = None
    containers_seen: set[str] = set()

    for hit in hits:
        ts = hit.get('timestamp')
        if not ts:
            continue

        # Track spec_memory from the first occurrence in the target container's metrics.
        src = (
            (_container_metrics(hit, container_name) or [])
            if container_name else (hit.get('metrics') or [])
        )
        for m in src:
            if m.get('name', '').lower() == 'memory' and m.get('specRequest') is not None:
                spec_mem = m['specRequest']

        for c in (hit.get('containers') or []):
            containers_seen.add(c.get('name', ''))

        if hit.get('eligiblePodCount') == 0:
            continue

        metrics = (
            _container_metrics(hit, container_name)
            if container_name else (hit.get('metrics') or [])
        )
        if container_name and metrics is None:
            continue

        mem: float | None = None
        for m in (metrics or []):
            if m.get('name', '').lower() == 'memory':
                adj = m.get('adjustedAverageValue')
                mem = adj if adj is not None else m.get('averageValue')

        if mem is not None:
            timestamps.append(ts)
            mem_values.append(mem)

    total_replicas: int | None = None
    for hit in reversed(hits):
        tr = hit.get('totalReplicas')
        if tr is not None:
            total_replicas = tr
            break

    meta: dict[str, Any] = {
        'spec_memory': spec_mem,
        'containers': sorted(containers_seen),
        'total_hits': len(hits),
        'total_replicas': total_replicas,
    }
    return timestamps, mem_values, meta


def _container_metrics(hit: dict, container_name: str) -> list[dict] | None:
    """Return the metrics list for the named container within a hit, or None."""
    target = next(
        (c for c in (hit.get('containers') or []) if c.get('name') == container_name),
        None,
    )
    return target.get('metrics', []) if target else None


def percentile(data: list[float], p: float) -> float:
    """Linear-interpolation percentile over a list of values."""
    if not data:
        return 0.0
    sd = sorted(data)
    n = len(sd)
    k = (p / 100.0) * (n - 1)
    lo = int(k)
    hi = lo + 1
    if hi >= n:
        return float(sd[lo])
    return sd[lo] + (k - lo) * (sd[hi] - sd[lo])


def compute_stats(values: list[float]) -> dict[str, float]:
    """Return p50/p90/p99/min/max/mean/count for a list of values."""
    if not values:
        return {}
    n = len(values)
    return {
        'p50':  percentile(values, 50),
        'p90':  percentile(values, 90),
        'p99':  percentile(values, 99),
        'min':  min(values),
        'max':  max(values),
        'mean': sum(values) / n,
        'count': n,
    }
