"""Connections: file systems and databases, with generated forms and a Test button."""

import flet as ft

from databridge.connectors.database import DIALECTS, installed_dialects
from databridge.connectors.registry import CONNECTORS
from databridge.core.security import decrypt_json
from databridge.services import connections as svc
from databridge.ui.common import background, mounted, ModelForm, card, chip, close_dialog, confirm, dialog, empty_state, guarded, page_header, toast


REST_AUTH_FIELDS = {
    "api_key": {"api_key_name", "api_key_in", "api_key"},
    "bearer": {"bearer_token"},
    "basic": {"username", "password"},
    "oauth2": {"token_url", "client_id", "client_secret", "scope", "audience", "client_auth"},
}
TYPE_ICONS = {"database": ft.Icons.STORAGE, "rest": ft.Icons.HTTP, "filesystem": ft.Icons.FOLDER}


def _summary(conn) -> str:
    c = conn.config
    if conn.type == "rest":
        auth = {"none": "no sign-in", "api_key": "API key", "bearer": "bearer token", "basic": "basic auth",
                "oauth2": "OAuth2"}.get(c.get("auth_type", "none"), "")
        tls = " · company CA" if c.get("ca_pem") else ""
        tls += " · client certificate" if c.get("client_cert_pem") else ""
        return f"REST · {c.get('base_url', '')} · {auth}{tls}"
    if conn.type == "database":
        where = c.get("host") or c.get("database") or "custom URL"
        return f"{c.get('dialect', '')} · {where}"
    return f"{c.get('protocol', 'file')} · {c.get('host') or ''}{c.get('root', '')} · {c.get('file_pattern', '*')}"


class ConnectionsView:
    def __init__(self, app):
        self.app = app
        self.page = app.page

    def build(self) -> ft.Control:
        conns = svc.list_connections()
        items: list[ft.Control] = []
        for c in conns:
            status = (chip("connected", ft.Colors.GREEN_600) if c.last_test_ok
                      else chip("failed", ft.Colors.RED_500) if c.last_test_ok is False else chip("not tested"))
            items.append(card(ft.Row([
                ft.Icon(TYPE_ICONS.get(c.type, ft.Icons.CABLE), color=ft.Colors.PRIMARY, size=30),
                ft.Column([
                    ft.Row([ft.Text(c.name, size=15, weight=ft.FontWeight.W_600), status], spacing=10),
                    ft.Text(_summary(c), size=12, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text(c.last_test_message or "", size=11, color=ft.Colors.ON_SURFACE_VARIANT,
                            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=2, expand=True),
                ft.OutlinedButton("Test", icon=ft.Icons.BOLT, on_click=background(self.page, lambda _, i=c.id: self.test(i))),
                ft.OutlinedButton("Explore", icon=ft.Icons.ACCOUNT_TREE,
                                  on_click=lambda _, i=c.id: self.app.navigate("explorer", connection_id=i)),
                *([ft.IconButton(ft.Icons.EDIT_OUTLINED, tooltip="Edit", on_click=lambda _, cc=c: self.edit(cc)),
                   ft.IconButton(ft.Icons.DELETE_OUTLINE, tooltip="Delete", on_click=lambda _, cc=c: confirm(
                       self.page, "Delete connection", f"Delete {cc.name}? Sources that use it stop refreshing.",
                       lambda: self.delete(cc)))] if self.app.can("manage_connections") else []),
            ], spacing=10)))

        admin = self.app.can("manage_connections")
        body = ft.Column(items, spacing=10) if items else empty_state(
            ft.Icons.CABLE, "No connections yet",
            "Connect a shared folder, SFTP server, cloud bucket, database or REST API to browse and pull data."
            + ("" if admin else " Ask an admin to add one."),
            ft.FilledButton("Add connection", icon=ft.Icons.ADD, on_click=lambda _: self.edit(None)) if admin else None)
        return ft.Column([
            page_header("Connections", "Where your data lives. Passwords are encrypted and never shown again."
                        + ("" if admin else " Only admins can add or change connections."),
                        [ft.FilledButton("New connection", icon=ft.Icons.ADD, on_click=lambda _: self.edit(None))]
                        if admin else []),
            body,
        ], spacing=18, scroll=ft.ScrollMode.AUTO, expand=True)

    def delete(self, conn) -> None:
        self.app.require("manage_connections")
        svc.delete_connection(conn.id)
        self.app.audit("connection.delete", conn.name)
        self.app.navigate("connections")

    def test(self, conn_id: int) -> None:
        self.app.require("use_connections")
        result = svc.test_connection(conn_id)
        toast(self.page, result.message, error=not result.ok)
        self.app.navigate("connections")

    def edit(self, conn) -> None:
        name = ft.TextField(label="Name *", value=conn.name if conn else "", dense=True, autofocus=True, width=470)
        type_dd = ft.Dropdown(
            label="Connection type", value=conn.type if conn else "filesystem", dense=True,
            options=[ft.DropdownOption(key=k, text=v.label) for k, v in CONNECTORS.items()],
            disabled=conn is not None, width=470,
        )
        form_holder = ft.Column(tight=True)
        hint = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT, width=470)
        certs = ft.Column(spacing=6, tight=True, visible=False)
        state: dict = {"pending_certs": {}}

        def render_form(_=None):
            cls = CONNECTORS[type_dd.value]
            values = dict(conn.config) if conn else {}
            if conn:
                values.update({k: "" for k in decrypt_json(conn.secret)})
            state["form"] = ModelForm(cls.config_model, values, cls.secret_fields, on_change=update_hint,
                                      exclude=cls.form_exclude)
            form_holder.controls = [state["form"].view()]
            certs.visible = type_dd.value == "rest"
            render_certs()
            if mounted(form_holder):  # the dialog is open: redraw now (type changed)
                form_holder.update()
                certs.update()
            update_hint()

        def render_certs():
            if type_dd.value != "rest":
                return
            pending = state["pending_certs"]
            have = svc.rest_certificates(conn.id) if conn and conn.type == "rest" else {"ca": [], "client": []}

            def describe(items, fallback):
                return "; ".join(f"{c['subject']} (valid {c['days_left']} more days)" for c in items) or fallback

            ca_text = ("New: " + pending["ca_name"]) if pending.get("ca_data") else describe(
                have["ca"], "None: the server's certificate must be signed by a public or OS-trusted CA")
            client_text = ("New: " + ", ".join(pending.get("client_names", []))) if pending.get("client_names") \
                else describe(have["client"], "None (only needed if the API requires mutual TLS)")
            certs.controls = [
                ft.Text("Certificates (company TLS)", size=13, weight=ft.FontWeight.W_600),
                ft.Row([ft.Text("Company CA", size=12, width=110), ft.Text(ca_text, size=12, expand=True),
                        ft.TextButton("Upload", on_click=lambda _: self.page.run_task(pick_cert, "ca"))]),
                ft.Row([ft.Text("Client certificate", size=12, width=110),
                        ft.Text(client_text, size=12, expand=True)]),
                ft.Row([ft.Container(width=110), ft.Text("Upload:", size=12),
                        ft.TextButton("Certificate", on_click=lambda _: self.page.run_task(pick_cert, "cert")),
                        ft.TextButton("Key", on_click=lambda _: self.page.run_task(pick_cert, "key")),
                        ft.TextButton(".p12 / .pfx", on_click=lambda _: self.page.run_task(pick_cert, "p12"))],
                       spacing=4),
                state.setdefault("key_pw", ft.TextField(label="Key or .p12 password (if any)", password=True,
                                                        can_reveal_password=True, dense=True, width=470)),
            ]
            if mounted(certs):
                certs.update()

        async def pick_cert(kind: str):
            from databridge.ai import tls
            from databridge.ui.uploads import pick_and_upload

            exts = {"ca": ["pem", "crt", "cer", "der"], "cert": ["pem", "crt", "cer"], "key": ["pem", "key"],
                    "p12": ["p12", "pfx"]}[kind]
            try:
                picked = await pick_and_upload(self.page, exts, 1024 * 1024)
                if not picked:
                    return
                file_name, data = picked
                pending = state["pending_certs"]
                if kind == "ca":
                    tls.normalize_ca(data)  # validate now
                    pending.update(ca_data=data, ca_name=file_name)
                else:
                    if kind == "cert":
                        tls.load_certs(data)
                    pending[{"cert": "cert_data", "key": "key_data", "p12": "p12_data"}[kind]] = data
                    pending.setdefault("client_names", []).append(file_name)
                render_certs()
            except Exception as ex:  # noqa: BLE001 - user-facing boundary
                toast(self.page, str(ex), error=True)

        def update_hint():
            if type_dd.value == "rest":
                auth = state["form"].controls["auth_type"].value
                for kind, fields in REST_AUTH_FIELDS.items():
                    for f in fields:
                        ctl = state["form"].controls.get(f)
                        if ctl is not None:
                            ctl.visible = auth == kind
                            if mounted(ctl):
                                ctl.update()
                hint.value = ("Requests only ever go to this server: next-page links and redirects to other servers "
                              "are refused, so credentials can't leak. Test calls the test path (or the base URL).")
                hint.color = ft.Colors.ON_SURFACE_VARIANT
            elif type_dd.value == "database":
                dialect = state["form"].controls["dialect"].value
                ok = installed_dialects().get(dialect, False)
                hint.value = ("Driver installed." if ok else
                              f"Driver not installed on the server: pip install {DIALECTS[dialect][2]}")
                hint.color = ft.Colors.GREEN_700 if ok else ft.Colors.AMBER_800
            else:
                hint.value = "For network shares, the server running DataBridge must be able to reach the host."
                hint.color = ft.Colors.ON_SURFACE_VARIANT
            if mounted(hint):
                hint.update()

        type_dd.on_select = render_form
        render_form()

        self.app.require("manage_connections")

        def save(test: bool):
            self.app.require("manage_connections")
            if not name.value.strip():
                raise ValueError("Give the connection a name")
            saved = svc.save_connection(name.value.strip(), type_dd.value, state["form"].values(),
                                        conn.id if conn else None)
            pending = state["pending_certs"]
            if type_dd.value == "rest" and pending:
                pw = state.get("key_pw").value if state.get("key_pw") else None
                svc.set_rest_certificates(saved.id, ca_data=pending.get("ca_data"),
                                          cert_data=pending.get("cert_data"), key_data=pending.get("key_data"),
                                          p12_data=pending.get("p12_data"), key_password=pw or None)
                state["pending_certs"] = {}
            self.app.audit("connection.update" if conn else "connection.create", saved.name, saved.type)
            close_dialog(self.page)
            if test:
                self.test(saved.id)
            else:
                toast(self.page, f"Saved {saved.name}")
                self.app.navigate("connections")

        dialog(self.page, "Edit connection" if conn else "New connection",
               ft.Column([name, type_dd, form_holder, hint, certs], spacing=12, scroll=ft.ScrollMode.AUTO),
               [ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
                ft.OutlinedButton("Save", on_click=background(self.page, lambda _: save(False))),
                ft.FilledButton("Save and test", on_click=background(self.page, lambda _: save(True)))],
               width=520, height=520)
