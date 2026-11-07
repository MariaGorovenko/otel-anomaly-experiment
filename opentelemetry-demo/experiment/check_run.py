"""Проверка полноты и структуры одного или нескольких прогонов."""
import argparse
import json
from collections import Counter, defaultdict
from statistics import median
from otel_reader import RUNS_DIR, OUTPUT_DIR, PHASES, load_run, boundaries, phase_at, logs, metrics


def inspect(run_id):
    directory, meta = load_run(run_id)
    phases = boundaries(meta)
    report = {'run_id': run_id, 'status': meta.get('status'), 'flag': meta.get('fault_flag'),
              'phases_present': list(phases), 'locust_requests': meta.get('locust_requests'),
              'locust_failures': meta.get('locust_failures'), 'signals': {}, 'warnings': []}
    if meta.get('status') != 'completed':
        report['warnings'].append('Прогон не завершён успешно')
    if len(phases) != 3 and meta.get('scenario') == 'fault':
        report['warnings'].append('Нет всех трёх полных фаз')
    for kind, iterator in [('logs', logs), ('metrics', metrics)]:
        path = directory / (kind + '.jsonl')
        if not path.exists() or path.stat().st_size == 0:
            report['signals'][kind] = {'error': 'Файл отсутствует или пуст'}
            report['warnings'].append(f'{kind}: нет данных')
            continue
        counts = Counter()
        services = defaultdict(Counter)
        timestamps = defaultdict(list)
        try:
            for entry in iterator(path):
                phase = phase_at(entry['time'], phases)
                counts[phase] += 1
                services[entry['service']][phase] += 1
                if entry['time'] and phase in PHASES:
                    timestamps[(entry['service'], phase)].append(entry['time'].timestamp())
        except (ValueError, OSError) as error:
            report['warnings'].append(f'{kind}: ошибка чтения: {error}')
        gaps = {}
        for (service, phase), times in timestamps.items():
            ordered = sorted(set(times))
            if len(ordered) > 1:
                differences = [b - a for a, b in zip(ordered, ordered[1:])]
                gaps[f'{service}/{phase}'] = {'unique_timestamps': len(ordered),
                    'median_gap_seconds': round(median(differences), 2),
                    'max_gap_seconds': round(max(differences), 2)}
        report['signals'][kind] = {'total_records_or_points': sum(counts.values()),
            'by_phase': dict(counts), 'by_service': {k: dict(v) for k, v in sorted(services.items())},
            'timestamp_gaps': gaps}
        for phase in phases:
            if counts[phase] == 0:
                report['warnings'].append(f'{kind}: нет записей в фазе {phase}')
        if counts['missing']:
            report['warnings'].append(f'{kind}: отсутствуют временные метки: {counts["missing"]}')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_ids', nargs='*', help='ID прогонов; без аргументов — все каталоги с metadata.json')
    parser.add_argument('--output', default=str(OUTPUT_DIR / 'quality_report.json'))
    args = parser.parse_args()
    ids = args.run_ids or sorted(p.name for p in RUNS_DIR.iterdir() if (p / 'metadata.json').exists())
    reports = []
    for run_id in ids:
        try:
            report = inspect(run_id)
            reports.append(report)
            print(f'\n{run_id}: {report["status"]} | {report["flag"]}')
            for kind, signal in report['signals'].items():
                print(f'  {kind}: {signal.get("total_records_or_points", 0)} | {signal.get("by_phase", signal.get("error"))}')
            for warning in report['warnings']:
                print('  ВНИМАНИЕ:', warning)
        except (OSError, ValueError, KeyError) as error:
            print(f'{run_id}: ОШИБКА: {error}')
            reports.append({'run_id': run_id, 'error': str(error)})
    destination = __import__('pathlib').Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\nОтчёт:', destination)


if __name__ == '__main__':
    main()
