"""Draws mapping arrows between the source list and the target list.

Flet does not report where a control sits on screen, so the studio gives every field row a fixed
height (ROW_H) and puts both lists and this canvas in one scrolling column. A field's anchor is then
simply index * ROW_H + ROW_H / 2, with no scroll maths.
"""

from dataclasses import dataclass

import flet as ft
import flet.canvas as cv

ROW_H = 40


@dataclass
class Link:
    src_idx: int
    tgt_idx: int
    color: str
    width: float = 1.8
    dashed: bool = False


def link_canvas(links: list[Link], width: int, rows: int) -> cv.Canvas:
    height = max(rows, 1) * ROW_H
    shapes: list[cv.Shape] = []
    x1, x2 = 2, width - 2
    mid = width / 2
    for lk in links:
        y1 = lk.src_idx * ROW_H + ROW_H / 2
        y2 = lk.tgt_idx * ROW_H + ROW_H / 2
        stroke = ft.Paint(color=lk.color, stroke_width=lk.width, style=ft.PaintingStyle.STROKE,
                          stroke_dash_pattern=[6, 4] if lk.dashed else None, anti_alias=True)
        fill = ft.Paint(color=lk.color, style=ft.PaintingStyle.FILL, anti_alias=True)
        shapes.append(cv.Path([cv.Path.MoveTo(x1, y1), cv.Path.CubicTo(mid, y1, mid, y2, x2 - 7, y2)], paint=stroke))
        shapes.append(cv.Path([cv.Path.MoveTo(x2 - 9, y2 - 5), cv.Path.LineTo(x2, y2),
                               cv.Path.LineTo(x2 - 9, y2 + 5), cv.Path.Close()], paint=fill))
        shapes.append(cv.Circle(x1 + 2, y1, 3, paint=fill))
    return cv.Canvas(shapes=shapes, width=width, height=height)
