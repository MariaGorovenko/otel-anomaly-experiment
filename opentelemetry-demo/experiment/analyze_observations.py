
#!/usr/bin/env python3
"""
OpenTelemetry Demo: analysis of telemetry coverage and observation windows.

Observation unit:
    one microservice x one time window

Input:
    ../data/raw/runs/<run_id>/
        metadata.json
        logs.jsonl
        metrics.jsonl

Output:
    ../data/analysis/observations/
        service_coverage.csv
        service_breakdown.csv
        unidentified_metrics.csv
        window_coverage.csv
        window_statistics.csv
        metric_stability.csv
        phase_summary.csv
        observation_report.json

Only Python standard library is required.
Input files are never modified.
"""

import argparse
import collections
import csv
import json
import math
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path


METRIC_TYPES = (
    "gauge",
    "sum",
    "histogram",
    "exponentialHistogram",
    "summary",
)

INFRA_SERVICES = {
    "load-generator",
    "otelcol-contrib",
    "frontend-proxy",
    "flagd",
    "flagd-ui",
    "telemetry-docs",
}

METHODS = (
    "service.name",
    "container.name",
    "container.id",
    "unidentified",
)

DEFAULT_WINDOWS = (30, 60, 120, 300)

NS_PER_SECOND = 1_000_000_000


# ------------------------------------------------------------
# General utilities
# ------------------------------------------------------------

def iso_to_ns(value):
    if not value:
        return None

    dt = datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp() * NS_PER_SECOND)


def ns_to_iso(value):
    return datetime.fromtimestamp(
        value / NS_PER_SECOND,
        tz=timezone.utc,
    ).isoformat()


def parse_timestamp(value):
    if value is None or value == "":
        return None

    try:
        result = int(value)
    except (ValueError, TypeError):
        return None

    return result if result > 0 else None


def percentile(values, percent):
    if not values:
        return 0.0

    values = sorted(values)

    if len(values) == 1:
        return float(values[0])

    position = (len(values) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)

    if lower == upper:
        return float(values[lower])

    fraction = position - lower

    return (
        values[lower] * (1 - fraction)
        + values[upper] * fraction
    )


def describe(values):
    if not values:
        return {
            "mean": 0,
            "median": 0,
            "p10": 0,
            "p25": 0,
            "p75": 0,
            "p90": 0,
            "min": 0,
            "max": 0,
        }

    return {
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "p10": round(percentile(values, 10), 3),
        "p25": round(percentile(values, 25), 3),
        "p75": round(percentile(values, 75), 3),
        "p90": round(percentile(values, 90), 3),
        "min": min(values),
        "max": max(values),
    }


def percentage(part, total):
    if not total:
        return 0.0

    return round(100 * part / total, 4)


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ------------------------------------------------------------
# OTLP attribute decoding
# ------------------------------------------------------------

def decode_value(value):
    if not isinstance(value, dict):
        return value

    for key in (
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
        "bytesValue",
    ):
        if key in value:
            return value[key]

    if "arrayValue" in value:
        values = value["arrayValue"].get("values", [])
        return [decode_value(v) for v in values]

    if "kvlistValue" in value:
        return attributes(
            value["kvlistValue"].get("values", [])
        )

    return None


def attributes(items):
    result = {}

    for item in items or []:
        key = item.get("key")

        if key:
            result[key] = decode_value(
                item.get("value", {})
            )

    return result


def valid_service_name(value):
    if value is None:
        return False

    name = str(value).strip()

    if not name:
        return False

    if name.startswith("unknown_service"):
        return False

    return True


# ------------------------------------------------------------
# Read OpenTelemetry JSONL
# ------------------------------------------------------------

def read_jsonl(path, invalid):
    with path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            if not line.strip():
                continue

            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                invalid[path.name] += 1
                continue

            if not isinstance(data, dict):
                invalid[path.name] += 1
                continue

            yield data


def iter_telemetry(path, kind, invalid):
    """
    Yield individual log records or metric data points.

    A JSONL line can contain many OTLP records.
    """

    for data in read_jsonl(path, invalid):

        if kind == "logs":
            resources = data.get("resourceLogs", [])
            scope_key = "scopeLogs"

        else:
            resources = data.get("resourceMetrics", [])
            scope_key = "scopeMetrics"

        for resource_item in resources:
            resource_attrs = attributes(
                resource_item.get(
                    "resource", {}
                ).get("attributes", [])
            )

            for scope in resource_item.get(scope_key, []):

                if kind == "logs":

                    for record in scope.get(
                        "logRecords", []
                    ):
                        record_attrs = attributes(
                            record.get("attributes", [])
                        )

                        timestamp = parse_timestamp(
                            record.get("timeUnixNano")
                        )

                        if timestamp is None:
                            timestamp = parse_timestamp(
                                record.get(
                                    "observedTimeUnixNano"
                                )
                            )

                        yield {
                            "kind": "logs",
                            "resource_attrs": resource_attrs,
                            "point_attrs": record_attrs,
                            "name": None,
                            "timestamp": timestamp,
                        }

                else:

                    for metric in scope.get(
                        "metrics", []
                    ):
                        metric_name = (
                            metric.get("name")
                            or "<unnamed>"
                        )

                        for metric_type in METRIC_TYPES:
                            metric_data = metric.get(
                                metric_type
                            )

                            if not metric_data:
                                continue

                            for point in metric_data.get(
                                "dataPoints", []
                            ):
                                point_attrs = attributes(
                                    point.get(
                                        "attributes", []
                                    )
                                )

                                timestamp = parse_timestamp(
                                    point.get(
                                        "timeUnixNano"
                                    )
                                )

                                yield {
                                    "kind": "metrics",
                                    "resource_attrs": resource_attrs,
                                    "point_attrs": point_attrs,
                                    "name": metric_name,
                                    "timestamp": timestamp,
                                }


# ------------------------------------------------------------
# Known Compose services
# ------------------------------------------------------------

def read_compose_services(path):
    """
    Read service names from the top-level services section.

    This is a conservative parser for the standard Compose layout.
    It does not implement the complete YAML specification.
    """

    if not path.exists():
        return set()

    result = set()
    in_services = False

    for line in path.read_text(
        encoding="utf-8"
    ).splitlines():

        if re.match(r"^services:\s*(#.*)?$", line):
            in_services = True
            continue

        if not in_services:
            continue

        if (
            line.strip()
            and not line.startswith((" ", "\t", "#"))
        ):
            break

        match = re.match(
            r'^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?$',
            line,
        )

        if match:
            result.add(match.group(1))

    return result


# ------------------------------------------------------------
# Service identification
# ------------------------------------------------------------

def build_container_map(run, invalid):
    """
    Learn container.id -> service.name mappings
    from records that contain both attributes.

    Ambiguous IDs are not used.
    """

    mapping = collections.defaultdict(set)

    for kind in ("logs", "metrics"):
        path = run / f"{kind}.jsonl"

        if not path.exists():
            continue

        for record in iter_telemetry(
            path, kind, invalid
        ):
            ra = record["resource_attrs"]
            pa = record["point_attrs"]

            service = ra.get("service.name")

            if not valid_service_name(service):
                continue

            container_id = (
                ra.get("container.id")
                or pa.get("container.id")
            )

            if container_id:
                mapping[str(container_id)].add(
                    str(service)
                )

    resolved = {}
    ambiguous = 0

    for container_id, names in mapping.items():
        if len(names) == 1:
            resolved[container_id] = next(
                iter(names)
            )
        else:
            ambiguous += 1

    return resolved, ambiguous


def identify_service(record, known_services, id_map):
    ra = record["resource_attrs"]
    pa = record["point_attrs"]

    # 1. Explicit OTLP service.name.
    service = ra.get("service.name")

    if valid_service_name(service):
        return str(service), "service.name"

    # 2. Container name validated against Compose.
    for attrs in (ra, pa):
        for key in (
            "container.name",
            "k8s.container.name",
        ):
            value = attrs.get(key)

            if not value:
                continue

            candidate = str(value).lstrip("/")

            if candidate in known_services:
                return candidate, "container.name"

    # 3. Container ID with unambiguous mapping.
    for attrs in (ra, pa):
        container_id = attrs.get("container.id")

        if container_id:
            service = id_map.get(
                str(container_id)
            )

            if service:
                return service, "container.id"

    # Host names and process names are not considered
    # unique microservice identifiers.
    return None, "unidentified"


# ------------------------------------------------------------
# Experiment phases
# ------------------------------------------------------------

def get_phases(metadata):
    phases = []

    for name, info in (
        metadata.get("phases") or {}
    ).items():

        if not isinstance(info, dict):
            continue

        start = iso_to_ns(info.get("start"))
        end = iso_to_ns(info.get("end"))

        if (
            start is not None
            and end is not None
            and end > start
        ):
            phases.append({
                "name": name,
                "start": start,
                "end": end,
            })

    if not phases:
        start = iso_to_ns(
            metadata["observation_started_at"]
        )

        end = iso_to_ns(
            metadata["observation_ended_at"]
        )

        phases = [{
            "name": metadata.get(
                "scenario", "observation"
            ),
            "start": start,
            "end": end,
        }]

    return sorted(
        phases,
        key=lambda item: item["start"],
    )


def locate_phase(timestamp, phases):
    for phase in phases:
        if (
            phase["start"]
            <= timestamp
            < phase["end"]
        ):
            return phase

    return None


def build_windows(phases, window_sizes, tolerance):
    """
    Build deterministic windows independently for each phase.

    A final window can be considered complete when its
    duration differs from the requested duration by no more
    than tolerance seconds.

    This handles sub-second phase boundary differences.
    """

    result = {}
    tolerance_ns = int(
        tolerance * NS_PER_SECOND
    )

    for size in window_sizes:
        span = size * NS_PER_SECOND

        for phase in phases:
            duration = (
                phase["end"] - phase["start"]
            )

            count = math.ceil(
                duration / span
            )

            for index in range(count):
                start = (
                    phase["start"]
                    + index * span
                )

                end = min(
                    start + span,
                    phase["end"],
                )

                if end <= start:
                    continue

                actual_duration = end - start

                complete = (
                    actual_duration
                    >= span - tolerance_ns
                )

                result[
                    (size, phase["name"], index)
                ] = {
                    "start": start,
                    "end": end,
                    "complete": complete,
                }

    return result


def get_window_index(
    timestamp,
    phase,
    size,
):
    span = size * NS_PER_SECOND

    return (
        timestamp - phase["start"]
    ) // span


def is_transition_window(
    start,
    end,
    phase_name,
    phases,
    guard_seconds,
):
    """
    Exclude windows touching a transition guard zone.

    Guard zones apply to phase boundaries between
    normal, fault and recovery, not the start/end
    of the entire run.
    """

    if guard_seconds <= 0:
        return False

    guard = int(
        guard_seconds * NS_PER_SECOND
    )

    boundaries = set()

    for phase in phases:
        boundaries.add(phase["start"])
        boundaries.add(phase["end"])

    first_start = min(
        p["start"] for p in phases
    )

    last_end = max(
        p["end"] for p in phases
    )

    boundaries.discard(first_start)
    boundaries.discard(last_end)

    for boundary in boundaries:
        if (
            start < boundary + guard
            and end > boundary - guard
        ):
            return True

    return False


# ------------------------------------------------------------
# Main run analysis
# ------------------------------------------------------------

def analyze_run(
    run,
    args,
    known_services,
):
    metadata = json.loads(
        (run / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    run_id = metadata.get(
        "run_id", run.name
    )

    scenario = metadata.get(
        "scenario", ""
    )

    root_service = (
        metadata.get("root_service") or ""
    )

    observation_start = iso_to_ns(
        metadata["observation_started_at"]
    )

    observation_end = iso_to_ns(
        metadata["observation_ended_at"]
    )

    phases = get_phases(metadata)

    windows = build_windows(
        phases,
        args.window_sizes,
        args.boundary_tolerance,
    )

    invalid = collections.Counter()

    id_map, ambiguous_ids = build_container_map(
        run,
        collections.Counter(),
    )

    coverage = collections.defaultdict(
        collections.Counter
    )

    service_breakdown = collections.defaultdict(
        collections.Counter
    )

    unassigned = collections.defaultdict(
        collections.Counter
    )

    unidentified = collections.defaultdict(
        lambda: {
            "points": 0,
            "resource_keys": set(),
            "point_keys": set(),
            "examples": set(),
        }
    )

    # (size, phase, index, service) -> observations
    cells = {}

    services_seen = set()

    for kind in ("logs", "metrics"):

        path = run / f"{kind}.jsonl"

        if not path.exists():
            continue

        print(
            f"  Reading {path.name}",
            flush=True,
        )

        for record in iter_telemetry(
            path, kind, invalid
        ):

            service, method = identify_service(
                record,
                known_services,
                id_map,
            )

            coverage[kind][method] += 1

            if service:
                service_breakdown[
                    (kind, service)
                ][method] += 1

            timestamp = record["timestamp"]

            if timestamp is None:
                unassigned[kind][
                    "missing_timestamp"
                ] += 1
                continue

            if not (
                observation_start
                <= timestamp
                < observation_end
            ):
                unassigned[kind][
                    "outside_observation"
                ] += 1
                continue

            coverage[kind][
                "inside_observation"
            ] += 1

            if not service:

                unassigned[kind][
                    "unidentified_inside"
                ] += 1

                if kind == "metrics":
                    metric_name = record["name"]

                    item = unidentified[
                        metric_name
                    ]

                    item["points"] += 1

                    ra = record[
                        "resource_attrs"
                    ]

                    pa = record[
                        "point_attrs"
                    ]

                    item["resource_keys"].update(
                        ra.keys()
                    )

                    item["point_keys"].update(
                        pa.keys()
                    )

                    for key in (
                        "host.name",
                        "container.name",
                        "container.id",
                        "service.name",
                        "process.executable.name",
                    ):
                        value = (
                            ra.get(key)
                            or pa.get(key)
                        )

                        if value is not None:
                            if len(
                                item["examples"]
                            ) < 10:
                                item["examples"].add(
                                    f"{key}={value}"
                                )

                continue

            if (
                not args.include_infra
                and service in INFRA_SERVICES
            ):
                unassigned[kind][
                    "infrastructure_excluded"
                ] += 1
                continue

            phase = locate_phase(
                timestamp, phases
            )

            if phase is None:
                unassigned[kind][
                    "phase_gap"
                ] += 1
                continue

            services_seen.add(service)

            for size in args.window_sizes:

                index = get_window_index(
                    timestamp,
                    phase,
                    size,
                )

                window_key = (
                    size,
                    phase["name"],
                    index,
                )

                if window_key not in windows:
                    continue

                cell_key = (
                    size,
                    phase["name"],
                    index,
                    service,
                )

                if cell_key not in cells:
                    cells[cell_key] = {
                        "logs": 0,
                        "metric_points": 0,
                        "metric_names": set(),
                        "metric_counts": (
                            collections.Counter()
                        ),
                    }

                cell = cells[cell_key]

                if kind == "logs":
                    cell["logs"] += 1

                else:
                    cell["metric_points"] += 1

                    name = record["name"]

                    cell["metric_names"].add(
                        name
                    )

                    cell["metric_counts"][
                        name
                    ] += 1

    # --------------------------------------------------------
    # Coverage
    # --------------------------------------------------------

    coverage_rows = []

    for kind in ("logs", "metrics"):
        total = sum(
            coverage[kind][method]
            for method in METHODS
        )

        for method in METHODS:
            count = coverage[kind][method]

            coverage_rows.append({
                "run_id": run_id,
                "kind": kind,
                "identification": method,
                "records": count,
                "percent": percentage(
                    count, total
                ),
                "total_records": total,
                "inside_observation": (
                    coverage[kind][
                        "inside_observation"
                    ]
                ),
            })

    breakdown_rows = []

    for (kind, service), methods in sorted(
        service_breakdown.items()
    ):
        for method, count in sorted(
            methods.items()
        ):
            breakdown_rows.append({
                "run_id": run_id,
                "kind": kind,
                "service": service,
                "method": method,
                "records": count,
            })

    unidentified_rows = []

    for name, info in sorted(
        unidentified.items(),
        key=lambda item: (
            -item[1]["points"],
            item[0],
        ),
    ):
        unidentified_rows.append({
            "run_id": run_id,
            "metric_name": name,
            "points": info["points"],
            "resource_attribute_keys": (
                "; ".join(
                    sorted(info["resource_keys"])
                )
            ),
            "point_attribute_keys": (
                "; ".join(
                    sorted(info["point_keys"])
                )
            ),
            "attribute_examples": (
                "; ".join(
                    sorted(info["examples"])
                )
            ),
            "reason": (
                "No unambiguous service mapping"
            ),
        })

    # --------------------------------------------------------
    # Observation windows
    # --------------------------------------------------------

    window_rows = []

    # Include zero-data windows for all services that
    # were identified somewhere within the run.
    # This is necessary for missingness statistics.
    for window_key, info in sorted(
        windows.items()
    ):
        size, phase_name, index = window_key

        transition = is_transition_window(
            info["start"],
            info["end"],
            phase_name,
            phases,
            args.transition_guard,
        )

        for service in sorted(
            services_seen
        ):
            cell = cells.get(
                (
                    size,
                    phase_name,
                    index,
                    service,
                ),
                {
                    "logs": 0,
                    "metric_points": 0,
                    "metric_names": set(),
                    "metric_counts": (
                        collections.Counter()
                    ),
                },
            )

            logs = cell["logs"]
            points = cell["metric_points"]

            metric_names = len(
                cell["metric_names"]
            )

            has_logs = logs > 0
            has_metrics = points > 0

            has_both = (
                has_logs and has_metrics
            )

            valid_window = (
                info["complete"]
                and not transition
            )

            metric_ok = (
                points
                >= args.min_metric_points
                and metric_names
                >= args.min_metric_names
            )

            strict = (
                valid_window
                and logs >= args.min_logs
                and metric_ok
            )

            allow_empty_logs = (
                valid_window
                and metric_ok
            )

            window_rows.append({
                "run_id": run_id,
                "scenario": scenario,
                "root_service": root_service,
                "window_seconds": size,
                "phase": phase_name,
                "service": service,
                "window_index": index,
                "window_start": ns_to_iso(
                    info["start"]
                ),
                "window_end": ns_to_iso(
                    info["end"]
                ),
                "complete_window": (
                    info["complete"]
                ),
                "transition_guard": transition,
                "log_records": logs,
                "metric_points": points,
                "distinct_metric_names": (
                    metric_names
                ),
                "has_logs": has_logs,
                "has_metrics": has_metrics,
                "has_both": has_both,
                "eligible_strict": strict,
                "eligible_allow_empty_logs": (
                    allow_empty_logs
                ),
            })

    # --------------------------------------------------------
    # Statistics by service, phase and window size
    # --------------------------------------------------------

    grouped = collections.defaultdict(
        list
    )

    phase_grouped = collections.defaultdict(
        list
    )

    for row in window_rows:
        grouped[
            (
                row["window_seconds"],
                row["phase"],
                row["service"],
            )
        ].append(row)

        phase_grouped[
            (
                row["window_seconds"],
                row["phase"],
            )
        ].append(row)

    statistics_rows = []

    for key, group in sorted(
        grouped.items()
    ):
        size, phase, service = key

        logs = [
            row["log_records"]
            for row in group
        ]

        points = [
            row["metric_points"]
            for row in group
        ]

        names = [
            row["distinct_metric_names"]
            for row in group
        ]

        log_stats = describe(logs)
        point_stats = describe(points)
        name_stats = describe(names)

        total = len(group)

        statistics_rows.append({
            "run_id": run_id,
            "window_seconds": size,
            "phase": phase,
            "service": service,
            "total_windows": total,
            "empty_logs_percent": percentage(
                sum(x == 0 for x in logs),
                total,
            ),
            "empty_metrics_percent": percentage(
                sum(x == 0 for x in points),
                total,
            ),
            "logs_mean": log_stats["mean"],
            "logs_median": log_stats["median"],
            "logs_p10": log_stats["p10"],
            "logs_p90": log_stats["p90"],
            "logs_max": log_stats["max"],
            "metric_points_mean": (
                point_stats["mean"]
            ),
            "metric_points_median": (
                point_stats["median"]
            ),
            "metric_points_p10": (
                point_stats["p10"]
            ),
            "metric_points_p90": (
                point_stats["p90"]
            ),
            "metric_names_median": (
                name_stats["median"]
            ),
            "metric_names_p10": (
                name_stats["p10"]
            ),
            "metric_names_p90": (
                name_stats["p90"]
            ),
            "strict_eligible": sum(
                row["eligible_strict"]
                for row in group
            ),
            "allow_empty_logs_eligible": sum(
                row["eligible_allow_empty_logs"]
                for row in group
            ),
        })

    phase_rows = []

    for key, group in sorted(
        phase_grouped.items()
    ):
        size, phase = key

        phase_rows.append({
            "run_id": run_id,
            "scenario": scenario,
            "root_service": root_service,
            "window_seconds": size,
            "phase": phase,
            "total_service_windows": len(group),
            "with_logs": sum(
                r["has_logs"] for r in group
            ),
            "with_metrics": sum(
                r["has_metrics"] for r in group
            ),
            "with_both": sum(
                r["has_both"] for r in group
            ),
            "complete": sum(
                r["complete_window"]
                for r in group
            ),
            "transition_excluded": sum(
                r["transition_guard"]
                for r in group
            ),
            "eligible_strict": sum(
                r["eligible_strict"]
                for r in group
            ),
            "eligible_allow_empty_logs": sum(
                r["eligible_allow_empty_logs"]
                for r in group
            ),
            "total_logs": sum(
                r["log_records"]
                for r in group
            ),
            "total_metric_points": sum(
                r["metric_points"]
                for r in group
            ),
        })

    # --------------------------------------------------------
    # Metric stability
    # --------------------------------------------------------

    stability_groups = collections.defaultdict(
        lambda: {
            "windows": set(),
            "metric_windows": (
                collections.defaultdict(set)
            ),
            "metric_points": (
                collections.Counter()
            ),
        }
    )

    for (
        size,
        phase,
        index,
        service,
    ), cell in cells.items():

        key = (size, phase, service)

        stability_groups[key]["windows"].add(
            index
        )

        for name, count in (
            cell["metric_counts"].items()
        ):
            stability_groups[key][
                "metric_windows"
            ][name].add(index)

            stability_groups[key][
                "metric_points"
            ][name] += count

    # Denominator: all complete, non-transition
    # windows for this phase, not only windows
    # where a metric happened to appear.
    valid_window_counts = (
        collections.Counter()
    )

    for (
        size,
        phase,
        index,
    ), info in windows.items():

        if not info["complete"]:
            continue

        if is_transition_window(
            info["start"],
            info["end"],
            phase,
            phases,
            args.transition_guard,
        ):
            continue

        valid_window_counts[
            (size, phase)
        ] += 1

    stability_rows = []

    for key, info in sorted(
        stability_groups.items()
    ):
        size, phase, service = key

        denominator = valid_window_counts[
            (size, phase)
        ]

        for metric_name, indices in sorted(
            info["metric_windows"].items()
        ):
            valid_indices = set()

            for index in indices:
                window = windows.get(
                    (size, phase, index)
                )

                if not window:
                    continue

                if not window["complete"]:
                    continue

                if is_transition_window(
                    window["start"],
                    window["end"],
                    phase,
                    phases,
                    args.transition_guard,
                ):
                    continue

                valid_indices.add(index)

            present = len(valid_indices)

            frequency = percentage(
                present,
                denominator,
            )

            stability_rows.append({
                "run_id": run_id,
                "window_seconds": size,
                "phase": phase,
                "service": service,
                "metric_name": metric_name,
                "valid_windows": denominator,
                "windows_with_metric": present,
                "presence_percent": frequency,
                "total_points": (
                    info["metric_points"][
                        metric_name
                    ]
                ),
                "stable_90_percent": (
                    frequency >= 90
                ),
            })

    report = {
        "run_id": run_id,
        "scenario": scenario,
        "root_service": root_service,
        "fault_flag": metadata.get(
            "fault_flag"
        ),
        "status": metadata.get("status"),
        "observation_start": (
            metadata["observation_started_at"]
        ),
        "observation_end": (
            metadata["observation_ended_at"]
        ),
        "identified_services": sorted(
            services_seen
        ),
        "identification": coverage_rows,
        "unassigned": {
            key: dict(value)
            for key, value in unassigned.items()
        },
        "ambiguous_container_ids": (
            ambiguous_ids
        ),
        "invalid_json_lines": dict(invalid),
        "thresholds": {
            "min_logs": args.min_logs,
            "min_metric_points": (
                args.min_metric_points
            ),
            "min_metric_names": (
                args.min_metric_names
            ),
            "boundary_tolerance_seconds": (
                args.boundary_tolerance
            ),
            "transition_guard_seconds": (
                args.transition_guard
            ),
        },
        "phase_summary": phase_rows,
        "notes": [
            "Identification is not anomaly labeling.",
            "Fault phase does not prove fault manifestation.",
            "Missing logs may be normal service behavior.",
            "Telemetry points are not independent ML samples.",
            "Metric stability uses metric names, not "
            "individual attribute series.",
        ],
    }

    return {
        "coverage": coverage_rows,
        "breakdown": breakdown_rows,
        "unidentified": unidentified_rows,
        "windows": window_rows,
        "statistics": statistics_rows,
        "stability": stability_rows,
        "phases": phase_rows,
        "report": report,
    }


# ------------------------------------------------------------
# Terminal summary
# ------------------------------------------------------------

def print_summary(results):
    coverage = results["coverage"]
    phases = results["phases"]

    print("\nSERVICE IDENTIFICATION")

    for kind in ("logs", "metrics"):
        rows = [
            row for row in coverage
            if row["kind"] == kind
        ]

        total = (
            rows[0]["total_records"]
            if rows else 0
        )

        identified = sum(
            row["records"]
            for row in rows
            if row["identification"]
            != "unidentified"
        )

        print(
            f"  {kind}: "
            f"{identified}/{total} "
            f"({percentage(identified, total):.2f}%)"
        )

    print("\nOBSERVATION WINDOWS")

    sizes = sorted({
        row["window_seconds"]
        for row in phases
    })

    for size in sizes:
        group = [
            row for row in phases
            if row["window_seconds"] == size
        ]

        strict = sum(
            row["eligible_strict"]
            for row in group
        )

        relaxed = sum(
            row["eligible_allow_empty_logs"]
            for row in group
        )

        total = sum(
            row["total_service_windows"]
            for row in group
        )

        print(
            f"  {size:>3}s | "
            f"total={total:>5} | "
            f"strict={strict:>5} | "
            f"allow_empty_logs={relaxed:>5}"
        )


# ------------------------------------------------------------
# Command line
# ------------------------------------------------------------

def main():
    script_path = Path(__file__).resolve()

    repo_dir = script_path.parent.parent

    default_runs = (
        repo_dir.parent
        / "data"
        / "raw"
        / "runs"
    )

    default_output = (
        repo_dir.parent
        / "data"
        / "analysis"
        / "observations"
    )

    parser = argparse.ArgumentParser(
        description=(
            "Analyze OpenTelemetry observations "
            "by service, phase and time window."
        )
    )

    parser.add_argument(
        "run_ids",
        nargs="*",
        help=(
            "Run IDs. If omitted, analyze all "
            "directories containing metadata.json."
        ),
    )

    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=default_runs,
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
    )

    parser.add_argument(
        "--compose",
        type=Path,
        default=repo_dir / "compose.yaml",
    )

    parser.add_argument(
        "--window-sizes",
        type=int,
        nargs="+",
        default=list(DEFAULT_WINDOWS),
    )

    parser.add_argument(
        "--min-logs",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--min-metric-points",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--min-metric-names",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--boundary-tolerance",
        type=float,
        default=1.0,
        help=(
            "Allowed shortfall in seconds when "
            "checking completeness of the last window."
        ),
    )

    parser.add_argument(
        "--transition-guard",
        type=float,
        default=0.0,
        help=(
            "Seconds excluded around phase transitions. "
            "Default 0; use e.g. 10 for sensitivity analysis."
        ),
    )

    parser.add_argument(
        "--include-infra",
        action="store_true",
    )

    args = parser.parse_args()

    if any(
        size <= 0
        for size in args.window_sizes
    ):
        parser.error(
            "Window sizes must be positive."
        )

    if (
        args.min_logs < 1
        or args.min_metric_points < 1
        or args.min_metric_names < 1
    ):
        parser.error(
            "Minimum thresholds must be >= 1."
        )

    if (
        args.boundary_tolerance < 0
        or args.transition_guard < 0
    ):
        parser.error(
            "Time tolerances must be non-negative."
        )

    args.window_sizes = sorted(
        set(args.window_sizes)
    )

    runs_dir = args.runs_dir.resolve()
    output = args.output.resolve()

    known_services = read_compose_services(
        args.compose
    )

    if not known_services:
        print(
            "WARNING: Compose services unavailable. "
            "container.name matching is disabled."
        )

    if args.run_ids:
        runs = [
            runs_dir / run_id
            for run_id in args.run_ids
        ]

    else:
        runs = sorted(
            path.parent
            for path in runs_dir.glob(
                "*/metadata.json"
            )
        )

    if not runs:
        parser.error(
            f"No runs found in {runs_dir}"
        )

    combined = {
        "coverage": [],
        "breakdown": [],
        "unidentified": [],
        "windows": [],
        "statistics": [],
        "stability": [],
        "phases": [],
        "reports": [],
    }

    for run in runs:
        metadata_path = (
            run / "metadata.json"
        )

        if not metadata_path.exists():
            print(
                f"SKIP: {run.name} "
                "(metadata.json missing)"
            )
            continue

        print(
            f"\nAnalyzing {run.name}",
            flush=True,
        )

        results = analyze_run(
            run,
            args,
            known_services,
        )

        for key in (
            "coverage",
            "breakdown",
            "unidentified",
            "windows",
            "statistics",
            "stability",
            "phases",
        ):
            combined[key].extend(
                results[key]
            )

        combined["reports"].append(
            results["report"]
        )

        print_summary(results)

    output.mkdir(
        parents=True,
        exist_ok=True,
    )

    columns = {
        "service_coverage.csv": [
            "run_id",
            "kind",
            "identification",
            "records",
            "percent",
            "total_records",
            "inside_observation",
        ],
        "service_breakdown.csv": [
            "run_id",
            "kind",
            "service",
            "method",
            "records",
        ],
        "unidentified_metrics.csv": [
            "run_id",
            "metric_name",
            "points",
            "resource_attribute_keys",
            "point_attribute_keys",
            "attribute_examples",
            "reason",
        ],
        "window_coverage.csv": [
            "run_id",
            "scenario",
            "root_service",
            "window_seconds",
            "phase",
            "service",
            "window_index",
            "window_start",
            "window_end",
            "complete_window",
            "transition_guard",
            "log_records",
            "metric_points",
            "distinct_metric_names",
            "has_logs",
            "has_metrics",
            "has_both",
            "eligible_strict",
            "eligible_allow_empty_logs",
        ],
        "window_statistics.csv": [
            "run_id",
            "window_seconds",
            "phase",
            "service",
            "total_windows",
            "empty_logs_percent",
            "empty_metrics_percent",
            "logs_mean",
            "logs_median",
            "logs_p10",
            "logs_p90",
            "logs_max",
            "metric_points_mean",
            "metric_points_median",
            "metric_points_p10",
            "metric_points_p90",
            "metric_names_median",
            "metric_names_p10",
            "metric_names_p90",
            "strict_eligible",
            "allow_empty_logs_eligible",
        ],
        "metric_stability.csv": [
            "run_id",
            "window_seconds",
            "phase",
            "service",
            "metric_name",
            "valid_windows",
            "windows_with_metric",
            "presence_percent",
            "total_points",
            "stable_90_percent",
        ],
        "phase_summary.csv": [
            "run_id",
            "scenario",
            "root_service",
            "window_seconds",
            "phase",
            "total_service_windows",
            "with_logs",
            "with_metrics",
            "with_both",
            "complete",
            "transition_excluded",
            "eligible_strict",
            "eligible_allow_empty_logs",
            "total_logs",
            "total_metric_points",
        ],
    }

    file_mapping = {
        "service_coverage.csv": "coverage",
        "service_breakdown.csv": "breakdown",
        "unidentified_metrics.csv": "unidentified",
        "window_coverage.csv": "windows",
        "window_statistics.csv": "statistics",
        "metric_stability.csv": "stability",
        "phase_summary.csv": "phases",
    }

    for filename, key in file_mapping.items():
        write_csv(
            output / filename,
            combined[key],
            columns[filename],
        )

    write_json(
        output / "observation_report.json",
        {
            "analysis_version": 2,
            "window_sizes": args.window_sizes,
            "runs": combined["reports"],
        },
    )

    print("\nAnalysis completed.")
    print(f"Runs analyzed: {len(combined['reports'])}")
    print(f"Output directory: {output}")


if __name__ == "__main__":
    main()
