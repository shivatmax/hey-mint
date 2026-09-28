"""Documents into a spreadsheet: "put every invoice in Downloads into a spreadsheet",
"make a table of these receipts: date, shop, total", "list the candidates' CVs with
email, years of experience and current company".

make_spreadsheet gathers the files (a folder, a pattern, or a list), reads each one -
the text of PDFs, Word, Pages-exported and text files on the Mac; scans, photos and
screenshots go to Gemini as they are - and Gemini Flash Lite pulls the same fields
out of every file, several at a time. When the user names no columns, they are chosen
from the first few files. The result is a real .xlsx (numbers as numbers, dates as
dates, a bold frozen header, a column with the source file) in the Spreadsheets folder
of Mint's storage, and it is opened.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("mint.tools.sheets")

READABLE = {".pdf", ".txt", ".md", ".csv", ".json", ".html", ".htm", ".rtf", ".doc", ".docx", ".odt", ".eml",
            ".png", ".jpg", ".jpeg", ".heic", ".webp", ".gif", ".tiff"}
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
          ".heic": "image/heic", ".gif": "image/gif", ".tiff": "image/tiff"}
MAX_FILES = 200
MAX_BYTES = 18_000_000          # Gemini's inline limit is 20 MB per request


def _files(source: str, pattern: str = "") -> list[Path]:
    """A folder (optionally with a pattern), a single file, or several paths separated by ';' or newlines."""
    from mint.tools import harness as harness_tools
    out: list[Path] = []
    for part in re.split(r"[;\n]+", str(source or "")):
        part = part.strip()
        if not part:
            continue
        path = Path(os.path.expanduser(part))
        if not path.is_absolute():
            try:
                found, _why = harness_tools._resolve(part, must_exist=True)       # "downloads", "desktop/receipts"
            except Exception:
                found = None
            path = found or Path.home() / part
        if path.is_dir():
            for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_file() and not entry.name.startswith(".") and entry.suffix.lower() in READABLE \
                        and (not pattern or fnmatch.fnmatch(entry.name.lower(), pattern.lower())):
                    out.append(entry)
        elif path.is_file():
            out.append(path)
    return out[:MAX_FILES]


def _content(path: Path):
    """What to show the model for one file: text, or the file itself (scans, photos)."""
    from google.genai import types
    from mint.tools import harness as harness_tools
    suffix = path.suffix.lower()
    if suffix in IMAGES and path.stat().st_size < MAX_BYTES:
        return types.Part.from_bytes(data=path.read_bytes(), mime_type=IMAGES[suffix])
    text, _note = harness_tools._text_of(path)
    if text.strip():
        return text[:40_000]
    if suffix == ".pdf" and path.stat().st_size < MAX_BYTES:          # a scan: Gemini reads the pages
        return types.Part.from_bytes(data=path.read_bytes(), mime_type="application/pdf")
    return None


def _choose_columns(what: str, samples: list[tuple[Path, object]]) -> list[str]:
    from mint.core import llm
    parts: list = [f"The user wants a spreadsheet of these files: {what or 'one row per file'}. Here are a few of "
                   "them. Choose 4 to 10 column names (short, Title Case) that capture what matters and appear in "
                   "most of them - for invoices e.g. Vendor, Invoice Number, Date, Due Date, Total, Currency. "
                   'Return JSON: {"columns": [...]}']
    for path, content in samples[:3]:
        parts += [f"\n--- {path.name} ---", content if not isinstance(content, str) else content[:6000]]
    text, _ = llm.generate(parts, json_mode=True)
    columns = llm.parse_json(text).get("columns") or []
    return [str(c).strip() for c in columns if str(c).strip()][:12] or ["Title", "Date", "Summary"]


def _extract(path: Path, content, columns: list[str], what: str) -> dict:
    from mint.core import llm
    prompt = (f"Extract these fields from the document '{path.name}' ({what or 'one row per file'}): "
              f"{', '.join(columns)}. Return JSON with exactly those keys. Numbers as plain numbers (no currency "
              "signs or thousands separators), dates as YYYY-MM-DD, text short. Unknown -> null. If the file "
              "clearly holds several items that each deserve a row (a statement with many transactions), return "
              '{"rows": [ {...}, ... ]} instead.')
    try:
        text, _ = llm.generate([prompt, content], json_mode=True)
        data = llm.parse_json(text)
    except Exception as error:
        log.info("%s: %s", path.name, error)
        return {"rows": [{"_error": str(error)[:120]}]}
    rows = data.get("rows") if isinstance(data, dict) and isinstance(data.get("rows"), list) else [data]
    return {"rows": [r for r in rows if isinstance(r, dict)]}


def _cell(value):
    """Numbers as numbers and dates as dates, so the sheet can sum and sort."""
    if isinstance(value, (int, float)) or value is None:
        return value
    text = str(value).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", text.replace(",", "")) and len(text) < 16 and not text.startswith("0"):
        try:
            return float(text.replace(",", "")) if "." in text else int(text.replace(",", ""))
        except ValueError:
            pass
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            pass
    return text


def _write(path: Path, columns: list[str], rows: list[tuple[str, dict]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    book = Workbook()
    sheet = book.active
    sheet.title = "Data"
    header = [*columns, "Source file"]
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E6F4F1")
    for source, row in rows:
        values = [_cell(row.get(c)) for c in columns] + [source]
        if row.get("_error"):
            values[0] = f"(could not read: {row['_error']})"
        sheet.append(values)
    for i, column in enumerate(sheet.columns, 1):
        width = max(len(str(c.value or "")) for c in column)
        sheet.column_dimensions[column[0].column_letter].width = min(60, max(10, width + 2))
        for c in column[1:]:
            if isinstance(c.value, dt.date):
                c.number_format = "yyyy-mm-dd"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    book.save(path)


def make_spreadsheet(source: str, what: str = "", columns=None, pattern: str = "", name: str = "",
                     open_after: bool = True) -> str:
    from mint.core import config
    files = _files(source, pattern)
    if not files:
        return f"FAILED: found no readable files in '{source}'" + (f" matching {pattern}" if pattern else "") + "."
    started = time.monotonic()
    with ThreadPoolExecutor(6) as pool:
        contents = list(pool.map(lambda p: (p, _content(p)), files))
    usable = [(p, c) for p, c in contents if c is not None]
    skipped = [p.name for p, c in contents if c is None]
    if not usable:
        return "FAILED: none of those files had anything readable in them."
    cols = [str(c).strip() for c in (columns or []) if str(c).strip()] or _choose_columns(what, usable)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda pc: (pc[0], _extract(pc[0], pc[1], cols, what)), usable))
    rows = [(p.name, r) for p, res in results for r in res["rows"]]
    title = re.sub(r"[^\w .-]", "", name or what or "Spreadsheet").strip()[:60] or "Spreadsheet"
    out = config.storage("Spreadsheets") / f"{title} {time.strftime('%Y-%m-%d %H.%M')}.xlsx"
    _write(out, cols, rows)
    if open_after:
        import subprocess
        subprocess.run(["open", str(out)], check=False)
    failed = sum(1 for _, r in rows if r.get("_error"))
    preview = "; ".join(", ".join(f"{c}: {r.get(c)}" for c in cols[:4]) for _, r in rows[:3])
    return (f"Made a spreadsheet of {len(usable)} file(s), {len(rows)} row(s), columns: {', '.join(cols)}, in "
            f"{time.monotonic() - started:.0f} s. Saved: {out}" + (" and opened it" if open_after else "") + "."
            + (f" Skipped (nothing readable): {', '.join(skipped[:8])}." if skipped else "")
            + (f" {failed} file(s) could not be read." if failed else "")
            + (f"\nFirst rows: {preview}" if preview else ""))


PROMPT = """Spreadsheets from files: "put my invoices / receipts / CVs / statements into a spreadsheet", "make a \
table of these PDFs" -> make_spreadsheet with the folder (or files) and what the rows are; pass columns only if \
the user names them. It reads every file (scans too) and saves an .xlsx in Mint's Spreadsheets folder."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="make_spreadsheet",
        description=("Read many documents (PDFs, Word, text, scans, photos of receipts) and put the same fields from "
                     "each into one .xlsx spreadsheet, one row per document (or per item in a statement), then open "
                     "it. For invoices, receipts, CVs, bank statements, contracts, forms."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "source": types.Schema(type=S, description="a folder ('~/Downloads', 'desktop/receipts'), a file, or "
                                                       "several paths separated by ';'"),
            "pattern": types.Schema(type=S, description="optional file pattern, e.g. '*.pdf' or '*invoice*'"),
            "what": types.Schema(type=S, description="what the files are / what each row is, e.g. 'invoices'"),
            "columns": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=S),
                                    description="only if the user named them"),
            "name": types.Schema(type=S, description="file name for the spreadsheet (optional)")},
            required=["source"]))]


def tool(args: dict) -> str:
    """Up to a handful of files: answered now. More: made in the background, with a message when done."""
    import threading
    job = (str(args.get("source") or ""), str(args.get("what") or ""), args.get("columns"),
           str(args.get("pattern") or ""), str(args.get("name") or ""))
    count = len(_files(job[0], job[3]))
    if count <= 6:
        return make_spreadsheet(*job)

    def run():
        try:
            result = make_spreadsheet(*job)
        except Exception as error:
            log.exception("spreadsheet failed")
            result = f"FAILED: {error}"
        try:
            from mint.tools.work import _notify_mint
            _notify_mint(f"(Mint's spreadsheet maker, not the user.) {result} Tell the user briefly.")
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="spreadsheet").start()
    return (f"Reading {count} files into a spreadsheet in the background (about {max(1, count // 6 * 15 // 60)} "
            "min). A message comes when it is ready; tell the user and carry on.")


HANDLERS = {"make_spreadsheet": tool}
