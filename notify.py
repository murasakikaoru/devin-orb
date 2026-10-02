"""Intercept Devin Windows toast notifications for the orb.

Fully event-driven, zero polling: a Win32 SetWinEventHook watches
EVENT_OBJECT_NAMECHANGE on the toast flyout window (class
"Windows.UI.Core.CoreWindow", owned by ShellExperienceHost.exe — the shell
renames it "New notification" for every toast it shows). On each hit the
flyout's own UI Automation tree yields the toast text: first text element is
the app name, the rest are title/body. Only Devin toasts are forwarded.

(The WinRT UserNotificationListener event API needs package identity, and
reading toast text through winsdk's NotificationBinding crashes on some
bindings — UIA on the flyout avoids both problems.)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time

EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
SUPPRESS_SECS = 15  # keep re-hiding a suppressed flyout until the toast dies


def _window_proc_name(hwnd) -> str:
    u32 = ctypes.windll.user32
    pid = wt.DWORD(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(260)
    n = wt.DWORD(260)
    ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
        h, 0, buf, ctypes.byref(n))
    ctypes.windll.kernel32.CloseHandle(h)
    return buf.value.rsplit(chr(92), 1)[-1] if ok else ""


def _is_toast_flyout(hwnd) -> bool:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    if buf.value != "Windows.UI.Core.CoreWindow":
        return False
    return _window_proc_name(hwnd).lower() == "shellexperiencehost.exe"


def _flyout_texts(hwnd) -> list[str]:
    """Read every text element in the toast flyout's UIA tree."""
    import uiautomation as auto
    texts = []

    def walk(c, depth=0):
        if depth > 12:
            return
        try:
            if c.ControlTypeName == "TextControl" and c.Name:
                texts.append(c.Name)
        except Exception:
            pass
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        for ch in kids:
            walk(ch, depth + 1)

    walk(auto.ControlFromHandle(hwnd))
    # strip private-use icon glyphs (dismiss/more buttons)
    return [t for t in texts
            if not all(0xE000 <= ord(ch) <= 0xF8FF for ch in t)]


def start_listener(on_notify, log):
    """Run the toast watcher in a daemon thread.

    on_notify(title: str, body: str) is called from that worker thread.
    Silently returns if uiautomation is unavailable.
    """
    try:
        import uiautomation as auto  # noqa: F401  (checked early)
    except Exception as e:
        log(f"notify: uiautomation unavailable: {e!r}")
        return

    u32 = ctypes.windll.user32
    WINEVENTPROC = ctypes.WINFUNCTYPE(
        None, wt.HANDLE, wt.DWORD, wt.HWND, ctypes.c_long, ctypes.c_long,
        wt.DWORD, wt.DWORD)

    flyouts: queue.Queue = queue.Queue()
    suppressed = {}  # hwnd -> deadline; shell re-shows the flyout for the
                     # dismiss animation, so we keep hiding it until it dies

    def _hook_cb(hook, event, hwnd, id_obj, id_child, tid, ms):
        if not hwnd:
            return
        if event == EVENT_OBJECT_NAMECHANGE and _is_toast_flyout(hwnd):
            u32.ShowWindow(hwnd, 0)  # SW_HIDE — suppress Windows' own banner
            suppressed.pop(hwnd, None)  # same hwnd is reused per toast
            flyouts.put(hwnd)
        elif event == EVENT_OBJECT_SHOW and hwnd in suppressed:
            if time.time() < suppressed[hwnd]:
                u32.ShowWindow(hwnd, 0)
            else:
                suppressed.pop(hwnd, None)

    hook_proc = WINEVENTPROC(_hook_cb)  # keep a ref alive

    def run():
        try:
            with auto.UIAutomationInitializerInThread():
                hook = u32.SetWinEventHook(
                    EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE,
                    0, hook_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
                hook2 = u32.SetWinEventHook(
                    EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW,
                    0, hook_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
                if not hook:
                    log("notify: SetWinEventHook failed")
                    return
                log("notify: watching toast flyout for Devin notifications")

                last = {"t": 0.0, "text": ""}
                msg = wt.MSG()
                # NOTE: out-of-context WinEvent callbacks are only delivered
                # while the thread pumps PeekMessage — GetMessage never wakes
                # for them. This is just a cheap message dispatcher; the only
                # work happens when a toast actually appears.
                while True:
                    while u32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                        u32.TranslateMessage(ctypes.byref(msg))
                        u32.DispatchMessageW(ctypes.byref(msg))
                    for h in [h for h, d in suppressed.items()
                              if time.time() > d]:
                        del suppressed[h]
                    try:
                        hwnd = flyouts.get_nowait()
                    except queue.Empty:
                        hwnd = 0
                    if hwnd:
                        # let the flyout's UIA tree populate
                        time.sleep(0.7)
                        try:
                            texts = _flyout_texts(hwnd)
                        except Exception as e:
                            log(f"notify: flyout read err {e!r}")
                            texts = []
                        is_devin = bool(
                            texts and texts[0].lower() == "devin")
                        if is_devin:
                            suppressed[hwnd] = time.time() + SUPPRESS_SECS
                            body = " — ".join(texts[1:]).strip()
                            if body:
                                now = time.time()
                                if body != last["text"] or \
                                        now - last["t"] > 6:
                                    last["t"], last["text"] = now, body
                                    log(f"notify: {body[:80]!r}")
                                    title = "Devin"
                                    head, sep, rest = body.partition(" — ")
                                    if sep and len(head) <= 40:
                                        title, body = head.strip(), \
                                            rest.strip()
                                    on_notify(title, body)
                        else:
                            # not Devin's toast (or unreadable) — restore the
                            # banner we suppressed so other apps still work
                            u32.ShowWindow(hwnd, 5)  # SW_SHOW
                    time.sleep(0.1)
        except Exception as e:
            log(f"notify loop died: {e!r}")

    threading.Thread(target=run, daemon=True, name="notif-listener").start()
