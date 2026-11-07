
import argparse
import json
import os
import subprocess
import time

from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT.parent / "data" / "raw" / "runs"

FLAGS_FILE = ROOT / "src" / "flagd" / "demo.flagd.json"

COMPOSE_FILES = [
    "compose.yaml",
    "compose.experiment.yaml",
    "compose.telemetry.yaml",
    "compose.loadtest.yaml",
    "compose.runs.yaml",
]

ROOT_SERVICES = {
    "adHighCpu": "ad",
    "adFailure": "ad",
    "paymentFailure": "payment",
    "paymentUnreachable": "payment",
    "productCatalogLockContention": "product-catalog",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def compose(args, run_dir=None):
    command = ["docker", "compose"]

    files = (
        COMPOSE_FILES
        if run_dir is not None
        else COMPOSE_FILES[:-1]
    )

    for filename in files:
        command.extend(["-f", filename])

    command.extend(args)

    env = os.environ.copy()

    if run_dir is not None:
        env["EXPERIMENT_RUN_DIR"] = str(run_dir.resolve())
    else:
        env.pop("EXPERIMENT_RUN_DIR", None)

    subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
    )


def validate_jsonl(path):
    valid = 0
    invalid = 0

    if not path.exists():
        return {
            "exists": False,
            "size_bytes": 0,
            "valid_lines": 0,
            "invalid_lines": 0,
        }

    with path.open(encoding="utf-8") as file:
        for line in file:
            try:
                json.loads(line)
                valid += 1
            except json.JSONDecodeError:
                invalid += 1

    return {
        "exists": True,
        "size_bytes": path.stat().st_size,
        "valid_lines": valid,
        "invalid_lines": invalid,
    }


def get_locust_stats():
    command = [
        "docker",
        "exec",
        "load-generator",
        "python",
        "-c",
        (
            "import urllib.request, sys; "
            "sys.stdout.write("
            "urllib.request.urlopen("
            "'http://localhost:8089/stats/requests', "
            "timeout=5).read().decode())"
        ),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
    )

    return json.loads(result.stdout)


def get_locust_total(stats):
    for item in stats.get("stats", []):
        if item.get("name") == "Aggregated":
            return item

    raise RuntimeError(
        "Aggregated Locust statistics not found"
    )


def read_flags():
    with FLAGS_FILE.open(encoding="utf-8") as file:
        return json.load(file)["flags"]


def read_flag_variant(flag):
    return read_flags()[flag]["defaultVariant"]


def set_flag(flag, variant):
    """
    Changes the flag using set_flag.py and verifies
    the resulting value in the local configuration file.

    This does not prove that the target service
    has already applied the flag.
    """

    subprocess.run(
        [
            "python3",
            str(ROOT / "experiment" / "set_flag.py"),
            flag,
            variant,
        ],
        cwd=ROOT,
        check=True,
    )

    actual = read_flag_variant(flag)

    if actual != variant:
        raise RuntimeError(
            f"Flag {flag}: expected {variant}, got {actual}"
        )

    return utc_now()


def switch_flag_record(flag, variant):
    """
    Records the request time and the time when
    the local configuration change was verified.

    The actual service application time is unknown.
    """

    record = {
        "flag": flag,
        "requested_variant": variant,
        "requested_at": utc_now(),
        "file_confirmed_at": None,
        "file_status": "pending",
        "service_applied": "unknown",
    }

    try:
        record["file_confirmed_at"] = set_flag(
            flag,
            variant,
        )
        record["file_status"] = "confirmed"

    except Exception as error:
        record["file_status"] = "failed"
        record["file_error"] = repr(error)

    return record


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--id",
        required=True,
    )

    parser.add_argument(
        "--scenario",
        choices=["normal", "fault"],
        default="normal",
    )

    parser.add_argument(
        "--duration",
        type=int,
        default=120,
    )

    parser.add_argument(
        "--warmup",
        type=int,
        default=30,
    )

    parser.add_argument(
        "--flag",
        default="adHighCpu",
    )

    parser.add_argument(
        "--variant",
        default="on",
    )

    parser.add_argument(
        "--baseline",
        type=int,
        default=120,
    )

    parser.add_argument(
        "--fault-duration",
        type=int,
        default=120,
    )

    parser.add_argument(
        "--recovery",
        type=int,
        default=120,
    )

    args = parser.parse_args()

    # Validate run ID

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789_-"
    )

    if not args.id or any(
        char not in allowed for char in args.id
    ):
        parser.error(
            "Run ID may contain only letters, "
            "digits, _ and -"
        )

    # Validate durations

    if args.duration <= 0 or args.warmup < 0:
        parser.error(
            "Duration must be positive "
            "and warmup nonnegative"
        )

    if args.scenario == "fault":
        if min(
            args.baseline,
            args.fault_duration,
            args.recovery,
        ) <= 0:
            parser.error(
                "Fault phase durations must be positive"
            )

        try:
            flags = read_flags()
        except Exception as error:
            parser.error(
                f"Cannot read flag configuration: {error}"
            )

        if (
            args.flag not in flags
            or args.variant not in flags[args.flag]["variants"]
        ):
            parser.error("Unknown flag or variant")

        if args.variant == "off":
            parser.error(
                "Fault variant cannot be off"
            )

        if flags[args.flag]["defaultVariant"] != "off":
            parser.error(
                f"Flag {args.flag} must be off "
                "before starting"
            )

    # Create run directory

    run_dir = RUNS_DIR / args.id

    if run_dir.exists():
        parser.error(
            f"Run directory already exists: {run_dir}"
        )

    run_dir.mkdir(parents=True)

    metadata_path = run_dir / "metadata.json"

    total_duration = (
        args.duration
        if args.scenario == "normal"
        else (
            args.baseline
            + args.fault_duration
            + args.recovery
        )
    )

    metadata = {
        "metadata_schema_version": 3,
        "run_id": args.id,
        "scenario": args.scenario,
        "warmup_seconds": args.warmup,
        "duration_seconds": total_duration,
        "collector_started_at": None,
        "observation_started_at": None,
        "observation_ended_at": None,
        "collector_stopped_at": None,
        "collector_restored": False,
        "status": "created",
        "phases": {},
        "switch_events": [],
    }

    if args.scenario == "fault":
        metadata.update({
            "fault_flag": args.flag,
            "fault_variant": args.variant,
            "root_service": ROOT_SERVICES.get(args.flag),
            "baseline_seconds": args.baseline,
            "fault_seconds": args.fault_duration,
            "recovery_seconds": args.recovery,
            "flag_restored": False,
        })

    def save():
        save_json(metadata_path, metadata)

    save()

    experiment_error = None

    try:
        # Start isolated Collector

        print("Starting Collector...", flush=True)

        compose(
            [
                "up",
                "-d",
                "--no-build",
                "--no-deps",
                "--force-recreate",
                "otel-collector",
            ],
            run_dir,
        )

        result = subprocess.run(
            [
                "docker",
                "inspect",
                "otel-collector",
                "--format",
                "{{.State.Running}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        if result.stdout.strip() != "true":
            raise RuntimeError(
                "Collector is not running"
            )

        metadata["collector_started_at"] = utc_now()
        metadata["status"] = "warming_up"
        save()

        # Warmup

        print(
            f"Warmup: {args.warmup} seconds",
            flush=True,
        )

        time.sleep(args.warmup)

        # Initial Locust statistics

        locust_before = get_locust_stats()

        metadata["locust_users_start"] = (
            locust_before.get("user_count")
        )

        metadata["observation_started_at"] = utc_now()
        metadata["status"] = "recording"
        save()

        # NORMAL SCENARIO

        if args.scenario == "normal":
            print(
                f"Normal observation: "
                f"{args.duration} seconds",
                flush=True,
            )

            metadata["phases"]["normal"] = {
                "start": metadata[
                    "observation_started_at"
                ],
            }

            save()

            time.sleep(args.duration)

            metadata["phases"]["normal"]["end"] = (
                utc_now()
            )

            save()

        # FAULT SCENARIO

        else:
            # Baseline

            metadata["phases"]["normal"] = {
                "start": utc_now(),
            }

            save()

            print(
                f"Baseline: {args.baseline} seconds",
                flush=True,
            )

            time.sleep(args.baseline)

            metadata["phases"]["normal"]["end"] = (
                utc_now()
            )

            save()

            # Enable fault

            print(
                f"Fault {args.flag}={args.variant}: "
                f"{args.fault_duration} seconds",
                flush=True,
            )

            fault_switch = switch_flag_record(
                args.flag,
                args.variant,
            )

            metadata["switch_events"].append(
                fault_switch
            )

            metadata["phases"]["fault"] = {
                "switch_requested_at": (
                    fault_switch["requested_at"]
                ),
                "switch_confirmed_at": (
                    fault_switch["file_confirmed_at"]
                ),
                "file_confirmed_at": (
                    fault_switch["file_confirmed_at"]
                ),
                "start": (
                    fault_switch["file_confirmed_at"]
                ),
                "start_boundary_basis": (
                    "file_confirmed_not_service_applied"
                ),
            }

            save()

            if not fault_switch["file_confirmed_at"]:
                raise RuntimeError(
                    "Failed to enable fault flag"
                )

            # Fault observation

            time.sleep(args.fault_duration)

            # Disable fault

            print(
                "Switching fault off...",
                flush=True,
            )

            off_switch = switch_flag_record(
                args.flag,
                "off",
            )

            metadata["switch_events"].append(
                off_switch
            )

            metadata["phases"]["fault"][
                "end_requested_at"
            ] = off_switch["requested_at"]

            metadata["phases"]["fault"]["end"] = (
                off_switch["file_confirmed_at"]
            )

            metadata["flag_restored"] = bool(
                off_switch["file_confirmed_at"]
            )

            save()

            if not off_switch["file_confirmed_at"]:
                raise RuntimeError(
                    "Failed to disable fault flag"
                )

            # Recovery

            metadata["phases"]["recovery"] = {
                "start": off_switch["file_confirmed_at"],
                "start_boundary_basis": (
                    "file_confirmed_not_service_applied"
                ),
            }

            save()

            print(
                f"Recovery: {args.recovery} seconds",
                flush=True,
            )

            time.sleep(args.recovery)

            metadata["phases"]["recovery"]["end"] = (
                utc_now()
            )

            save()

        # End observation

        metadata["observation_ended_at"] = utc_now()

        # Final Locust statistics

        locust_after = get_locust_stats()

        before_total = get_locust_total(
            locust_before
        )

        after_total = get_locust_total(
            locust_after
        )

        requests = (
            after_total["num_requests"]
            - before_total["num_requests"]
        )

        failures = (
            after_total["num_failures"]
            - before_total["num_failures"]
        )

        if requests < 0 or failures < 0:
            raise RuntimeError(
                "Locust counters reset during run"
            )

        observation_start = datetime.fromisoformat(
            metadata["observation_started_at"]
        )

        observation_end = datetime.fromisoformat(
            metadata["observation_ended_at"]
        )

        actual_duration = (
            observation_end - observation_start
        ).total_seconds()

        locust_result = {
            "users_start": metadata[
                "locust_users_start"
            ],
            "users_end": locust_after.get(
                "user_count"
            ),
            "requests": requests,
            "failures": failures,
            "average_rps": (
                requests / actual_duration
                if actual_duration > 0
                else None
            ),
            "failure_ratio": (
                failures / requests
                if requests
                else None
            ),
        }

        save_json(
            run_dir / "locust_stats.json",
            locust_result,
        )

        metadata["locust_requests"] = requests
        metadata["locust_failures"] = failures

        metadata["locust_users_end"] = (
            locust_after.get("user_count")
        )

        metadata["status"] = "stopping"
        save()

    except BaseException as error:
        experiment_error = error
        metadata["error"] = repr(error)
        metadata["status"] = "failed"
        save()

    finally:
        # Safety reset

        if args.scenario == "fault":
            print(
                "Safety reset: switching fault off...",
                flush=True,
            )

            try:
                reset = switch_flag_record(
                    args.flag,
                    "off",
                )

                metadata["switch_events"].append(
                    reset
                )

                metadata["flag_reset_at"] = (
                    reset["file_confirmed_at"]
                )

                metadata["flag_restored"] = bool(
                    reset["file_confirmed_at"]
                )

                if not metadata["flag_restored"]:
                    raise RuntimeError(
                        "Safety reset failed"
                    )

            except Exception as error:
                metadata["flag_reset_error"] = repr(
                    error
                )
                metadata["status"] = "failed"

        # Stop isolated Collector

        print(
            "Stopping Collector...",
            flush=True,
        )

        try:
            compose(
                [
                    "stop",
                    "-t",
                    "30",
                    "otel-collector",
                ],
                run_dir,
            )

            metadata["collector_stopped_at"] = (
                utc_now()
            )

        except Exception as error:
            metadata["stop_error"] = repr(error)
            metadata["status"] = "failed"

        # Validate collected files

        metadata["logs"] = validate_jsonl(
            run_dir / "logs.jsonl"
        )

        metadata["metrics"] = validate_jsonl(
            run_dir / "metrics.jsonl"
        )

        files_ok = all(
            metadata[name]["exists"]
            and metadata[name]["valid_lines"] > 0
            and metadata[name]["invalid_lines"] == 0
            for name in ("logs", "metrics")
        )

        phase_ok = (
            args.scenario == "normal"
            or (
                metadata.get("flag_restored")
                and all(
                    metadata["phases"]
                    .get(phase, {})
                    .get("end")
                    for phase in (
                        "normal",
                        "fault",
                        "recovery",
                    )
                )
            )
        )

        if (
            experiment_error is None
            and "stop_error" not in metadata
            and "flag_reset_error" not in metadata
            and files_ok
            and metadata["observation_ended_at"]
            and phase_ok
        ):
            metadata["status"] = "completed"
        else:
            metadata["status"] = "failed"

        # Restore pilot Collector configuration

        print(
            "Restoring Collector "
            "to pilot configuration...",
            flush=True,
        )

        try:
            compose(
                [
                    "up",
                    "-d",
                    "--no-build",
                    "--no-deps",
                    "--force-recreate",
                    "otel-collector",
                ]
            )

            metadata["collector_restored"] = True

        except Exception as error:
            metadata["restore_error"] = repr(
                error
            )
            metadata["status"] = "failed"

        save()

    # Final result

    print(
        json.dumps(
            metadata,
            indent=2,
            ensure_ascii=False,
        )
    )

    print(f"Results: {run_dir}")

    if metadata["status"] != "completed":
        raise SystemExit(
            "Experiment failed; check metadata.json"
        )


if __name__ == "__main__":
    main()
