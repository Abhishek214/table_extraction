"""
Table extraction from PDF files.
Works on both bordered and borderless tables, and handles multi-page tables too.
No hardcoded keywords or column names - just geometry and structure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import pdfplumber

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TableRegion:
    """Holds one detected table with its content."""

    title: str
    columns: list[str]
    rows: list[list[str]]
    page: int
    page_end: int | None = None

    @property
    def ncols(self) -> int:
        return len(self.columns)

    @property
    def nrows(self) -> int:
        return len(self.rows)

    def signature(self) -> str:
        """Column signature for matching same table across pages."""
        return "|".join(c.strip().lower() for c in self.columns if c.strip())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_tables(pdf_path: str) -> list[TableRegion]:
    """Load a PDF and pull out all the tables found in it.

    Per page: try bordered extraction first, fall back to borderless
    (word clustering) if nothing shows up. Then merge tables that
    continue on the next page.
    """
    regions: list[TableRegion] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_tables = _extract_bordered(page) or _extract_borderless(page)
            for t in page_tables:
                t.title = _get_title(page, t)
            regions.extend(page_tables)

    merged = _merge_pages(regions)
    logger.info("Extracted %d table(s) from %s", len(merged), pdf_path)
    return merged


# ---------------------------------------------------------------------------
# Bordered tables (pdfplumber default)
# ---------------------------------------------------------------------------

def _extract_bordered(page: pdfplumber.page.Page) -> list[TableRegion]:
    """Try pdfplumber's built-in table finder for tables with visible borders.

    Background decoration (header bars, footer lines, watermarks) can also
    look like a grid to pdfplumber. We filter out tables that span most of
    the page - a real table is usually a bounded sub-region.
    """
    found: list[TableRegion] = []
    for table in page.find_tables():
        if not _is_plausable_region(table, page):
            continue
        raw = table.extract()
        region = _raw_to_region(raw, page.page_number)
        if region and _is_valid(region):
            found.append(region)
    return found


def _is_plausable_region(
    table: "pdfplumber.table.Table",
    page: pdfplumber.page.Page,
    max_area_ratio: float = 0.6,
) -> bool:
    """Check if the found grid is actually a table, not page decoration.

    Decoration often bleeds past the page edges and covers almost the whole
    page. A real table sits in a bounded area.
    """
    x0, top, x1, bottom = table.bbox
    page_area = page.width * page.height
    if page_area <= 0:
        return False

    # bbox going outside the page is a red flag
    if x0 < page.bbox[0] - 1 or top < page.bbox[1] - 1:
        return False
    if x1 > page.bbox[2] + 1 or bottom > page.bbox[3] + 1:
        return False

    area_ratio = (x1 - x0) * (bottom - top) / page_area
    if area_ratio > max_area_ratio:
        return False

    return True


def _raw_to_region(raw: list[list[Any]], page_num: int) -> TableRegion | None:
    """Convert pdfplumber's list-of-lists output into a TableRegion."""
    # drop fully empty rows
    rows = [
        [str(cell).strip() if cell is not None else "" for cell in row]
        for row in raw
        if any(cell is not None and str(cell).strip() for cell in row)
    ]
    if len(rows) < 2:
        return None

    # if first row has far fewer cells than second, call it a title
    if len(rows) > 1 and len(rows[0]) < len(rows[1]) * 0.6:
        title = " ".join(c for c in rows[0] if c)
        header = rows[1]
        data = rows[2:]
    else:
        title = ""
        header = rows[0]
        data = rows[1:]

    # pad short rows to match header count
    n_cols = len(header)
    data = [
        row + [""] * (n_cols - len(row)) if len(row) < n_cols else row[:n_cols]
        for row in data
    ]
    if not data:
        return None

    return TableRegion(title=title, columns=header, rows=data, page=page_num)


# ---------------------------------------------------------------------------
# Borderless tables (word-position clustering)
# ---------------------------------------------------------------------------

def _extract_borderless(page: pdfplumber.page.Page) -> list[TableRegion]:
    """Fallback for tables without visible borders.

    1. Extract all words with their bounding boxes.
    2. Group into physical lines (rows) by y-coordinate.
    3. Find header row, use its word positions as column anchors.
    4. Assign each word to nearest anchor -> forms columns.
    5. Filter out non-table rows (sparse rows, long prose text, etc).
    """
    words = page.extract_words(keep_blank_chars=False, x_tolerance=3, y_tolerance=3)
    if not words:
        return []

    # group into rows
    rows: list[list[dict[str, Any]]] = []
    for w in words:
        placed = False
        for row in rows:
            if abs(w["top"] - row[0]["top"]) <= 4:
                row.append(w)
                placed = True
                break
        if not placed:
            rows.append([w])

    # sort left-to-right within rows, top-to-bottom overall
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    rows.sort(key=lambda r: r[0]["top"])

    if len(rows) < 3:
        return []

    # skip rows above title line if there's one
    for i, row in enumerate(rows):
        line = " ".join(w["text"] for w in row).strip()
        if re.match(r"^Table\s+\S+.*:", line, re.IGNORECASE):
            rows = rows[i + 1 :]
            break

    if len(rows) < 3:
        return []

    # find header row
    header_row_idx = None
    for i, row in enumerate(rows):
        if len(row) < 2:
            continue
        if i + 1 < len(rows) and (len(row) >= len(rows[i + 1]) or len(row) >= 2):
            header_row_idx = i
            break

    if header_row_idx is None:
        return []

    # merge close words in header (e.g. "Invoice" + "#" -> "Invoice #")
    header_words: list[dict[str, Any]] = []
    i = 0
    while i < len(rows[header_row_idx]):
        w = dict(rows[header_row_idx][i])
        while i + 1 < len(rows[header_row_idx]) and rows[header_row_idx][i + 1]["x0"] - w["x1"] <= 12:
            w["text"] = w["text"] + " " + rows[header_row_idx][i + 1]["text"]
            w["x1"] = rows[header_row_idx][i + 1]["x1"]
            i += 1
        header_words.append(w)
        i += 1

    col_anchors = [(w["x0"] + w["x1"]) / 2 for w in header_words]
    n_cols = len(col_anchors)
    if n_cols < 2:
        return []

    header = [w["text"] for w in header_words]

    # header shouldn't be a bullet marker sitting alone
    for cell in header:
        if len(cell.strip()) <= 1 and not cell.strip().isalnum():
            return []

    # assign data words to columns
    candidate_rows: list[tuple[list[str], float]] = []
    for row_words in rows[header_row_idx + 1:]:
        cells: list[str] = [""] * n_cols
        for w in row_words:
            cx = (w["x0"] + w["x1"]) / 2
            best_idx = -1
            best_dist = float("inf")
            for j, anchor in enumerate(col_anchors):
                dist = abs(cx - anchor)
                if dist < best_dist:
                    best_dist = dist
                    best_idx = j
            if best_idx >= 0 and best_dist <= 70:
                txt = w["text"]
                cells[best_idx] = (cells[best_idx] + " " + txt).strip()
        y = row_words[0]["top"] if row_words else 0
        candidate_rows.append((cells, y))

    # trim footer by detecting big vertical gaps
    if len(candidate_rows) >= 3:
        gaps = [candidate_rows[i][1] - candidate_rows[i - 1][1] for i in range(1, len(candidate_rows))]
        median_gap = sorted(gaps)[len(gaps) // 2]
        if median_gap > 0:
            threshold = median_gap * 2.0
            for i, g in enumerate(gaps):
                if g > threshold:
                    candidate_rows = candidate_rows[:i + 1]
                    break

    # check font height consistency - tables have uniform body text size
    # titles/headings vary line to line
    span = [rows[header_row_idx]] + [
        row_words for row_words in rows[header_row_idx + 1: header_row_idx + 1 + len(candidate_rows)]
    ]
    heights = [w["bottom"] - w["top"] for row in span for w in row]
    if heights:
        median_h = sorted(heights)[len(heights) // 2]
        if median_h > 0:
            spread = (max(heights) - min(heights)) / median_h
            if spread > 0.35:
                return []

    # structural filters - not keyword based
    data: list[list[str]] = []
    for row, _ in candidate_rows:
        non_empty = sum(1 for c in row if c.strip())
        total_text = " ".join(c for c in row if c.strip())
        if non_empty == 0:
            continue
        # skip sparse rows that look like narrative prose
        if non_empty <= 2 and len(total_text) > 60:
            continue
        # skip separator lines
        if all(set(c) <= {"-", "=", "|", " ", ""} for c in row if c):
            continue
        data.append(row)

    if len(data) < 1:
        return []

    # check cell lengths - table cells are short and atomic
    # wrapped sentences are long
    all_words_in_cells = [len(c.split()) for r in data for c in r if c.strip()]
    if all_words_in_cells:
        if max(all_words_in_cells) > 8:
            return []
        avg_words = sum(all_words_in_cells) / len(all_words_in_cells)
        if avg_words > 2.5:
            return []

    region = TableRegion(title="", columns=header, rows=data, page=page.page_number)
    if not _is_valid(region):
        return []

    return [region]


def _is_valid(t: TableRegion) -> bool:
    """Basic sanity check - enough columns and rows, cells are filled enough."""
    if t.ncols < 2 or t.nrows < 1:
        return False
    total_cells = t.nrows * t.ncols
    filled = sum(1 for r in t.rows for c in r if c.strip())
    if total_cells > 0 and filled / total_cells < 0.15:
        return False
    return True


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------

def _get_title(page: pdfplumber.page.Page, table: TableRegion) -> str:
    """Look for a 'Table X: ...' line above the table on this page."""
    if table.title:
        return table.title

    text = page.extract_text() or ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.search(r"^Table\s+\S+.*:", line, re.IGNORECASE):
            return line
    return ""


# ---------------------------------------------------------------------------
# Multi-page merging
# ---------------------------------------------------------------------------

def _merge_pages(regions: list[TableRegion]) -> list[TableRegion]:
    """Merge tables that span multiple pages into single logical tables."""
    if not regions:
        return []

    regions = sorted(regions, key=lambda r: r.page)
    merged: list[TableRegion] = []

    for r in regions:
        if not merged:
            merged.append(r)
            continue

        prev = merged[-1]

        # same number of columns?
        if prev.ncols != r.ncols:
            merged.append(r)
            continue

        # column headers similar enough?
        def col_sig(t: TableRegion) -> str:
            return "|".join(c.strip().lower() for c in t.columns if c.strip())

        parts_a = [p for p in col_sig(prev).split("|") if p]
        parts_b = [p for p in col_sig(r).split("|") if p]
        if parts_a and parts_b:
            matches = sum(1 for x, y in zip(parts_a, parts_b) if x == y)
            similar = matches / max(len(parts_a), len(parts_b)) >= 0.6
        else:
            similar = False

        if not similar:
            merged.append(r)
            continue

        # if first row of new table repeats the header, skip it
        if prev.rows and r.rows:
            if len(r.rows[0]) == len(prev.columns):
                hdr_matches = sum(
                    1 for a, b in zip(r.rows[0], prev.columns)
                    if a.strip().lower() == b.strip().lower()
                )
                if hdr_matches / len(prev.columns) >= 0.7:
                    r.rows = r.rows[1:]

        # all good - extend
        prev.rows.extend(r.rows)
        prev.page_end = r.page
        merged[-1] = prev

    return merged