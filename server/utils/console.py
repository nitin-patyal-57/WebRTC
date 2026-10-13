"""Windows console fix: disable QuickEdit mode.

With QuickEdit on, a single mouse click in the console window puts it in
"select" mode, which blocks every write to stdout/stderr. The first log line
then freezes the asyncio event loop and the whole server hangs until someone
presses a key (Ctrl+C / Enter / Esc) in the window.
"""

import sys

ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
STD_INPUT_HANDLE = -10


def disable_quick_edit() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return  # no console attached (service, redirected stdin, ...)
        new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass
