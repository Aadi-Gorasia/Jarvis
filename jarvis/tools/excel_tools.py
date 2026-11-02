"""Spreadsheet tools (.xlsx via openpyxl). For heavy transformations agents can also use run_python with pandas."""
import json

from . import policy
from .base import tool


def _fmt(v) -> str:
    s = str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def _load(path: str):
    from openpyxl import load_workbook

    p = policy.resolve(path)
    if p.suffix.lower() not in (".xlsx", ".xlsm"):
        raise ValueError("only .xlsx/.xlsm files are supported here; convert .xls/.csv with run_python and pandas")
    return p, load_workbook(p)


@tool(scope="agent")
def excel_inspect(path: str, max_rows: int = 15) -> str:
    """Summarise an Excel workbook: each sheet's size, merged cells and its first rows (formulas are shown as text). Always do this before editing a sheet."""
    p, wb = _load(path)
    out = [f"File: {p}"]
    for ws in wb.worksheets:
        out.append(f"\n## Sheet '{ws.title}'  range={ws.dimensions}  rows={ws.max_row}  cols={ws.max_column}")
        if ws.merged_cells.ranges:
            out.append("merged: " + ", ".join(str(r) for r in list(ws.merged_cells.ranges)[:10]))
        for i, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row, int(max_rows))), 1):
            cells = [f"{c.coordinate}={_fmt(c.value)}" for c in row if c.value is not None]
            out.append(" | ".join(cells) if cells else f"(row {i} empty)")
    return "\n".join(out)[:9000]


@tool(scope="agent")
def excel_read(path: str, sheet: str, cell_range: str = "A1:J40") -> str:
    """Read a range of cells (like 'A1:F50') from a sheet. Empty cells are skipped."""
    p, wb = _load(path)
    ws = wb[sheet]
    rng = ws[cell_range]
    if not isinstance(rng, tuple):
        rng = ((rng,),)
    elif rng and not isinstance(rng[0], tuple):
        rng = (rng,)
    lines = []
    for row in rng:
        cells = [f"{c.coordinate}={_fmt(c.value)}" for c in row if c.value is not None]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)[:9000] or "(range is empty)"


@tool(scope="agent")
def excel_write(path: str, sheet: str, cells_json: str, save_as: str = "") -> str:
    """Write cells into an Excel file. cells_json is a JSON object like {"A1": "Haus", "B1": "house", "C2": "=SUM(A1:B1)"}. The sheet is created if missing. Leave save_as empty to change the file in place (a backup is made first), or give a new path to write a copy. Note: charts, images and pivot tables in the original may not survive; prefer save_as for such files."""
    p, wb = _load(path)
    cells = json.loads(cells_json)
    target = policy.resolve(save_as) if save_as else p
    bak = policy.guard_write(target)
    ws = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)
    for addr, val in cells.items():
        ws[addr] = val
    wb.save(target)
    return f"Wrote {len(cells)} cells to {target} [{sheet}]" + (f"; backup at {bak}" if bak else "")
