
#!/usr/bin/env python3

import argparse
import hashlib
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

RUNS_DIR = PROJECT_ROOT / "data/raw/runs"
ARCHIVES_DIR = PROJECT_ROOT / "data/raw/archives"

SCENARIOS = (
    "adFailure",
    "adHighCpu",
    "paymentFailure",
    "paymentUnreachable",
    "productCatalogLockContention",
)

REQUIRED_FILES = {
    "logs.jsonl",
    "metrics.jsonl",
    "metadata.json",
    "locust_stats.json",
}

CHUNK_SIZE = 1024 * 1024


def sha256(stream):
    digest = hashlib.sha256()

    while chunk := stream.read(CHUNK_SIZE):
        digest.update(chunk)

    return digest.hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return sha256(stream)


def get_files(run_dir):
    return sorted(
        path
        for path in run_dir.rglob("*")
        if path.is_file()
        and path.name != ".DS_Store"
        and not path.name.startswith("._")
    )


def validate_run(run_dir):
    if not run_dir.is_dir():
        raise RuntimeError(
            f"Run directory not found: {run_dir}"
        )

    for filename in sorted(REQUIRED_FILES):
        path = run_dir / filename

        if not path.is_file():
            raise RuntimeError(
                f"Missing required file: {path}"
            )

        if path.stat().st_size == 0:
            raise RuntimeError(
                f"Empty required file: {path}"
            )


def verify_archive(archive_path, run_dir):
    files = get_files(run_dir)

    expected = {
        path.relative_to(RUNS_DIR).as_posix(): path
        for path in files
    }

    with zipfile.ZipFile(archive_path, "r") as archive:
        bad_file = archive.testzip()

        if bad_file is not None:
            raise RuntimeError(
                f"CRC verification failed: {bad_file}"
            )

        members = [
            item.filename
            for item in archive.infolist()
            if not item.is_dir()
        ]

        if len(members) != len(expected):
            raise RuntimeError(
                "Unexpected number of archived files"
            )

        if set(members) != set(expected):
            raise RuntimeError(
                "Archive contents differ from source"
            )

        for name, source_path in expected.items():
            source_hash = file_hash(source_path)

            with archive.open(name) as stream:
                archive_hash = sha256(stream)

            if source_hash != archive_hash:
                raise RuntimeError(
                    f"SHA-256 mismatch: {name}"
                )


def archive_run(run_dir, overwrite=False):
    archive_path = ARCHIVES_DIR / f"{run_dir.name}.zip"

    if archive_path.exists() and not overwrite:
        verify_archive(archive_path, run_dir)
        return archive_path, "SKIPPED (verified)"

    temp_path = archive_path.with_suffix(".zip.tmp")

    try:
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:

            for path in get_files(run_dir):
                archive.write(
                    path,
                    arcname=path.relative_to(RUNS_DIR).as_posix(),
                )

        verify_archive(temp_path, run_dir)

        # Replace the final archive only after verification.
        temp_path.replace(archive_path)

    finally:
        if temp_path.exists():
            temp_path.unlink()

    return archive_path, "CREATED"


def format_size(size):
    return f"{size / (1024 ** 2):.2f} MiB"


def main():
    parser = argparse.ArgumentParser(
        description="Archive and verify fault telemetry runs"
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing archives after verification",
    )

    parser.add_argument(
        "--cycle",
        type=int,
        choices=range(1, 21),
        metavar="1-20",
        help="Archive only the five runs of a selected cycle",
    )

    args = parser.parse_args()

    cycles = (
        [args.cycle]
        if args.cycle is not None
        else range(1, 21)
    )

    run_ids = [
        f"fault_{scenario}_{cycle:03d}"
        for scenario in SCENARIOS
        for cycle in cycles
    ]

    run_dirs = [
        RUNS_DIR / run_id
        for run_id in run_ids
    ]

    print(f"Source:      {RUNS_DIR}")
    print(f"Destination: {ARCHIVES_DIR}")
    print(f"Expected runs: {len(run_dirs)}")
    print()

    # Validate all selected runs before creating archives.
    for run_dir in run_dirs:
        validate_run(run_dir)

    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)

    total_raw = 0
    total_archived = 0

    created = 0
    skipped = 0
    failed = []

    for index, run_dir in enumerate(run_dirs, start=1):
        try:
            raw_size = sum(
                path.stat().st_size
                for path in get_files(run_dir)
            )

            archive_path, status = archive_run(
                run_dir,
                overwrite=args.overwrite,
            )

            archive_size = archive_path.stat().st_size

            total_raw += raw_size
            total_archived += archive_size

            if status.startswith("SKIPPED"):
                skipped += 1
            else:
                created += 1

            ratio = raw_size / archive_size

            print(
                f"[{index:03d}/{len(run_dirs):03d}] "
                f"{run_dir.name}: "
                f"{format_size(raw_size)} -> "
                f"{format_size(archive_size)} | "
                f"{ratio:.1f}x | {status}",
                flush=True,
            )

        except Exception as error:
            failed.append(run_dir.name)

            print(
                f"[{index:03d}/{len(run_dirs):03d}] "
                f"{run_dir.name}: FAILED: {error}",
                flush=True,
            )

    print()
    print("=" * 65)
    print("ARCHIVING SUMMARY")
    print("=" * 65)

    print(f"Expected:  {len(run_dirs)}")
    print(f"Created:   {created}")
    print(f"Skipped:   {skipped}")
    print(f"Verified:  {created + skipped}")
    print(f"Failed:    {len(failed)}")

    print(f"Raw size:     {format_size(total_raw)}")
    print(f"Archive size: {format_size(total_archived)}")

    if total_raw and total_archived:
        ratio = total_raw / total_archived
        reduction = (1 - total_archived / total_raw) * 100

        print(f"Compression ratio: {ratio:.2f}x")
        print(f"Space reduction:   {reduction:.2f}%")

    if failed:
        print("\nFailed runs:")

        for run_id in failed:
            print(f"  - {run_id}")

        return 1

    print("\nPASS: all selected archives verified successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
