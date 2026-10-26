# MXPIO Sync v0.1.3

**STM32CubeMXの生成コードを、PlatformIO + STM32Cube HAL + C++へ安全に同期するCLIツール**

- `main.c`の`main()`を、`HAL_Init()`や`SystemClock_Config()`、`MX_*_Init()`を含む **`MX_Init()`** に変換
- ユーザーが自由に編集できる **`src/main.cpp`** を初回に作成し、以降は**一切上書きしない**
- CubeMXの`stm32xxx_hal_msp.c`、`stm32xxx_it.c`、HAL設定ヘッダ、各周辺機能のC/Hファイルを専用領域にコピー
- `.ioc`のMCUに基づきPlatformIO設定を生成。今回のSTM32G431CBは`genericSTM32G431CB`として認識
- `sync`で**差分のみ**更新。生成ファイルを編集した場合は**上書きを拒否**
- 同期の直前にバックアップを取り、`rollback`で直前の変更を復元
- `diff`で変更プレビュー、`check`で整合性確認、`--build`でPlatformIOビルド
- `watch`でCubeMXの変更を監視（デフォルトは通知のみ）
- Windows 11 / macOS / Linux、Python 3.10以降で動作する設計。外部Python依存パッケージなし

> **重要:** `.ioc`だけではC言語の初期化コードは生成できません。STM32CubeMXの **GENERATE CODE** で `Core/Src/main.c` 等を生成してから使います。ツールがCubeMXのGUIを操作したり、`.ioc`からHAL初期化コードを直接合成したりするわけではありません。

## 割り込みベクタの自動設定（v0.1.3）

生成する `MX_Init()` の先頭で、HALが割り込みを有効にする前に `SCB->VTOR` を
アプリのベクタテーブルへ設定します。リンクされた `g_pfnVectors` のアドレスを使うため、
Flash先頭アドレスを固定せず、リンカで指定した配置先にも追従します。設定後は
`__DSB()` / `__ISB()` で変更を反映します。

新規プロジェクトは `mxpio init`、既存プロジェクトは更新後の `mxpio sync` で有効になります。
`main.cpp` は上書きしません。以前追加した同じベクタを指す手動設定は削除できます。
`build_src_flags` の `-DUSER_VECT_TAB_ADDRESS` はフレームワークの `SystemInit()` に
適用されないため、この自動設定には使用しません。

VTORを持つCMSISコア（`SCB_VTOR_TBLOFF_Msk` が定義される環境）が対象です。
VTORのないコアでは設定を生成コードの条件付きコンパイルで省略し、
メモリ再マップ処理は自動追加しません。STM32G431CB以外の動作は未検証です。
ブートローダから直接ジャンプする場合のMSP・割り込み・周辺機能の引き継ぎや、
独自startupでの別名のベクタシンボルには別途対応が必要です。
CubeMXで無効な周辺割り込みを有効化したり、タイマ/PWMを開始したりする処理は追加しません。

## 1. インストール（Windows PowerShell）

ZIPを展開後、`pyproject.toml` があるディレクトリで実行します。

```powershell
cd "C:\path\to\mxpio_tool"
py -m pip install .
mxpio --help
```

編集しながら開発する場合:

```powershell
py -m pip install -e .
```

`mxpio`コマンドが見つからない場合は、展開したディレクトリで以下でも実行できます。

```powershell
$env:PYTHONPATH = "$PWD\src"
py -m mxpio --help
```

PlatformIOでビルドするには、別途PlatformIO CLI（`pio`コマンド）が必要です。

## 2. STM32CubeMX側の準備

1. STM32CubeMXで`.ioc`を開き、GPIO、Encoder Mode、PWM、I2Cなどを設定
2. `Project Manager`のツールチェーンは **Makefile** など、C/Hコードを生成できるものを使用
3. **GENERATE CODE** をクリックし、次の構成を用意

```text
edge_controller_mx/
├── edge_controller_mx.ioc
└── Core/
    ├── Inc/
    │   ├── main.h
    │   ├── stm32g4xx_hal_conf.h
    │   └── ...
    └── Src/
        ├── main.c
        ├── stm32g4xx_hal_msp.c
        ├── stm32g4xx_it.c
        └── ...
```

`Src/` と `Inc/` がプロジェクト直下にある構成にも対応します。

> CubeMXで `main.c` の`while (1)`にアプリのコードを書いている場合は、**先にそのコードを移してください**。黙って捨てないようツールは変換を拒否します。`USER CODE`のうち、`main()`外や初期化段階のものはCコード側に維持します。

## 3. `.ioc`の確認（CubeMXコード生成前でも可能）

```powershell
mxpio inspect --ioc "C:\path\to\edge_controller_mx.ioc"
```

このコマンドはMCU、PlatformIO board候補、CubeMXバージョン、使用周辺機能、SYSCLKを一覧表示します。**確認専用**であり、ファームウェアを生成しません。

## 4. 初回変換

以下は今回のSTM32G431CB構成での例です（実際のパスに置き換えてください）。

```powershell
mxpio init `
  --source "C:\Users\ユーザー\Documents\basic_research\workspace\edge_controller_mx" `
  --out "C:\Users\ユーザー\Documents\basic_research\workspace\edge-controller" `
  --board genericSTM32G431CB
```

`--board`を省略すると`.ioc`の`Mcu.CPN`から推測します。**正式なPlatformIOボードIDはデバイスごとに確認してください。** STM32G431CBについては該当IDを確認しています。

初回変換後:

```text
edge-controller/
├── platformio.ini              # 一部分をMXPIOが管理
├── .mxpio/
│   └── manifest.json           # 生成ファイルのハッシュ、元パス等
├── include/
│   ├── mxpio/
│   │   ├── mx_init.h           # C/C++兼用MX_Init宣言・ハンドル公開
│   │   └── mx_handles.h        # HALハンドルのextern "C"宣言を自動生成
│   └── mx_generated/
│       ├── main.h
│       ├── stm32g4xx_hal_conf.h
│       ├── tim.h
│       └── ...
└── src/
    ├── main.cpp                # ユーザー専用。以後更新しない
    └── mx_generated/
        ├── mx_boot.c           # 元のmain.c由来。MX_Init()を実装
        ├── stm32g4xx_hal_msp.c
        ├── stm32g4xx_it.c
        └── ...
```

`main.cpp`の初期状態:

```cpp
#include "mxpio/mx_init.h"

int main()
{
    MX_Init();  // HAL_Init() → クロック → GPIO → TIM → I2C ...

    // 必要なタイマ開始操作や、ユーザーのアプリ初期化はここへ
    while (true) {
        // 自分で書いたC++コード
    }
}
```

`mxpio/mx_init.h` は自動生成され、CubeMXの `main.h` と `mx_handles.h` を読み込みます。そのため、`main.cpp` では HAL のGPIO関数などを追加のHALヘッダなしで呼び出せます。例えば、CubeMXでPC4を `GPIO_Output` に設定してから次のように記述できます。

```cpp
#include "mxpio/mx_init.h"

int main() {
    MX_Init();
    while (true) {
        HAL_GPIO_TogglePin(GPIOC, GPIO_PIN_4);
        HAL_Delay(500);
    }
}
```

`HAL_Delay(500)`で約500msごとに反転するため、点滅1周期は約1秒です。設定したピン以外のGPIOやPeripheral Clockが使えるとは限りません。

v0.1.2以降では、実際に移行するCファイルのグローバルなHALハンドル定義から、`mxpio/mx_handles.h` に宣言を自動生成します。`main.c`にまとめる生成方式と、`tim.c` / `i2c.c`等へ分ける生成方式の両方が対象です。

```cpp
#include "mxpio/mx_init.h"

// 手動のextern宣言やextern "C"ブロックは不要。
// CubeMXでTIM8とI2C2を有効にして生成・同期した場合:
void start_pwm() {
    HAL_TIM_PWM_Start(&htim8, TIM_CHANNEL_1);
}
```

`TIM_HandleTypeDef`等の型はHALから読み込み、`htim8` / `hi2c2` / `hdma_*`等の公開ハンドルだけをextern宣言します。変数本体の定義を追加しないため、二重定義は起こしません。`static`変数、関数内変数、関数引数、関数プロトタイプ、単なるextern宣言は対象外です。条件付きの定義は同じ`#if`条件で宣言します。ハンドルの追加・削除は次の`mxpio sync`に追従し、自動生成ヘッダも衝突検査・バックアップ・rollbackの対象です。

標準的な`XXX_HandleTypeDef handle;`形式（初期化、複数変数、配列・ポインタを含む）を対象とし、宣言を独自マクロで生成する形式やCファイル内だけに定義したマクロに依存する条件は対象外です。既存プロジェクトも、ツール更新後に通常の`mxpio diff` / `mxpio sync`で移行できます。ユーザー所有の`main.cpp`は変更しません。

`MX_Init()`は**周辺機能を初期化するだけ**です。CubeMXで生成される `HAL_TIM_PWM_Start()` や `HAL_TIM_Encoder_Start()` が必ず自動実行されるわけではありません。必要な開始処理は明示的に記述してください。

## 5. CubeMXの設定変更を同期する

CubeMXで設定を変更して **GENERATE CODE** を実行後、出力先で:

```powershell
cd "C:\Users\ユーザー\Documents\basic_research\workspace\edge-controller"
mxpio diff
mxpio sync
mxpio check
```

別フォルダにいる場合:

```powershell
mxpio sync --out "C:\path\to\edge-controller"
```

生成元を移動した場合:

```powershell
mxpio sync --out "C:\path\to\edge-controller" --source "C:\new\CubeMX\directory"
```

次回は移動後の元パスがmanifestに記録されます。

### 自動監視（任意）

```powershell
# CubeMXで生成コードが変わったら差分だけ表示する
mxpio watch

# 更新が安定した後に自動同期する（衝突時は停止）
mxpio watch --apply

# 自動同期後にビルドも行う
mxpio watch --apply --build
```

`watch`はポーリング監視です。**CubeMXのGENERATE CODE自体は自動実行しません。** 保存中のファイルを読み込む危険を減らすため、変更が2回の監視周期で安定した後に動作します。`Ctrl+C`で終了します。

## 6. 検査・ビルド

```powershell
# 生成領域に手動変更や削除がないか検査
mxpio check

# さらにPlatformIO CLIで実際にコンパイル
mxpio check --build
```

`check`単独では**コンパイル成功を保証しません**。実際のビルド確認には`--build`が必要です。ハードウェア動作（タイマ周波数、GPIOの実測、割り込み）は別途実機で検証してください。

PlatformIO自体の操作:

```powershell
pio run
pio run -t upload
```

## 7. バックアップと復元

`sync`で変更がある場合、変更対象ファイルと直前manifestのZIPを`.mxpio/backups/`に保存します。

```powershell
mxpio rollback
```

現在の生成ファイルが手動編集されている場合、復元は拒否されます。自分で安全性を確認した場合に限り:

```powershell
mxpio rollback --force
```

`--force`は変更を失う可能性があるため注意してください。`src/main.cpp`は復元対象に含まれません。

> 初回`init`の直前バックアップは作成しません。`rollback`は主に2回目以降の`sync`を戻す機能です。通常のGit併用も推奨します。

## 8. よくあるエラー

| エラー | 理由 / 対応 |
|---|---|
| `Core/Src/main.c ... が必要` | `.ioc`だけ。CubeMXでGENERATE CODEする |
| `while(1)内にユーザー処理` | ループの処理を自分の`main.cpp`へ移した後、再生成する |
| `既存ファイルと衝突` | 出力先に同名ファイルがある。別の新規フォルダを使用する |
| `前回生成後に手動編集` | 自動生成領域が変更された。必要な変更はCubeMX側で行い、生成領域を元に戻す |
| `platformio.iniにMXPIO管理ブロックなし` | 既存のPlatformIOプロジェクトには上書きしない。新規出力先を使う |
| `pioが見つかりません` | PlatformIO CLIをインストール・PATH設定する |
| `framework ...`やHAL関数のリンクエラー | MCU ID / HAL config / CubeMXとPlatformIOのHALバージョン差を確認する |

## 9. 対応範囲と制約（重要）

### 対応・想定

- **STM32Cube HAL**を使うCubeMX標準Cコード生成（`Core/Src`/`Core/Inc`、または`Src`/`Inc`）
- `main.c`内に初期化コード、標準の空の`while(1)`がある構成
- STM32G431CB向けの基本HAL構成を主な想定として開発
- TIM Encoder Mode、PWM、UART、I2C等について **CubeMXの生成コードをそのまま移植**。特別な再実装はしない

### 未対応・注意点

1. **CubeMXのCコード生成を代行しない**（`.ioc`のみでビルド可能なファームウェアは作れない）。
2. **FreeRTOS、USB、Middleware**、複雑なカスタム構成は未対応。検出した場合は変換を停止する。
3. LL API専用プロジェクト、C++生成コード、非標準の`main()`、`while(1)`にユーザーコードがあるものは、自動変換を保証しない。
4. `stm32xxx_hal_conf.h`はCubeMX側のものを優先し、`board_build.stm32cube.custom_config_header = yes`を設定する。**CubeMX生成の`system_stm32xxx.c`やstartupファイルは使用せずPlatformIO提供版を利用**。特殊なsystem構成では一致しない場合がある。
5. CubeMX側とPlatformIO側で **HALライブラリのバージョンが異なる**とコンパイルエラー等が出る可能性がある。
6. CubeMXの`syscalls.c`と`sysmem.c`はコピーしない。独自のmalloc / _sbrk / syscalls が必要なプロジェクトでは手動対応する。
7. `platformio.ini`の**管理ブロック内は編集しない**。追加設定（`monitor_speed`、`lib_deps`など）は管理ブロックの外に書く。
8. 自動同期は**ユーザーのAPI利用方法**（例: TIM2からTIM3へ置換したC++コード）までは修正しない。
9. カスタムヘッダのサブディレクトリ配置、CubeMXの特殊なフォルダ構成、一部のボードID自動推測は検証していない。
10. v0.1.0は**ソース変換とファイル同期の自動テスト済み**だが、この配布環境にはPlatformIO CLIがないため、実機向けSTM32ファームウェアのコンパイル・書き込みテストは**未実施**。

このツールは非標準コードを無理に推定して変換するよりも、**停止して原因を明示する**方針です。

## 10. 自動テスト

展開したプロジェクトルートで:

```powershell
py -m pip install -e .
py -m unittest discover -s tests -v
```

## 11. 参考ドキュメント

- PlatformIO STM32Cube framework: https://docs.platformio.org/en/stable/frameworks/stm32cube.html
- STM32G431CB board: https://docs.platformio.org/en/latest/boards/ststm32/genericSTM32G431CB.html

## 12. フォルダ分離ルール

| 領域 | 所有者 | syncで更新するか |
|---|---|---|
| `src/main.cpp` | ユーザー | **しない** |
| `src/mx_generated/` | ツール | **する** |
| `include/mx_generated/` | ツール | **する** |
| `include/mxpio/mx_init.h` | ツール | **する** |
| `platformio.ini`の管理ブロック | ツール | **する** |
| `platformio.ini`の管理ブロック外 | ユーザー | **保持する** |
| その他の手動追加ファイル | ユーザー | **触れない** |

**推奨ワークフロー：CubeMXではハード設定、PlatformIOではC++のアプリ実装。**
