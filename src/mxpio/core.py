"""Core transformations and filesystem operations.

IMPORTANT: Never write to a project before all generated-file conflicts are checked.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .handles import generate_handles_header

STATE_DIR = '.mxpio'
MANIFEST_NAME = 'manifest.json'
START = '; >>> MXPIO MANAGED BEGIN (do not edit)'
END = '; <<< MXPIO MANAGED END'
SKIP_SOURCES = {'syscalls.c', 'sysmem.c'}

VECTOR_TABLE_SETUP = '''\n  /* mxpio: Select the linked application's vectors before HAL enables interrupts.
     The linker symbol also follows non-zero Flash offsets. */
#if defined(SCB_VTOR_TBLOFF_Msk)
  extern const uint32_t g_pfnVectors[];
  SCB->VTOR = (uint32_t)(uintptr_t)g_pfnVectors;
  __DSB();
  __ISB();
#endif
'''


class MXPIOError(Exception):
    """Human-readable and recoverable conversion failure."""


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(path: Path) -> str | None:
    return sha_bytes(path.read_bytes()) if path.is_file() else None


def write_bytes_atomic(dest: Path, value: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.mxpio.tmp')
    try:
        tmp.write_bytes(value)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def parse_ioc(ioc: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not ioc.exists():
        return values
    for line in ioc.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            values[k] = v
    return values


def find_ioc(root: Path) -> Path:
    files = sorted(root.glob('*.ioc'))
    if len(files) != 1:
        raise MXPIOError(f'{root}: .ioc が1つ必要です（検出 {len(files)} 個）。--source にCubeMX生成プロジェクトを指定してください。')
    return files[0]


def detect_board(ioc_data: dict[str, str]) -> str:
    part = ioc_data.get('Mcu.CPN', '').upper()
    match = re.match(r'^STM32([A-Z][0-9]{3}[A-Z0-9]{2})', part)
    if not match:
        raise MXPIOError('MCU名からPlatformIO board IDを推定できません。--board を明示してください。')
    # Only G431CB is presently verified; all other names are best-effort guesses.
    return 'genericSTM32' + match.group(1)


def _masked(s: str) -> str:
    """Mask strings and comments while preserving offsets/newlines."""
    chars = list(s)
    i, n = 0, len(s)
    while i < n:
        if s.startswith('//', i):
            j = s.find('\n', i)
            j = n if j < 0 else j
            for k in range(i, j): chars[k] = ' '
            i = j
        elif s.startswith('/*', i):
            j = s.find('*/', i + 2)
            if j < 0: raise MXPIOError('Cソースに未終了のブロックコメントがあります。')
            j += 2
            for k in range(i, j):
                if chars[k] != '\n': chars[k] = ' '
            i = j
        elif s[i] in ('"', "'"):
            quote = s[i]
            j = i + 1
            while j < n:
                if s[j] == '\\': j += 2; continue
                if s[j] == quote: j += 1; break
                j += 1
            if j > n or (j == n and s[n-1] != quote):
                raise MXPIOError('Cソースに未終了の文字列があります。')
            for k in range(i, j):
                if chars[k] != '\n': chars[k] = ' '
            i = j
        else:
            i += 1
    return ''.join(chars)


def matching_brace(masked: str, brace_index: int) -> int:
    if masked[brace_index] != '{':
        raise MXPIOError('Cの波括弧解析に失敗しました。')
    level = 0
    for i in range(brace_index, len(masked)):
        if masked[i] == '{': level += 1
        if masked[i] == '}':
            level -= 1
            if level == 0: return i
    raise MXPIOError('Cソースの波括弧が閉じられていません。')


def transform_main(source: str) -> tuple[str, list[str]]:
    """Replaces C main() with MX_Init() and removes *empty* generated loop only.

    All code outside main, including globals, init function definitions,
    interrupt callbacks, and USER CODE sections, is preserved verbatim.
    """
    masked = _masked(source)
    m = re.search(r'\bint\s+main\s*\(\s*(?:void\s*)?\)\s*\{', masked)
    if not m:
        raise MXPIOError('main.c に通常形式の int main(void) がありません。複雑なmainは自動変換を拒否します。')
    if re.search(r'\b(?:void|int)\s+MX_Init\s*\(', masked):
        raise MXPIOError('既にMX_Init() が定義されています。名称衝突を解消してください。')
    open_brace = masked.rfind('{', m.start(), m.end())
    close_brace = matching_brace(masked, open_brace)
    body_masked = masked[open_brace + 1: close_brace]
    wm = re.search(r'\bwhile\s*\(\s*1\s*\)\s*\{', body_masked)
    if not wm:
        raise MXPIOError('main()内の標準的な while(1) が見つかりません。意図せずコードを削除しないため停止します。')
    loop_open = open_brace + 1 + wm.end() - 1
    loop_close = matching_brace(masked, loop_open)
    if masked[loop_open + 1:loop_close].strip():
        raise MXPIOError('CubeMX main.c のwhile(1)内にユーザー処理があります。先にmain.cppへ移してから同期してください。')
    if masked[loop_close + 1:close_brace].strip():
        raise MXPIOError('CubeMX main.c のwhile(1)以降に処理があります。安全に自動変換できません。')
    function_start = m.start()
    before_main = source[:function_start]
    # Keep C prelude: initializers, globals, prototype declarations, USER CODE pre-loop.
    before_loop = source[open_brace + 1:open_brace + 1 + wm.start()]
    # Preserve trailing comments (e.g. USER CODE END 3) within main as comments.
    trailing = source[loop_close + 1: close_brace]
    after_main = source[close_brace + 1:]
    transformed = (before_main + 'void MX_Init(void)\n{' + VECTOR_TABLE_SETUP + before_loop +
                   '\n  /* mxpio: CubeMX generated while(1) moved to user main.cpp. */\n  return;\n' +
                   trailing + '}\n' + after_main)
    return transformed, ['main.cのmain()をMX_Init()へ変換しました。ユーザーのwhile(1)処理は自動移行されません。']


def detect_layout(source: Path) -> tuple[Path, Path]:
    paths = [(source / 'Core' / 'Src', source / 'Core' / 'Inc'),
             (source / 'Src', source / 'Inc')]
    for src, inc in paths:
        if (src / 'main.c').is_file() and inc.is_dir():
            return src, inc
    raise MXPIOError('CubeMX生成済みの Core/Src/main.c と Core/Inc、または Src/main.c と Inc が必要です。\n'
                     '.iocだけではHAL初期化コードを生成できません。CubeMXのGENERATE CODEを先に実行してください。')


def extract(source: Path, output: Path, board: str | None, env_name: str | None) -> tuple[dict[str, bytes], dict[str, Any], list[str]]:
    src, inc = detect_layout(source)
    ioc = find_ioc(source)
    cfg = parse_ioc(ioc)
    chosen_board = board or detect_board(cfg)
    chosen_env = env_name or 'mxpio'
    if not re.fullmatch(r'[\w.-]+', chosen_env):
        raise MXPIOError('--env には英数字、_、-、.のみ使えます。')
    family_match = re.search(r'STM32([A-Z][0-9])', cfg.get('Mcu.CPN', ''))
    family = family_match.group(1).lower() if family_match else ''
    if not family:
        raise MXPIOError('IOCからSTM32ファミリを判定できません。')
    warnings = []
    if chosen_board != detect_board(cfg):
        warnings.append('指定boardとIOCからの推定boardが異なります。MCU一致を必ず確認してください。')
    if chosen_board != 'genericSTM32G431CB':
        warnings.append(f'board={chosen_board} はこの配布版で実機・ビルド未検証です。PlatformIOのboard IDを確認してください。')
    if cfg.get('ProjectManager.FirmwarePackage'):
        warnings.append('CubeMXとPlatformIOのSTM32 HALライブラリバージョンが異なる場合があります。必ずビルドを確認してください。')
    if ioc.stat().st_mtime > (src / 'main.c').stat().st_mtime + 1:
        warnings.append('.ioc がmain.cより新しいです。CubeMXでGENERATE CODEを実行済みか確認してください。')
    main_data = (src / 'main.c').read_text(encoding='utf-8-sig')
    converted, notes = transform_main(main_data)
    warnings.extend(notes)
    files: dict[str, bytes] = {}
    gen_src = 'src/mx_generated'
    gen_inc = 'include/mx_generated'
    files[f'{gen_src}/mx_boot.c'] = converted.encode('utf-8')
    for f in sorted(src.glob('*.c')):
        if f.name == 'main.c': continue
        if f.name in SKIP_SOURCES:
            warnings.append(f'{f.name}はPlatformIO環境との重複を避けるため移行しません。')
            continue
        if re.fullmatch(r'system_stm32[a-z0-9]+\.c', f.name):
            warnings.append(f'{f.name}はPlatformIO標準システム実装を使うため移行しません。')
            continue
        files[f'{gen_src}/{f.name}'] = f.read_bytes()
    header_names = []
    for f in sorted(inc.glob('*.h')):
        files[f'{gen_inc}/{f.name}'] = f.read_bytes()
        header_names.append(f.name)
    hal_conf = f'stm32{family}xx_hal_conf.h'
    if hal_conf not in header_names:
        raise MXPIOError(f'{hal_conf}がありません。CubeMX生成ファイルを確認してください。')
    if 'main.h' not in header_names:
        raise MXPIOError('Core/Inc/main.h がありません。')
    handle_sources = {name: data.decode('utf-8-sig') for name, data in files.items()
                      if name.startswith(gen_src + '/') and name.endswith('.c')}
    try:
        files['include/mxpio/mx_handles.h'] = generate_handles_header(handle_sources, _masked)
    except ValueError as exc:
        raise MXPIOError(str(exc)) from exc
    files['include/mxpio/mx_init.h'] = ('#ifndef MXPIO_MX_INIT_H\n#define MXPIO_MX_INIT_H\n'
       '#include "main.h"  /* CubeMX HAL declarations and board pin definitions */\n'
       '#include "mx_handles.h"  /* C-linkage declarations for generated HAL handles */\n'
       '#ifdef __cplusplus\nextern "C" {\n#endif\n'
       'void MX_Init(void);\n'
       '#ifdef __cplusplus\n}\n#endif\n#endif\n').encode()
    files['src/main.cpp'] = ('''/* User-owned file: mxpio never overwrites this after creation. */
#include "mxpio/mx_init.h"

int main()
{
    MX_Init();  // HAL_Init, clock, GPIO, TIM, I2C, etc. from CubeMX

    // Start peripherals as needed, e.g. HAL_TIM_Base_Start_IT(&htim7);
    // HAL_TIM_Encoder_Start(&htim2, TIM_CHANNEL_ALL);
    // HAL_TIM_PWM_Start(&htim8, TIM_CHANNEL_1);
    while (true) {
        // Application code (C++).
    }
}
''').encode()
    # The auto-generated ini block lives in the selected environment; user entries are never cleared.
    managed = ('\n'.join([START,
        'platform = ststm32',
        f'board = {chosen_board}',
        'framework = stm32cube',
        'board_build.stm32cube.custom_config_header = yes',
        'build_flags =',
        '    -Iinclude/mx_generated',
        END]) + '\n')
    metadata = {'source': os.path.relpath(source.resolve(), output.resolve()),
                'ioc_name': ioc.name, 'mcu': cfg.get('Mcu.CPN', ''),
                'board': chosen_board, 'env': chosen_env,
                'managed_ini': managed, 'tool_version': __version__,
                'family': family}
    if (source / 'Middlewares').exists() or (source / 'USB_DEVICE').exists():
        raise MXPIOError('Middleware/USB構成はv0.1で未対応です。未移行コードを黙って省略しないため停止します。')
    if (source / 'Core' / 'Src').exists() and src.name != 'Src':
        raise MXPIOError('異常なCubeMXディレクトリ構成です。')
    return files, metadata, warnings


def compose_ini(existing: bytes | None, metadata: dict[str, Any]) -> bytes:
    block = metadata['managed_ini']
    env = metadata['env']
    if existing is None:
        return (f'; PlatformIO project generated by mxpio {__version__}\n'
                f'[env:{env}]\n{block}\n; Add your custom settings below this line.\n').encode()
    text = existing.decode('utf-8-sig')
    # Keep the project's newline style when replacing the managed block.
    newline = '\r\n' if '\r\n' in text else '\n'
    block = block.replace('\n', newline)
    if START in text and END in text:
        # The block is located inside the same section as the last one; assert association.
        sections = list(re.finditer(r'^\s*\[([^\]]+)\]\s*$', text, re.MULTILINE))
        start_idx = text.index(START)
        containing = [m.group(1) for m in sections if m.start() < start_idx]
        if not containing or containing[-1] != f'env:{env}':
            raise MXPIOError('PlatformIO管理ブロックのenvがmanifestと不一致です。手動修正せず復元してください。')
        if text.count(START) != 1 or text.count(END) != 1:
            raise MXPIOError('管理ブロックの数が不正です。')
        s, e = text.index(START), text.index(END) + len(END)
        result = text[:s] + block.rstrip('\r\n') + text[e:]
        return result.encode()
    raise MXPIOError('既存のplatformio.iniにMXPIO管理ブロックがありません。安全のため採用を拒否します。別フォルダにinitしてください。')


def manifest_path(output: Path) -> Path:
    return output / STATE_DIR / MANIFEST_NAME


def read_manifest(output: Path) -> dict[str, Any]:
    path = manifest_path(output)
    if not path.is_file():
        raise MXPIOError(f'{path}: 初期化されていません。最初に mxpio init を実行してください。')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as ex:
        raise MXPIOError(f'manifestが読み込めません: {ex}') from ex
    if data.get('schema') != 1:
        raise MXPIOError('manifest形式が未対応です。')
    return data


def safe_path(output: Path, rel: str) -> Path:
    dest = (output / rel).resolve()
    if output.resolve() not in dest.parents:
        raise MXPIOError(f'危険なファイルパス: {rel}')
    return dest


@dataclass
class Plan:
    incoming: dict[str, bytes]
    add: list[str]
    update: list[str]
    remove: list[str]
    unchanged: list[str]
    conflicts: list[str]
    notices: list[str]
    metadata: dict[str, Any]

    @property
    def changed(self) -> bool:
        return bool(self.add or self.update or self.remove)


def plan(source: Path, output: Path, old: dict[str, Any] | None, board: str | None, env_name: str | None) -> Plan:
    source = source.resolve()
    output = output.resolve()
    files, metadata, notes = extract(source, output, board, env_name)
    if old is not None:
        # board/env changes need intentional fresh init, rather than silently mixing configs.
        if old['config']['env'] != metadata['env'] or old['config']['board'] != metadata['board']:
            raise MXPIOError('board/env変更は既存プロジェクトで自動実行しません。新規プロジェクトを作成してください。')
    existing_ini = (output / 'platformio.ini').read_bytes() if (output / 'platformio.ini').is_file() else None
    files['platformio.ini'] = compose_ini(existing_ini, metadata)
    # On subsequent sync, never touch user-owned main.cpp, but still flag collision on first init.
    if old is not None:
        del files['src/main.cpp']
    previous = old.get('generated', {}) if old else {}
    incoming = {p: v for p, v in files.items() if p != 'src/main.cpp' or old is None}
    add, update, remove, unchanged, conflicts = [], [], [], [], []
    for rel, value in sorted(incoming.items()):
        current_hash = digest(safe_path(output, rel))
        new_hash = sha_bytes(value)
        old_hash = previous.get(rel)
        # platformio.ini may contain user-managed lines: compare only managed block, not whole file.
        if rel == 'platformio.ini' and old and current_hash != old_hash:
            ini_text = existing_ini.decode('utf-8-sig').replace('\r\n', '\n') if existing_ini else ''
            m = re.search(re.escape(START) + r'(.*?)' + re.escape(END), ini_text, re.DOTALL)
            old_block_hash = old.get('managed_ini_hash')
            if m is None or sha_bytes(m.group(0).encode()) != old_block_hash:
                conflicts.append('platformio.ini (管理ブロックが手動変更されています)')
                continue
            # outside managed block modifications are normal
            old_hash = current_hash
        if current_hash is None:
            if old_hash is not None:  # user deleted generated file
                conflicts.append(rel + ' (生成ファイルが削除されています)')
            else:
                add.append(rel)
        elif old_hash is None:
            conflicts.append(rel + ' (既存ファイルと衝突)')
        elif rel != 'platformio.ini' and current_hash != old_hash:
            conflicts.append(rel + ' (前回生成後に手動編集されています)')
        elif current_hash == new_hash:
            unchanged.append(rel)
        else:
            update.append(rel)
    for rel, old_hash in sorted(previous.items()):
        if rel not in incoming:
            current_hash = digest(safe_path(output, rel))
            if current_hash != old_hash:
                conflicts.append(rel + ' (削除対象が変更されています)')
            else:
                remove.append(rel)
    if old is None:
        for possible_main in ('src/main.c', 'src/main.cc'):
            if (output / possible_main).is_file():
                conflicts.append(possible_main + ' (既存のmainと生成main.cppが重複します。新規出力先を使用してください)')

    return Plan(incoming, add, update, remove, unchanged, conflicts, notes, metadata)


def write_manifest(output: Path, p: Plan, old: dict[str, Any] | None) -> dict[str, Any]:
    new_generated = {name: digest(safe_path(output, name)) for name in p.incoming
                     if name != 'src/main.cpp'}
    # first init, main.cpp is user-owned and not tracked
    block_match = re.search(re.escape(START) + r'.*?' + re.escape(END),
                            (output / 'platformio.ini').read_text(encoding='utf-8'), re.DOTALL)
    return {'schema': 1, 'version': __version__, 'updated_utc': datetime.now(timezone.utc).isoformat(),
            'config': {k: v for k, v in p.metadata.items() if k != 'managed_ini'},
            'generated': new_generated,
            'managed_ini_hash': sha_bytes(block_match.group(0).encode()) if block_match else None,
            'history_count': (old or {}).get('history_count', 0) + (1 if old else 0)}


def make_backup(output: Path, changed: list[str]) -> Path:
    backups = output / STATE_DIR / 'backups'
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    file = backups / (stamp + '.zip')
    records: dict[str, bool] = {}
    with zipfile.ZipFile(file, 'w', zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(set(changed + [f'{STATE_DIR}/{MANIFEST_NAME}'])):
            orig = safe_path(output, rel)
            records[rel] = orig.is_file()
            if orig.is_file():
                archive.write(orig, arcname='files/' + rel)
        archive.writestr('records.json', json.dumps(records, ensure_ascii=False))
    return file


def apply(output: Path, p: Plan, old: dict[str, Any] | None) -> Path | None:
    if p.conflicts:
        raise MXPIOError('衝突のため一切書き換えません:\n - ' + '\n - '.join(p.conflicts))
    if not p.changed and old is not None:
        return None
    changed = p.add + p.update + p.remove
    backup = make_backup(output, changed) if old is not None else None
    try:
        for rel in p.remove:
            safe_path(output, rel).unlink()
        for rel in p.add + p.update:
            write_bytes_atomic(safe_path(output, rel), p.incoming[rel])
        info = write_manifest(output, p, old)
        write_bytes_atomic(manifest_path(output), (json.dumps(info, ensure_ascii=False, indent=2) + '\n').encode())
    except Exception:
        # A rollback is performed by reading the backup; no user files are overwritten.
        if backup is not None:
            _restore_zip(output, backup)
            backup.unlink(missing_ok=True)
        raise
    return backup


def _restore_zip(output: Path, snapshot: Path) -> None:
    with zipfile.ZipFile(snapshot) as archive:
        records = json.loads(archive.read('records.json'))
        for rel, existed in records.items():
            target = safe_path(output, rel)
            if existed:
                write_bytes_atomic(target, archive.read('files/' + rel))
            else:
                target.unlink(missing_ok=True)


def rollback(output: Path, force: bool = False) -> Path:
    manifest = read_manifest(output)
    backups = sorted((output / STATE_DIR / 'backups').glob('*.zip'))
    if not backups:
        raise MXPIOError('ロールバック用のバックアップがありません。')
    selected = backups[-1]
    with zipfile.ZipFile(selected) as archive:
        records = json.loads(archive.read('records.json'))
        # Rollback may only touch managed paths, or a previous manifest.
        for rel in records:
            if rel.startswith('src/') and not rel.startswith('src/mx_generated/'):
                raise MXPIOError('バックアップ内の未知のsrcファイルを検出しました。')
            if rel not in (list(manifest['generated']) + [f'{STATE_DIR}/{MANIFEST_NAME}', 'platformio.ini']):
                # A deleted file might only exist in the previous manifest.
                previous = None
                try:
                    previous = json.loads(archive.read('files/' + STATE_DIR + '/' + MANIFEST_NAME))
                except KeyError:
                    pass
                if not previous or rel not in previous.get('generated', {}):
                    raise MXPIOError('バックアップに未知のパスがあります。復元を停止します。')
        if not force:
            for rel in records:
                if rel in manifest['generated'] and rel != 'platformio.ini':
                    if digest(safe_path(output, rel)) != manifest['generated'][rel]:
                        raise MXPIOError(f'{rel}が手動編集されました。ロールバックは停止しました。--forceで強制可能。')
            if 'platformio.ini' in records:
                ini = (output / 'platformio.ini').read_text(encoding='utf-8')
                m = re.search(re.escape(START) + r'.*?' + re.escape(END), ini, re.DOTALL)
                if not m or sha_bytes(m.group(0).encode()) != manifest['managed_ini_hash']:
                    raise MXPIOError('platformio.iniの管理ブロックが編集されています。復元を中止します。')
                if digest(output / 'platformio.ini') != manifest['generated'].get('platformio.ini'):
                    raise MXPIOError('platformio.iniが同期後に編集されています。復元による損失を避けるため停止しました。')
    _restore_zip(output, selected)
    selected.unlink(missing_ok=True)
    return selected


def source_signature(source: Path) -> str:
    src, inc = detect_layout(source)
    paths = sorted([*src.glob('*.c'), *inc.glob('*.h'), *source.glob('*.ioc')])
    h = hashlib.sha256()
    for file in paths:
        h.update(file.name.encode())
        h.update(file.read_bytes())
    return h.hexdigest()


def build(output: Path) -> int:
    program = shutil.which('pio') or shutil.which('platformio')
    if program is None:
        raise MXPIOError('PlatformIO CLI（pio）が見つかりません。pip install platformio 等で導入してから check --build を実行してください。')
    return subprocess.run([program, 'run', '-d', str(output)], check=False).returncode


def check(output: Path) -> list[str]:
    manifest = read_manifest(output)
    errors = []
    for rel, expected in sorted(manifest['generated'].items()):
        if rel == 'platformio.ini':
            try:
                content = safe_path(output, rel).read_text(encoding='utf-8')
                m = re.search(re.escape(START) + r'.*?' + re.escape(END), content, re.DOTALL)
                if not m or sha_bytes(m.group(0).encode()) != manifest.get('managed_ini_hash'):
                    errors.append('platformio.iniの管理ブロックが変更されています。')
            except OSError:
                errors.append('platformio.iniがありません。')
        elif digest(safe_path(output, rel)) != expected:
            errors.append(f'{rel}: ファイルが変更/削除されています。')
    if not (output / 'src/main.cpp').exists():
        errors.append('src/main.cpp がありません。')
    src = (output / manifest['config']['source']).resolve()
    if not src.exists():
        errors.append(f'CubeMX生成元がありません: {src}')
    return errors
