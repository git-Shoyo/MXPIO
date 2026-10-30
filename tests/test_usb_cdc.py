import unittest
from pathlib import Path
from test_mxpio import WorkFlowTests
from mxpio.core import MXPIOError, plan, apply, read_manifest


class UsbCdcTests(WorkFlowTests):
    def add_usb(self):
        ioc = self.source / 'cube.ioc'
        ioc.write_text(ioc.read_text() + 'USB_DEVICE.VirtualModeFS=Cdc_FS\n')
        for name in ('App/usb_device.c', 'App/usb_device.h', 'App/usbd_cdc_if.c',
                     'App/usbd_cdc_if.h', 'App/usbd_desc.c', 'App/usbd_desc.h',
                     'Target/usbd_conf.c', 'Target/usbd_conf.h'):
            p = self.source / 'USB_DEVICE' / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('// ' + name)
        (self.source / 'Middlewares/ST/STM32_USB_Device_Library').mkdir(parents=True)

    def test_usb_sync_tracks_generated_files_and_preserves_application(self):
        old = self._init()
        app = self.output / 'src/main.cpp'
        app.write_text('// application stays here')
        self.add_usb()
        update = plan(self.source, self.output, old, None, 'mxpio')
        self.assertFalse(update.conflicts)
        self.assertIn('src/mx_generated/usbd_cdc_if.c', update.add)
        apply(self.output, update, old)
        self.assertEqual(app.read_text(), '// application stays here')
        old = read_manifest(self.output)
        (self.source / 'USB_DEVICE/App/usbd_cdc_if.c').write_text('// new USB callback')
        update = plan(self.source, self.output, old, None, 'mxpio')
        self.assertIn('src/mx_generated/usbd_cdc_if.c', update.update)
        (self.output / 'src/mx_generated/usbd_cdc_if.c').write_text('// local edit')
        update = plan(self.source, self.output, old, None, 'mxpio')
        self.assertTrue(update.conflicts)

    def test_missing_cdc_file_still_rejected(self):
        self.add_usb()
        (self.source / 'USB_DEVICE/App/usbd_cdc_if.c').unlink()
        with self.assertRaisesRegex(MXPIOError, 'USB CDC'):
            plan(self.source, self.output, None, None, 'mxpio')

    def test_unrelated_middleware_still_rejected(self):
        self.add_usb()
        (self.source / 'Middlewares/Third_Party/FreeRTOS').mkdir(parents=True)
        with self.assertRaisesRegex(MXPIOError, 'Middleware'):
            plan(self.source, self.output, None, None, 'mxpio')

    def test_other_usb_class_still_rejected(self):
        (self.source / 'USB_DEVICE').mkdir()
        with self.assertRaisesRegex(MXPIOError, 'USB'):
            plan(self.source, self.output, None, None, 'mxpio')
