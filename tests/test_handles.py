import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mxpio.core import MXPIOError, _masked, apply, check, plan, read_manifest, rollback
from mxpio.handles import generate_handles_header


class HandleScannerTests(unittest.TestCase):
    def header(self, source):
        return generate_handles_header({'main.c': source}, _masked).decode()

    def test_only_public_global_objects_are_exported(self):
        header = self.header("""
// TIM_HandleTypeDef commented_out;
/* I2C_HandleTypeDef also_commented; */
#define DECLARE_HANDLE TIM_HandleTypeDef macro_only;
extern TIM_HandleTypeDef declared_elsewhere;
static TIM_HandleTypeDef private_timer;
TIM_HandleTypeDef make_timer(void);
void configure(TIM_HandleTypeDef *parameter);
typedef struct { TIM_HandleTypeDef member; } Holder;
void callback(TIM_HandleTypeDef *parameter) {
    TIM_HandleTypeDef local_timer;
    const char *text = "TIM_HandleTypeDef string_only; { }";
}
TIM_HandleTypeDef public_timer;
I2C_HandleTypeDef public_i2c;
""")
        self.assertIn('extern TIM_HandleTypeDef public_timer;', header)
        self.assertIn('extern I2C_HandleTypeDef public_i2c;', header)
        for name in ('commented_out', 'also_commented', 'macro_only', 'declared_elsewhere',
                     'private_timer', 'make_timer', 'configure', 'member', 'parameter',
                     'local_timer', 'string_only'):
            self.assertNotIn(name, header)

    def test_initializers_multiple_objects_arrays_and_qualifiers(self):
        header = self.header("""
TIM_HandleTypeDef htim1 = { .value = 1 }, htim2;
DMA_HandleTypeDef hdma[(1 + 1)] = {{0}, {0}}, *dma_pointer;
volatile UART_HandleTypeDef huart;
const SPI_HandleTypeDef hspi = {0};
""")
        for declaration in (
            'extern TIM_HandleTypeDef htim1;', 'extern TIM_HandleTypeDef htim2;',
            'extern DMA_HandleTypeDef hdma[(1 + 1)];',
            'extern DMA_HandleTypeDef *dma_pointer;',
            'extern volatile UART_HandleTypeDef huart;',
            'extern const SPI_HandleTypeDef hspi;',
        ):
            self.assertIn(declaration, header)
        self.assertNotIn('.value', header)

    def test_nested_preprocessor_conditions_and_continuations(self):
        header = self.header("""
#if defined(FIRST) && \
    defined(SECOND)
#ifdef NESTED
TIM_HandleTypeDef nested;
#endif
#elif defined(THIRD)
TIM_HandleTypeDef alternative;
#else
TIM_HandleTypeDef fallback;
#endif
#if 0
TIM_HandleTypeDef disabled;
#endif
TIM_HandleTypeDef always;
""")
        self.assertIn('#if defined(FIRST) &&', header)
        self.assertIn('#ifdef NESTED\nextern TIM_HandleTypeDef nested;\n#endif\n#endif', header)
        self.assertIn('#elif defined(THIRD)\nextern TIM_HandleTypeDef alternative;', header)
        self.assertIn('#else\nextern TIM_HandleTypeDef fallback;', header)
        self.assertIn('#if 0\nextern TIM_HandleTypeDef disabled;\n#endif', header)
        self.assertIn('extern TIM_HandleTypeDef always;', header)
        self.assertNotIn('\\\n', header)

    def test_duplicate_definitions_do_not_duplicate_the_declaration(self):
        header = generate_handles_header({
            'a.c': 'TIM_HandleTypeDef htim1;',
            'b.c': 'extern TIM_HandleTypeDef htim1;\nTIM_HandleTypeDef htim1;',
        }, _masked).decode()
        self.assertEqual(header.count('extern TIM_HandleTypeDef htim1;'), 1)

    def test_preprocessor_string_literals_are_not_erased(self):
        header = self.header('#if __has_include("device.h")\nTIM_HandleTypeDef guarded;\n#endif\n')
        self.assertIn('#if __has_include("device.h")', header)
        self.assertIn('extern TIM_HandleTypeDef guarded;', header)

    def test_header_with_no_handles_is_valid(self):
        header = self.header('void empty(void) {}')
        self.assertIn('extern "C" {', header)
        self.assertNotIn('extern TIM_', header)


class HandleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'cube'
        self.output = self.root / 'pio'
        self.src = self.source / 'Core/Src'
        self.inc = self.source / 'Core/Inc'
        self.src.mkdir(parents=True)
        self.inc.mkdir(parents=True)
        (self.source / 'cube.ioc').write_text('Mcu.CPN=STM32G431CBU6\n')
        (self.src / 'main.c').write_text(
            '#include "main.h"\nTIM_HandleTypeDef htim2;\n'
            'int main(void) { while (1) {} }\n')
        (self.inc / 'main.h').write_text(
            '#ifndef TEST_MAIN_H\n#define TEST_MAIN_H\n'
            'typedef struct { int value; } TIM_HandleTypeDef;\n'
            'typedef struct { int value; } I2C_HandleTypeDef;\n'
            'typedef struct { int value; } DMA_HandleTypeDef;\n#endif\n')
        (self.inc / 'stm32g4xx_hal_conf.h').write_text('#define HAL_TIM_MODULE_ENABLED\n')

    def init(self):
        incoming = plan(self.source, self.output, None, None, 'mxpio')
        self.assertEqual(incoming.conflicts, [])
        apply(self.output, incoming, None)
        return read_manifest(self.output)

    def next_plan(self, old):
        return plan(self.source, self.output, old, old['config']['board'], old['config']['env'])

    def test_split_peripheral_files_and_dma_are_exported(self):
        (self.src / 'i2c.c').write_text(
            '#include "main.h"\nI2C_HandleTypeDef hi2c2;\nDMA_HandleTypeDef hdma_i2c2_rx;\n')
        # These files are excluded from conversion, so their globals must not leak.
        (self.src / 'syscalls.c').write_text('TIM_HandleTypeDef skipped_syscall;')
        (self.src / 'system_stm32g4xx.c').write_text('TIM_HandleTypeDef skipped_system;')
        self.init()
        header = (self.output / 'include/mxpio/mx_handles.h').read_text()
        self.assertIn('extern TIM_HandleTypeDef htim2;', header)
        self.assertIn('extern I2C_HandleTypeDef hi2c2;', header)
        self.assertIn('extern DMA_HandleTypeDef hdma_i2c2_rx;', header)
        self.assertNotIn('skipped_', header)
        self.assertIn('#include "mx_handles.h"',
                      (self.output / 'include/mxpio/mx_init.h').read_text())

    def test_handle_changes_sync_and_rollback_preserve_user_files(self):
        old = self.init()
        user = self.output / 'src/main.cpp'
        user.write_text('// my application stays unchanged\n')
        ini = self.output / 'platformio.ini'
        ini.write_text(ini.read_text() + '\nbuild_src_flags = -std=gnu++20\n')
        (self.src / 'main.c').write_text(
            (self.src / 'main.c').read_text().replace('htim2;', 'htim8;'))
        incoming = self.next_plan(old)
        self.assertIn('include/mxpio/mx_handles.h', incoming.update)
        apply(self.output, incoming, old)
        header = (self.output / 'include/mxpio/mx_handles.h').read_text()
        self.assertIn('htim8;', header)
        self.assertNotIn('htim2;', header)
        self.assertEqual(user.read_text(), '// my application stays unchanged\n')
        self.assertIn('-std=gnu++20', ini.read_text())
        self.assertEqual(check(self.output), [])
        rollback(self.output)
        self.assertIn('htim2;', (self.output / 'include/mxpio/mx_handles.h').read_text())
        self.assertEqual(user.read_text(), '// my application stays unchanged\n')

    def test_pre_handle_generation_project_upgrades_and_rolls_back(self):
        old = self.init()
        # Reproduce the previous release's generated state and manifest.
        header = self.output / 'include/mxpio/mx_handles.h'
        header.unlink()
        del old['generated']['include/mxpio/mx_handles.h']
        init_header = self.output / 'include/mxpio/mx_init.h'
        init_header.write_text(init_header.read_text().replace(
            '#include "mx_handles.h"  /* C-linkage declarations for generated HAL handles */\n', ''))
        from mxpio.core import digest
        old['generated']['include/mxpio/mx_init.h'] = digest(init_header)
        import json
        (self.output / '.mxpio/manifest.json').write_text(json.dumps(old))
        incoming = self.next_plan(old)
        self.assertIn('include/mxpio/mx_handles.h', incoming.add)
        self.assertIn('include/mxpio/mx_init.h', incoming.update)
        apply(self.output, incoming, old)
        self.assertEqual(check(self.output), [])
        rollback(self.output)
        self.assertFalse(header.exists())
        self.assertNotIn('mx_handles.h', init_header.read_text())

    def test_crlf_ini_stays_unchanged_when_handles_are_added(self):
        old = self.init()
        ini = self.output / 'platformio.ini'
        windows_ini = ini.read_text().replace('\n', '\r\n').encode()
        ini.write_bytes(windows_ini)
        (self.src / 'main.c').write_text(
            (self.src / 'main.c').read_text().replace('htim2;', 'htim2;\nTIM_HandleTypeDef htim8;'))
        incoming = self.next_plan(old)
        self.assertEqual(incoming.conflicts, [])
        self.assertIn('platformio.ini', incoming.unchanged)
        apply(self.output, incoming, old)
        self.assertEqual(ini.read_bytes(), windows_ini)
        self.assertEqual(check(self.output), [])

    def test_manually_edited_generated_handle_header_blocks_sync(self):
        old = self.init()
        header = self.output / 'include/mxpio/mx_handles.h'
        header.write_text(header.read_text() + '// manual\n')
        incoming = self.next_plan(old)
        self.assertTrue(any('mx_handles.h' in item for item in incoming.conflicts))
        with self.assertRaises(MXPIOError):
            apply(self.output, incoming, old)

    @unittest.skipUnless(shutil.which('gcc') and shutil.which('g++'), 'C/C++ compiler required')
    def test_generated_header_compiles_as_c_and_links_from_cpp(self):
        (self.src / 'i2c.c').write_text(
            '#include "main.h"\nI2C_HandleTypeDef hi2c2;\n')
        self.init()
        cpp = self.output / 'src/main.cpp'
        cpp.write_text(
            '#include "mxpio/mx_init.h"\n#include "mxpio/mx_init.h"\n'
            'int main() { MX_Init(); htim2.value = 7; hi2c2.value = 9; '
            'return htim2.value + hi2c2.value == 16 ? 0 : 1; }\n')
        c_probe = self.root / 'probe.c'
        c_probe.write_text('#include "mxpio/mx_init.h"\nvoid *get_timer(void) { return &htim2; }\n')
        includes = ['-I' + str(self.output / 'include'),
                    '-I' + str(self.output / 'include/mx_generated')]
        objects = []
        for index, source in enumerate([
                self.output / 'src/mx_generated/mx_boot.c',
                self.output / 'src/mx_generated/i2c.c', c_probe]):
            obj = self.root / f'{index}.o'
            result = subprocess.run([shutil.which('gcc'), '-std=c11', *includes,
                                     '-c', str(source), '-o', str(obj)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            objects.append(str(obj))
        executable = self.root / 'link-test.exe'
        result = subprocess.run([shutil.which('g++'), '-std=c++20', *includes,
                                 str(cpp), *objects, '-o', str(executable)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(subprocess.run([str(executable)]).returncode, 0)
