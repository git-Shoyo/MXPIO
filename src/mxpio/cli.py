"""Command line for mxpio."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .core import (MXPIOError, apply, build, check, plan, read_manifest,
                   rollback, source_signature, detect_board, parse_ioc)


def arguments() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='mxpio',
        description='CubeMX生成HALコードをPlatformIO C++プロジェクトへ安全に同期します。')
    parser.add_argument('--version', action='version', version=f'mxpio {__version__}')
    subs = parser.add_subparsers(dest='cmd', required=True)
    inspect = subs.add_parser('inspect', help='.iocファイルからMCU・周辺機能を確認（コード生成は不要）')
    inspect.add_argument('--ioc', type=Path, required=True)
    init = subs.add_parser('init', help='初回変換（既存ファイルは保護）')
    init.add_argument('--source', '-s', type=Path, required=True, help='.iocとCore/Src/main.cがあるCubeMX生成ディレクトリ')
    init.add_argument('--out', '-o', type=Path, required=True, help='出力先PlatformIOディレクトリ')
    init.add_argument('--board', help='PlatformIOボードID。省略するとIOCから推定')
    init.add_argument('--env', default='mxpio', help='PlatformIO env名（デフォルトmxpio）')
    init.add_argument('--build', action='store_true', help='変換後にpio runでビルド')

    for name in ('sync', 'diff', 'watch'):
        p = subs.add_parser(name, help={'sync':'変更を同期する', 'diff':'同期の差分だけ表示する', 'watch':'CubeMX変更を継続監視する'}[name])
        p.add_argument('--out', '-o', type=Path, default=Path('.'))
        p.add_argument('--source', '-s', type=Path, help='CubeMX生成元パスを変更（必要な場合のみ）')
        if name == 'sync':
            p.add_argument('--build', action='store_true')
        if name == 'watch':
            p.add_argument('--apply', action='store_true', help='変更を自動同期（指定なしでは差分表示のみ）')
            p.add_argument('--build', action='store_true', help='--apply時にビルド')
            p.add_argument('--interval', type=float, default=2.0, help='監視周期（秒）')

    p = subs.add_parser('check', help='生成ファイルの整合性を確認し、必要ならビルド')
    p.add_argument('--out', '-o', type=Path, default=Path('.'))
    p.add_argument('--build', action='store_true')
    p = subs.add_parser('rollback', help='直前の同期をロールバック')
    p.add_argument('--out', '-o', type=Path, default=Path('.'))
    p.add_argument('--force', action='store_true', help='手動編集済み生成ファイルをバックアップの状態へ戻す')
    return parser


def _source(out: Path, source: Path | None, manifest: dict) -> Path:
    if source:
        return source.resolve()
    return (out / manifest['config']['source']).resolve()


def show(p) -> None:
    for title, paths in [('追加', p.add), ('更新', p.update), ('削除', p.remove), ('変更なし', p.unchanged), ('衝突', p.conflicts)]:
        if paths:
            print(f'[{title}] {len(paths)} 件')
            for path in paths:
                print('  - ' + path)
    if not p.changed and not p.conflicts:
        print('更新はありません。')
    for note in p.notices:
        print('  [情報] ' + note)


def act(command: str, out: Path, source: Path, old: dict | None, board: str | None, env: str | None, run_build: bool = False):
    p = plan(source, out, old, board, env)
    show(p)
    if p.conflicts:
        print('同期を停止しました。生成ファイルの手動編集や既存ファイル衝突を解消してください。', file=sys.stderr)
        return 2
    if command == 'diff':
        return 0
    snapshot = apply(out, p, old)
    print('同期完了。' + (f' バックアップ: {snapshot}' if snapshot else '（変更なし）'))
    if run_build:
        return build(out)
    return 0


def watch_loop(out: Path, source: Path, old: dict, apply_changes: bool, run_build: bool, interval: float) -> int:
    if interval < 0.5:
        raise MXPIOError('監視周期は0.5秒以上を指定してください。')
    last = source_signature(source)
    pending = None
    print(f'監視中: {source}（Ctrl+Cで終了）。auto-sync={apply_changes}')
    try:
        while True:
            time.sleep(interval)
            latest = source_signature(source)
            if latest == last:
                pending = None
                continue
            if pending != latest:
                pending = latest
                continue  # require stable fingerprint for two polls
            last = latest
            pending = None
            print('\nCubeMX生成ファイルの変更を検出しました。')
            try:
                # Refresh state after each successful auto-sync.
                old = read_manifest(out)
                rc = act('sync' if apply_changes else 'diff', out, source, old,
                         old['config']['board'], old['config']['env'], run_build if apply_changes else False)
                if rc:
                    print('同期または検査に失敗しました。監視を継続します。', file=sys.stderr)
            except MXPIOError as exc:
                print(f'注意: {exc}', file=sys.stderr)
    except KeyboardInterrupt:
        print('\n監視終了。')
    return 0


def main(argv: list[str] | None = None) -> int:
    a = arguments().parse_args(argv)
    try:
        if a.cmd == 'inspect':
            if not a.ioc.is_file():
                raise MXPIOError(f'.iocが見つかりません: {a.ioc}')
            cfg = parse_ioc(a.ioc)
            print(f'CubeMXファイル: {a.ioc.resolve()}')
            print(f'MCU: {cfg.get("Mcu.CPN", "不明")}')
            print(f'PlatformIO board候補: {detect_board(cfg)}')
            print(f'CubeMX version: {cfg.get("MxCube.Version", "不明")}')
            num = int(cfg.get('Mcu.IPNb', '0'))
            peripherals = [cfg.get(f'Mcu.IP{i}', '') for i in range(num)]
            print('周辺機能: ' + ', '.join([x for x in peripherals if x]))
            print(f'設定SYSCLK: {cfg.get("RCC.SYSCLKFreq_VALUE", "不明")} Hz')
            if not (a.ioc.parent / 'Core/Src/main.c').exists() and not (a.ioc.parent / 'Src/main.c').exists():
                print('注意: main.cがありません。実際の変換前にCubeMXでGENERATE CODEが必要です。')
            return 0
        if a.cmd == 'init':
            out = a.out.resolve()
            if (out / '.mxpio' / 'manifest.json').exists():
                raise MXPIOError('初期化済みのプロジェクトです。mxpio sync を使用してください。')
            return act('init', out, a.source.resolve(), None, a.board, a.env, a.build)
        out = a.out.resolve()
        if a.cmd == 'check':
            problems = check(out)
            if problems:
                print('検査NG:\n - ' + '\n - '.join(problems), file=sys.stderr)
                return 2
            print('整合性検査OK（PlatformIOのコンパイル可否はまだ未検査）。')
            return build(out) if a.build else 0
        if a.cmd == 'rollback':
            path = rollback(out, a.force)
            print(f'ロールバック完了: {path.name}')
            return 0
        old = read_manifest(out)
        src = _source(out, a.source, old)
        if a.cmd == 'watch':
            return watch_loop(out, src, old, a.apply, a.build, a.interval)
        return act(a.cmd, out, src, old, old['config']['board'], old['config']['env'],
                   getattr(a, 'build', False))
    except (MXPIOError, OSError, UnicodeError) as exc:
        print(f'エラー: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
