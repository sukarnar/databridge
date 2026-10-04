"""Dialog plumbing: closing the dialog must not close a snackbar instead, and errors show inside the dialog."""

import flet as ft

from databridge.ui import common


class FakePage:
    """Just enough of ft.Page: a stack of shown dialogs, like Flet's own."""

    def __init__(self):
        self.shown = []

    def show_dialog(self, d):
        d.open = True
        self.shown.append(d)

    def pop_dialog(self):
        top = next((d for d in reversed(self.shown) if d.open), None)
        if top:
            top.open = False
        return top


def _no_update(monkeypatch):
    monkeypatch.setattr(ft.AlertDialog, "update", lambda self: None)


def test_close_dialog_closes_the_dialog_not_a_snackbar_on_top(monkeypatch):
    _no_update(monkeypatch)
    page = FakePage()
    dlg = common.dialog(page, "Add as source", ft.Text("x"), [ft.TextButton("Add")])
    common.toast(page, "A source with that name already exists", error=True)  # snackbar now on top
    snack = page.shown[-1]
    common.close_dialog(page)
    assert dlg.open is False and snack.open is True  # Flet's pop_dialog() would have closed the snackbar


def test_errors_from_dialog_buttons_show_inside_the_dialog(monkeypatch):
    _no_update(monkeypatch)
    page = FakePage()
    dlg = common.dialog(page, "Edit", ft.Text("x"), [ft.TextButton("Save")])

    def save(_):
        raise ValueError("Give the connection a name")

    common.guarded(page, save)(None)
    assert dlg._db_status.view.visible and dlg._db_status.text.value == "Give the connection a name"
    assert len(page.shown) == 1  # no snackbar hidden behind the dialog
    common.close_dialog(page)
    common.guarded(page, save)(None)  # no dialog open: a snackbar as before
    assert isinstance(page.shown[-1], ft.SnackBar)


def test_friendly_errors():
    assert common.friendly_error(ValueError("Keep at least one column")) == "Keep at least one column"
    assert "KeyError" in common.friendly_error(KeyError("x"))  # unexpected: type kept, pointer to the log


def test_background_handlers_run_off_the_event_loop_and_ignore_repeat_clicks():
    import asyncio
    import threading

    from databridge.ui.common import background

    class Page:
        updates = 0

        def update(self):
            Page.updates += 1

    seen, gate = [], threading.Event()

    def slow(_e):
        seen.append(threading.current_thread() is threading.main_thread())
        gate.wait(2)

    handler = background(Page(), slow)
    assert asyncio.iscoroutinefunction(handler)  # Flet awaits it instead of running it on the loop

    async def clicks():
        first = asyncio.create_task(handler(None))
        await asyncio.sleep(0.05)
        await handler(None)  # a second click while the first runs: ignored, returns at once
        gate.set()
        await first

    asyncio.run(clicks())
    assert seen == [False] and Page.updates == 1
