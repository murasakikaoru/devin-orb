"""Watch Windows Action Center for Devin toast notifications.

Uses WinRT UserNotificationListener (works from unpackaged desktop apps once
the user grants notification access). Devin Desktop posts toasts under the
AUMID "Exafunction.Windsurf". The NotificationChanged event requires package
identity, so we poll the action-center list and diff notification IDs instead.
"""

from __future__ import annotations

import threading
import time

POLL_SECS = 2.5
_DEVIN_AUMIDS = ("exafunction.windsurf", "devin")


def _match(aumid: str, name: str) -> bool:
    s = (aumid + " " + name).lower()
    return any(k in s for k in _DEVIN_AUMIDS)


def _extract_text(notif) -> str:
    """Pull the visible text out of a UserNotification's toast binding."""
    try:
        v = notif.notification.visual
        parts = []
        for i in range(v.bindings.size):
            b = v.bindings.get_at(i)
            els = b.get_text_elements()
            for j in range(els.size):
                parts.append(els.get_at(j).text)
        return "\n".join(p for p in parts if p)
    except Exception:
        return ""


def start_listener(on_notify, log):
    """Run the WinRT poll loop in a daemon thread.

    on_notify(title: str, body: str) is called from a worker thread.
    Silently returns if winsdk is unavailable or access is denied.
    """
    try:
        import asyncio
        from winsdk.windows.ui.notifications import NotificationKinds
        from winsdk.windows.ui.notifications.management import (
            UserNotificationListener)
    except Exception as e:
        log(f"notify: winsdk unavailable: {e!r}")
        return

    def run():
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
            asyncio.get_event_loop().run_until_complete(_main())
        except Exception as e:
            log(f"notify loop died: {e!r}")

    async def _main():
        listener = UserNotificationListener.current
        try:
            acc = await listener.request_access_async()
        except Exception as e:
            log(f"notify: access request failed: {e!r}")
            return
        if int(acc) != 1:  # UserNotificationListenerAccessStatus.ALLOWED
            log(f"notify: access denied ({acc})")
            return

        # snapshot existing toasts so old ones don't re-fire us
        try:
            cur = await listener.get_notifications_async(NotificationKinds.TOAST)
            known = {cur.get_at(i).id for i in range(cur.size)}
        except Exception:
            known = set()

        last = {"t": 0.0, "text": ""}
        log("notify: polling action center for Devin toasts")

        while True:
            await asyncio.sleep(POLL_SECS)
            try:
                cur = await listener.get_notifications_async(
                    NotificationKinds.TOAST)
                ids = set()
                for i in range(cur.size):
                    n = cur.get_at(i)
                    ids.add(n.id)
                    if n.id in known:
                        continue
                    aumid = n.app_info.app_user_model_id
                    name = n.app_info.display_info.display_name
                    if not _match(aumid, name):
                        continue
                    text = _extract_text(n)
                    if not text:
                        continue
                    # debounce identical rapid-fire toasts
                    now = time.time()
                    if text == last["text"] and now - last["t"] < 6:
                        continue
                    last["t"], last["text"] = now, text
                    # "Session name: message..." -> title + body
                    title = "Devin"
                    body = text.strip()
                    head, sep, rest = body.partition("：")
                    if not sep:
                        head, sep, rest = body.partition(": ")
                    if sep and len(head) <= 30:
                        title, body = head.strip(), rest.strip()
                    log(f"notify: [{name}] {body[:80]!r}")
                    on_notify(title, body)
                known = ids  # forget toasts removed from action center
            except Exception as e:
                log(f"notify poll err: {e!r}")

    threading.Thread(target=run, daemon=True, name="notif-listener").start()
