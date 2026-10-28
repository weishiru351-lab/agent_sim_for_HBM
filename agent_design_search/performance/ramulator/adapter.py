"""Convert routed transfers to HBM2 transactions; no workload or DSL interpretation."""

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time

from agent_design_search.contracts import Request, DRAMRequest
from agent_design_search.inputs.hardware import Hardware
from agent_design_search.json_io import read, save, digest


BRIDGE = Path(__file__).resolve().parent
LOCK = read(BRIDGE / 'lock.json')
REVISION = LOCK['ramulator2']['commit']
DEFAULT_CONFIG = Hardware(channels=4, banks_per_channel=2, columns=2, rows=4)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def native_address(vector, channels):
    """Canonical CacheLineInterleave + RoBaRaCoCh address, used as identity only.

    Both native mappers are pass-through: these bits are NOT decoded a second
    time. Column is already in 32-byte transaction units (prefetch removed).
    """
    ch, pc, sid, bg, bank, row, column = vector
    if sid != 0:
        raise ValueError('HBM2_2Gb has one Sid')
    block = (((row * 4 + bank) * 4 + bg) * 2 + pc) * 32 + column
    return (block * channels + ch) * 32


def decode_native_address(address, channels):
    block = address // 32
    ch, block = block % channels, block // channels
    column, block = block % 32, block // 32
    pc, block = block % 2, block // 2
    bg, block = block % 4, block // 4
    bank, row = block % 4, block // 4
    return [ch, pc, 0, bg, bank, row, column]


def validate_geometry(cfg):
    if (cfg.granule_bytes != 64 or cfg.channel_bits > 3 or cfg.bank_bits > 5
            or cfg.column_bits > 4 or cfg.row_bits > 14):
        raise ValueError('Bridge requires 64-byte requests, <=8 channels, <=32 banks/channel, <=16 logical columns, <=16384 rows')


def compile_transactions(transfers, cfg):
    validate_geometry(cfg)
    if not transfers or any(t.request.kind != 'read' for t in transfers):
        raise ValueError('Initial bridge is read-only; mixed/write traces are rejected, never silently converted')
    transactions = []
    channels = 1 << cfg.channel_bits
    ids = set()
    for transfer in sorted(transfers, key=lambda t: (t.request.arrival, t.request.request_id)):
        request = transfer.request
        ch, flat_bank, row, column = transfer.channel, transfer.bank, transfer.row, transfer.column
        if (type(request.address) is not int or not 0 <= request.address < cfg.capacity_bytes
                or request.address % cfg.granule_bytes):
            raise ValueError('Logical requests must be 64-byte aligned')
        if (type(request.arrival) is not int or request.arrival < 0 or type(request.request_id) is not int
                or request.request_id < 0 or request.request_id in ids or transfer.valid_bytes):
            raise ValueError('Bridge requires unique IDs, nonnegative arrivals and full-granule reads')
        for value, count in ((ch, channels), (flat_bank, 1 << cfg.bank_bits),
                             (row, 1 << cfg.row_bits), (column, 1 << cfg.column_bits)):
            if type(value) is not int or not 0 <= value < count:
                raise ValueError('Routed coordinate outside hardware')
        ids.add(request.request_id)
        # flat bank: low 2 bits -> Bank, next 2 -> BankGroup, next -> PseudoChannel.
        for part in range(2):
            vector = [ch, flat_bank // 16, 0, (flat_bank // 4) % 4, flat_bank % 4, row, column * 2 + part]
            transactions.append({'transaction_id': len(transactions), 'request_id': request.request_id,
                                 'part': part, 'kind': 'read', 'size_bytes': 32, 'arrival': request.arrival,
                                 'addr_vec': vector, 'physical_address': native_address(vector, channels)})
    return transactions


def profile(cfg, queue_depth=16):
    if type(queue_depth) is not int or queue_depth < 1:
        raise ValueError('queue_depth must be a positive integer')
    result = subprocess.run([sys.executable, str(BRIDGE / 'prepare.py'), 'config',
                             '--channels', str(1 << cfg.channel_bits), '--queue-depth', str(queue_depth)],
                            capture_output=True, text=True, encoding='utf-8', timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def prepare(transfers, output, cfg=DEFAULT_CONFIG, max_cycles=1000000, queue_depth=16):
    if type(max_cycles) is not int or max_cycles < 1:
        raise ValueError('Positive integer cycle limit required')
    requests = [t.request for t in transfers]
    transactions = compile_transactions(transfers, cfg)
    if max(r.arrival for r in requests) >= max_cycles:
        raise ValueError('Cycle limit must exceed all request arrival times')
    native = profile(cfg, queue_depth)
    bundle = {'protocol': 'hbm-ramulator-routed-read-v2', 'upstream_commit': REVISION,
              'config': cfg.to_dict(), 'routed_requests': [asdict(t) for t in transfers], 'ramulator_config': native,
              'trace_id': digest([asdict(r) for r in requests]),
              'requests': [asdict(r) for r in requests], 'transactions': transactions, 'max_cycles': max_cycles,
              'contract': {'logical_bytes': 64, 'transaction_bytes': 32, 'transactions_per_request': 2,
                           'arrival_unit': 'DRAM clock cycles; not calibrated to toy time or real kernel time',
                           'completion': 'both read callbacks completed; max completion over two halves',
                           'injection': 'all ready transactions per channel until queue rejects; retry next cycle; no cross-channel head-of-line blocking',
                           'bank_embedding': 'low 2 bits Bank, next 2 BankGroup, next bit PseudoChannel; Sid=0',
                           'mapping': 'Already routed upstream; both native mappers pass through addr_vec',
                           'limits': ['read-only', 'synthetic subspace of HBM2 geometry', 'no actual KV/HBM trace claim']}}
    bundle['bundle_id'] = content_hash(bundle)
    output = Path(output)
    if output.exists():
        raise ValueError('Do not overwrite prepared bundles')
    save(output, bundle)
    return {'bundle_id': bundle['bundle_id'], 'logical_requests': len(requests), 'transactions': len(transactions),
            'simulation_status': 'not_run', 'backend_calls': 0}


def validate_result(bundle, result):
    if result.get('backend') != 'ramulator-2.1-hbm2-read-v1' or result.get('upstream_commit') != REVISION or result.get('bundle_id') != bundle['bundle_id']:
        raise ValueError('Backend/revision/bundle mismatch')
    if (type(result.get('final_tick')) is not int or result['final_tick'] < 0
            or type(result.get('tCK_ns')) not in (int, float)
            or not math.isfinite(result['tCK_ns']) or result['tCK_ns'] <= 0):
        raise ValueError('Invalid backend clock metadata')
    transactions = bundle['transactions']
    if result.get('completed_transactions') != len(transactions) or result.get('accepted_transactions') != len(transactions):
        raise ValueError('Backend did not accept and complete every transaction')
    events = result.get('events', [])
    if len(events) != len(transactions):
        raise ValueError('Missing completion events')
    by_id = {e['transaction_id']: e for e in events}
    if len(by_id) != len(events) or set(by_id) != set(range(len(transactions))):
        raise ValueError('Duplicate/unknown transaction completions')
    request_finishes = {}
    for tx in transactions:
        e = by_id[tx['transaction_id']]
        if (e['request_id'] != tx['request_id'] or e['arrival'] != tx['arrival']
                or any(type(e[k]) is not int for k in ('arrival', 'accepted', 'finish'))
                or not e['arrival'] <= e['accepted'] <= e['finish'] <= result['final_tick']):
            raise ValueError('Completion identity or timestamp mismatch')
        request_finishes[tx['request_id']] = max(request_finishes.get(tx['request_id'], 0), e['finish'])
    requests = bundle['requests']
    latencies = sorted(request_finishes[r['request_id']] - r['arrival'] for r in requests)
    return {'backend': result['backend'], 'completed_requests': len(requests),
            'completed_transactions': len(transactions),
            'completion_cycles': max(request_finishes.values()) - min(r['arrival'] for r in requests),
            'avg_latency': sum(latencies) / len(latencies), 'p95_latency': latencies[math.ceil(.95 * len(latencies)) - 1],
            'tCK_ns': result['tCK_ns'], 'read_requests': len(requests), 'write_requests': 0,
            'request_finishes': [{'request_id': r['request_id'], 'finish': request_finishes[r['request_id']]} for r in requests]}


def run(bundle_path, driver, output, timeout=60):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Positive finite timeout required')
    bundle = read(bundle_path)
    if bundle.get('bundle_id') != content_hash({k: v for k, v in bundle.items() if k != 'bundle_id'}):
        raise ValueError('Prepared bundle changed')
    if bundle.get('upstream_commit') != REVISION:
        raise ValueError('Unsupported Ramulator revision')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    driver = Path(driver).resolve()
    result = {'bundle_id': bundle['bundle_id'], 'status': 'not_run', 'backend_calls': 0,
              'backend_seconds': 0, 'upstream_commit': REVISION}
    if not driver.is_file():
        result['reason'] = 'Compiled driver unavailable; prepare/build the pinned Ramulator environment'
        save(output / 'result.json', result)
        return result
    result['driver_sha256'] = hashlib.sha256(driver.read_bytes()).hexdigest()
    native_path = output.resolve() / 'native_result.json'
    started = time.perf_counter()
    try:
        process = subprocess.Popen([str(driver), str(Path(bundle_path).resolve()), str(native_path)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result['backend_calls'] = 1
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            (output / 'stdout.log').write_bytes(stdout)
            (output / 'stderr.log').write_bytes(stderr)
            raise RuntimeError('Backend timeout before completed result')
        (output / 'stdout.log').write_bytes(stdout)
        (output / 'stderr.log').write_bytes(stderr)
        if process.returncode:
            raise RuntimeError(f'Backend exit code {process.returncode}; see stderr.log')
        native = read(native_path)
        result['metrics'] = validate_result(bundle, native)
        result['status'] = 'ok'
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        result.update(status='backend_error' if result['backend_calls'] else 'not_run', reason=str(error))
    result['backend_seconds'] = time.perf_counter() - started
    save(output / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    prepare_cmd = commands.add_parser('prepare')
    prepare_cmd.add_argument('--requests', type=Path, required=True, help='JSON list of routed DRAMRequest records')
    prepare_cmd.add_argument('--hardware', type=Path)
    prepare_cmd.add_argument('--max-cycles', type=int, default=1000000)
    prepare_cmd.add_argument('--queue-depth', type=int, default=16)
    prepare_cmd.add_argument('--output', type=Path, required=True)
    run_cmd = commands.add_parser('run')
    run_cmd.add_argument('--bundle', type=Path, required=True)
    run_cmd.add_argument('--driver', type=Path, required=True)
    run_cmd.add_argument('--output', type=Path, required=True)
    run_cmd.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if args.action == 'prepare':
        cfg = Hardware.parse(read(args.hardware)) if args.hardware else DEFAULT_CONFIG
        transfers = [DRAMRequest(**{**item, 'request': Request(**item['request'])}) for item in read(args.requests)]
        result = prepare(transfers, args.output, cfg, args.max_cycles, args.queue_depth)
    else:
        result = run(args.bundle, args.driver, args.output, args.timeout)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get('status') in ('not_run', 'backend_error'):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
