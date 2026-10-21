"""Console renderer for AnalysisResult.

`render_analysis` is the only public API. It returns a str ready for print().
It never prints, writes, or calls sys.exit itself.
All sizing arithmetic stays in sizing.py / cli.py — this module only formats.
"""
from jvm_sizing.sizing import AnalysisResult, FleetDeltas, SizingRecommendation
from jvm_sizing.units import fmt_bytes, fmt_jvm_flag_mem, fmt_k8s_mem

_HR = '─' * 60


def render_analysis(result: AnalysisResult) -> str:
    parts: list[str] = []

    parts.append(_section("WORKLOAD: " + result.workload.name))
    parts.append(f"  Container: {result.workload.container}")

    parts.append(_section("CONTAINER MEMORY WORKING SET"))
    parts.append(_fmt_stats("Per-pod average", result.mem_stats))
    if result.current_config.memory_limit:
        parts.append(f"  Current limit: {fmt_bytes(result.current_config.memory_limit)}")

    cc = result.current_config
    if cc.raw_flags:
        parts.append(_section("PARSED JVM FLAGS"))
        parts.append(f"  {cc.raw_flags}")
        _byte_keys = ('Xmx', 'Xms', 'MaxHeapSize', 'InitialHeapSize')
        for k, v in cc.parsed.items():
            val = fmt_bytes(v) if k in _byte_keys else v
            parts.append(f"    {k}: {val}")
        if cc.jvm_source:
            parts.append(f"  Source: {cc.jvm_source}")

    if cc.anti_patterns:
        parts.append(_section("CONFIGURATION FINDINGS"))
        for msg in cc.anti_patterns:
            parts.append(f"  ⚠  {msg}")

    parts.append(_section("RECOMMENDATION"))
    parts.append(f"  Risk profile: {result.profile_display}")
    parts.append("")
    parts.append(f"  Observed p99 working set:  {fmt_bytes(int(result.mem_stats.p99))}")
    parts.append(f"  Observed maximum:          {fmt_bytes(int(result.mem_stats.max))}")

    rec = result.recommendation
    if rec is not None and rec.live_set is not None:
        parts.append(f"  Heap-used snapshot:        {fmt_bytes(rec.live_set)}")
    if rec is not None and rec.metaspace is not None:
        parts.append(f"  Metaspace used:            {fmt_bytes(rec.metaspace)}")
    parts.append("")

    if rec is not None:
        parts.append(_section("CALCULATION BASIS"))
        for line in _calc_basis_lines(rec, result.mem_stats.p99, result.ws_margin,
                                      result.rec_request, result.rec_request_candidate,
                                      result.request_target_capped):
            parts.append(line)
        parts.append("")

    if rec is None:
        path = result.path
        if path == 'no_input':
            parts.append("  Effective JVM flags are required for a recommendation.")
            parts.append("  Provide --flags \"...\" from kubectl describe pod or jcmd PID VM.flags.")
        elif path == 'no_limit':
            parts.append("  Memory limit (--memory-limit BYTES) is required for a recommendation.")
            parts.append("  Working-set statistics are shown above for reference.")
        elif path == 'no_xmx':
            parts.append("  Effective heap size could not be determined from the provided flags.")
            parts.append("  Provide -Xmx, MaxRAMPercentage + --memory-limit, or VM.flags output.")
        else:
            parts.append("  Recommendation unavailable.")
    else:
        table_rows = [
            _table_row("JVM maximum heap", cc.current_xmx, rec.xmx),
            _table_row("JVM initial heap", cc.current_xms, rec.xms),
            _table_row("Memory request",   cc.current_request, result.rec_request),
            _table_row("Memory limit",     cc.memory_limit, rec.container_limit),
        ]
        parts.append(_comparison_table(table_rows))
        parts.append("")

        if result.collector_warning:
            parts.append(f"  ⚠  {result.collector_warning}")
            parts.append("")

        severity = result.limit_severity
        if severity == 'withhold':
            parts.append(f"  ⚠  {result.limit_warning_msg}")
            if rec is not None:
                parts.append(f"     Candidate limit: {fmt_bytes(rec.container_limit)}"
                             f"  |  p99: {fmt_bytes(int(result.mem_stats.p99))}")
            parts.append("     Reconcile inputs before applying.")
        else:
            if severity == 'staged':
                parts.append(f"  ⚠  Staged deployment required: {result.limit_warning_msg}")
                parts.append("")
            elif severity == 'hwm':
                parts.append(f"  NOTE: High-water mark — {result.limit_warning_msg}")
                parts.append("")

            if cc.current_request is None and result.rec_request is not None:
                parts.append("  NOTE: Current memory request unknown. Recommended request is derived from")
                parts.append("        historical working set; the current-to-recommended difference is unavailable.")
                parts.append("")

            if result.request_target_capped and severity not in ('staged', 'withhold'):
                parts.append("  NOTE: Historical p99-based request target exceeds the recommended limit.")
                parts.append("        Recommended request is set equal to the limit.")
                parts.append("        This assumes JVM changes will reduce resident memory.")
                parts.append("        Validate with a staged deployment before rolling out broadly.")
                parts.append("")

            if cc.style_ambiguous:
                parts.append("  NOTE: Supplied flags contain both effective heap sizes and RAM-percentage")
                parts.append("        values, so the original configuration style cannot be determined.")
                parts.append("        Absolute output selected. If the workload is configured with")
                parts.append("        RAM-percentage flags, rerun with --heap-style percentage.")
                parts.append("        This changes only the output style, not the sizing calculation.")
                parts.append("")

            style_label = ("percentage (MaxRAMPercentage)" if cc.heap_style == 'percentage'
                           else "absolute (-Xmx/-Xms)")
            parts.append(f"  JVM configuration ({style_label}):")
            for line in _jvm_config_block(rec, cc.heap_style, result.rec_request).splitlines():
                parts.append(f"    {line}")

            if result.missing_effective_flags:
                parts.append("")
                parts.append("  NOTE: Effective JVM configuration was not provided. Before applying the")
                parts.append("        recommendation, verify the current heap flags and remove any conflicting")
                parts.append("        -Xmx, -Xms, MaxRAMPercentage, or InitialRAMPercentage settings.")

            if result.fleet_deltas is not None:
                parts.append("")
                parts.extend(_fmt_fleet_deltas(result.fleet_deltas))

    if result.jcmd_pod:
        parts.append("")
        parts.append(f"  JVM measurement source pod: {result.jcmd_pod}")

    return "\n".join(parts)


# ── Section and formatting helpers ────────────────────────────────────────────

def _section(title: str) -> str:
    return f"\n{_HR}\n  {title}\n{_HR}"


def _fmt_stats(label: str, s) -> str:
    return (
        f"  {label}:\n"
        f"    p50 {fmt_bytes(int(s.p50)):>12}   p90 {fmt_bytes(int(s.p90)):>12}"
        f"   p99 {fmt_bytes(int(s.p99)):>12}   max {fmt_bytes(int(s.max)):>12}"
    )


def _table_row(label: str, cur, rec) -> tuple:
    cur_s  = fmt_bytes(cur) if cur is not None else 'Unknown'
    rec_s  = fmt_bytes(rec) if rec is not None else 'N/A'
    diff_s = _fmt_diff(rec, cur) if rec is not None else 'N/A'
    return (label, cur_s, rec_s, diff_s)


def _fmt_diff(rec_val, cur_val) -> str:
    if cur_val is None:
        return 'N/A'
    diff = rec_val - cur_val
    s = fmt_bytes(abs(diff))
    if diff > 0:
        return f"+{s}"
    if diff < 0:
        return f"-{s}"
    return "0 B"


def _comparison_table(rows: list[tuple]) -> str:
    lines = [f"  {'':26s}  {'Current':>12}  {'Recommended':>12}  {'Difference':>12}"]
    lines.append(f"  {'':26s}  {'─'*12}  {'─'*12}  {'─'*12}")
    for label, cur_s, rec_s, diff_s in rows:
        lines.append(f"  {label:26s}  {cur_s:>12}  {rec_s:>12}  {diff_s:>12}")
    return "\n".join(lines)


def _jvm_config_block(rec: SizingRecommendation, style: str, rec_request: int | None) -> str:
    limit_s = fmt_k8s_mem(rec.container_limit)
    req_line = f"  requests: {fmt_k8s_mem(int(rec_request))}\n" if rec_request is not None else ""
    if style == 'percentage':
        return (
            f"# Remove -Xmx and -Xms if present — absolute flags override percentage flags\n"
            f"-XX:MaxRAMPercentage={rec.max_ram_pct}\n"
            f"-XX:InitialRAMPercentage={rec.init_ram_pct}    # Thoras cost-oriented default\n"
            f"# MinRAMPercentage: leave at current value (only relevant to very small JVMs)\n"
            f"\n"
            f"Container memory:\n"
            f"{req_line}"
            f"  limits:   {limit_s}"
        )
    return (
        f"-Xmx{fmt_jvm_flag_mem(rec.xmx)}\n"
        f"-Xms{fmt_jvm_flag_mem(rec.xms)}    # ~45% of Xmx — Thoras cost-oriented default\n"
        f"# Remove -XX:MaxRAMPercentage and -XX:InitialRAMPercentage if present\n"
        f"\n"
        f"Container memory:\n"
        f"{req_line}"
        f"  limits:   {limit_s}"
    )


def _fmt_fleet_deltas(d: FleetDeltas) -> list[str]:
    """Format fleet impact lines from pre-computed FleetDeltas fields."""
    sign = "-" if d.limit_delta_bytes < 0 else "+"
    per_pod = fmt_bytes(abs(d.limit_delta_bytes))
    lines = [
        f"  Fleet limit delta:   {sign}{d.limit_fleet_gib:.0f} GiB"
        f"  ({d.replica_count} pods × {sign}{per_pod})"
    ]
    if d.request_delta_bytes is not None and d.request_fleet_gib is not None and d.request_delta_bytes != 0:
        req_sign = "-" if d.request_delta_bytes < 0 else "+"
        lines.append(
            f"  Fleet request delta: {req_sign}{d.request_fleet_gib:.0f} GiB  (schedulable capacity)"
        )
    elif d.request_baseline_fleet_gib is not None:
        lines.append(f"  Fleet request:       {d.request_baseline_fleet_gib:.0f} GiB  (schedulable capacity)")
    return lines


def _calc_basis_lines(
    rec: SizingRecommendation,
    p99_for_request: float,
    ws_margin: float,
    rec_request: int | None,
    rec_request_candidate: int | None,
    request_target_capped: bool = False,
) -> list[str]:
    """Format calculation basis lines from raw SizingRecommendation fields."""
    lines: list[str] = []
    if rec.basis == 'measurements':
        assert rec.live_set is not None
        assert rec.metaspace is not None
        assert rec.overhead is not None
        assert rec.headroom is not None
        assert rec.gc_mult is not None
        mult_label = f"{rec.profile_name} multiplier" if rec.profile_name else "custom multiplier"
        lines.append(
            f"  Xmx:   {fmt_bytes(rec.live_set)} × {rec.gc_mult}"
            f" {mult_label} ≈ {fmt_bytes(rec.xmx)}"
        )
        lines.append(
            f"  Limit: {fmt_bytes(rec.xmx)} Xmx + {fmt_bytes(rec.metaspace)} metaspace"
            f" + {fmt_bytes(rec.overhead)} non-heap fallback allowance"
        )
        lines.append(
            f"         + {fmt_bytes(rec.headroom)} safety headroom"
            f" ≈ {fmt_bytes(rec.container_limit)}"
        )
    else:  # working_set
        assert rec.p99 is not None
        assert rec.obs_max is not None
        assert rec.profile_margin is not None
        assert rec.overhead is not None
        assert rec.current_xmx_in is not None
        assert rec.current_limit_in is not None
        assert rec.p99_candidate_bytes is not None
        assert rec.max_candidate_bytes is not None
        margin = rec.profile_margin
        p99_cand = rec.p99_candidate_bytes
        max_cand = rec.max_candidate_bytes
        selected_label = 'p99' if rec.selected_candidate == 'p99' else 'maximum-floor'
        lines.append(
            f"  p99 candidate:           {fmt_bytes(int(rec.p99))} × {margin:.2f}"
            f" ≈ {fmt_bytes(p99_cand)}"
        )
        lines.append(
            f"  maximum-floor candidate: {fmt_bytes(int(rec.obs_max))} × 1.05"
            f" ≈ {fmt_bytes(max_cand)}"
        )
        lines.append(f"  Selected: {selected_label} candidate because it is larger")
        constraint = rec.xmx_constraint or 'heap_ratio'
        xmx_s = fmt_bytes(rec.xmx)
        overhead_s = fmt_bytes(rec.overhead)
        if constraint == 'overhead_gap':
            lines.append(f"  Xmx: {overhead_s} minimum outside-heap gap → {xmx_s}")
        elif constraint == '80pct_cap':
            lines.append(f"  Xmx: current {rec.current_heap_pct}% heap ratio capped at 80% → {xmx_s}")
        else:
            lines.append(f"  Xmx: current {rec.current_heap_pct}% heap ratio preserved → {xmx_s}")

    # Request trace — uses pre-computed values from AnalysisResult, no re-calculation
    if p99_for_request and ws_margin and rec_request is not None and rec_request_candidate is not None:
        lines.append("")
        lines.append("  Request:")
        lines.append(
            f"    Historical p99: {fmt_bytes(int(p99_for_request))} × {ws_margin:.2f}"
            f" ≈ {fmt_bytes(rec_request_candidate)}"
        )
        lines.append(f"    Recommended limit: {fmt_bytes(rec.container_limit)}")
        if request_target_capped:
            lines.append(f"    Result: {fmt_bytes(int(rec_request))} (capped — target exceeds limit)")
        else:
            lines.append(f"    Result: {fmt_bytes(int(rec_request))}")

    return lines
