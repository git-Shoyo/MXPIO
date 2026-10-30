import json
import unittest

import test_mxpio as fixtures
from mxpio.core import MXPIOError, apply, plan, read_manifest, rollback, source_signature


class NucleoSupportTests(unittest.TestCase):
    setUp = fixtures.WorkFlowTests.setUp
    tearDown = fixtures.WorkFlowTests.tearDown
    _init = fixtures.WorkFlowTests._init

    def add_nucleo(self):
        (self.source / "cube.ioc").write_text(
            "Mcu.CPN=STM32G431KBT6\nboard=NUCLEO-G431KB\n")
        header = self.source / "Core/Inc/main.h"
        header.write_text(header.read_text() + '\n#include "stm32g4xx_nucleo.h"\n')
        bsp = self.source / "Drivers/BSP/STM32G4xx_Nucleo"
        bsp.mkdir(parents=True)
        for name, data in {
            "stm32g4xx_nucleo.h": '#include "stm32g4xx_nucleo_errno.h"\n',
            "stm32g4xx_nucleo_errno.h": "#define BSP_ERROR_NONE 0\n",
            "stm32g4xx_nucleo.c": '#include "stm32g4xx_nucleo.h"\nvoid BSP_LED_Init(void) {}\n',
        }.items():
            (bsp / name).write_text(data)
        return bsp

    def test_init_generates_buildable_board_and_bsp_layout(self):
        self.add_nucleo()
        p = plan(self.source, self.output, None, None, "mxpio")
        self.assertEqual(p.metadata["board"], "nucleo_g431kb")
        self.assertFalse(any(name.startswith("boards/") for name in p.incoming))
        self.assertIn("src/mx_generated/bsp/STM32G4xx_Nucleo/stm32g4xx_nucleo.c", p.add)
        self.assertIn("include/mx_generated/bsp/STM32G4xx_Nucleo/stm32g4xx_nucleo_errno.h", p.add)
        ini = p.incoming["platformio.ini"].decode()
        self.assertIn("-Iinclude/mx_generated/bsp/STM32G4xx_Nucleo", ini)
        self.assertIn("-DUSE_NUCLEO_32", ini)
        self.assertNotIn(str(self.source), ini)
        apply(self.output, p, None)
        again = plan(self.source, self.output, read_manifest(self.output), None, "mxpio")
        self.assertFalse(again.changed)
        self.assertFalse(again.conflicts)

    def test_bsp_changes_sync_rollback_and_conflict_guard(self):
        bsp = self.add_nucleo()
        old = self._init()
        relative = "src/mx_generated/bsp/STM32G4xx_Nucleo/stm32g4xx_nucleo.c"
        original = (self.output / relative).read_bytes()
        signature = source_signature(self.source)
        (bsp / "stm32g4xx_nucleo.c").write_text("// updated BSP\n")
        self.assertNotEqual(signature, source_signature(self.source))
        application = self.output / "src/main.cpp"
        application.write_text("// user application")
        update = plan(self.source, self.output, old, None, "mxpio")
        self.assertIn(relative, update.update)
        apply(self.output, update, old)
        self.assertEqual(application.read_text(), "// user application")
        rollback(self.output)
        self.assertEqual((self.output / relative).read_bytes(), original)
        (self.output / relative).write_text("// user edited generated BSP")
        update = plan(self.source, self.output, read_manifest(self.output), None, "mxpio")
        self.assertTrue(any(relative in item for item in update.conflicts))
        with self.assertRaises(MXPIOError):
            apply(self.output, update, old)

    def test_board_definition_is_added_to_legacy_manifest(self):
        self.add_nucleo()
        p = plan(self.source, self.output, None, "genericSTM32G431KB", "mxpio")
        board = json.loads(p.incoming["boards/genericSTM32G431KB.json"])
        self.assertEqual(board["build"]["mcu"], "stm32g431kbt6")
        self.assertEqual(board["upload"]["maximum_size"], 131072)
        self.assertEqual(board["upload"]["maximum_ram_size"], 32768)
        apply(self.output, p, None)
        old = read_manifest(self.output)
        relative = "boards/genericSTM32G431KB.json"
        (self.output / relative).unlink()
        del old["generated"][relative]
        p = plan(self.source, self.output, old, "genericSTM32G431KB", "mxpio")
        self.assertIn(relative, p.add)
        self.assertFalse(p.conflicts)

    def test_explicit_supported_board_needs_no_local_definition(self):
        self.add_nucleo()
        p = plan(self.source, self.output, None, "nucleo_g431kb", "mxpio")
        self.assertFalse(any(name.startswith("boards/") for name in p.incoming))
        self.assertIn("-DUSE_NUCLEO_32", p.incoming["platformio.ini"].decode())

    def test_unused_bsp_is_not_copied(self):
        self.add_nucleo()
        (self.source / "Core/Inc/main.h").write_text('#include "stm32g4xx_hal.h"')
        p = plan(self.source, self.output, None, None, "mxpio")
        self.assertFalse(any("/bsp/" in name for name in p.incoming))
        self.assertNotIn("-DUSE_NUCLEO", p.incoming["platformio.ini"].decode())

    def test_missing_bsp_stops_before_any_output_is_written(self):
        bsp = self.add_nucleo()
        (bsp / "stm32g4xx_nucleo.c").unlink()
        with self.assertRaisesRegex(MXPIOError, "BSP生成ファイル"):
            plan(self.source, self.output, None, None, "mxpio")
        self.assertFalse(self.output.exists())

    def test_cproject_symbol_used_without_ioc_board(self):
        self.add_nucleo()
        (self.source / "cube.ioc").write_text("Mcu.CPN=STM32G431KBT6\n")
        cproject = self.source / ".cproject"
        cproject.write_text('<cproject><listOptionValue value="USE_NUCLEO_32"/></cproject>')
        before = source_signature(self.source)
        p = plan(self.source, self.output, None, None, "mxpio")
        self.assertIn("-DUSE_NUCLEO_32", p.incoming["platformio.ini"].decode())
        cproject.write_text('<cproject><listOptionValue value="USE_NUCLEO_64"/></cproject>')
        self.assertNotEqual(before, source_signature(self.source))

    def test_missing_or_conflicting_board_symbols_are_rejected(self):
        self.add_nucleo()
        (self.source / "cube.ioc").write_text("Mcu.CPN=STM32G431KBT6\n")
        with self.assertRaisesRegex(MXPIOError, "USE_NUCLEO"):
            plan(self.source, self.output, None, None, "mxpio")
        (self.source / "cube.ioc").write_text("Mcu.CPN=STM32G431KBT6\nboard=NUCLEO-G431KB\n")
        (self.source / ".cproject").write_text(
            '<cproject><listOptionValue value="USE_NUCLEO_64"/></cproject>')
        with self.assertRaisesRegex(MXPIOError, "USE_NUCLEO"):
            plan(self.source, self.output, None, None, "mxpio")

    def test_64_pin_board_uses_64_pin_symbol(self):
        self.add_nucleo()
        (self.source / "cube.ioc").write_text("Mcu.CPN=STM32G431RBT6\nboard=NUCLEO-G431RB\n")
        p = plan(self.source, self.output, None, "nucleo_g431rb", "mxpio")
        self.assertIn("-DUSE_NUCLEO_64", p.incoming["platformio.ini"].decode())
        self.assertNotIn("-DUSE_NUCLEO_32", p.incoming["platformio.ini"].decode())
