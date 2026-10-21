"""JVM flag parsing, heap style detection, collector classification, and anti-pattern detection."""
import re
from typing import Any

from jvm_sizing.units import _GiB, _MiB, fmt_bytes, parse_jvm_mem_size

_KNOWN_GC_FLAGS = (
    'UseG1GC', 'UseZGC', 'UseShenandoahGC', 'UseSerialGC', 'UseParallelGC',
)


def parse_jvm_flags(flag_str: str) -> dict[str, Any]:
    """Parse a JAVA_TOOL_OPTIONS / VM.flags string into a structured dict.

    Keys: '_raw', 'GC', 'Xmx', 'Xms', 'MaxHeapSize', 'InitialHeapSize',
          'MaxRAMPercentage', 'InitialRAMPercentage', 'MinRAMPercentage'.
    """
    flags: dict[str, Any] = {'_raw': flag_str.strip()}

    for name, pattern in [
        ('MaxRAMPercentage',     r'-XX:MaxRAMPercentage=([0-9.]+)'),
        ('InitialRAMPercentage', r'-XX:InitialRAMPercentage=([0-9.]+)'),
        ('MinRAMPercentage',     r'-XX:MinRAMPercentage=([0-9.]+)'),
    ]:
        matches = re.findall(pattern, flag_str)
        if matches:
            flags[name] = float(matches[-1])

    for prefix in ('Xmx', 'Xms'):
        matches = re.findall(rf'-{prefix}([0-9]+)([gGmMkK]?)', flag_str)
        if matches:
            flags[prefix] = parse_jvm_mem_size(matches[-1][0], matches[-1][1])

    for flag_name, pattern in [
        ('MaxHeapSize',     r'-XX:MaxHeapSize=([0-9]+)'),
        ('InitialHeapSize', r'-XX:InitialHeapSize=([0-9]+)'),
    ]:
        matches = re.findall(pattern, flag_str)
        if matches:
            flags[flag_name] = int(matches[-1])

    for gc_flag in _KNOWN_GC_FLAGS:
        if f'+{gc_flag}' in flag_str:
            flags['GC'] = gc_flag

    return flags


def detect_anti_patterns(
    flags: dict[str, Any],
    memory_limit: int | None = None,
) -> list[str]:
    """Return a list of human-readable configuration warnings for the supplied flags."""
    issues: list[str] = []

    if 'Xmx' in flags and 'Xms' in flags and flags['Xmx'] == flags['Xms']:
        issues.append(
            f"FIXED HEAP: -Xms ({fmt_bytes(flags['Xms'])}) = -Xmx ({fmt_bytes(flags['Xmx'])}). "
            "This favors predictable heap capacity but prevents heap-size elasticity. "
            "Thoras recommends a lower Xms for cost-oriented sizing."
        )

    explicit_pinned = 'Xmx' in flags and 'Xms' in flags and flags['Xmx'] == flags['Xms']
    if (
        'MaxHeapSize' in flags and 'InitialHeapSize' in flags
        and flags['MaxHeapSize'] == flags['InitialHeapSize']
        and not explicit_pinned
    ):
        issues.append(
            f"LARGE INITIAL HEAP: Effective InitialHeapSize ({fmt_bytes(flags['InitialHeapSize'])}) "
            f"equals MaxHeapSize ({fmt_bytes(flags['MaxHeapSize'])}). "
            "The JVM starts at its current ceiling, reducing memory elasticity. "
            "This does not prove a hard minimum without explicit -Xms=-Xmx. "
            "Thoras recommends a lower initial heap for cost-oriented sizing."
        )

    if (
        'MaxRAMPercentage' in flags and 'InitialRAMPercentage' in flags
        and flags['MaxRAMPercentage'] == flags['InitialRAMPercentage']
        and 'Xmx' not in flags
    ):
        issues.append(
            f"LARGE INITIAL HEAP: InitialRAMPercentage ({flags['InitialRAMPercentage']}) = "
            f"MaxRAMPercentage ({flags['MaxRAMPercentage']}). "
            "This favors predictable startup capacity but reduces memory elasticity. "
            "For cost-oriented sizing, decouple Initial from Max."
        )

    max_pct = flags.get('MaxRAMPercentage')
    if max_pct is not None and 'Xmx' not in flags:
        if memory_limit is not None:
            effective_xmx = (max_pct / 100.0) * memory_limit
            remaining = memory_limit - effective_xmx
            if remaining < _GiB:
                issues.append(
                    f"HIGH MaxRAMPercentage ({max_pct}%): leaves less than 1 GiB "
                    f"({fmt_bytes(int(remaining))}) for non-heap memory (metaspace, thread stacks, "
                    "code cache, native libraries). Risk of container OOMKill."
                )
        elif max_pct >= 85:
            issues.append(
                f"HIGH MaxRAMPercentage ({max_pct}%): leaves less than 15% "
                "for non-heap. Risk of OOMKill if metaspace or thread stacks grow."
            )

    return issues


def resolve_current_xmx(
    flags: dict[str, Any],
    current_limit: int | None,
) -> tuple[float | None, float | None]:
    """Derive current Xmx and Xms from flags.

    Priority: MaxHeapSize > Xmx > MaxRAMPercentage × current_limit.
    Returns (None, None) when Xmx cannot be determined.
    """
    xmx: float | None = None
    if 'MaxHeapSize' in flags:
        xmx = float(flags['MaxHeapSize'])
    elif 'Xmx' in flags:
        xmx = float(flags['Xmx'])
    elif 'MaxRAMPercentage' in flags and current_limit:
        xmx = flags['MaxRAMPercentage'] / 100.0 * current_limit

    if xmx is None:
        return None, None

    xms: float | None = None
    if 'InitialHeapSize' in flags:
        xms = float(flags['InitialHeapSize'])
    elif 'Xms' in flags:
        xms = float(flags['Xms'])
    elif 'InitialRAMPercentage' in flags and current_limit:
        xms = flags['InitialRAMPercentage'] / 100.0 * current_limit

    return xmx, xms


def detect_heap_style(
    flags: dict[str, Any],
    heap_style_arg: str,
    flags_supplied: bool,
) -> str:
    """Return 'absolute' or 'percentage' for JVM configuration output style.

    Auto-detection: explicit -Xmx/-Xms → absolute; percentage flags without
    explicit heap sizes → percentage; ambiguous (VM.flags ergonomics) → absolute.
    """
    if heap_style_arg == 'absolute':
        return 'absolute'
    if heap_style_arg == 'percentage':
        return 'percentage'
    if not flags_supplied:
        return 'absolute'
    if 'Xmx' in flags or 'Xms' in flags:
        return 'absolute'
    if 'MaxRAMPercentage' in flags or 'InitialRAMPercentage' in flags:
        # MaxHeapSize alongside percentage flags indicates VM.flags ergonomic output —
        # provenance cannot be confirmed, so default to absolute for safety.
        if 'MaxHeapSize' in flags:
            return 'absolute'
        return 'percentage'
    return 'absolute'


def heap_style_ambiguous(flags: dict[str, Any], heap_style_arg: str) -> bool:
    """True when auto-detection fell back to absolute due to ambiguous flag provenance."""
    if heap_style_arg in ('absolute', 'percentage'):
        return False
    return (
        ('MaxRAMPercentage' in flags or 'InitialRAMPercentage' in flags)
        and 'MaxHeapSize' in flags
        and 'Xmx' not in flags
        and 'Xms' not in flags
    )


def collector_warning_text(
    detected_gc: str | None,
    assume_g1: bool,
) -> str | None:
    """Return a collector warning string, or None when no warning is needed.

    Precedence: an explicit GC flag always overrides --assume-g1.
    G1 → no warning; non-G1 → calibration warning regardless of --assume-g1;
    unknown + attested → no warning; unknown + unattested → unknown-collector warning.
    """
    if detected_gc == 'UseG1GC':
        return None
    if detected_gc is not None:
        gc_name = detected_gc.replace('Use', '')
        return (
            f"{gc_name} detected. This recommendation uses Thoras defaults that are not "
            f"separately calibrated for {gc_name}. Validate GC behavior and native-memory "
            f"headroom through your standard staged rollout."
        )
    if assume_g1:
        return None
    return (
        "Collector not confirmed. This recommendation uses Thoras defaults developed "
        "around G1 workloads. Verify the collector and validate through your standard "
        "staged rollout."
    )
