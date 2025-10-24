"""Windows automation helpers for Merlis PM workflow."""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterable

import win32api
import win32clipboard
import win32con
import win32gui


def bring_window_to_front(hwnd: int | None) -> None:
    """Show and focus the given window handle."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)


def move_cursor(x: int, y: int, settle: float = 0.02) -> None:
    """Move the cursor to the absolute screen coordinate."""
    win32api.SetCursorPos((int(x), int(y)))
    if settle > 0:
        time.sleep(settle)


def click_left(x: int, y: int, hold: float = 0.03) -> None:
    """Perform a left mouse click at the given screen position."""
    move_cursor(x, y)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(max(hold, 0.01))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


@contextmanager
def _clipboard_guard() -> Iterable[None]:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        yield
    finally:
        win32clipboard.CloseClipboard()


def paste_text_via_clipboard(text: str) -> None:
    """Copy text to the clipboard and paste with CTRL+V."""
    if not text:
        return
    with _clipboard_guard():
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    key_down(win32con.VK_CONTROL)
    key_press(win32con.VK_V)
    key_up(win32con.VK_CONTROL)


def key_down(vk: int) -> None:
    win32api.keybd_event(vk, 0, 0, 0)


def key_up(vk: int) -> None:
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def key_press(vk: int, hold: float = 0.03) -> None:
    key_down(vk)
    time.sleep(max(hold, 0.01))
    key_up(vk)


def press_enter(hold: float = 0.03) -> None:
    """Send an ENTER key press with optional hold duration."""
    key_press(win32con.VK_RETURN, hold)


def send_space(delay: float = 0.0, hold: float = 0.5) -> None:
    """Press and release the space key after an optional delay."""
    if delay > 0:
        time.sleep(delay)
    key_press(win32con.VK_SPACE, hold=max(hold, 0.01))
