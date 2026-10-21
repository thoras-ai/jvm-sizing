"""CLI entry point and analysis orchestration.

`main()` returns an int exit code — it never calls sys.exit directly.
`run()` is side-effect-free: it does not print or write files.
UserInputError is raised for invalid inputs and caught cleanly by main().
Unexpected exceptions propagate so programming defects stay visible.
"""
import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime

from jvm_sizing.console import render_analysis
from jvm_sizing.diagnostics import (
    DiagnosticsPackage,
    compute_stats,
    discover_containers,
    parse_metrics,
)
from jvm_sizing.jvm import (
    collector_warning_text,
    detect_anti_patterns,
    detect_heap_style,
    heap_style_ambiguous,
    parse_jvm_flags,
    resolve_current_xmx,
)
from jvm_sizing.sizing import (
    PROFILE_GC_MULT,
    PROFILE_WS_MARGIN,
    AnalysisResult,
    CurrentConfig,
    FleetDeltas,
    MemoryStats,
    SizingRecommendation,
    WorkloadSummary,
    calc_from_measurements,
    calc_from_working_set,
    calculate_memory_request,
    limit_warning_level,
)
from jvm_sizing.units import _300MiB, jvm_bytes, k8s_bytes, parse_k8s_quantity


class UserInputError(Exception):
    """Raised for invalid or inconsistent user-supplied inputs."""


# ── Argument parsing ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='analyze_jvm',
        description=(
            "Thoras JVM Memory Sizing Analyzer — reads a diagnostics package "
            "and produces JVM memory sizing recommendations."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Diagnostics analysis only (no flags — shows working-set statistics):
  python3 analyze_jvm.py myapp-diagnostics.zip --memory-limit 50Gi

  # Working-set-based recommendation (no jcmd required):
  python3 analyze_jvm.py myapp-diagnostics.zip \\
    --flags="-XX:+UseG1GC -XX:MaxRAMPercentage=75.0 -XX:InitialRAMPercentage=75.0" \\
    --memory-limit 50Gi

  # Working-set recommendation with risk profile override:
  python3 analyze_jvm.py myapp-diagnostics.zip \\
    --flags="-XX:+UseG1GC -XX:MaxRAMPercentage=75.0" \\
    --memory-limit 50Gi --risk-profile reliability

  # JVM-measured recommendation (after collecting jcmd GC.heap_info):
  python3 analyze_jvm.py myapp-diagnostics.zip \\
    --flags="-XX:+UseG1GC -XX:MaxRAMPercentage=75.0 -XX:InitialRAMPercentage=75.0" \\
    --memory-limit 50Gi \\
    --live-set 900Mi --metaspace 130Mi \\
    --jcmd-pod myapp-6d8f9b-xk2pq

  # G1GC confirmed but not in JAVA_TOOL_OPTIONS (pass --assume-g1):
  python3 analyze_jvm.py myapp-diagnostics.zip \\
    --memory-limit 50Gi \\
    --live-set 900Mi --metaspace 130Mi \\
    --jcmd-pod myapp-6d8f9b-xk2pq --assume-g1

  # Force percentage-style output:
  python3 analyze_jvm.py myapp-diagnostics.zip \\
    --flags="-XX:+UseG1GC -XX:MaxRAMPercentage=75.0" \\
    --memory-limit 50Gi --heap-style percentage

  # Multi-container pod — specify the JVM container:
  python3 analyze_jvm.py diagnostics.zip --container myapp \\
    --flags="-XX:+UseG1GC ..." --memory-limit 50Gi
""",
    )
    p.add_argument('package', help='Diagnostics zip file or extracted directory')
    p.add_argument('--container',
                   help='Container name to analyze. Required for multi-container pods; auto-selected when only one container is present.')
    p.add_argument('--flags',
                   help='JVM flags string — prefer jcmd VM.flags output (includes GC and ergonomic heap sizes)')
    p.add_argument('--memory-limit', type=k8s_bytes, metavar='BYTES',
                   help='Current container memory limit — raw bytes or Kubernetes quantity (e.g. 50Gi, 512Mi)')
    p.add_argument('--live-set', type=jvm_bytes, metavar='BYTES',
                   help='Heap-used snapshot from jcmd GC.heap_info (taken after GC.run). Supply with --metaspace for a JVM-measured recommendation.')
    p.add_argument('--metaspace', type=jvm_bytes, metavar='BYTES',
                   help='Metaspace used from jcmd GC.heap_info — raw bytes or Kubernetes quantity (e.g. 256Mi). Required when --live-set is supplied.')
    p.add_argument('--risk-profile', choices=['savings', 'balanced', 'reliability'],
                   default='balanced',
                   help='Risk profile controlling sizing margins (default: balanced). savings=tighter margins; reliability=larger margins.')
    p.add_argument('--heap-style', choices=['auto', 'absolute', 'percentage'], default='auto',
                   help='JVM configuration style in output (default: auto — preserves workload\'s current style).')
    p.add_argument('--gc-multiplier', type=float, default=None, metavar='N',
                   help='Override heap-used-snapshot multiplier (range 2.0–3.0; default comes from --risk-profile).')
    p.add_argument('--working-set-margin', type=float, default=None, metavar='N',
                   help='Override working-set margin multiplier (e.g. 1.20; default comes from --risk-profile). Affects the p99 limit candidate on the diagnostics-only path and the memory request on both paths.')
    p.add_argument('--overhead-bytes', type=k8s_bytes, default=_300MiB, metavar='BYTES',
                   help='Minimum outside-heap allowance for thread stacks, code cache, direct buffers, and native libs (default 300 MiB). In JVM-measured mode, measured metaspace is added separately. Increase for native-heavy workloads.')
    p.add_argument('--headroom-ratio', type=float, default=0.12, metavar='N',
                   help='Headroom ratio between Xmx and container limit in JVM-measured path (default 0.12 = 12%%).')
    p.add_argument('--assume-g1', action='store_true',
                   help='Suppress the unknown-collector warning when no GC flag is visible in --flags and you have confirmed G1GC from GC logs or JMX. Has no effect when a non-G1 collector is explicitly detected.')
    p.add_argument('--jcmd-pod', metavar='POD',
                   help='Pod name used for jcmd measurements (required with --live-set; documented in output for traceability).')
    return p


# ── Input validation ──────────────────────────────────────────────────────────

def _validate(args: argparse.Namespace) -> None:
    if (args.live_set is None) != (args.metaspace is None):
        raise UserInputError(
            "--live-set and --metaspace must be supplied together. Provide both or neither."
        )
    if args.live_set is not None:
        if not math.isfinite(args.live_set) or args.live_set <= 0:
            raise UserInputError(f"--live-set must be a positive finite byte count (got {args.live_set}).")
        if not math.isfinite(args.metaspace) or args.metaspace <= 0:
            raise UserInputError(f"--metaspace must be a positive finite byte count (got {args.metaspace}).")
        if not args.jcmd_pod:
            raise UserInputError(
                "--jcmd-pod POD is required with --live-set. Record which pod the measurements came from."
            )
    if args.memory_limit is not None:
        if not math.isfinite(args.memory_limit) or args.memory_limit <= 0:
            raise UserInputError(f"--memory-limit must be a positive finite byte count (got {args.memory_limit}).")
    if args.gc_multiplier is not None:
        if not math.isfinite(args.gc_multiplier):
            raise UserInputError(f"--gc-multiplier must be a finite number (got {args.gc_multiplier}).")
        if not (2.0 <= args.gc_multiplier <= 3.0):
            raise UserInputError(
                f"--gc-multiplier must be between 2.0 and 3.0 (got {args.gc_multiplier}). "
                "Contact your Thoras account team for guidance on non-standard multipliers."
            )
    if not math.isfinite(args.overhead_bytes) or args.overhead_bytes < 0:
        raise UserInputError(f"--overhead-bytes must be a non-negative finite byte count (got {args.overhead_bytes}).")
    if not math.isfinite(args.headroom_ratio) or not (0.01 <= args.headroom_ratio <= 0.50):
        raise UserInputError(f"--headroom-ratio must be between 0.01 and 0.50 (got {args.headroom_ratio}).")
    if args.working_set_margin is not None:
        if not math.isfinite(args.working_set_margin) or args.working_set_margin <= 0:
            raise UserInputError(f"--working-set-margin must be a positive finite number (got {args.working_set_margin}).")


# ── Package loading helpers ───────────────────────────────────────────────────

@dataclass
class _PackageData:
    pkg: DiagnosticsPackage
    selected_container: str
    timestamps: list[str]
    mem_values: list[float]
    meta: dict
    workload_name: str
    loading_notes: list[str]


def _load_package_data(args: argparse.Namespace) -> _PackageData:
    """Load and validate the diagnostics package. Raises UserInputError on any failure."""
    pkg = DiagnosticsPackage(args.package)
    loading_notes: list[str] = []

    ast_paths = pkg.find_all("AIScaleTarget.json")
    if len(ast_paths) == 0:
        raise UserInputError(
            "AIScaleTarget.json not found. This package may be incomplete or not a supported Thoras workload export."
        )
    if len(ast_paths) > 1:
        paths_str = "\n".join(f"    {p}" for p in ast_paths)
        raise UserInputError(
            "This package contains multiple workloads and is not supported.\n"
            "Export diagnostics for a single workload from the Thoras dashboard.\n\n"
            f"Workloads found:\n{paths_str}"
        )

    loading_notes.append(f"Loading diagnostics from {args.package} …")

    raw = pkg.read("metrics.json")
    if not raw:
        raise UserInputError("metrics.json not found in the diagnostics package.")

    available_containers = discover_containers(raw)
    if not available_containers:
        raise UserInputError(
            "This diagnostics package does not contain container-level working-set data.\n"
            "Container metrics are required for accurate JVM sizing.\n"
            "Export current workload diagnostics to get container-level history."
        )

    if args.container:
        if args.container not in available_containers:
            container_list = "\n".join(f"  - {c}" for c in available_containers)
            raise UserInputError(
                f'Container "{args.container}" not found in this diagnostics package.\n\n'
                f'Available containers:\n\n{container_list}\n\n'
                f'Re-run with: --container CONTAINER_NAME'
            )
        selected_container = args.container
    elif len(available_containers) == 1:
        selected_container = available_containers[0]
    else:
        container_list = "\n".join(f"  - {c}" for c in available_containers)
        raise UserInputError(
            f'Multiple containers found in this diagnostics package:\n\n'
            f'{container_list}\n\n'
            f'Re-run with: --container CONTAINER_NAME'
        )

    timestamps, mem_values, meta = parse_metrics(raw, container_name=selected_container)
    if not timestamps:
        raise UserInputError(
            f"No metric snapshots found for container '{selected_container}' in metrics.json "
            f"(total hits: {meta['total_hits']})."
        )

    window = f"{timestamps[0][:10]} to {timestamps[-1][:10]}"
    loading_notes.append(f"  {len(timestamps)} snapshots, {window}")

    try:
        t0 = datetime.fromisoformat(timestamps[0].replace('Z', '+00:00'))
        t1 = datetime.fromisoformat(timestamps[-1].replace('Z', '+00:00'))
        days = (t1 - t0).total_seconds() / 86400
    except Exception:
        days = len(timestamps) / (60 * 24)
    if days < 1:
        loading_notes.append("  WARNING: Less than 24 hours of data. Percentiles may not be representative.")
    elif days < 3:
        loading_notes.append(f"  WARNING: {days:.1f} days of data. 3 days recommended; more is better.")

    workload_name = _load_workload_name(pkg)

    return _PackageData(
        pkg=pkg,
        selected_container=selected_container,
        timestamps=timestamps,
        mem_values=mem_values,
        meta=meta,
        workload_name=workload_name,
        loading_notes=loading_notes,
    )


def _load_workload_name(pkg: DiagnosticsPackage) -> str:
    raw = pkg.read("AIScaleTarget.json")
    if not raw:
        return "unknown"
    try:
        ast = json.loads(raw)
        ns   = ast.get('metadata', {}).get('namespace', '')
        name = ast.get('metadata', {}).get('name', '')
        if ns and name:
            return f"{ns}/{name}"
    except Exception:
        pass
    return "unknown"


def _load_replica_count(pkg: DiagnosticsPackage, meta_replicas) -> int | None:
    if meta_replicas is not None:
        try:
            return int(meta_replicas)
        except (TypeError, ValueError):
            pass
    raw = pkg.read("AIScaleTarget.json")
    if not raw:
        return None
    try:
        ast = json.loads(raw)
        status = ast.get('status', {})
        suggestion = status.get('latestSuggestion', {})
        rc = suggestion.get('currentReplicas') or status.get('replicas')
        if rc is not None:
            return int(rc)
    except Exception:
        pass
    return None


def _load_target_fallbacks(pkg: DiagnosticsPackage, container_name: str) -> tuple[str | None, float | None]:
    """Return (jvm_flags_str, memory_limit_bytes) from target.json, or (None, None)."""
    raw = pkg.read("target.json")
    if not raw:
        return None, None
    try:
        spec = json.loads(raw)
        pod_spec = (spec.get('spec', {}).get('template', {}).get('spec', {})
                    or spec.get('spec', {}))
        containers = pod_spec.get('containers', [])
        tc = next((c for c in containers if c.get('name') == container_name), None)
        if not tc:
            return None, None
        flags_str = None
        for env in tc.get('env', []):
            if env.get('name') in ('JAVA_TOOL_OPTIONS', 'JAVA_OPTIONS', 'JDK_JAVA_OPTIONS'):
                val = env.get('value')
                if val:
                    flags_str = val
                    break
        limit_bytes = None
        limits = tc.get('resources', {}).get('limits', {})
        if 'memory' in limits:
            try:
                limit_bytes = parse_k8s_quantity(limits['memory'])
            except ValueError:
                pass
        return flags_str, limit_bytes
    except Exception:
        return None, None


# ── Recommendation assembly ───────────────────────────────────────────────────

def _assemble_recommendation(
    args: argparse.Namespace,
    mem_stats: MemoryStats,
    flags_supplied: bool,
    memory_limit: float | None,
    current_xmx: float | None,
    gc_mult: float,
    ws_margin: float,
    risk_profile: str,
) -> tuple[SizingRecommendation | None, str, str, str | None, bool]:
    """Return (rec, path, limit_severity, limit_warning_msg, missing_effective_flags)."""
    has_measurements = args.live_set is not None
    missing_effective_flags = False
    rec: SizingRecommendation | None = None
    path: str
    limit_severity = 'none'
    limit_warning_msg: str | None = None

    if has_measurements:
        if not flags_supplied:
            path = 'measured_no_flags'
            missing_effective_flags = True
        else:
            path = 'measured'
        rec = calc_from_measurements(
            live_set       = int(args.live_set),
            metaspace      = int(args.metaspace),
            gc_mult        = gc_mult,
            overhead_bytes = int(args.overhead_bytes),
            headroom_ratio = args.headroom_ratio,
            profile_name   = None if args.gc_multiplier is not None else risk_profile,
        )
        if rec is not None:
            limit_severity, limit_warning_msg = limit_warning_level(
                rec.container_limit, mem_stats.p99, mem_stats.max
            )

    elif flags_supplied:
        if memory_limit is None:
            path = 'no_limit'
        elif current_xmx is None:
            path = 'no_xmx'
        else:
            path = 'working_set'
            rec = calc_from_working_set(
                p99            = mem_stats.p99,
                obs_max        = mem_stats.max,
                profile_margin = ws_margin,
                current_xmx    = int(current_xmx),
                current_limit  = int(memory_limit),
                overhead_bytes = int(args.overhead_bytes),
            )
            if rec is not None:
                limit_severity, limit_warning_msg = limit_warning_level(
                    rec.container_limit, mem_stats.p99, mem_stats.max
                )
    else:
        path = 'no_input'

    return rec, path, limit_severity, limit_warning_msg, missing_effective_flags


# ── Main orchestration ────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> tuple[AnalysisResult, int]:
    """Return (result, exit_code). Does not print or write files. Always exits 0 on success."""
    pd = _load_package_data(args)
    pkg, selected_container = pd.pkg, pd.selected_container
    timestamps, mem_values, meta = pd.timestamps, pd.mem_values, pd.meta

    # Legacy target.json fallback
    target_flags, target_limit = _load_target_fallbacks(pkg, selected_container)

    # Risk profile
    risk_profile = args.risk_profile
    gc_mult   = args.gc_multiplier if args.gc_multiplier is not None else PROFILE_GC_MULT[risk_profile]
    ws_margin = args.working_set_margin if args.working_set_margin is not None else PROFILE_WS_MARGIN[risk_profile]

    # JVM flags
    flags_str  = args.flags
    jvm_source: str | None = None
    if not flags_str and target_flags:
        flags_str  = target_flags
        jvm_source = "target.json (env)"
    flags          = parse_jvm_flags(flags_str) if flags_str else {}
    flags_supplied = bool(flags_str and flags_str.strip())
    detected_gc: str | None = flags.get('GC')

    # Memory limit (CLI flag takes priority over target.json)
    memory_limit: float | None = args.memory_limit or target_limit

    # Replica count
    replica_count = _load_replica_count(pkg, meta.get('total_replicas'))

    # Effective heap sizes
    current_xmx, current_xms = resolve_current_xmx(flags, memory_limit)

    # Current memory request from diagnostics spec
    current_request: float | None = meta.get('spec_memory')

    anti_patterns  = detect_anti_patterns(flags, memory_limit=memory_limit)
    heap_style     = detect_heap_style(flags, args.heap_style, flags_supplied)
    style_ambiguous = heap_style_ambiguous(flags, args.heap_style)

    # Stats — keep as float for sizing precision
    stats = compute_stats(mem_values)
    mem_stats = MemoryStats(
        p50=stats['p50'],
        p90=stats['p90'],
        p99=stats['p99'],
        max=stats['max'],
    )

    # CurrentConfig — constructed after all inputs are resolved
    parsed_flags = {k: v for k, v in flags.items() if k != '_raw'}
    current_config = CurrentConfig(
        raw_flags=flags_str or '',
        parsed=parsed_flags,
        anti_patterns=tuple(anti_patterns),
        current_xmx=int(current_xmx) if current_xmx is not None else None,
        current_xms=int(current_xms) if current_xms is not None else None,
        current_request=int(current_request) if current_request is not None else None,
        memory_limit=int(memory_limit) if memory_limit is not None else None,
        heap_style=heap_style,
        style_ambiguous=style_ambiguous,
        jvm_source=jvm_source,
    )

    rec, path, limit_severity, limit_warning_msg, missing_effective_flags = _assemble_recommendation(
        args, mem_stats, flags_supplied, memory_limit, current_xmx, gc_mult, ws_margin, risk_profile,
    )

    collector_warning = collector_warning_text(detected_gc, args.assume_g1)

    # Memory request — compute both candidate and capped value
    rec_request: int | None = None
    rec_request_candidate: int | None = None
    request_target_capped = False
    if rec is not None:
        rec_request_candidate, rec_request, request_target_capped = calculate_memory_request(
            mem_stats.p99, ws_margin, rec.container_limit
        )

    # Fleet deltas — all arithmetic here; console.py only formats
    fleet_deltas: FleetDeltas | None = None
    if rec is not None and replica_count is not None and memory_limit is not None:
        from jvm_sizing.units import _GiB
        limit_delta = rec.container_limit - int(memory_limit)
        limit_fleet_gib = abs(limit_delta) * replica_count / _GiB
        request_delta: int | None = None
        request_fleet_gib: float | None = None
        request_baseline_fleet_gib: float | None = None
        if rec_request is not None:
            if current_request is not None:
                request_delta = rec_request - int(current_request)
                request_fleet_gib = abs(request_delta) * replica_count / _GiB
            else:
                request_baseline_fleet_gib = rec_request * replica_count / _GiB
        fleet_deltas = FleetDeltas(
            replica_count=replica_count,
            limit_delta_bytes=limit_delta,
            limit_fleet_gib=limit_fleet_gib,
            request_delta_bytes=request_delta,
            request_fleet_gib=request_fleet_gib,
            request_baseline_fleet_gib=request_baseline_fleet_gib,
        )

    # Profile display label
    override_parts = []
    if path in ('measured', 'measured_no_flags') and args.gc_multiplier is not None:
        override_parts.append(f"gc-multiplier overridden to {args.gc_multiplier}")
    if rec is not None and args.working_set_margin is not None:
        override_parts.append(f"working-set-margin overridden to {args.working_set_margin}")
    profile_display = (
        risk_profile.capitalize()
        + (f" ({', '.join(override_parts)})" if override_parts else "")
    )

    result = AnalysisResult(
        workload=WorkloadSummary(
            name=pd.workload_name,
            container=selected_container,
        ),
        mem_stats=mem_stats,
        current_config=current_config,
        path=path,
        profile_display=profile_display,
        ws_margin=ws_margin,
        recommendation=rec,
        rec_request=rec_request,
        rec_request_candidate=rec_request_candidate,
        request_target_capped=request_target_capped,
        limit_severity=limit_severity,
        limit_warning_msg=limit_warning_msg,
        collector_warning=collector_warning,
        missing_effective_flags=missing_effective_flags,
        jcmd_pod=args.jcmd_pod,
        fleet_deltas=fleet_deltas,
        loading_notes=tuple(pd.loading_notes),
    )
    return result, 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        _validate(args)
        result, exit_code = run(args)
    except UserInputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for note in result.loading_notes:
        print(note)
    print(render_analysis(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
