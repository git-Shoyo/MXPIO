import unittest

import test_mxpio as fixtures
from mxpio.core import MXPIOError, apply, detect_board, plan, read_manifest


class BoardDetectionTests(unittest.TestCase):
    def test_cubemx_board_names_map_to_platformio_ids(self):
        for suffix, part in (
            ("F303K8", "STM32F303K8T6"),
            ("G431KB", "STM32G431KBT6"),
            ("G431RB", "STM32G431RBT6"),
            ("G474RE", "STM32G474RET6"),
            ("G491RE", "STM32G491RET6"),
        ):
            with self.subTest(board=suffix):
                self.assertEqual(detect_board({"board": "NUCLEO-" + suffix, "Mcu.CPN": part}),
                                 "nucleo_" + suffix.lower())

    def test_custom_unknown_and_absent_board_keep_mcu_detection(self):
        for name in ("", "custom", "NUCLEO-UNKNOWN"):
            with self.subTest(board=name):
                self.assertEqual(detect_board({"board": name, "Mcu.CPN": "STM32F303K8T6"}),
                                 "genericSTM32F303K8")

    def test_board_name_normalization(self):
        self.assertEqual(detect_board({"board": " nucleo-f303k8 ", "Mcu.CPN": "stm32f303k8t6"}),
                         "nucleo_f303k8")

    def test_inconsistent_board_and_mcu_are_rejected(self):
        for part in ("STM32G431KBT6", ""):
            with self.subTest(part=part), self.assertRaisesRegex(MXPIOError, "一致しません"):
                detect_board({"board": "NUCLEO-F303K8", "Mcu.CPN": part})


class BoardWorkflowTests(unittest.TestCase):
    setUp = fixtures.WorkFlowTests.setUp
    tearDown = fixtures.WorkFlowTests.tearDown

    def use_f303(self):
        (self.source / "cube.ioc").write_text("Mcu.CPN=STM32F303K8T6\nboard=NUCLEO-F303K8\n")
        (self.source / "Core/Inc/stm32g4xx_hal_conf.h").unlink()
        (self.source / "Core/Inc/stm32f3xx_hal_conf.h").write_text("#define HAL_GPIO_MODULE_ENABLED")
        (self.source / "Core/Inc/main.h").write_text('#include "stm32f3xx_hal.h"')

    def test_f303_init_emits_board_in_ini_and_manifest(self):
        self.use_f303()
        p = plan(self.source, self.output, None, None, "mxpio")
        self.assertIn("board = nucleo_f303k8", p.incoming["platformio.ini"].decode())
        apply(self.output, p, None)
        old = read_manifest(self.output)
        self.assertEqual(old["config"]["board"], "nucleo_f303k8")
        again = plan(self.source, self.output, old, None, "mxpio")
        self.assertFalse(again.changed)
        self.assertFalse(again.conflicts)

    def test_explicit_board_and_legacy_manifest_are_preserved(self):
        self.use_f303()
        p = plan(self.source, self.output, None, "genericSTM32F303K8", "mxpio")
        apply(self.output, p, None)
        old = read_manifest(self.output)
        again = plan(self.source, self.output, old, None, "mxpio")
        self.assertEqual(again.metadata["board"], "genericSTM32F303K8")
        self.assertFalse(again.changed)
        self.assertFalse(again.conflicts)
        with self.assertRaisesRegex(MXPIOError, "board/env変更"):
            plan(self.source, self.output, old, "nucleo_f303k8", "mxpio")
