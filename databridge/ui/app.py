"""Flet studio shell: sign-in gate, role-aware navigation rail and swappable views."""

import asyncio
import logging

import flet as ft

from databridge.core.auth import ROLES, can
from databridge.services import users as user_svc
from databridge.ui.common import guarded, toast

log = logging.getLogger(__name__)
PREF_KEY = "databridge.session"


class App:
    # key, label, icon, selected icon, permission needed to see it
    NAV = [
        ("dashboard", "Home", ft.Icons.DASHBOARD_OUTLINED, ft.Icons.DASHBOARD, "view"),
        ("connections", "Connections", ft.Icons.CABLE_OUTLINED, ft.Icons.CABLE, "use_connections"),
        ("explorer", "Explorer", ft.Icons.ACCOUNT_TREE_OUTLINED, ft.Icons.ACCOUNT_TREE, "use_connections"),
        ("sources", "Sources", ft.Icons.TABLE_CHART_OUTLINED, ft.Icons.TABLE_CHART, "view"),
        ("targets", "Targets", ft.Icons.SCHEMA_OUTLINED, ft.Icons.SCHEMA, "view"),
        ("mappings", "Mappings", ft.Icons.COMPARE_ARROWS_OUTLINED, ft.Icons.COMPARE_ARROWS, "view"),
        ("endpoints", "Endpoints", ft.Icons.API_OUTLINED, ft.Icons.API, "view"),
        ("ai", "AI", ft.Icons.AUTO_AWESOME_OUTLINED, ft.Icons.AUTO_AWESOME, "use_ai"),
        ("keys", "API keys", ft.Icons.KEY_OUTLINED, ft.Icons.KEY, "manage_keys"),
        ("runs", "Runs", ft.Icons.HISTORY_OUTLINED, ft.Icons.HISTORY, "view"),
        ("users", "Users", ft.Icons.MANAGE_ACCOUNTS_OUTLINED, ft.Icons.MANAGE_ACCOUNTS, "manage_users"),
        ("docs", "Documentation", ft.Icons.MENU_BOOK_OUTLINED, ft.Icons.MENU_BOOK, "view"),
    ]
    VIEW_PERMISSION = {k: p for k, *_, p in NAV} | {"studio": "view", "workflow": "use_ai"}

    def __init__(self, page: ft.Page, user, token: str):
        self.page = page
        self.user = user
        self.token = token
        self.nav = [n for n in self.NAV if can(user.role, n[4])]
        self.content = ft.Container(expand=True, padding=ft.Padding.symmetric(horizontal=28, vertical=20))
        self.rail = ft.NavigationRail(
            selected_index=0,
            label_type=ft.NavigationRailLabelType.ALL,
            min_width=88,
            group_alignment=-0.95,
            scrollable=True,
            leading=ft.Container(
                ft.Column([
                    ft.Icon(ft.Icons.HUB, color=ft.Colors.PRIMARY, size=30),
                    ft.Text("DataBridge", size=11, weight=ft.FontWeight.W_700),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
                padding=ft.Padding.only(top=12, bottom=8),
            ),
            trailing=self.user_menu(),
            destinations=[ft.NavigationRailDestination(icon=i, selected_icon=si, label=label)
                          for _, label, i, si, _ in self.nav],
            on_change=lambda e: self.navigate(self.nav[e.control.selected_index][0]),
        )

    # ------------------------------------------------------------ permissions and audit

    def can(self, permission: str) -> bool:
        return can(self.user.role, permission)

    def audit(self, action: str, target: str = "", detail: str = "") -> None:
        user_svc.audit(self.user.username, action, target, detail, self.page.client_ip or "")

    def require(self, permission: str) -> None:
        """Server-side guard for handlers: raises if the signed-in user lacks the permission."""
        if not self.can(permission):
            self.audit("security.denied", permission)
            raise PermissionError("Your role does not allow this action")

    # ------------------------------------------------------------ navigation

    def user_menu(self) -> ft.Control:
        initials = "".join(p[0] for p in (self.user.full_name or self.user.username).split()[:2]).upper()
        return ft.Container(
            ft.PopupMenuButton(
                content=ft.Column([
                    ft.CircleAvatar(content=ft.Text(initials or "?", size=13), radius=18),
                    ft.Text(self.user.username, size=10, no_wrap=True, max_lines=1, width=80,
                            text_align=ft.TextAlign.CENTER, overflow=ft.TextOverflow.ELLIPSIS),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2),
                tooltip="Account",
                items=[
                    ft.PopupMenuItem(content=ft.Text(f"{self.user.full_name or self.user.username} · {self.user.role}",
                                                     weight=ft.FontWeight.W_600), disabled=True),
                    ft.PopupMenuItem(content=ft.Text("Change password"), icon=ft.Icons.PASSWORD,
                                     on_click=lambda _: change_password_dialog(self)),
                    ft.PopupMenuItem(content=ft.Text("Sign out"), icon=ft.Icons.LOGOUT,
                                     on_click=lambda _: self.page.run_task(self.sign_out)),
                ],
            ),
            padding=ft.Padding.only(top=24),
        )

    def view_class(self, key: str):
        from databridge.ui.views import (ai, connections, dashboard, docs, endpoints, explorer, keys, mappings, runs,
                                         sources, studio, targets, users, workflows)

        return {
            "dashboard": dashboard.DashboardView,
            "connections": connections.ConnectionsView,
            "explorer": explorer.ExplorerView,
            "sources": sources.SourcesView,
            "targets": targets.TargetsView,
            "mappings": mappings.MappingsView,
            "endpoints": endpoints.EndpointsView,
            "keys": keys.KeysView,
            "runs": runs.RunsView,
            "users": users.UsersView,
            "studio": studio.MappingStudio,
            "ai": ai.AIView,
            "workflow": workflows.WorkflowEditor,
            "docs": docs.DocsView,
        }[key]

    def navigate(self, key: str, **kwargs) -> None:
        # Re-check the session on every screen change: catches expiry, idle timeout, disabled accounts,
        # role changes and "sign out everywhere".
        fresh = user_svc.validate_session(self.token)
        if not fresh:
            self.page.run_task(self.sign_out, "Your session has ended. Please sign in again.")
            return
        if fresh.role != self.user.role:
            self.user = fresh
            self.page.run_task(start_shell, self.page, fresh, self.token, key)
            return
        self.user = fresh
        if not self.can(self.VIEW_PERMISSION.get(key, "view")):
            self.audit("security.denied", key)
            toast(self.page, "Your role does not have access to that page", error=True)
            key = "dashboard"
        keys = [k for k, *_ in self.nav]
        rail_key = {"studio": "mappings", "workflow": "ai"}.get(key, key)
        if rail_key in keys:
            self.rail.selected_index = keys.index(rail_key)

        def build():
            view = self.view_class(key)(self, **kwargs)
            self.content.content = view.build()

        guarded(self.page, build)()
        if key != "docs":
            self.last_page = key  # Documentation opens at the topic for the page you came from
        self.page.update()

    async def sign_out(self, message: str = "") -> None:
        user_svc.sign_out(self.token, self.user.username, self.page.client_ip or "")
        await _forget_token(self.page)
        show_login(self.page, message)

    def build(self) -> ft.Control:
        return ft.Row([self.rail, ft.VerticalDivider(width=1), self.content], expand=True, spacing=0)


# ================================================================== sign-in flow


async def _forget_token(page: ft.Page) -> None:
    try:
        await ft.SharedPreferences().remove(PREF_KEY)
    except Exception:  # noqa: BLE001 - storage may be unavailable (private browsing)
        pass


def _center(card: ft.Control) -> ft.Control:
    return ft.Container(ft.Container(card, width=440), alignment=ft.Alignment.CENTER, expand=True,
                        bgcolor=ft.Colors.SURFACE_CONTAINER_LOW)


def _brand() -> ft.Control:
    return ft.Row([ft.Icon(ft.Icons.HUB, color=ft.Colors.PRIMARY, size=34),
                   ft.Text("DataBridge", size=24, weight=ft.FontWeight.W_700)],
                  alignment=ft.MainAxisAlignment.CENTER, spacing=8)


def show_login(page: ft.Page, message: str = "") -> None:
    page.controls.clear()
    no_users = user_svc.count_users() == 0
    username = ft.TextField(label="User name", autofocus=True, prefix_icon=ft.Icons.PERSON_OUTLINE,
                            autocorrect=False, enable_suggestions=False, width=340)
    password = ft.TextField(label="Password", password=True, can_reveal_password=True,
                            prefix_icon=ft.Icons.LOCK_OUTLINE, width=340)
    remember = ft.Checkbox(label="Keep me signed in on this device", value=False)
    error = ft.Text(message, color=ft.Colors.ERROR if message else None, size=13, width=340,
                    visible=bool(message))
    button = ft.FilledButton("Sign in", icon=ft.Icons.LOGIN, width=340, height=44)

    async def submit(_=None):
        button.disabled = True
        error.visible = False
        page.update()
        try:
            result = await asyncio.to_thread(
                user_svc.authenticate, username.value or "", password.value or "", bool(remember.value),
                page.client_ip or "", page.client_user_agent or "")
        except user_svc.AuthError as e:
            password.value = ""
            error.value, error.color, error.visible = str(e), ft.Colors.ERROR, True
            button.disabled = False
            page.update()
            return
        if remember.value:
            try:
                await ft.SharedPreferences().set(PREF_KEY, result.token)
            except Exception:  # noqa: BLE001
                log.warning("Could not store session token in the browser")
        await start_shell(page, result.user, result.token)

    button.on_click = submit
    password.on_submit = submit
    username.on_submit = lambda _: password.focus()

    if no_users:
        body: list[ft.Control] = [
            ft.Text("No accounts exist yet", size=16, weight=ft.FontWeight.W_600),
            ft.Text("Create the first admin on the server, then refresh this page:", size=13),
            ft.Container(ft.Text("python -m databridge.manage create-admin --username admin", selectable=True,
                                 font_family="monospace", size=12),
                         bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST, padding=10, border_radius=8, width=340),
            ft.Text("Or set DATABRIDGE_ADMIN_USERNAME and DATABRIDGE_ADMIN_PASSWORD and restart.", size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT, width=340),
        ]
    else:
        body = [username, password, ft.Container(remember, width=340), error, button,
                ft.Text("Accounts are created by an administrator.", size=12, color=ft.Colors.ON_SURFACE_VARIANT)]
    page.add(_center(ft.Card(ft.Container(
        ft.Column([_brand(), ft.Text("Sign in to the mapping studio", size=13, color=ft.Colors.ON_SURFACE_VARIANT,
                                     text_align=ft.TextAlign.CENTER, width=340), ft.Container(height=8), *body],
                  horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=12, tight=True),
        padding=36), elevation=2)))
    page.update()


def show_forced_password_change(page: ft.Page, user, token: str) -> None:
    page.controls.clear()
    current = ft.TextField(label="Temporary password", password=True, can_reveal_password=True, width=340,
                           autofocus=True)
    new = ft.TextField(label="New password", password=True, can_reveal_password=True, width=340)
    confirm = ft.TextField(label="Repeat new password", password=True, can_reveal_password=True, width=340)
    error = ft.Text("", color=ft.Colors.ERROR, size=13, width=340, visible=False)

    async def submit(_=None):
        if new.value != confirm.value:
            error.value, error.visible = "The new passwords do not match", True
            page.update()
            return
        try:
            user_svc.change_own_password(user.id, current.value or "", new.value or "", page.client_ip or "")
        except user_svc.AuthError as e:
            error.value, error.visible = str(e), True
            page.update()
            return
        await start_shell(page, user_svc.get_user(user.id), token)

    confirm.on_submit = submit
    page.add(_center(ft.Card(ft.Container(ft.Column([
        _brand(),
        ft.Text(f"Welcome, {user.full_name or user.username}. Choose your own password to continue.",
                size=13, width=340, text_align=ft.TextAlign.CENTER),
        current, new, confirm,
        ft.Text("At least 10 characters with three of: lower case, upper case, digit, symbol.",
                size=12, color=ft.Colors.ON_SURFACE_VARIANT, width=340),
        error,
        ft.FilledButton("Save and continue", width=340, height=44, on_click=submit),
        ft.TextButton("Sign out", on_click=lambda _: page.run_task(_sign_out_to_login, page, user, token)),
    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=12, tight=True), padding=36), elevation=2)))
    page.update()


async def _sign_out_to_login(page: ft.Page, user, token: str) -> None:
    user_svc.sign_out(token, user.username, page.client_ip or "")
    await _forget_token(page)
    show_login(page)


def change_password_dialog(app: App) -> None:
    from databridge.ui.common import close_dialog, dialog

    current = ft.TextField(label="Current password", password=True, can_reveal_password=True, autofocus=True)
    new = ft.TextField(label="New password", password=True, can_reveal_password=True)
    confirm = ft.TextField(label="Repeat new password", password=True, can_reveal_password=True)
    error = ft.Text("", color=ft.Colors.ERROR, size=13, visible=False)

    def save(_):
        if new.value != confirm.value:
            error.value, error.visible = "The new passwords do not match", True
            error.update()
            return
        try:
            user_svc.change_own_password(app.user.id, current.value or "", new.value or "", app.page.client_ip or "")
        except user_svc.AuthError as e:
            error.value, error.visible = str(e), True
            error.update()
            return
        close_dialog(app.page)
        toast(app.page, "Password changed")

    dialog(app.page, "Change password", ft.Column([
        current, new, confirm,
        ft.Text("At least 10 characters with three of: lower case, upper case, digit, symbol.", size=12,
                color=ft.Colors.ON_SURFACE_VARIANT),
        error], tight=True, spacing=10), [
        ft.TextButton("Cancel", on_click=lambda _: close_dialog(app.page)),
        ft.FilledButton("Change password", on_click=save),
    ], width=420)


async def start_shell(page: ft.Page, user, token: str, first_view: str = "dashboard") -> None:
    if user.must_change_password:
        show_forced_password_change(page, user, token)
        return
    page.controls.clear()
    app = App(page, user, token)
    page.data = app
    page.add(app.build())
    app.navigate(first_view)


async def _boot(page: ft.Page) -> None:
    token = None
    try:
        token = await ft.SharedPreferences().get(PREF_KEY)
    except Exception:  # noqa: BLE001
        token = None
    user = user_svc.validate_session(token) if isinstance(token, str) else None
    if user:
        await start_shell(page, user, token)
    else:
        if token:
            await _forget_token(page)
        show_login(page)


def main(page: ft.Page) -> None:
    page.title = "DataBridge Studio"
    page.padding = 0
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
    page.run_task(_boot, page)


__all__ = ["App", "main", "ROLES"]
