"""Pure sizing calculations, risk profiles, and typed output models.

All functions are side-effect-free. No printing, no sys.exit, no formatting.
"""
import math
from dataclasses import dataclass
from typing import Any

from jvm_sizing.units import _MiB, _300MiB

# Risk profile constants — the policy multipliers Thoras uses by default.
PROFILE_GC_MULT:   dict[str, float] = {'savings': 2.0, 'balanced': 2.5, 'reliability': 3.0}
PROFILE_WS_MARGIN: dict[str, float] = {'savings': 1.10, 'balanced': 1.20, 'reliability': 1.30}


# ── Output models ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MemoryStats:
    p50: float
    p90: float
    p99: float
    max: float


@dataclass(frozen=True)
class SizingRecommendation:
    """Raw sizing recommendation values — all formatting happens in console.py."""
    basis: str             # 'measurements' | 'working_set'
    xmx: int
    xms: int
    container_limit: int
    max_ram_pct: float
    init_ram_pct: float
    # JVM-measured path fields (None on working_set path)
    live_set: int | None = None
    metaspace: int | None = None
    overhead: int | None = None
    headroom: int | None = None
    gc_mult: float | None = None
    profile_name: str | None = None
    # Working-set path fields (None on measurements path)
    p99: float | None = None
    obs_max: float | None = None
    profile_margin: float | None = None
    current_xmx_in: int | None = None
    current_limit_in: int | None = None
    xmx_constraint: str | None = None        # 'overhead_gap' | '80pct_cap' | 'heap_ratio'
    selected_candidate: str | None = None    # 'p99' | 'max_floor'
    p99_candidate_bytes: int | None = None   # int(p99 × margin)
    max_candidate_bytes: int | None = None   # int(obs_max × 1.05)
    current_heap_pct: int | None = None      # round(current_xmx / current_limit × 100)


@dataclass(frozen=True)
class CurrentConfig:
    """Current JVM and container configuration derived from flags and inputs."""
    raw_flags: str
    parsed: dict[str, Any]
    anti_patterns: tuple[str, ...]
    current_xmx: int | None
    current_xms: int | None
    current_request: int | None
    memory_limit: int | None
    heap_style: str          # 'absolute' | 'percentage'
    style_ambiguous: bool
    jvm_source: str | None


@dataclass(frozen=True)
class FleetDeltas:
    """Pre-computed fleet impact values — formatting only happens in console.py."""
    replica_count: int
    limit_delta_bytes: int          # signed: rec_limit − current_limit
    limit_fleet_gib: float          # abs(limit_delta) × replicas / GiB
    request_delta_bytes: int | None # signed: rec_request − current_request; None when unknown
    request_fleet_gib: float | None # abs(request_delta) × replicas / GiB; None when unknown
    request_baseline_fleet_gib: float | None  # rec_request × replicas / GiB when no current baseline


@dataclass(frozen=True)
class WorkloadSummary:
    name: str
    container: str


@dataclass(frozen=True)
class AnalysisResult:
    """Complete output of one analysis run — everything console.py needs to render."""
    workload: WorkloadSummary
    mem_stats: MemoryStats
    current_config: CurrentConfig
    path: str              # 'no_input'|'no_limit'|'no_xmx'|'working_set'|'measured'|'measured_no_flags'
    profile_display: str
    ws_margin: float
    recommendation: SizingRecommendation | None
    rec_request: int | None
    rec_request_candidate: int | None  # uncapped p99 × margin candidate
    request_target_capped: bool
    limit_severity: str    # 'none' | 'hwm' | 'staged' | 'withhold'
    limit_warning_msg: str | None
    collector_warning: str | None
    missing_effective_flags: bool
    jcmd_pod: str | None
    fleet_deltas: FleetDeltas | None
    loading_notes: tuple[str, ...]  # printed before the analysis block


# ── Calculation functions ─────────────────────────────────────────────────────

def calc_from_measurements(
    live_set: int,
    metaspace: int,
    gc_mult: float,
    overhead_bytes: int = _300MiB,
    headroom_ratio: float = 0.12,
    profile_name: str | None = None,
) -> SizingRecommendation:
    """JVM-measured recommendation from jcmd GC.heap_info values."""
    xmx = live_set * gc_mult
    xms = xmx * 0.45
    headroom = xmx * headroom_ratio
    container_limit = math.ceil((xmx + metaspace + overhead_bytes + headroom) / _MiB) * _MiB
    max_ram_pct = round((xmx / container_limit) * 100, 1)
    init_ram_pct = round(max_ram_pct * 0.45, 1)
    return SizingRecommendation(
        basis='measurements',
        xmx=int(xmx),
        xms=int(xms),
        container_limit=int(container_limit),
        max_ram_pct=max_ram_pct,
        init_ram_pct=init_ram_pct,
        live_set=int(live_set),
        metaspace=int(metaspace),
        overhead=int(overhead_bytes),
        headroom=int(headroom),
        gc_mult=gc_mult,
        profile_name=profile_name,
    )


def calc_from_working_set(
    p99: float,
    obs_max: float,
    profile_margin: float,
    current_xmx: int,
    current_limit: int,
    overhead_bytes: int = _300MiB,
) -> SizingRecommendation | None:
    """Working-set recommendation from historical diagnostics (no jcmd required)."""
    if current_xmx is None or not current_limit:
        return None

    p99_candidate = p99 * profile_margin
    max_candidate = obs_max * 1.05
    selected_candidate = 'p99' if p99_candidate >= max_candidate else 'max_floor'
    raw_limit = max(p99_candidate, max_candidate)
    container_limit = math.ceil(raw_limit / _MiB) * _MiB

    current_heap_ratio = current_xmx / current_limit
    recommended_heap_ratio = min(current_heap_ratio, 0.80)

    xmx_from_ratio = container_limit * recommended_heap_ratio
    xmx_from_gap   = container_limit - overhead_bytes
    xmx = min(xmx_from_ratio, xmx_from_gap)
    if xmx <= 0:
        return None

    if xmx_from_gap < xmx_from_ratio:
        xmx_constraint = 'overhead_gap'
    elif current_heap_ratio > 0.80:
        xmx_constraint = '80pct_cap'
    else:
        xmx_constraint = 'heap_ratio'

    xms = xmx * 0.45
    max_ram_pct = round((xmx / container_limit) * 100, 1)
    init_ram_pct = round(max_ram_pct * 0.45, 1)

    return SizingRecommendation(
        basis='working_set',
        xmx=int(xmx),
        xms=int(xms),
        container_limit=int(container_limit),
        max_ram_pct=max_ram_pct,
        init_ram_pct=init_ram_pct,
        p99=p99,
        obs_max=obs_max,
        profile_margin=profile_margin,
        current_xmx_in=int(current_xmx),
        current_limit_in=int(current_limit),
        xmx_constraint=xmx_constraint,
        selected_candidate=selected_candidate,
        overhead=int(overhead_bytes),
        p99_candidate_bytes=int(p99_candidate),
        max_candidate_bytes=int(max_candidate),
        current_heap_pct=round(current_xmx / current_limit * 100),
    )


def calculate_memory_request(
    p99: float,
    margin: float,
    recommended_limit: int,
) -> tuple[int, int, bool]:
    """Return (candidate, rec_request, target_capped).

    candidate: p99 × margin ceiled to the nearest MiB.
    rec_request: min(candidate, recommended_limit).
    target_capped: True when candidate exceeds the limit.
    """
    candidate = math.ceil(p99 * margin / _MiB) * _MiB
    rec_request = min(candidate, recommended_limit)
    return candidate, rec_request, candidate > recommended_limit


def limit_warning_level(
    rec_limit: int,
    p99: float,
    obs_max: float,
) -> tuple[str, str | None]:
    """Classify a recommended limit against historical usage.

    Returns (severity, message) where severity is one of:
        'none'     — limit covers the observed maximum.
        'hwm'      — covers p99 but not the observed maximum.
        'staged'   — below p99 but above 30% guardrail.
        'withhold' — below 30% of p99; configuration is suppressed.
    """
    if rec_limit >= obs_max:
        return 'none', None
    if rec_limit >= p99:
        return 'hwm', (
            "Recommended limit covers historical p99 but not the observed maximum. "
            "An isolated high-water mark would not be covered. "
            "Apply with a staged deployment and monitor for OOM events."
        )
    if rec_limit >= 0.30 * p99:
        return 'staged', (
            "Recommended limit is below historical p99. "
            "Apply using a staged deployment and validate that actual usage "
            "stays within the new limit before rolling out broadly."
        )
    return 'withhold', (
        "Recommended limit is below 30% of historical p99 — a Thoras safety guardrail. "
        "Verify inputs are correct and rerun, or raise this with your Thoras account team."
    )
