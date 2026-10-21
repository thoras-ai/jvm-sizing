# JVM Memory Sizing

Use `analyze_jvm.py` with workload-specific Thoras diagnostics to produce JVM and Kubernetes memory recommendations.

The script only reads the local diagnostics package and values you provide. It does not connect to Kubernetes, run commands in your environment, or make changes.

## Requirements

- Python 3.12 or later; no third-party packages are required.
- A workload-specific Thoras diagnostics zip file or extracted directory containing container-level memory history.
- For a diagnostics-based recommendation, the workload's effective JVM flags and current container memory limit.

## Standard workflow

Run the script with the diagnostics package, effective JVM flags, and current memory limit:

```bash
python3 analyze_jvm.py <diagnostics-package> \
  --flags="-XX:+UseG1GC -XX:MaxRAMPercentage=75 -XX:InitialRAMPercentage=25" \
  --memory-limit 4Gi
```

`--flags` describes the workload's current JVM configuration. Copy the effective flags as they are; passing them to this script does not change the JVM.

`--memory-limit` is the current Kubernetes memory limit for the JVM container. It accepts Kubernetes quantities such as `4Gi` or `512Mi`.

If the package contains one container, the script selects it automatically. If it contains multiple containers, the script lists their names and asks you to rerun with the JVM container:

```bash
python3 analyze_jvm.py <diagnostics-package> \
  --container application \
  --flags="..." \
  --memory-limit 4Gi
```

Without effective JVM flags, the script can show the historical memory analysis but cannot produce the standard diagnostics-based recommendation.

## Optional JVM measurements

If you have a heap-used snapshot and metaspace measurement from `jcmd`, provide all three measurement arguments together:

```bash
python3 analyze_jvm.py <diagnostics-package> \
  --flags="..." \
  --memory-limit 4Gi \
  --live-set 922182K \
  --metaspace 132045K \
  --jcmd-pod application-7c8d9f6b5-x2abc
```

| Argument | Value to provide |
|---|---|
| `--live-set` | Heap `used` value from `jcmd <pid> GC.heap_info`, collected after requesting GC. |
| `--metaspace` | Metaspace `used` value from the same `GC.heap_info` output. |
| `--jcmd-pod` | Name of the pod from which both measurements were collected. |

Requesting garbage collection can briefly pause the application. Follow your organization's operational process when collecting these optional measurements.

## Useful options

| Option | When to use it |
|---|---|
| `--risk-profile savings` | Prefer tighter sizing margins. |
| `--risk-profile balanced` | Use the recommended default. |
| `--risk-profile reliability` | Prefer larger sizing margins. |
| `--heap-style percentage` | Emit `MaxRAMPercentage` and `InitialRAMPercentage`. |
| `--heap-style absolute` | Emit `-Xmx` and `-Xms`. |
| `--assume-g1` | Suppress the unknown-collector warning only when G1GC was confirmed independently but is not visible in the supplied flags. |

### Heap configuration style

The default, `--heap-style auto`, follows explicit `-Xmx`/`-Xms` or percentage-only flags when their origin is clear. Effective `VM.flags` output can contain both calculated heap sizes and RAM percentages; in that ambiguous case, the script uses absolute output and explains how to request percentage output instead.

Changing `--heap-style` changes only how the same recommendation is expressed. Do not apply absolute and percentage heap settings together.

### Garbage collector

The defaults are calibrated around G1GC. An unknown or non-G1 collector produces a warning but does not prevent a recommendation. If a non-G1 collector is explicitly detected, `--assume-g1` does not suppress the warning.

## Output

The script prints one terminal result containing:

- The selected workload and container.
- Historical container working-set statistics.
- Parsed JVM configuration and relevant warnings.
- Current, recommended, and changed values for JVM maximum heap, JVM initial heap, Kubernetes memory request, and Kubernetes memory limit.
- The exact configuration style to apply.

The script does not create an HTML report or any other output file. If a safety guardrail withholds the apply-ready configuration, reconcile the inputs and rerun before making changes.

For collection instructions, calculation details, and advanced overrides, see the [JVM Memory Sizing Guide](jvm_sizing_guide.html) or run:

```bash
python3 analyze_jvm.py --help
```
