"""Windows Explorer file drops for Tk, using only the standard library.

``WindowsFileDrop(root, on_files)`` accepts actual filesystem files exposed by
the Shell as HDROP; virtual attachments (for example, an unsaved mail attachment)
are not supported. The caller decides which file types/count to open. Unicode
filenames are returned unchanged, without Tcl list parsing or brace stripping.

Create and close the bridge on the Tk window's thread, after its widgets have
been created. The Windows subclass procedure only reads/releases HDROP data and
queues it. A Tk timer invokes ``on_files(tuple_of_paths)`` outside the native
window procedure, so opening a PDF or a confirmation dialog cannot re-enter it.

API references:
https://learn.microsoft.com/windows/win32/api/commctrl/nf-commctrl-setwindowsubclass
https://learn.microsoft.com/windows/win32/api/shellapi/nf-shellapi-dragqueryfilew
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import queue
import sys
import threading
from tkinter import TclError

WM_DROPFILES = 0x0233
WM_NCDESTROY = 0x0082
GA_ROOT = 2
_ALL_FILES = 0xFFFFFFFF
_POLL_MS = 40

# Windows does not retain the Python callback. Keep it (and its owner) alive
# until it has been removed, even if the caller accidentally drops its bridge.
_active_subclasses: dict[tuple[int, int], "WindowsFileDrop"] = {}


class _WinAPI:
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("File drag and drop is supported on Windows only.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # WPARAM, LPARAM, LRESULT, UINT_PTR and DWORD_PTR are pointer-sized,
        # including on 64-bit Python. A default ctypes c_int would truncate them.
        self.subclass_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_size_t,
        )
        self.GetAncestor = self.user32.GetAncestor
        self.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self.GetAncestor.restype = wintypes.HWND
        self.IsWindow = self.user32.IsWindow
        self.IsWindow.argtypes = (wintypes.HWND,)
        self.IsWindow.restype = wintypes.BOOL
        self.GetWindowThreadProcessId = self.user32.GetWindowThreadProcessId
        self.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.GetCurrentThreadId = self.kernel32.GetCurrentThreadId
        self.GetCurrentThreadId.argtypes = ()
        self.GetCurrentThreadId.restype = wintypes.DWORD
        self.SetWindowSubclass = self.comctl32.SetWindowSubclass
        self.SetWindowSubclass.argtypes = (
            wintypes.HWND, self.subclass_proc_type, ctypes.c_size_t, ctypes.c_size_t,
        )
        self.SetWindowSubclass.restype = wintypes.BOOL
        self.RemoveWindowSubclass = self.comctl32.RemoveWindowSubclass
        self.RemoveWindowSubclass.argtypes = (
            wintypes.HWND, self.subclass_proc_type, ctypes.c_size_t,
        )
        self.RemoveWindowSubclass.restype = wintypes.BOOL
        self.DefSubclassProc = self.comctl32.DefSubclassProc
        self.DefSubclassProc.argtypes = (
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        )
        self.DefSubclassProc.restype = ctypes.c_ssize_t
        self.DragAcceptFiles = self.shell32.DragAcceptFiles
        self.DragAcceptFiles.argtypes = (wintypes.HWND, wintypes.BOOL)
        self.DragAcceptFiles.restype = None
        self.DragQueryFileW = self.shell32.DragQueryFileW
        self.DragQueryFileW.argtypes = (
            wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT,
        )
        self.DragQueryFileW.restype = wintypes.UINT
        self.DragFinish = self.shell32.DragFinish
        self.DragFinish.argtypes = (wintypes.HANDLE,)
        self.DragFinish.restype = None


def _load_api():
    return _WinAPI()


def _native_error(message):
    code = ctypes.get_last_error() if sys.platform == "win32" else 0
    return OSError(code or 1, message)


class WindowsFileDrop:
    """Attach native file dropping to one Tk root; call ``close`` before exit."""

    def __init__(self, root, on_files):
        if not callable(on_files):
            raise TypeError("on_files must be callable")
        self.root = root
        self.on_files = on_files
        self._owner_thread = threading.get_ident()
        self._api = _load_api()
        self._queue = queue.SimpleQueue()
        self._after_id = self._destroy_binding = None
        self._installed = False
        self._closed = False
        self._subclass_id = id(self)
        # winfo_id is Tk's inner client window. Windows Shell drops belong on
        # its native top-level wrapper (the ancestor, not the desktop owner).
        self._hwnd = int(self._api.GetAncestor(int(root.winfo_id()), GA_ROOT) or 0)
        if not self._hwnd or not self._api.IsWindow(self._hwnd):
            raise _native_error("The PDF window is not ready to accept files.")
        if self._api.GetWindowThreadProcessId(self._hwnd, None) != self._api.GetCurrentThreadId():
            raise RuntimeError("File dropping must be installed on the Tk window's thread.")
        self._proc = self._api.subclass_proc_type(self._window_proc)
        if not self._api.SetWindowSubclass(self._hwnd, self._proc, self._subclass_id, 0):
            raise _native_error("Unable to install the file-drop handler.")
        self._installed = True
        _active_subclasses[(self._hwnd, self._subclass_id)] = self
        try:
            self._api.DragAcceptFiles(self._hwnd, True)
            self._destroy_binding = root.bind("<Destroy>", self._on_destroy, add="+")
            self._after_id = root.after(_POLL_MS, self._dispatch)
        except BaseException:
            self.close()
            raise

    @property
    def enabled(self):
        return self._installed and not self._closed

    def _read_files(self, hdrop):
        """Copy all Unicode names before HDROP is released, with no Tk calls."""
        count = self._api.DragQueryFileW(hdrop, _ALL_FILES, None, 0)
        paths = []
        for index in range(count):
            length = self._api.DragQueryFileW(hdrop, index, None, 0)
            if not length:
                raise _native_error("Unable to read a dropped filename.")
            buffer = ctypes.create_unicode_buffer(length + 1)
            copied = self._api.DragQueryFileW(hdrop, index, buffer, len(buffer))
            if copied != length:
                raise _native_error("A dropped filename was incomplete.")
            paths.append(buffer.value)
        return tuple(paths)

    def _window_proc(self, hwnd, message, wparam, lparam, subclass_id, ref_data):
        if message == WM_DROPFILES:
            try:
                if not self._closed:
                    paths = self._read_files(wparam)
                    if paths:
                        self._queue.put(("files", paths))
            except BaseException as exc:
                # Never let an exception cross the ctypes callback boundary.
                self._queue.put(("error", exc))
            finally:
                self._api.DragFinish(wparam)
            return 0
        if message == WM_NCDESTROY:
            # This fallback also covers destruction not initiated by Editor.
            # It deliberately does not call Tcl from inside the native callback.
            self._closed = True
            self._empty_queue()
            self._detach(hwnd, disable_drop=False)
        return self._api.DefSubclassProc(hwnd, message, wparam, lparam)

    def _dispatch(self):
        self._after_id = None
        if self._closed:
            return
        try:
            # Bound each timer's work so a burst of drops cannot starve Tk.
            for _ in range(20):
                if self._closed:
                    break
                try:
                    kind, value = self._queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "error":
                    raise value
                self.on_files(value)
        finally:
            if not self._closed:
                self._after_id = self.root.after(_POLL_MS, self._dispatch)

    def _empty_queue(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _detach(self, hwnd, *, disable_drop=True):
        if not self._installed:
            return
        if disable_drop:
            self._api.DragAcceptFiles(hwnd, False)
        removed = self._api.RemoveWindowSubclass(hwnd, self._proc, self._subclass_id)
        if removed or not self._api.IsWindow(hwnd):
            self._installed = False
            _active_subclasses.pop((self._hwnd, self._subclass_id), None)
        # If Windows refuses removal while the window is still live, retain
        # the disabled callback until WM_NCDESTROY. Freeing it now could crash
        # the process the next time Windows dispatches a message to the window.

    def _on_destroy(self, event):
        if event.widget is self.root:
            self.close()

    def close(self):
        """Stop dispatching, release HDROP support, and safely remove the hook."""
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("File dropping must be closed on the Tk window's thread.")
        self._closed = True
        self._empty_queue()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except (TclError, RuntimeError):
                pass
            self._after_id = None
        if self._destroy_binding is not None:
            try:
                self.root.unbind("<Destroy>", self._destroy_binding)
            except (TclError, RuntimeError):
                pass
            self._destroy_binding = None
        self._detach(self._hwnd)
