"""显示资料库存、原始字段摘要，并可把地址空间采集资料输出到文件。"""
import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):  # 支持 python -I /abs/cli.py 的隔离直跑
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source_data.io import load
from source_data.domain import summarize
from source_data.address_space import (collect_material,
                                       make_session_adapters)


def _inspect(path):
    value, digest = load(path)
    return {'source_sha256': digest,
            'summary': summarize(value, Path(path).parent)}


def _collect(path, out_path):
    value, digest = load(path)
    factory = make_session_adapters(value)
    material = collect_material(
        value, factory, value['roots'], source_digest=digest)
    rendered = json.dumps(material, ensure_ascii=False, indent=2,
                          sort_keys=False)
    if out_path is None:
        print(rendered)
    else:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + '\n', encoding='utf8')
    return factory, material


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    inspect_p = sub.add_parser('inspect')
    inspect_p.add_argument('file')
    collect_p = sub.add_parser('collect')
    collect_p.add_argument('file')
    collect_p.add_argument('-o', '--output')
    args = parser.parse_args()
    if args.command == 'inspect':
        print(json.dumps(_inspect(args.file), ensure_ascii=False, indent=2))
    elif args.command == 'collect':
        factory, material = _collect(args.file, args.output)
        leftovers = {sid: adapter.remaining()
                     for sid, adapter in factory.adapters.items()
                     if adapter.remaining()}
        if leftovers:
            raise SystemExit(f'资料仍有未消费步骤: {leftovers}')
        if args.output:
            print(json.dumps({'output': args.output,
                              'summary': material['summary']},
                             ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
