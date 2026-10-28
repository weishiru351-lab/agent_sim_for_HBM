"""Fetch verified upstream archives, export HBM2 config, or build the C++ driver."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LOCK = json.loads((HERE / 'lock.json').read_text(encoding='utf-8'))


def source_paths(fetch=False):
    local = ROOT / '.local'
    destination = local / 'sources'
    paths = {}
    for name, entry in LOCK.items():
        archive_path = local / (name + '-pinned.zip')
        if not archive_path.exists():
            if not fetch:
                raise RuntimeError('Missing source archive; run prepare.py fetch first')
            local.mkdir(exist_ok=True)
            with urllib.request.urlopen(entry['archive_url'], timeout=60) as response:
                archive_path.write_bytes(response.read())
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != entry['archive_sha256']:
            raise ValueError('Source archive hash mismatch: ' + name)
        with zipfile.ZipFile(archive_path) as archive:
            if archive.comment.decode() != entry['commit']:
                raise ValueError('Archive commit mismatch: ' + name)
            for item in archive.infolist():
                target = (destination / item.filename).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise ValueError('Unsafe archive path')
            path = destination / entry['directory']
            if not path.exists():
                if not fetch:
                    raise RuntimeError('Missing extracted source; run prepare.py fetch first')
                archive.extractall(destination)
            # Verify upstream source content, not merely an editable revision marker.
            for item in archive.infolist():
                if not item.is_dir() and (destination / item.filename).read_bytes() != archive.read(item):
                    raise ValueError('Extracted source modified: ' + item.filename)
            paths[name] = path
    return paths


def config(channels, queue_depth):
    paths = source_paths()
    sys.path.insert(0, str(paths['ramulator2'] / 'python'))
    import ramulator
    dram = ramulator.dram.HBM2(org_preset='HBM2_2Gb', timing_preset='HBM2_2000Mbps')
    controller = ramulator.controller.HBM12(
        dram=dram, scheduler=ramulator.scheduler.FRFCFS(),
        refresh_manager=ramulator.refresh_manager.AllBank(), row_policy=ramulator.row_policy.Open(),
        addr_mapper=ramulator.addr_mapper.PassThroughAddrMapper(),
        read_buffer_size=queue_depth, write_buffer_size=queue_depth)
    memory = ramulator.memory_system.GenericDRAM(
        clock_ratio=1, controllers=[controller] * channels,
        channel_mapper=ramulator.channel_mapper.PassThroughChannelMapper())
    return {'frontend': ramulator.frontend.External(clock_ratio=1).to_config(),
            'memory_system': memory.to_config()}


def build(jobs):
    if shutil.which('cmake') is None:
        raise RuntimeError('CMake unavailable. Build in a C++20 environment; no simulator execution has occurred.')
    paths = source_paths(fetch=True)
    build_dir = ROOT / 'build' / 'ramulator21'
    command = ['cmake', '-S', str(HERE), '-B', str(build_dir), '-DCMAKE_BUILD_TYPE=Release',
               '-DRAMULATOR_SOURCE=' + str(paths['ramulator2']),
               '-DFETCHCONTENT_SOURCE_DIR_YAML-CPP=' + str(paths['yaml-cpp']),
               '-DFETCHCONTENT_SOURCE_DIR_FMT=' + str(paths['fmt'])]
    subprocess.run(command, check=True)
    subprocess.run(['cmake', '--build', str(build_dir), '--parallel', str(jobs)], check=True)
    return {'build_directory': str(build_dir), 'revision': LOCK['ramulator2']['commit']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('fetch', 'verify', 'config', 'build'))
    parser.add_argument('--channels', type=int, default=4)
    parser.add_argument('--queue-depth', type=int, default=16)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.channels <= 8 or args.channels & (args.channels - 1) or args.queue_depth < 1 or args.jobs < 1:
        parser.error('Invalid channel count, queue depth, or job count')
    try:
        if args.action == 'config':
            result = config(args.channels, args.queue_depth)
        elif args.action == 'build':
            result = build(args.jobs)
        else:
            result = {name: str(path) for name, path in source_paths(fetch=args.action == 'fetch').items()}
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
