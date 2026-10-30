"""Project-local board definitions and CubeMX board support files."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def local_board_files(board: str) -> dict[str, bytes]:
    # This MCU has no generic board in PlatformIO ststm32 20.0.0.
    # Keep the ID used by existing manifests and provide the definition locally.
    if board != "genericSTM32G431KB":
        return {}
    definition = {
        "build": {
            "core": "stm32", "cpu": "cortex-m4",
            "extra_flags": "-DSTM32G4 -DSTM32G4xx -DSTM32G431xx",
            "f_cpu": "170000000L", "mcu": "stm32g431kbt6",
            "product_line": "STM32G431xx",
            "variant": "STM32G4xx/G431K(6-8-B)(T-U)_G441KB(T-U)",
        },
        "connectivity": ["can"],
        "debug": {
            "jlink_device": "STM32G431KB", "openocd_target": "stm32g4x",
            "svd_path": "STM32G431xx.svd",
        },
        "frameworks": ["arduino", "cmsis", "libopencm3", "stm32cube"],
        "name": "STM32G431KB (32k RAM, 128k Flash)",
        "upload": {
            "maximum_ram_size": 32768, "maximum_size": 131072,
            "protocol": "stlink",
            "protocols": ["stlink", "jlink", "cmsis-dap", "blackmagic", "mbed"],
        },
        "url": "https://www.st.com/en/microcontrollers-microprocessors/stm32g431kb.html",
        "vendor": "ST",
    }
    return {f"boards/{board}.json": (json.dumps(definition, indent=2) + "\n").encode()}


def nucleo_support(source: Path, family: str, cfg: dict[str, str],
                   generated: dict[str, bytes]) -> tuple[dict[str, bytes], list[str]]:
    """Copy the referenced G4 Nucleo BSP into tracked, self-contained output."""
    if family != "g4":
        return {}, []
    include = re.compile(rb'^\s*#\s*include\s*[<"]stm32g4xx_nucleo\.h[>"]', re.MULTILINE)
    if not any(include.search(data) for name, data in generated.items()
               if name.endswith((".c", ".h"))):
        return {}, []

    bsp = source / "Drivers/BSP/STM32G4xx_Nucleo"
    for required in ("stm32g4xx_nucleo.c", "stm32g4xx_nucleo.h", "stm32g4xx_nucleo_errno.h"):
        if not (bsp / required).is_file():
            raise ValueError(f"Nucleo BSP生成ファイルがありません: {bsp / required}")

    # CubeIDE records this required symbol outside Core's generated headers.
    symbols = set()
    cproject = source / ".cproject"
    if cproject.is_file():
        try:
            tree = ET.parse(cproject)
        except ET.ParseError as exc:
            raise ValueError(f".cprojectのBSP設定を読み込めません: {exc}") from exc
        for item in tree.iter("listOptionValue"):
            value = item.get("value", "")
            if re.fullmatch(r"USE_NUCLEO_(?:32|64)(?:=1)?", value):
                symbols.add(value.split("=", 1)[0])
    # Also support CubeMX exports without CubeIDE metadata.
    known_boards = {
        "NUCLEO-G431KB": "USE_NUCLEO_32",
        "NUCLEO-G431RB": "USE_NUCLEO_64",
        "NUCLEO-G474RE": "USE_NUCLEO_64",
        "NUCLEO-G491RE": "USE_NUCLEO_64",
    }
    for key in ("board", "BSP_IP_NAME"):
        symbol = known_boards.get(cfg.get(key, "").upper())
        if symbol:
            symbols.add(symbol)
    if len(symbols) != 1:
        raise ValueError("Nucleo BSPのUSE_NUCLEO_32/64を一意に判定できません。IOCと.cprojectのボード設定を確認してください。")

    files = {}
    include_dirs = set()
    for file in sorted(bsp.rglob("*")):
        if not file.is_file() or file.suffix not in (".c", ".h"):
            continue
        rel = file.relative_to(bsp).as_posix()
        base = "src" if file.suffix == ".c" else "include"
        target = f"{base}/mx_generated/bsp/STM32G4xx_Nucleo/{rel}"
        files[target] = file.read_bytes()
        if file.suffix == ".h":
            include_dirs.add(Path(target).parent.as_posix())
    flags = [f"-I{path}" for path in sorted(include_dirs)]
    flags.append(f"-D{next(iter(symbols))}")
    return files, flags
