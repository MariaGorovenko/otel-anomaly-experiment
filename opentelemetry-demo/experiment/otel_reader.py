"""Общие функции чтения JSONL OpenTelemetry Demo. Только стандартная библиотека."""
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO.parent / 'data' / 'raw' / 'runs'
OUTPUT_DIR = REPO.parent / 'data' / 'processed' / 'analysis'
PHASES = ('normal', 'fault', 'recovery')


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc) if value else None


def ns_time(value):
    try:
        return datetime.fromtimestamp(int(value) / 1e9, tz=timezone.utc) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def attributes(items):
    result = {}
    for item in items or []:
        value = item.get('value') or {}
        result[item.get('key', '')] = next((v for k, v in value.items() if k.endswith('Value')), None)
    return result


def load_run(run_id):
    directory = RUNS_DIR / run_id
    metadata = json.loads((directory / 'metadata.json').read_text(encoding='utf-8'))
    return directory, metadata


def boundaries(metadata):
    result = {}
    for name in PHASES:
        item = metadata.get('phases', {}).get(name, {})
        start, end = parse_time(item.get('start')), parse_time(item.get('end'))
        if start and end and start < end:
            result[name] = (start, end)
    return result


def phase_at(timestamp, phases):
    if timestamp is None:
        return 'missing'
    for name in PHASES:
        if name in phases and phases[name][0] <= timestamp < phases[name][1]:
            return name
    return 'outside'


def jsonl(path):
    with path.open(encoding='utf-8') as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f'{path}, строка {number}: {error}') from error


def logs(path):
    for batch in jsonl(path):
        for resource in batch.get('resourceLogs', []):
            service = str(attributes(resource.get('resource', {}).get('attributes')) .get('service.name') or 'unknown')
            for scope in resource.get('scopeLogs', []):
                for record in scope.get('logRecords', []):
                    body = record.get('body') or {}
                    message = str(next((v for k, v in body.items() if k.endswith('Value')), ''))
                    yield {'service': service, 'time': ns_time(record.get('timeUnixNano') or record.get('observedTimeUnixNano')),
                           'severity': str(record.get('severityText') or ''), 'severity_number': record.get('severityNumber') or 0,
                           'body': message, 'attributes': attributes(record.get('attributes'))}


def metrics(path):
    for batch in jsonl(path):
        for resource in batch.get('resourceMetrics', []):
            service = str(attributes(resource.get('resource', {}).get('attributes')).get('service.name') or 'unknown')
            for scope in resource.get('scopeMetrics', []):
                for metric in scope.get('metrics', []):
                    kind = next((k for k in ('gauge', 'sum', 'histogram', 'exponentialHistogram', 'summary') if k in metric), None)
                    if kind is None:
                        continue
                    info = metric[kind]
                    for point in info.get('dataPoints', []):
                        yield {'service': service, 'name': metric.get('name', ''), 'unit': metric.get('unit', ''),
                               'kind': kind, 'temporality': info.get('aggregationTemporality'),
                               'monotonic': info.get('isMonotonic', False),
                               'time': ns_time(point.get('timeUnixNano')), 'start_time': ns_time(point.get('startTimeUnixNano')),
                               'value': point.get('asDouble', point.get('asInt')),
                               'count': point.get('count'), 'sum': point.get('sum'),
                               'attributes': attributes(point.get('attributes'))}
