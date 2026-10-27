"""
dump_all.py — 把哈啰抓包全部解成明文 JSON

  python evwatch/dump_all.py                # captures/小哈智电/ → captures/out/
  python evwatch/dump_all.py --env dev      # 换环境(默认 pro)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evwatch.hb_crypto import ENV_KEYS, parse_capture  # noqa: E402
from evwatch import paths  # noqa: E402


def collect(capdir: str):
    """按 [序号] 配对 request / response"""
    pairs: dict[int, dict] = {}
    for f in glob.glob(os.path.join(capdir, '[[]*.txt')):
        m = re.search(r'\[(\d+)\]', os.path.basename(f))
        if not m:
            continue
        cap = parse_capture(f)
        pairs.setdefault(int(m.group(1)), {})[cap.kind] = cap
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=paths.HELLOBIKE_CAPTURE_DIR)
    ap.add_argument('--env', default='pro', choices=sorted(ENV_KEYS))
    ap.add_argument('--out', default=paths.capture('out'))
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    pairs = collect(args.dir)
    ok = 0
    for i in sorted(pairs):
        for kind in ('request', 'response'):
            cap = pairs[i].get(kind)
            if not cap:
                continue
            try:
                obj = cap.plain(args.env)
            except Exception:
                continue
            ok += 1
            name = f'{i:03d}_{kind}_{cap.action or "x"}.json'.replace('/', '_')
            with open(os.path.join(args.out, name), 'w', encoding='utf-8') as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=2)
            brief = json.dumps(obj, ensure_ascii=False)
            print(f'[{i:>3}] {kind:8s} {cap.action[:38]:38s} {brief[:80]}')
    print(f'\n解出 {ok} 条 -> {os.path.abspath(args.out)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
