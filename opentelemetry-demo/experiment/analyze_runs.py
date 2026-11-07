"""Сводный анализ логов и метрик по фазам; CSV и PNG-графики."""
import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from otel_reader import RUNS_DIR, OUTPUT_DIR, PHASES, load_run, boundaries, phase_at, logs, metrics

# Показатели, релевантные пяти пилотным инъекциям.
TARGETS = {
    'adHighCpu': [('ad', 'jvm.cpu.recent_utilization')],
    'adFailure': [('ad', 'rpc.server.duration'), ('ad', 'demo.ad.requests')],
    'paymentFailure': [('payment', 'http.client.request.duration'), ('checkout', 'rpc.server.call.duration')],
    'paymentUnreachable': [('checkout', 'rpc.server.call.duration'), ('checkout', 'http.client.request.duration')],
    'productCatalogLockContention': [('product-catalog', 'db.client.operation.duration'),
                                     ('product-catalog', 'rpc.server.call.duration')],
}
ERROR_PATTERN = re.compile(r'\b(error|failed|failure|exception|unavailable|invalid token|timeout|lock contention)\b', re.I)


def point_measure(point):
    """Gauge -> значение; histogram -> sum/count (среднее в интервале или накопленное)."""
    if point['kind'] == 'gauge' and point['value'] is not None:
        return float(point['value']), 'gauge'
    if point['kind'] in ('histogram', 'exponentialHistogram') and point['count'] and point['sum'] is not None:
        return float(point['sum']) / float(point['count']), 'histogram_sum/count'
    return None, None


def analyze(run_id, output):
    directory, meta = load_run(run_id)
    phases = boundaries(meta)
    flag = meta.get('fault_flag')
    target = set(TARGETS.get(flag, []))
    errors = Counter()
    log_counts = Counter()
    error_examples = defaultdict(Counter)
    for record in logs(directory / 'logs.jsonl'):
        phase = phase_at(record['time'], phases)
        log_counts[(phase, record['service'])] += 1
        if record['severity_number'] >= 17 or ERROR_PATTERN.search(record['body']):
            errors[(phase, record['service'])] += 1
            error_examples[(phase, record['service'])][record['body'][:160]] += 1
    metric_values = defaultdict(list)
    plot_values = defaultdict(list)
    cumulative = defaultdict(list)
    for point in metrics(directory / 'metrics.jsonl'):
        key = (point['service'], point['name'])
        if key not in target:
            continue
        phase = phase_at(point['time'], phases)
        if phase not in PHASES:
            continue
        # Для накопительных гистограмм нужны приращения sum/count между точками.
        if point['kind'] in ('histogram', 'exponentialHistogram') and point['temporality'] == 2:
            series_key = (*key, tuple(sorted((k, str(v)) for k, v in point['attributes'].items())))
            cumulative[series_key].append(point)
            continue
        value, method = point_measure(point)
        if value is None:
            continue
        metric_values[(phase, *key, method)].append(value)
        plot_values[key].append((point['time'], value, phase, method))
    for (service, metric, _attrs), points in cumulative.items():
        points.sort(key=lambda p: p['time'] or phases.get('normal', (None,))[0])
        for prev, current in zip(points, points[1:]):
            if prev['time'] is None or current['time'] is None:
                continue
            # Сброс счётчика/смена начала агрегирования — не интерпретируем.
            if current['start_time'] != prev['start_time']:
                continue
            delta_count = float(current['count'] or 0) - float(prev['count'] or 0)
            delta_sum = float(current['sum'] or 0) - float(prev['sum'] or 0)
            if delta_count <= 0 or delta_sum < 0:
                continue
            # Приращение пересекает границу фаз: не приписываем его одной фазе.
            phase = phase_at(current['time'], phases)
            if phase != phase_at(prev['time'], phases) or phase not in PHASES:
                continue
            value = delta_sum / delta_count
            metric_values[(phase, service, metric, 'cumulative_histogram_delta_mean')].append(value)
            plot_values[(service, metric)].append((current['time'], value, phase, 'cumulative_histogram_delta_mean'))
    rows = []
    for phase in PHASES:
        for service in sorted({s for _, s in log_counts} | {s for s, _ in target}):
            rows.append({'run_id': run_id, 'flag': flag, 'status': meta.get('status'),
                'phase': phase, 'service': service, 'signal': 'logs', 'metric': 'log_records',
                'method': 'count', 'n': log_counts[(phase, service)], 'mean': '', 'errors': errors[(phase, service)]})
    for (phase, service, metric, method), values in sorted(metric_values.items()):
        rows.append({'run_id': run_id, 'flag': flag, 'status': meta.get('status'),
            'phase': phase, 'service': service, 'signal': 'metrics', 'metric': metric,
            'method': method, 'n': len(values), 'mean': round(mean(values), 8), 'errors': ''})
    plots_dir = output / 'plots' / run_id
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print('  matplotlib не установлен: графики пропущены (pip install matplotlib)')
    else:
        plots_dir.mkdir(parents=True, exist_ok=True)
        for (service, metric), values in plot_values.items():
            values.sort(key=lambda item: item[0])
            fig, ax = plt.subplots(figsize=(10, 4))
            for phase in PHASES:
                selected = [(t, v) for t, v, p, _ in values if p == phase]
                if selected:
                    ax.plot([t for t, _ in selected], [v for _, v in selected], marker='.', label=phase)
            for phase in ('fault', 'recovery'):
                if phase in phases:
                    ax.axvline(phases[phase][0], linestyle='--', alpha=0.6)
            ax.set_title(f'{run_id}: {service} / {metric}')
            ax.set_ylabel('CPU fraction' if metric == 'jvm.cpu.recent_utilization' else 'Metric value (native units)')
            ax.legend()
            fig.autofmt_xdate()
            fig.tight_layout()
            safe = re.sub(r'[^a-zA-Z0-9_-]+', '_', f'{service}_{metric}')
            fig.savefig(plots_dir / f'{safe}.png', dpi=140)
            plt.close(fig)
    print(f'{run_id}: {meta.get("status")} | logs={sum(log_counts.values())}, errors={sum(errors.values())}, metric series={len(plot_values)}')
    return rows, {f'{phase}/{service}': examples.most_common(5)
                  for (phase, service), examples in error_examples.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_ids', nargs='*')
    parser.add_argument('--output-dir', default=str(OUTPUT_DIR))
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    ids = args.run_ids or sorted(p.name for p in RUNS_DIR.iterdir() if (p / 'metadata.json').exists())
    rows, examples = [], {}
    for run_id in ids:
        try:
            result, run_examples = analyze(run_id, output)
            rows.extend(result)
            examples[run_id] = run_examples
        except (OSError, ValueError, KeyError) as error:
            print(f'{run_id}: ОШИБКА: {error}')
    with (output / 'summary.csv').open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.DictWriter(file, fieldnames=['run_id', 'flag', 'status', 'phase', 'service',
                                                  'signal', 'metric', 'method', 'n', 'mean', 'errors'])
        writer.writeheader()
        writer.writerows(rows)
    (output / 'error_examples.json').write_text(json.dumps(examples, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Результаты:', output)
    print('Примечание: кумулятивные гистограммы анализируются по приращениям внутри одной фазы; разреженные интервалы исключаются.')


if __name__ == '__main__':
    main()
