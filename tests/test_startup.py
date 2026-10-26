"""Startup behavior and upgrades from projects generated before 0.1.3."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mxpio.core import apply, check, plan, read_manifest, rollback


MAIN = """#include "main.h"
int main(void)
{
    user_before_hal();
    HAL_Init();
    while (1) {}
}
"""


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "cube"
        self.output = self.root / "pio"
        self.src = self.source / "Core/Src"
        self.inc = self.source / "Core/Inc"
        self.src.mkdir(parents=True)
        self.inc.mkdir(parents=True)
        (self.source / "cube.ioc").write_text("Mcu.CPN=STM32G431CBU6\n")
        (self.src / "main.c").write_text(MAIN)
        (self.inc / "main.h").write_text(
            "#include <stdint.h>\nvoid user_before_hal(void);\nvoid HAL_Init(void);\n")
        (self.inc / "stm32g4xx_hal_conf.h").write_text("/* fixture */\n")

    def init(self):
        incoming = plan(self.source, self.output, None, None, "mxpio")
        self.assertEqual(incoming.conflicts, [])
        apply(self.output, incoming, None)
        return read_manifest(self.output)

    def next_plan(self, old):
        return plan(self.source, self.output, old, old["config"]["board"], "mxpio")

    def test_old_project_upgrades_without_editing_user_main_or_ini(self):
        with patch("mxpio.core.VECTOR_TABLE_SETUP", ""):
            old = self.init()
        user = self.output / "src/main.cpp"
        user.write_bytes(b"// user application: keep exactly\r\nint main() { MX_Init(); }\r\n")
        ini = self.output / "platformio.ini"
        ini.write_bytes(ini.read_bytes() +
                        b"\nbuild_src_flags =\n    -std=gnu++20\n    -DUSER_VECT_TAB_ADDRESS\n")
        user_before, ini_before = user.read_bytes(), ini.read_bytes()
        incoming = self.next_plan(old)
        self.assertEqual(incoming.conflicts, [])
        self.assertEqual(incoming.update, ["src/mx_generated/mx_boot.c"])
        self.assertEqual(incoming.add, [])
        self.assertEqual(incoming.remove, [])
        apply(self.output, incoming, old)
        boot = self.output / "src/mx_generated/mx_boot.c"
        self.assertIn("SCB->VTOR", boot.read_text())
        self.assertEqual(user.read_bytes(), user_before)
        self.assertEqual(ini.read_bytes(), ini_before)
        self.assertEqual(check(self.output), [])
        again = self.next_plan(read_manifest(self.output))
        self.assertFalse(again.changed)
        self.assertEqual(again.conflicts, [])
        rollback(self.output)
        self.assertNotIn("SCB->VTOR", boot.read_text())
        self.assertEqual(user.read_bytes(), user_before)
        self.assertEqual(ini.read_bytes(), ini_before)
        self.assertEqual(check(self.output), [])

    @unittest.skipUnless(shutil.which("gcc"), "C compiler required")
    def test_vectors_and_barriers_run_before_user_initializers_and_hal(self):
        # With VTOR, execute the generated code against a simulated core whose
        # vector pointer starts wrong. Without VTOR, no SCB or vector symbol is
        # available, verifying the generated code also compiles on that path.
        header = r"""
#ifndef TEST_MAIN_H
#define TEST_MAIN_H
#include <stdint.h>
extern unsigned stage;
void user_before_hal(void);
void HAL_Init(void);
#if TEST_HAS_VTOR
#define SCB_VTOR_TBLOFF_Msk 0xffffff80UL
typedef struct { uint32_t VTOR; } TestSCB;
extern TestSCB test_scb;
#define SCB (&test_scb)
void test_dsb(void);
void test_isb(void);
#define __DSB() test_dsb()
#define __ISB() test_isb()
#endif
#endif
"""
        probe = r"""
#include "main.h"
#include <assert.h>
unsigned stage;
#if TEST_HAS_VTOR
TestSCB test_scb = {0x1fff0000U};
/* The linked symbol's address, rather than a fixed Flash base, is expected. */
const uint32_t g_pfnVectors[64] = {0};
void test_dsb(void) {
    assert(stage == 0);
    assert(SCB->VTOR == (uint32_t)(uintptr_t)g_pfnVectors);
    stage = 1;
}
void test_isb(void) {
    assert(stage == 1);
    stage = 2;
}
#endif
void user_before_hal(void) {
    assert(stage == (TEST_HAS_VTOR ? 2U : 0U));
    stage = 3;
}
void HAL_Init(void) {
    assert(stage == 3);
    stage = 4;
}
void MX_Init(void);
int main(void) {
    MX_Init();
    assert(stage == 4);
    return 0;
}
"""
        (self.inc / "main.h").write_text(header)
        self.init()
        probe_path = self.root / "probe.c"
        probe_path.write_text(probe)
        for has_vtor in (0, 1):
            with self.subTest(has_vtor=has_vtor):
                executable = self.root / f"startup-{has_vtor}.exe"
                result = subprocess.run(
                    [shutil.which("gcc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                     f"-DTEST_HAS_VTOR={has_vtor}",
                     "-I" + str(self.output / "include/mx_generated"),
                     str(self.output / "src/mx_generated/mx_boot.c"),
                     str(probe_path), "-o", str(executable)],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                result = subprocess.run([str(executable)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
