#!/usr/bin/env python3
"""Reproducible batch collection of five OpenTelemetry Demo fault scenarios.

Place in opentelemetry-demo/experiment/. No third-party Python packages required.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT.parent / 'data' / 'raw' / 'runs'
FLAGS = ROOT / 'src' / 'flagd' / 'demo.flagd.json'
PLAN = ROOT / 'experiment' / 'fault_batch_plan.json'
LOG = ROOT / 'experiment' / 'fault_batch_events.jsonl'
LOCK = ROOT / 'experiment' / '.fault_batch.lock'
SCENARIOS = [
    ('adHighCpu', 'on'),
    ('adFailure', 'on'),
    ('paymentFailure', '100%'),
    ('paymentUnreachable', 'on'),
    ('productCatalogLockContention', 'on'),
]
COMPOSE = ['docker', 'compose', '-f', 'compose.yaml', '-f', 'compose.experiment.yaml',
           '-f', 'compose.telemetry.yaml', '-f', 'compose.loadtest.yaml']
EXPECTED_SERVICES = [
    'ad', 'astronomy-db', 'cart', 'checkout', 'currency', 'email', 'flagd',
    'flagd-ui', 'frontend', 'frontend-proxy', 'image-provider', 'load-generator',
    'otel-collector', 'payment', 'product-catalog', 'quote', 'recommendation',
    'shipping', 'telemetry-docs', 'valkey-cart',
]


def now():
    return datetime.now(timezone.utc).isoformat()


def event(kind, **data):
    record = {'time': now(), 'event': kind, **data}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(f"[{record['time']}] {kind}: {json.dumps(data, ensure_ascii=False)}", flush=True)


def command(args, *, capture=True, timeout=45):
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=capture,
                          check=True, timeout=timeout)


def make_plan(seed):
    rng = random.Random(seed)
    # Four blocks of five rotations. Each scenario occurs exactly four times
    # in each of the five positions across 20 cycles.
    cycles = []
    for block in range(4):
        base = list(SCENARIOS)
        rng.shuffle(base)
        rotations = [base[i:] + base[:i] for i in range(5)]
        rng.shuffle(rotations)
        cycles.extend(rotations)
    counter = {flag: 0 for flag, _ in SCENARIOS}
    entries = []
    for cycle_number, cycle in enumerate(cycles, 1):
        for position, (flag, variant) in enumerate(cycle, 1):
            counter[flag] += 1
            entries.append({'cycle': cycle_number, 'position': position,
                            'run_id': f'fault_{flag}_{counter[flag]:03d}',
                            'flag': flag, 'variant': variant})
    return {'version': 1, 'seed': seed, 'protocol': {'warmup': 180, 'baseline': 300,
            'fault_duration': 600, 'recovery': 300, 'pause': 180,
            'expected_locust_users': 5}, 'entries': entries}


def load_or_create_plan(seed):
    if PLAN.exists():
        plan = json.loads(PLAN.read_text(encoding='utf-8'))
        if plan != make_plan(plan.get('seed')):
            raise RuntimeError('Saved plan has unexpected contents; refusing to proceed')
        if seed is not None and plan['seed'] != seed:
            raise RuntimeError(f"Plan already uses seed {plan['seed']}; requested {seed}")
        return plan
    seed = 20261122 if seed is None else seed
    plan = make_plan(seed)
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    event('plan_created', path=str(PLAN), seed=seed)
    return plan


def flags_off():
    flags = json.loads(FLAGS.read_text(encoding='utf-8'))['flags']
    for name, variant in SCENARIOS:
        if name not in flags:
            raise RuntimeError(f'Missing feature flag: {name}')
        obj = flags[name]
        if obj.get('defaultVariant') != 'off' or obj.get('state') != 'ENABLED':
            raise RuntimeError(f'{name}: expected ENABLED and defaultVariant=off')
        if variant not in obj.get('variants', {}):
            raise RuntimeError(f'{name}: variant {variant!r} unavailable')


def check_containers():
    result = command(COMPOSE + ['ps', '-a', '--format', 'json'])
    output = result.stdout.strip()
    if not output:
        raise RuntimeError('docker compose ps returned no containers')
    try:
        rows = json.loads(output)
        if isinstance(rows, dict):
            rows = [rows]
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    services = {row.get('Service'): row for row in rows}
    for name in EXPECTED_SERVICES:
        row = services.get(name)
        if not row:
            raise RuntimeError(f'Container missing: {name}')
        if row.get('State', '').lower() != 'running':
            raise RuntimeError(f'{name}: state={row.get("State")}')
        if row.get('Health', '').lower() == 'unhealthy':
            raise RuntimeError(f'{name}: unhealthy')
        # A service with an active healthcheck must finish starting.
        if row.get('Health', '').lower() == 'starting':
            raise RuntimeError(f'{name}: healthcheck still starting')
    return len(services)


def check_locust(expected):
    code = ("import json,urllib.request; "
            "x=json.load(urllib.request.urlopen('http://localhost:8089/stats/requests',timeout=5)); "
            "print(json.dumps({'users':x.get('user_count'),'stats':len(x.get('stats',[]))}))")
    result = command(['docker', 'exec', 'load-generator', 'python', '-c', code])
    data = json.loads(result.stdout)
    if data['users'] != expected or data['stats'] == 0:
        raise RuntimeError(f'Locust not ready: {data}')
    return data


def preflight(expected):
    flags_off()
    count = check_containers()
    locust = check_locust(expected)
    event('preflight_ok', containers=count, locust=locust)


def verify_run(entry, expected):
    path = RUNS / entry['run_id']
    metadata = json.loads((path / 'metadata.json').read_text(encoding='utf-8'))
    def require(test, description):
        if not test:
            raise RuntimeError(f'{entry["run_id"]}: {description}')
    require(metadata.get('status') == 'completed', f"status={metadata.get('status')}")
    require(metadata.get('scenario') == 'fault', 'not a fault run')
    require(metadata.get('fault_flag') == entry['flag'], 'wrong flag')
    require(metadata.get('fault_variant') == entry['variant'], 'wrong variant')
    require(metadata.get('warmup_seconds') == 180, 'wrong warmup')
    require(metadata.get('baseline_seconds') == 300, 'wrong baseline')
    require(metadata.get('fault_seconds') == 600, 'wrong fault duration')
    require(metadata.get('recovery_seconds') == 300, 'wrong recovery duration')
    require(metadata.get('flag_restored') is True, 'flag not restored')
    require(metadata.get('collector_restored') is True, 'collector not restored')
    require(metadata.get('locust_users_start') == expected and
            metadata.get('locust_users_end') == expected, 'unexpected Locust users')
    require(metadata.get('locust_requests', 0) > 0, 'no Locust requests')
    require((path / 'locust_stats.json').is_file(), 'locust_stats.json missing')
    for phase in ('normal', 'fault', 'recovery'):
        item = metadata.get('phases', {}).get(phase, {})
        require(item.get('start') and item.get('end'), f'{phase} boundaries missing')
    for name in ('logs', 'metrics'):
        item = metadata.get(name, {})
        require(item.get('exists') and item.get('valid_lines', 0) > 0 and
                item.get('invalid_lines') == 0, f'{name} missing or invalid')
    return metadata


def acquire_lock():
    try:
        fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise RuntimeError(f'Lock exists: {LOCK}. Check that no other batch is running; remove only after verification.')
    with os.fdopen(fd, 'w') as f:
        f.write(f'pid={os.getpid()} time={now()}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycles', type=int, default=1,
                        help='Number of consecutive cycles to collect (default: 1)')
    parser.add_argument('--start-cycle', type=int, default=1,
                        help='First cycle, 1..20 (default: 1)')
    parser.add_argument('--seed', type=int, default=None,
                        help='Random seed when creating the plan (default: 20261122)')
    parser.add_argument('--resume', action='store_true',
                        help='Skip already completed and verified runs')
    parser.add_argument('--plan-only', action='store_true',
                        help='Create/show schedule without running experiments')
    args = parser.parse_args()
    if not 1 <= args.start_cycle <= 20 or args.cycles < 1 or args.start_cycle + args.cycles > 21:
        parser.error('Invalid cycle range; must be within 1..20')
    os.chdir(ROOT)
    RUNS.mkdir(parents=True, exist_ok=True)
    plan = load_or_create_plan(args.seed)
    selected = [x for x in plan['entries'] if args.start_cycle <= x['cycle'] < args.start_cycle + args.cycles]
    print(f"Plan: {PLAN} | seed={plan['seed']}")
    for e in selected:
        print(f"cycle {e['cycle']:02d} pos {e['position']}: {e['run_id']} ({e['variant']})")
    if args.plan_only:
        return
    acquire_lock()
    try:
        expected = plan['protocol']['expected_locust_users']
        # Fail before any new run if a previous run has incomplete data.
        for e in selected:
            if (RUNS / e['run_id']).exists():
                if not args.resume:
                    raise RuntimeError(f"Run directory exists: {e['run_id']}; use --resume only if completed")
                verify_run(e, expected)
        preflight(expected)
        pending = [e for e in selected if not (RUNS / e['run_id']).exists()]
        if not pending:
            event('nothing_to_do', start_cycle=args.start_cycle, cycles=args.cycles)
            return
        # On resume, keep a protective pause before the first pending run
        # when its predecessor in the selected schedule was already completed.
        if args.resume and pending and pending[0] != selected[0]:
            event('resume_protective_pause_start', seconds=180)
            time.sleep(180)
            event('resume_protective_pause_end')
        for index, entry in enumerate(pending):
            preflight(expected)
            event('run_start', **entry)
            cmd = [sys.executable, str(ROOT / 'experiment' / 'run.py'),
                   '--id', entry['run_id'], '--scenario', 'fault',
                   '--flag', entry['flag'], '--variant', entry['variant'],
                   '--warmup', '180', '--baseline', '300',
                   '--fault-duration', '600', '--recovery', '300']
            subprocess.run(cmd, cwd=ROOT, check=True)
            metadata = verify_run(entry, expected)
            flags_off()
            event('run_completed', run_id=entry['run_id'],
                  locust_requests=metadata['locust_requests'],
                  locust_failures=metadata['locust_failures'])
            if index + 1 < len(pending):
                event('protective_pause_start', seconds=180, after=entry['run_id'])
                time.sleep(180)
                preflight(expected)
                event('protective_pause_end', after=entry['run_id'])
        event('batch_completed', start_cycle=args.start_cycle, cycles=args.cycles)
    except BaseException as exc:
        event('batch_stopped', error=repr(exc))
        raise
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
