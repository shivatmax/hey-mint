"""Spreadsheets (.xlsx)."""

from __future__ import annotations

import re
from pathlib import Path

from . import sandbox as sb
from .registry import Ctx, check, fail, ok, setup


def number(cell) -> float | None:
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return float(cell)
    if isinstance(cell, str):
        cleaned = re.sub(r"[^\d.\-]", "", cell.replace(",", ""))
        try:
            return float(cleaned) if cleaned not in ("", ".", "-") else None
        except ValueError:
            return None
    return None


def find_row(rows: list[list], word: str) -> list | None:
    word = word.lower()
    return next((r for r in rows if any(word in str(c).lower() for c in r if c is not None)), None)


def row_has_number(row: list, value: float, tolerance: float = 0.011) -> bool:
    return any(n is not None and abs(n - value) <= tolerance for n in (number(c) for c in row))


def new_sheets(ctx: Ctx, folder: Path | None = None) -> list[Path]:
    found = sb.new_files(ctx.started, (".xlsx",))
    if folder is not None:
        found = [p for p in found if folder in p.parents]
    return found


def best_sheet(paths: list[Path], words: list[str]) -> tuple[Path | None, list[list]]:
    """The spreadsheet whose rows mention the most of `words`."""
    best, best_rows, best_hits = None, [], -1
    for path in paths:
        try:
            rows = sb.xlsx_rows(path)
        except Exception:  # noqa: BLE001 - an unreadable file is simply not the answer
            continue
        hits = sum(1 for w in words if find_row(rows, w))
        if hits > best_hits:
            best, best_rows, best_hits = path, rows, hits
    return best, best_rows


def rows_check(ctx: Ctx, expected: dict[str, float], folder: Path | None = None, what: str = "rows"):
    paths = new_sheets(ctx, folder)
    if not paths:
        return fail("no new .xlsx" + (f" in {folder.name}" if folder else ""))
    path, rows = best_sheet(paths, list(expected))
    problems = []
    for word, value in expected.items():
        row = find_row(rows, word)
        if row is None:
            problems.append(f"no row for {word}")
        elif value is not None and not row_has_number(row, value):
            problems.append(f"{word} row lacks {value}")
    if problems:
        return fail(f"{path.name}: " + "; ".join(problems))
    where = "" if sb.ROOT in path.parents else " (saved in Mint's Documents folder)"
    return ok(f"{path.name}: all {len(expected)} {what} correct{where}")


RECEIPTS = {"blue-bottle.txt": ("Blue Bottle Coffee", "2026-09-02", 4.50),
            "city-cabs.txt": ("City Cabs", "2026-09-05", 23.80),
            "office-depot.txt": ("Office Depot", "2026-09-10", 112.35),
            "pizza-palace.txt": ("Pizza Palace", "2026-09-18", 36.00)}


@setup
def sheets_receipts(ctx: Ctx):
    for name, (vendor, date, total) in RECEIPTS.items():
        sb.write(f"Receipts/{name}", f"{vendor.upper()}\n{date}\n\n1 x item ........ {total:.2f}\n"
                                     f"TOTAL  USD {total:.2f}\nThank you!\n")


@check
def sheets_receipts_ok(ctx: Ctx):
    # Short keys: "Blue Bottle" or "BLUE BOTTLE COFFEE" both count.
    return rows_check(ctx, {"Blue Bottle": 4.50, "Cabs": 23.80, "Depot": 112.35, "Pizza": 36.00}, what="receipts")


FRUIT = {"Apples": 2.40, "Bananas": 1.85, "Cherries": 6.90, "Dates": 4.15, "Elderberries": 7.30}


@setup
def sheets_web_table(ctx: Ctx):
    pass


@check
def sheets_web_table_ok(ctx: Ctx):
    return rows_check(ctx, FRUIT, what="fruit prices")


@setup
def sheets_edit_existing(ctx: Ctx):
    import openpyxl
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Budget"
    for row in (("Category", "Amount"), ("Rent", 1800), ("Food", 600), ("Utilities", 240)):
        sheet.append(row)
    ctx.p().mkdir(parents=True, exist_ok=True)
    book.save(ctx.p("budget.xlsx"))


@check
def sheets_edit_existing_ok(ctx: Ctx):
    candidates = [ctx.p("budget.xlsx")] + [p for p in new_sheets(ctx) if "budget" in p.name.lower()]
    reasons = []
    for path in dict.fromkeys(candidates):
        if not path.exists():
            continue
        rows, formulas = sb.xlsx_rows(path), sb.xlsx_formulas(path)
        travel = find_row(rows, "travel")
        if travel is None or not row_has_number(travel, 1200):
            reasons.append(f"{path.name}: no Travel 1200 row")
            continue
        if not all(find_row(rows, w) for w in ("rent", "food", "utilities")):
            reasons.append(f"{path.name}: lost an existing row")
            continue
        total = find_row(rows, "total")
        if total is not None and row_has_number(total, 3840):
            return ok(f"{path.name}: Travel added, Total = 3840")
        formula_row = find_row(formulas, "total")
        if formula_row and any(isinstance(c, str) and re.match(r"=\s*SUM\(\s*B2\s*:\s*B5\s*\)", c, re.I)
                               for c in formula_row):
            return ok(f"{path.name}: Travel added, Total =SUM(B2:B5)")
        reasons.append(f"{path.name}: no Total of 3840")
    return fail("; ".join(reasons) or "budget.xlsx is gone")


TEAM = {"Asha": "14 March", "Ben": "2 July", "Carla": "30 November", "Dev": "9 January", "Emi": "21 August",
        "Finn": "5 May"}


@setup
def sheets_birthdays(ctx: Ctx):
    sb.write("team.txt", "Team birthdays (from the HR sheet)\n\n" +
             "\n".join(f"{name} - {day}" for name, day in TEAM.items()) + "\n")


@check
def sheets_birthdays_ok(ctx: Ctx):
    paths = new_sheets(ctx)
    if not paths:
        return fail("no new .xlsx")
    path, rows = best_sheet(paths, list(TEAM))
    missing = [n for n in TEAM if find_row(rows, n) is None]
    if missing:
        return fail(f"{path.name}: missing {missing}")
    undated = [n for n in TEAM if len([c for c in find_row(rows, n) if c not in (None, "")]) < 2]
    if undated:
        return fail(f"{path.name}: no birthday next to {undated}")
    return ok(f"{path.name}: 6 people with birthdays")
