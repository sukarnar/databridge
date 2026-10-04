---
title: Formula reference
icon: FUNCTIONS
permission: view
pages: []
keywords: formula, function, if, concat, date, text, number, and, or, contains, expression
---
# Formula reference

Formulas work like spreadsheet formulas. They are used for mapping rules, row filters, data checks and AI workflow conditions.

## Basics

- **Columns:** written in square brackets: `[Customer Name]`.
- **Text:** in double quotes: `"Closed"`.
- **Joining text:** the `&` operator, e.g. `[First] & " " & [Last]`.
- **Arithmetic:** `+ - * /`. Numbers stored as text (like `1,200.50` or `$15`) are converted automatically.
- **Comparisons:** `= <> < > <= >=`. They give TRUE or FALSE.

Examples:

| Goal | Formula |
|---|---|
| Clean an ID | `UPPER(TRIM([Cust No]))` |
| Full name | `CONCAT([First], " ", [Last])` |
| Line total | `ROUND([Qty] * [Unit Price], 2)` |
| Parse a US date | `DATEVALUE([Order Date], "%m/%d/%Y")` |
| Year-month | `TEXT([Order Date], "%Y-%m")` |
| Size band | `IF([Amount] > 1000, "large", "small")` |
| Keep open, high-priority rows | `AND([Status] = "Open", [Priority] >= 3)` |
| One of several values | `IN([Region], "EMEA", "APAC")` |

## All functions

{{functions}}

Formulas are checked and compiled; they can't run code or reach files.
