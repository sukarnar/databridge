"""Documentation: the user guide, filtered to the reader's role, with search and links into the tool."""

import flet as ft

from databridge.services import guide
from databridge.ui.common import mounted, page_header

GROUPS = [("Using DataBridge", 0, 19), ("AI", 20, 29), ("Administration", 30, 999)]


def _icon(name: str):
    return getattr(ft.Icons, name, ft.Icons.ARTICLE_OUTLINED)


class DocsView:
    def __init__(self, app, topic: str | None = None, q: str | None = None):
        self.app, self.page = app, app.page
        role = app.user.role
        self.topics = guide.topics(role)
        chosen = guide.get(topic, role) if topic else guide.topic_for_page(getattr(app, "last_page", None), role)
        self.current = chosen or (self.topics[0] if self.topics else None)
        self.query = q or ""
        self.toc = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO, expand=True)
        self.content = ft.Column(spacing=14, scroll=ft.ScrollMode.AUTO, expand=True)
        self.search_box = ft.TextField(hint_text="Search the guide", prefix_icon=ft.Icons.SEARCH, dense=True,
                                       value=self.query, on_change=self.on_search, on_submit=self.on_search)

    # ---------------------------------------------------------------- layout
    def build(self) -> ft.Control:
        self.render_toc()
        self.render_content()
        return ft.Column([
            page_header("Documentation", f"User guide for your role ({self.app.user.role}). "
                                         "Tip: open Documentation from any page to jump to its topic."),
            ft.Row([
                ft.Container(ft.Column([self.search_box, self.toc], spacing=10, expand=True), width=280),
                ft.VerticalDivider(width=1),
                ft.Container(self.content, expand=True, padding=ft.Padding.only(left=12, right=8)),
            ], expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
        ], spacing=14, expand=True)

    def render_toc(self) -> None:
        items: list[ft.Control] = []
        for label, lo, hi in GROUPS:
            group = [t for t in self.topics if lo <= t.order <= hi]
            if not group:
                continue
            items.append(ft.Container(ft.Text(label.upper(), size=11, weight=ft.FontWeight.W_600,
                                              color=ft.Colors.ON_SURFACE_VARIANT),
                                      padding=ft.Padding.only(left=8, top=10, bottom=2)))
            for t in group:
                selected = self.current is not None and t.slug == self.current.slug and not self.query
                items.append(ft.Container(
                    ft.Row([ft.Icon(_icon(t.icon), size=18,
                                    color=ft.Colors.PRIMARY if selected else ft.Colors.ON_SURFACE_VARIANT),
                            ft.Text(t.title, size=13, weight=ft.FontWeight.W_600 if selected else None,
                                    expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)], spacing=10),
                    padding=ft.Padding.symmetric(horizontal=10, vertical=8), border_radius=ft.BorderRadius.all(8),
                    bgcolor=ft.Colors.SECONDARY_CONTAINER if selected else None, ink=True,
                    on_click=lambda _, s=t.slug: self.open(s)))
        self.toc.controls = items
        if mounted(self.toc):
            self.toc.update()

    def render_content(self) -> None:
        if self.query.strip():
            self.content.controls = self.search_results()
        elif not self.current:
            self.content.controls = [ft.Text("No guide topics are available for your role.")]
        else:
            self.content.controls = self.topic_controls(self.current)
        if mounted(self.content):
            self.content.update()
            self.page.run_task(self.content.scroll_to, offset=0, duration=0)  # new topic starts at the top

    def topic_controls(self, t: guide.Topic) -> list[ft.Control]:
        nav_keys = {k for k, *_ in self.app.nav}
        open_page = next((p for p in t.pages if p in nav_keys), None)
        label = next((lbl for k, lbl, *_ in self.app.nav if k == open_page), "")
        idx = next(i for i, x in enumerate(self.topics) if x.slug == t.slug)
        prev_t = self.topics[idx - 1] if idx > 0 else None
        next_t = self.topics[idx + 1] if idx + 1 < len(self.topics) else None
        return [
            *([ft.Row([ft.FilledTonalButton(f"Open {label}", icon=ft.Icons.OPEN_IN_NEW,
                                            on_click=lambda _, k=open_page: self.app.navigate(k))],
                      alignment=ft.MainAxisAlignment.END)] if open_page else []),
            ft.Markdown(guide.render(t, self.app.user.role), selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                        on_tap_link=self.on_link, code_theme=ft.MarkdownCodeTheme.ATOM_ONE_LIGHT),
            ft.Divider(),
            ft.Row([
                ft.TextButton(f"Previous: {prev_t.title}", icon=ft.Icons.ARROW_BACK,
                              on_click=lambda _, s=prev_t.slug: self.open(s)) if prev_t else ft.Container(),
                ft.TextButton(f"Next: {next_t.title}", icon=ft.Icons.ARROW_FORWARD,
                              on_click=lambda _, s=next_t.slug: self.open(s)) if next_t else ft.Container(),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ]

    def search_results(self) -> list[ft.Control]:
        results = guide.search(self.query, self.app.user.role)
        head = ft.Text(f"{len(results)} result(s) for “{self.query.strip()}”" if results else
                       f"Nothing found for “{self.query.strip()}”. Try fewer or different words.",
                       size=14, weight=ft.FontWeight.W_600)
        cards = [ft.Container(ft.Column([
            ft.Row([ft.Icon(_icon(t.icon), size=18, color=ft.Colors.PRIMARY),
                    ft.Text(t.title, size=14, weight=ft.FontWeight.W_600)], spacing=8),
            ft.Text(snippet, size=12, color=ft.Colors.ON_SURFACE_VARIANT)], spacing=4),
            padding=12, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=ft.BorderRadius.all(10),
            ink=True, on_click=lambda _, s=t.slug: self.open(s, clear_search=True)) for t, snippet in results]
        return [head, *cards]

    # ---------------------------------------------------------------- events
    def open(self, slug: str, clear_search: bool = True) -> None:
        t = guide.get(slug, self.app.user.role)
        if not t:
            return
        self.current = t
        if clear_search and self.query:
            self.query = ""
            self.search_box.value = ""
            self.search_box.update()
        self.render_toc()
        self.render_content()

    def on_search(self, e) -> None:
        self.query = e.control.value or ""
        self.render_toc()
        self.render_content()

    def on_link(self, e) -> None:
        url = str(e.data or "")
        if url.startswith("guide:"):
            self.open(url.split(":", 1)[1])
        elif url.startswith("app:"):
            key = url.split(":", 1)[1]
            if key in {k for k, *_ in self.app.nav}:
                self.app.navigate(key)
        elif url.startswith(("http://", "https://")):
            self.page.run_task(ft.UrlLauncher().launch_url, url)
