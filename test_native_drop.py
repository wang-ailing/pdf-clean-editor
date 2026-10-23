"""File-drop regression tests with no desktop input, windows or clipboard use."""
from __future__ import annotations

import ctypes
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import native_drop


def utf16_length(value):
    return len(value.encode("utf-16-le")) // 2


class FakeRoot:
    def __init__(self):
        self.calls = []
        self.timers = {}
        self.bindings = {}
        self.serial = 0

    def winfo_id(self):
        return 0x1_01020304

    def bind(self, event, callback, add=None):
        self.calls.append(("bind", event, add))
        self.bindings["destroy-1"] = callback
        return "destroy-1"

    def unbind(self, event, token):
        self.calls.append(("unbind", event, token))
        self.bindings.pop(token, None)

    def after(self, delay, callback):
        self.calls.append(("after", delay))
        self.serial += 1
        token = f"timer-{self.serial}"
        self.timers[token] = callback
        return token

    def after_cancel(self, token):
        self.calls.append(("after_cancel", token))
        self.timers.pop(token, None)

    def tick(self):
        token = next(iter(self.timers))
        callback = self.timers.pop(token)
        callback()


class FakeAPI:
    def __init__(self):
        self.hwnd = 0x2_05060708
        self.files = [r"C:\论文 {研究}\正文 译文.pdf"]
        self.calls = []
        self.fail_query = self.fail_install = self.fail_remove = False
        self.window_thread = 44
        self.subclass_proc_type = lambda callback: callback

    def GetAncestor(self, hwnd, mode):
        self.calls.append(("ancestor", hwnd, mode))
        return self.hwnd

    def IsWindow(self, hwnd):
        return hwnd == self.hwnd

    def GetWindowThreadProcessId(self, hwnd, process_id):
        return self.window_thread

    def GetCurrentThreadId(self):
        return 44

    def SetWindowSubclass(self, hwnd, proc, subclass_id, reference):
        self.calls.append(("install", hwnd, subclass_id, reference))
        self.proc, self.subclass_id = proc, subclass_id
        return not self.fail_install

    def RemoveWindowSubclass(self, hwnd, proc, subclass_id):
        self.calls.append(("remove", hwnd, subclass_id))
        if proc is not self.proc:
            raise AssertionError("Must remove the exact installed callback")
        return not self.fail_remove

    def DefSubclassProc(self, hwnd, message, wparam, lparam):
        self.calls.append(("forward", hwnd, message, wparam, lparam))
        return 0x3_090A0B0C

    def DragAcceptFiles(self, hwnd, enabled):
        self.calls.append(("accept", hwnd, enabled))

    def DragQueryFileW(self, hdrop, index, buffer, capacity):
        self.calls.append(("query", hdrop, index, capacity))
        if index == native_drop._ALL_FILES:
            return len(self.files)
        if self.fail_query:
            raise OSError("Synthetic query failure")
        value = self.files[index]
        length = utf16_length(value)
        if buffer is not None:
            if capacity != length + 1:
                raise AssertionError("Unicode buffer must include its terminator")
            buffer.value = value
        return length

    def DragFinish(self, hdrop):
        self.calls.append(("finish", hdrop))

    def send(self, message=native_drop.WM_DROPFILES, wparam=0x4_11223344, lparam=0):
        return self.proc(self.hwnd, message, wparam, lparam, self.subclass_id, 0)


class NativeDropTests(unittest.TestCase):
    def setUp(self):
        self.root, self.api, self.delivered = FakeRoot(), FakeAPI(), []
        self.loader = patch.object(native_drop, "_load_api", return_value=self.api)
        self.loader.start()
        self.bridge = None

    def tearDown(self):
        if self.bridge:
            self.api.fail_remove = False
            self.bridge.close()
        self.loader.stop()
        self.assertFalse(native_drop._active_subclasses)

    def create_bridge(self, callback=None):
        self.bridge = native_drop.WindowsFileDrop(self.root, callback or self.delivered.append)
        return self.bridge

    def test_unicode_multiple_names_are_queued_without_tcl_parsing(self):
        bridge = self.create_bridge()
        self.api.files = [
            r"C:\论文 {研究}\正文 译文.pdf",
            "C:\\资料\\{literal} emoji \U0001f4d6.PDF",
            "C:\\" + "长路径资料\\" * 80 + "最终稿.pdf",
        ]
        before_tk = list(self.root.calls)
        self.assertEqual(self.api.send(), 0)
        self.assertEqual(self.root.calls, before_tk, "Native callback must not call Tk")
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.api.calls[-1], ("finish", 0x4_11223344))
        self.root.tick()
        self.assertEqual(self.delivered, [tuple(self.api.files)])
        self.assertTrue(bridge.enabled)
        self.assertIn(("ancestor", 0x1_01020304, native_drop.GA_ROOT), self.api.calls)
        self.assertIn(("accept", self.api.hwnd, True), self.api.calls)

    def test_query_failure_still_releases_hdrop_then_reports_on_tk_timer(self):
        self.create_bridge()
        self.api.fail_query = True
        self.assertEqual(self.api.send(), 0)
        self.assertEqual(self.api.calls[-1], ("finish", 0x4_11223344))
        with self.assertRaisesRegex(OSError, "Synthetic query failure"):
            self.root.tick()
        self.assertEqual(self.delivered, [])
        self.assertEqual(len(self.root.timers), 1)

    def test_empty_drop_is_released_and_not_dispatched(self):
        self.create_bridge()
        self.api.files = []
        self.api.send()
        self.root.tick()
        self.assertEqual(self.delivered, [])
        self.assertEqual([call for call in self.api.calls if call[0] == "finish"],
                         [("finish", 0x4_11223344)])

    def test_close_cancels_queued_work_and_is_idempotent(self):
        bridge = self.create_bridge()
        self.api.send()
        # Simulate a timer Tk had already fetched before close was called.
        stale_timer = next(iter(self.root.timers.values()))
        bridge.close()
        bridge.close()
        stale_timer()
        self.assertFalse(bridge.enabled)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.root.timers, {})
        self.assertEqual(self.root.bindings, {})
        self.assertEqual(len([call for call in self.api.calls if call[0] == "remove"]), 1)
        self.assertIn(("accept", self.api.hwnd, False), self.api.calls)
        self.api.send()
        self.assertEqual(self.api.calls[-1], ("finish", 0x4_11223344))

    def test_unhandled_messages_preserve_native_chain_and_large_return_value(self):
        self.create_bridge()
        self.assertEqual(self.api.send(message=0x1234, wparam=0x5_00000001, lparam=-7),
                         0x3_090A0B0C)
        self.assertEqual(self.api.calls[-1],
                         ("forward", self.api.hwnd, 0x1234, 0x5_00000001, -7))

    def test_native_destroy_removes_hook_without_calling_tk_and_discards_queue(self):
        bridge = self.create_bridge()
        self.api.send()
        before_tk = list(self.root.calls)
        self.assertEqual(self.api.send(message=native_drop.WM_NCDESTROY), 0x3_090A0B0C)
        self.assertEqual(before_tk, self.root.calls)
        self.assertFalse(bridge.enabled)
        self.assertFalse(native_drop._active_subclasses)
        self.root.tick()
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.root.timers, {})

    def test_failed_removal_keeps_callback_pinned_until_native_destroy(self):
        bridge = self.create_bridge()
        self.api.fail_remove = True
        bridge.close()
        self.assertFalse(bridge.enabled)
        self.assertIs(native_drop._active_subclasses[(self.api.hwnd, id(bridge))], bridge)
        self.api.fail_remove = False
        self.api.send(message=native_drop.WM_NCDESTROY)
        self.assertFalse(native_drop._active_subclasses)

    def test_child_destroy_does_not_unregister_root_drop(self):
        bridge = self.create_bridge()
        destroy = self.root.bindings["destroy-1"]
        destroy(SimpleNamespace(widget=object()))
        self.assertTrue(bridge.enabled)
        destroy(SimpleNamespace(widget=self.root))
        self.assertFalse(bridge.enabled)

    def test_callback_runs_on_polling_thread_and_can_close_bridge(self):
        thread_ids = []

        def callback(paths):
            thread_ids.append(threading.get_ident())
            self.bridge.close()

        self.create_bridge(callback)
        self.api.send()
        self.assertEqual(thread_ids, [])
        self.root.tick()
        self.assertEqual(thread_ids, [threading.get_ident()])
        self.assertEqual(self.root.timers, {})

    def test_failed_install_does_not_pin_callback_or_accept_drops(self):
        self.api.fail_install = True
        with self.assertRaises(OSError):
            self.create_bridge()
        self.assertFalse(native_drop._active_subclasses)
        self.assertFalse(any(call[0] == "accept" for call in self.api.calls))
        self.assertEqual(self.root.calls, [])

    def test_cross_thread_install_is_rejected_before_subclassing(self):
        self.api.window_thread = 99
        with self.assertRaisesRegex(RuntimeError, "thread"):
            self.create_bridge()
        self.assertFalse(any(call[0] == "install" for call in self.api.calls))

    @unittest.skipUnless(sys.platform == "win32", "Windows ABI check")
    def test_native_abi_uses_pointer_sized_callback_and_handles(self):
        api = native_drop._WinAPI()
        self.assertEqual(ctypes.sizeof(api.subclass_proc_type._restype_), ctypes.sizeof(ctypes.c_void_p))
        for index in (2, 3, 4, 5):
            self.assertEqual(ctypes.sizeof(api.subclass_proc_type._argtypes_[index]),
                             ctypes.sizeof(ctypes.c_void_p))
        self.assertIs(api.DragQueryFileW.argtypes[0], ctypes.wintypes.HANDLE)
        self.assertIs(api.GetAncestor.restype, ctypes.wintypes.HWND)

    @unittest.skipUnless(sys.platform == "win32", "Windows synthetic message integration")
    def test_hidden_own_window_receives_real_unicode_hdrop_without_desktop_input(self):
        import tkinter as tk
        from ctypes import wintypes

        # Only this newly created, withdrawn window receives the message.
        # No desktop enumeration, mouse/keyboard events, or clipboard access.
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        received = []
        expected = (r"C:\资料 {研究}\论文 译文.pdf", "C:\\资料\\图示 \U0001f4d6.PDF")
        bridge = None
        hdrop = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        user32.SendMessageW.argtypes = (
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        )
        user32.SendMessageW.restype = ctypes.c_ssize_t

        class DROPFILES(ctypes.Structure):
            _fields_ = [("pFiles", wintypes.DWORD), ("pt", wintypes.POINT),
                        ("fNC", wintypes.BOOL), ("fWide", wintypes.BOOL)]

        try:
            with patch.object(native_drop, "_load_api", return_value=native_drop._WinAPI()):
                bridge = native_drop.WindowsFileDrop(root, received.append)
            self.assertTrue(bridge.enabled)
            header = DROPFILES()
            header.pFiles = ctypes.sizeof(DROPFILES)
            header.fWide = True
            payload = bytes(header) + ("\0".join(expected) + "\0\0").encode("utf-16-le")
            hdrop = kernel32.GlobalAlloc(0x42, len(payload))
            self.assertTrue(hdrop)
            address = kernel32.GlobalLock(hdrop)
            self.assertTrue(address)
            ctypes.memmove(address, payload, len(payload))
            kernel32.GlobalUnlock(hdrop)
            result = user32.SendMessageW(bridge._hwnd, native_drop.WM_DROPFILES, hdrop, 0)
            hdrop = None  # The bridge has released it using DragFinish.
            self.assertEqual(result, 0)
            self.assertEqual(received, [], "Native callback must only enqueue")
            root.after(100, root.quit)
            root.mainloop()
            self.assertEqual(received, [expected])
            bridge.close()
            bridge.close()
            self.assertFalse(bridge.enabled)
            self.assertFalse(native_drop._active_subclasses)
        finally:
            if hdrop:
                kernel32.GlobalFree(hdrop)
            if bridge:
                bridge.close()
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
