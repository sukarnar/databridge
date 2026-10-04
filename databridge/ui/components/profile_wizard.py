"""Sheet Profile wizard: pick sheet and header row, review types, then save the source."""

from collections.abc import Callable
from pathlib import PurePath
from typing import Any

import flet as ft

from databridge.ingest.sheet_profile import (
    SPREADSHEET_EXT,
    SheetProfile,
    column_letter,
    ext_of,
    list_sheets,
    parse_file,
    read_grid,
    suggest_profile,
)
from databridge.ui.common import close_dialog, dialog, guarded, type_badge, type_dropdown

GRID_ROWS = 14


class ProfileWizard:
    """Shows raw rows with the header highlighted, and the parsed result with type overrides.

    on_save(name, profile) is called when the user confirms.
    """

    def __init__(self, page: ft.Page, filename: str, content: bytes,
                 on_save: Callable[[str, SheetProfile], None], title: str = "Import spreadsheet",
                 name: str | None = None, profile: SheetProfile | None = None,
                 check_name: Callable[[str], str] | None = None):
        self.page, self.filename, self.content, self.on_save = page, filename, content, on_save
        self.check_name = check_name  # raises (shown in the wizard, which stays open) when the name can't be used
        self.is_sheet = ext_of(filename) in SPREADSHEET_EXT
        self.sheets = list_sheets(content, filename) if self.is_sheet else []
        self.profile = profile or suggest_profile(content, filename, None)
        self.title = title
        self.name = ft.TextField(label="Source name", value=name or PurePath(filename).stem.replace("_", " ").title(),
                                 dense=True, width=260, visible=name is None)
        self.grid_view = ft.Column(spacing=0)
        self.fields_view = ft.Column(spacing=4)
        self.notes = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

    def open(self) -> None:
        p = self.profile
        sheet_dd = ft.Dropdown(label="Sheet", value=p.sheet, dense=True, width=200, visible=self.is_sheet,
                               options=[ft.DropdownOption(key=s, text=s) for s in self.sheets])
        header_dd = ft.Dropdown(label="Header row", value=str(p.header_row), dense=True, width=130,
                                options=[ft.DropdownOption(key="0", text="No header")] +
                                [ft.DropdownOption(key=str(i), text=f"Row {i}") for i in range(1, 31)])
        rows_dd = ft.Dropdown(label="Header spans", value=str(p.header_rows), dense=True, width=130,
                              options=[ft.DropdownOption(key=str(i), text=f"{i} row{'s' if i > 1 else ''}") for i in (1, 2, 3)])
        skip = ft.TextField(label="Skip rows starting with", value=", ".join(p.skip_patterns), dense=True, width=240)

        def changed(_=None, resuggest: bool = False):
            if resuggest:
                self.profile = suggest_profile(self.content, self.filename, sheet_dd.value)
                header_dd.value, rows_dd.value = str(self.profile.header_row), str(self.profile.header_rows)
            self.profile.sheet = sheet_dd.value if self.is_sheet else None
            self.profile.header_row = int(header_dd.value)
            self.profile.header_rows = int(rows_dd.value)
            self.profile.skip_patterns = [s.strip() for s in (skip.value or "").split(",") if s.strip()]
            self.refresh()

        sheet_dd.on_select = guarded(self.page, lambda e: changed(e, resuggest=True))
        header_dd.on_select = guarded(self.page, changed)
        rows_dd.on_select = guarded(self.page, changed)
        skip.on_blur = guarded(self.page, changed)
        skip.on_submit = guarded(self.page, changed)

        def save(_):
            name = self.name.value.strip()
            if not name:
                raise ValueError("Give the source a name")
            if self.check_name is not None:
                name = self.check_name(name)
            close_dialog(self.page)
            self.on_save(name, self.profile)

        self.refresh(initial=True)
        content = ft.Column([
            ft.Container(ft.Row([self.name, sheet_dd, header_dd, rows_dd, skip], spacing=10, wrap=True, run_spacing=14),
                         padding=ft.Padding.only(top=8)),
            ft.Text("1. Raw rows: blue = header, grey = skipped", size=13, weight=ft.FontWeight.W_600),
            ft.Container(ft.Row([self.grid_view], scroll=ft.ScrollMode.AUTO), height=GRID_ROWS * 28 + 8,
                         border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=ft.BorderRadius.all(8)),
            ft.Text("2. Columns and detected types (change a type to override it)", size=13, weight=ft.FontWeight.W_600),
            self.notes,
            self.fields_view,
        ], spacing=10, scroll=ft.ScrollMode.AUTO)
        dialog(self.page, f"{self.title} · {self.filename}", content, [
            ft.TextButton("Cancel", on_click=lambda _: close_dialog(self.page)),
            ft.FilledButton("Save", icon=ft.Icons.CHECK, on_click=guarded(self.page, save)),
        ], width=980, height=620)

    def refresh(self, initial: bool = False) -> None:
        p = self.profile
        if self.is_sheet or ext_of(self.filename) in {".csv", ".tsv", ".txt"}:
            grid = read_grid(self.content, self.filename, p.sheet, max_rows=GRID_ROWS)
            self.grid_view.controls = self._grid(grid)
        else:
            self.grid_view.controls = [ft.Text("Structured file: columns are read directly.", size=12)]
        result = parse_file(self.content, self.filename, p)
        self.notes.value = " · ".join([f"{result.df.height:,} data rows", *result.notes])
        self.fields_view.controls = [self._field_row(f) for f in result.fields]
        if not initial:
            self.grid_view.update()
            self.fields_view.update()
            self.notes.update()

    def _grid(self, grid: list[list[Any]]) -> list[ft.Control]:
        p = self.profile
        width = max((len(r) for r in grid), default=0)
        header_range = range(p.header_row, p.header_row + p.header_rows) if p.header_row else range(0)
        lower = [s.lower() for s in p.skip_patterns]

        def cell(text: str, w: int, bold=False, color=None, bg=None):
            return ft.Container(ft.Text(text, size=11.5, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                                        weight=ft.FontWeight.W_600 if bold else None, color=color),
                                width=w, height=28, bgcolor=bg, padding=ft.Padding.symmetric(horizontal=6),
                                alignment=ft.Alignment.CENTER_LEFT,
                                border=ft.Border.only(right=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
                                                      bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)))

        out = [ft.Row([cell("", 44, bg=ft.Colors.SURFACE_CONTAINER_HIGHEST)] +
                      [cell(column_letter(c), 120, bold=True, bg=ft.Colors.SURFACE_CONTAINER_HIGHEST)
                       for c in range(width)], spacing=0)]
        for i, row in enumerate(grid, start=1):
            first = next((str(v).strip().lower() for v in row if v not in (None, "")), "")
            is_header = i in header_range
            before = p.header_row and i < p.header_row
            skipped = before or not any(v not in (None, "") for v in row) or any(first.startswith(s) for s in lower)
            bg = (ft.Colors.with_opacity(0.14, ft.Colors.PRIMARY) if is_header
                  else ft.Colors.with_opacity(0.05, ft.Colors.ON_SURFACE) if skipped else None)
            color = ft.Colors.OUTLINE if skipped and not is_header else None
            out.append(ft.Row([cell(str(i), 44, color=ft.Colors.ON_SURFACE_VARIANT, bg=ft.Colors.SURFACE_CONTAINER_HIGHEST)] +
                              [cell("" if v is None else str(v), 120, bold=is_header, color=color, bg=bg) for v in row],
                              spacing=0))
        return out

    def _field_row(self, f: dict[str, Any]) -> ft.Control:
        def set_type(e, name=f["name"]):
            if e.control.value == f["type"] and name not in self.profile.type_overrides:
                return
            self.profile.type_overrides[name] = e.control.value
            self.refresh()

        samples = ", ".join(str(s) for s in f.get("samples", [])[:4])
        return ft.Row([
            ft.Text(f.get("column_letter", ""), width=28, size=12, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(f["name"], width=200, size=13, weight=ft.FontWeight.W_500, no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS),
            type_dropdown(self.profile.type_overrides.get(f["name"], f["type"]), label="", width=150,
                          on_select=guarded(self.page, set_type)),
            ft.Text(f"{f.get('null_pct', 0)}% blank", size=12, width=80, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(samples, size=12, expand=True, no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                    color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(f.get("note", ""), size=11.5, color=ft.Colors.AMBER_800, width=220, no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS, tooltip=f.get("note")),
        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)


__all__ = ["ProfileWizard", "type_badge"]
