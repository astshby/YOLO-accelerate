#!/usr/bin/env python3
"""Remove scan shifting from a validated pin trace; retain full requests/snapshots."""
import argparse
import hashlib
import json
from pathlib import Path


def compact(source, destination):
    shift = 0
    snapshot = None
    bits = 0
    requests = captures = 0
    with source.open() as inp, destination.open('w') as out:
        for line in inp:
            value = int(line, 16)
            rst, enable, update, capture, din, dout = [(value >> bit) & 1 for bit in range(5,-1,-1)]
            if rst:
                shift = 0
            elif update:
                out.write(f'D {shift:0429x}\n')
                requests += 1
                shift = 0
            elif enable:
                shift = ((shift << 1) | din) & ((1 << 1716)-1)
            if capture:
                if snapshot is not None:
                    raise ValueError('overlapping capture')
                snapshot, bits = dout, 1
            elif enable and snapshot is not None:
                snapshot = (snapshot << 1) | dout
                bits += 1
                if bits == 813:
                    out.write(f'C {snapshot:0204x}\n')
                    captures += 1
                    snapshot = None
    if snapshot is not None or not requests or not captures:
        raise ValueError('incomplete or empty pin trace')
    return {'requests': requests, 'captures': captures,
            'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'compact_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
            'scope': 'force only measurement input_shift_q; compare snapshot[812:128]; cycle counters excluded after time compression'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path)
    p.add_argument('destination', type=Path)
    a = p.parse_args()
    print(json.dumps(compact(a.source, a.destination)))
