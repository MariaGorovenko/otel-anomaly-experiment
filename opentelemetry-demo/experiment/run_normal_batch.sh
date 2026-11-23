#!/usr/bin/env bash
set -Eeuo pipefail

# Place this script in opentelemetry-demo/experiment/.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUNS_DIR="$ROOT/../data/raw/runs"
mkdir -p "$RUNS_DIR"

# Refuse to start if any of the intended run IDs already exists.
for n in $(seq 1 20); do
  printf -v id 'normal_%03d' "$n"
  if [[ -e "$RUNS_DIR/$id" ]]; then
    echo "ERROR: $RUNS_DIR/$id already exists; nothing started." >&2
    exit 1
  fi
done

python3 - <<'PY'
import json
from pathlib import Path
flags=json.loads(Path('src/flagd/demo.flagd.json').read_text(encoding='utf-8'))['flags']
names=['adHighCpu','adFailure','paymentFailure','paymentUnreachable','productCatalogLockContention']
bad=[name for name in names if flags[name]['defaultVariant'] != 'off' or flags[name].get('state') != 'ENABLED']
if bad:
    raise SystemExit('Check feature flags before collecting normal data: ' + ', '.join(bad))
print('Selected fault flags: off (local file; service application not independently verified)')
PY

for n in $(seq 1 20); do
  printf -v id 'normal_%03d' "$n"
  echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') START $id ====="
  python3 experiment/run.py --id "$id" --scenario normal --warmup 180 --duration 1800
  python3 - "$RUNS_DIR/$id" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); m=json.loads((p/'metadata.json').read_text())
assert m.get('status')=='completed', f"Run not completed: {m.get('status')}"
assert m.get('collector_restored') is True, 'Collector was not restored'
assert m.get('locust_users_start') == 5 and m.get('locust_users_end') == 5, 'Unexpected Locust user count'
assert m.get('locust_requests', 0)>0, 'No requests'
for name in ('logs','metrics'):
    v=m.get(name,{})
    assert v.get('exists') and v.get('valid_lines',0)>0 and v.get('invalid_lines')==0, f'Invalid {name}'
assert (p/'locust_stats.json').is_file(), 'Missing Locust statistics'
print('PASS:', p.name, 'requests:', m['locust_requests'])
PY
  echo "===== $(date -u '+%Y-%m-%dT%H:%M:%SZ') FINISH $id ====="
done

echo 'All 10 runs completed.'
