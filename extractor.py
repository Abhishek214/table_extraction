"""
Generalized table extraction from PDF documents.

Approach — layout-agnostic, zero hardcoded keywords:

1. Bordered tables   → pdfplumber default (uses lines/rects geometry).
2. Borderless tables → word-position clustering (x-alignment of words across rows).
3. Multi-page tables → merge by matching column signatures and title patterns.

No hardcoded header keywords, no common-word lists, no footer filters.
All decisions are geometric or structural.
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
    """A detected table with its metadata."""

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
        """Column signature for matching tables across pages."""
        return "|".join(c.strip().lower() for c in self.columns if c.strip())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_tables(pdf_path: str) -> list[TableRegion]:
    """Extract all tables from a PDF file.

    Strategy per page:
        1. Try pdfplumber default (bordered tables).
        2. If nothing found → word-position clustering (borderless tables).
    Post-processing:
        3. Merge tables that continue across pages.
    """
    regions: list[TableRegion] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_tables = _extract_bordered(page) or _extract_borderless(page)
            for t in page_tables:
                t.title = _pick_title(page, t)
            regions.extend(page_tables)

    merged = _merge_across_pages(regions)
    logger.info("Extracted %d table(s) from %s", len(merged), pdf_path)
    return merged


# ---------------------------------------------------------------------------
# Strategy 1 — Bordered tables (pdfplumber default)
# ---------------------------------------------------------------------------

def _extract_bordered(page: pdfplumber.page.Page) -> list[TableRegion]:
    """Use pdfplumber's line/rectangle based extraction."""
    found: list[TableRegion] = []
    for raw in page.extract_tables() or []:
        region = _plumb_table_to_region(raw, page.page_number)
        if region and _is_valid(region):
            found.append(region)
    return found


def _plumb_table_to_region(
    raw: list[list[Any]], page_num: int
) -> TableRegion | None:
    """Convert pdfplumber table list-of-lists → TableRegion."""
    # Drop completely empty rows
    rows = [
        [str(cell).strip() if cell is not None else "" for cell in row]
        for row in raw
        if any(cell is not None and str(cell).strip() for cell in row)
    ]
    if len(rows) < 2:
        return None

    # Heuristic: if row 0 has far fewer cells than row 1, row 0 is a title.
    if len(rows) > 1 and len(rows[0]) < len(rows[1]) * 0.6:
        title = " ".join(c for c in rows[0] if c)
        header = rows[1]
        data = rows[2:]
    else:
        title = ""
        header = rows[0]
        data = rows[1:]

    data = _pad_rows(data, len(header))
    if not data:
        return None

    return TableRegion(title=title, columns=header, rows=data, page=page_num)


# ---------------------------------------------------------------------------
# Strategy 2 — Borderless tables (word-position clustering)
# ---------------------------------------------------------------------------

def _extract_borderless(page: pdfplumber.page.Page) -> list[TableRegion]:
    """
    Detect borderless tables by using the header row as column anchors.

    Algorithm:
        1. Find table title → restrict word set to region below it.
        2. Group remaining words into rows by y-coordinate.
        3. First non-noise row = header. Merge close header words (e.g.
           "Invoice" + "#") into single column headers.
        4. Use header word centers as column anchors.
        5. Assign each data word to the nearest column anchor.
        6. Filter structurally invalid rows.
    """
    words = page.extract_words(
        keep_blank_chars=False,
        x_tolerance=3,
        y_tolerance=3,
    )
    if not words:
        return []

    # 1. Restrict to table region (below title if one exists)
    table_words = _restrict_to_table_region(words)
    if len(table_words) < 4:
        return []

    # 2. Group words into rows by y-coordinate
    rows = _words_to_rows(table_words)
    if len(rows) < 3:
        return []

    # 3. Identify header row and derive column anchors from it
    header_row_idx = _find_header_row(rows)
    if header_row_idx is None:
        return []

    header_words = _merge_close_words(rows[header_row_idx], gap_threshold=12)
    col_anchors = [_word_center(w) for w in header_words]
    n_cols = len(col_anchors)
    if n_cols < 2:
        return []

    header = [w["text"] for w in header_words]

    # 4. Assign data rows to columns using nearest-anchor matching
    candidate_rows: list[tuple[list[str], float]] = []
    for row_words in rows[header_row_idx + 1 :]:
        row = _assign_words_to_anchors(row_words, col_anchors, max_dist=70)
        y = row_words[0]["top"] if row_words else 0
        candidate_rows.append((row, y))

    # 5. Trim footer by detecting abnormal row spacing
    candidate_rows = _trim_footer_by_spacing(candidate_rows)

    # 6. Structural filters — NOT keyword-based
    data: list[list[str]] = []
    for row, _ in candidate_rows:
        non_empty = sum(1 for c in row if c.strip())
        total_text = " ".join(c for c in row if c.strip())
        if non_empty == 0:
            continue
        if non_empty <= 2 and len(total_text) > 60:
            continue  # narrative text row
        if _is_separator_line(row):
            continue
        data.append(row)

    if len(data) < 1:
        return []

    return [TableRegion(title="", columns=header, rows=data, page=page.page_number)]


def _restrict_to_table_region(
    words: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Keep only words that fall inside the likely table region.
    If a title pattern ("Table X: ...") is found, only words below it are kept.
    Otherwise all words are returned (the caller will try its best).
    """
    # Look for title pattern in the full word list
    text = " ".join(w["text"] for w in words)
    title_match = re.search(r"Table\s+\S+.*?:", text, re.IGNORECASE)
    if not title_match:
        return words

    title_text = title_match.group(0)
    # Find the y-position of the title words
    title_words = []
    remaining = list(words)
    for part in title_text.split():
        for i, w in enumerate(remaining):
            if w["text"].rstrip(":") == part.rstrip(":"):
                title_words.append(w)
                remaining = remaining[i + 1 :]
                break

    if not title_words:
        return words

    title_bottom = max(w["bottom"] for w in title_words)
    # Keep words that are clearly below the title
    return [w for w in words if w["top"] >= title_bottom - 2]


def _words_to_rows(
    words: list[dict[str, Any]], y_tol: float = 4
) -> list[list[dict[str, Any]]]:
    """Cluster words into rows based on vertical proximity."""
    rows: list[list[dict[str, Any]]] = []
    for w in words:
        placed = False
        for row in rows:
            if abs(w["top"] - row[0]["top"]) <= y_tol:
                row.append(w)
                placed = True
                break
        if not placed:
            rows.append([w])

    # Sort words left-to-right within each row; sort rows top-to-bottom
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    rows.sort(key=lambda r: r[0]["top"])
    return rows


def _find_header_row(rows: list[list[dict[str, Any]]]) -> int | None:
    """
    Identify the header row among word-rows.
    Heuristic: the first row with a consistent word-count that is followed
    by rows of similar or greater word density. Returns the row index or None.
    """
    for i, row in enumerate(rows):
        # Skip very sparse rows (likely noise above the table)
        non_empty_words = len(row)
        if non_empty_words < 2:
            continue
        # Header row should have at least as many words as the average data row
        if i + 1 < len(rows):
            next_row_len = len(rows[i + 1])
            if non_empty_words >= next_row_len or non_empty_words >= 2:
                return i
    return None


def _merge_close_words(
    words: list[dict[str, Any]], gap_threshold: float = 12
) -> list[dict[str, Any]]:
    """
    Merge adjacent words that are very close horizontally.
    E.g. 'Invoice' followed 3 px later by '#' → single word 'Invoice #'.
    """
    if not words:
        return words

    merged: list[dict[str, Any]] = []
    i = 0
    while i < len(words):
        w = dict(words[i])  # shallow copy
        # Absorb subsequent words that are within gap_threshold
        while (
            i + 1 < len(words)
            and words[i + 1]["x0"] - w["x1"] <= gap_threshold
        ):
            w["text"] = w["text"] + " " + words[i + 1]["text"]
            w["x1"] = words[i + 1]["x1"]
            i += 1
        merged.append(w)
        i += 1

    return merged


def _word_center(w: dict[str, Any]) -> float:
    return (w["x0"] + w["x1"]) / 2


def _assign_words_to_anchors(
    words: list[dict[str, Any]],
    anchors: list[float],
    max_dist: float = 70,
) -> list[str]:
    """
    Assign each word to the nearest column anchor.
    Words farther than max_dist from any anchor are ignored.
    """
    cells: list[str] = [""] * len(anchors)
    for w in words:
        cx = _word_center(w)
        best_idx = -1
        best_dist = float("inf")
        for i, anchor in enumerate(anchors):
            dist = abs(cx - anchor)
            if dist < best_dist:
                best_dist = dist
                best_idx = i

        if best_idx >= 0 and best_dist <= max_dist:
            txt = w["text"]
            if cells[best_idx]:
                cells[best_idx] += " " + txt
            else:
                cells[best_idx] = txt

    return cells


def _trim_footer_by_spacing(
    rows: list[tuple[list[str], float]],
    multiplier: float = 2.0,
) -> list[tuple[list[str], float]]:
    """
    Remove rows that appear below a large vertical gap — indicative of
    footer / narrative text that is not part of the table.

    Parameters
    ----------
    rows: list of (row_cells, y_position) tuples.
    multiplier: a gap larger than multiplier × median_spacing triggers trim.

    Returns
    -------
    Filtered list of rows up to (but not including) the first abnormal gap.
    """
    if len(rows) < 3:
        return rows

    gaps = [rows[i][1] - rows[i - 1][1] for i in range(1, len(rows))]
    median_gap = sorted(gaps)[len(gaps) // 2]
    if median_gap <= 0:
        return rows

    threshold = median_gap * multiplier
    for i, g in enumerate(gaps):
        if g > threshold:
            return rows[: i + 1]

    return rows


def _is_separator_line(row: list[str]) -> bool:
    """Check if a row consists only of dashes, equals, pipes, and spaces."""
    return all(set(c) <= {"-", "=", "|", " ", ""} for c in row if c)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pad_rows(rows: list[list[str]], n_cols: int) -> list[list[str]]:
    """Ensure every row has exactly n_cols cells."""
    result: list[list[str]] = []
    for row in rows:
        if len(row) < n_cols:
            row = row + [""] * (n_cols - len(row))
        elif len(row) > n_cols:
            row = row[:n_cols]
        result.append(row)
    return result


def _is_valid(t: TableRegion) -> bool:
    """Structural validation — is this actually a table?"""
    if t.ncols < 2 or t.nrows < 1:
        return False

    # Check fill ratio — tables should have reasonably populated cells
    total_cells = t.nrows * t.ncols
    filled = sum(1 for r in t.rows for c in r if c.strip())
    if total_cells > 0 and filled / total_cells < 0.15:
        return False

    return True


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------

def _pick_title(page: pdfplumber.page.Page, table: TableRegion) -> str:
    """Look for a title line above the table in the page text."""
    if table.title:
        return table.title

    text = page.extract_text() or ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Pattern: "Table X: ..." or "Table X (something): ..."
        if re.search(r"^Table\s+\S+.*:", line, re.IGNORECASE):
            return line
    return ""


# ---------------------------------------------------------------------------
# Multi-page merging
# ---------------------------------------------------------------------------

def _merge_across_pages(regions: list[TableRegion]) -> list[TableRegion]:
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

        if _should_merge(prev, r):
            prev.rows.extend(r.rows)
            prev.page_end = r.page
        else:
            merged.append(r)

    return merged


def _should_merge(a: TableRegion, b: TableRegion) -> bool:
    """Decide whether table `b` is a continuation of table `a`."""
    # Must be adjacent or same-page (shouldn't happen after sort, but safety)
    if b.page <= a.page:
        return False

    # Column count must match
    if a.ncols != b.ncols:
        return False

    # Column signatures must be similar
    if not _signatures_match(a.signature(), b.signature()):
        return False

    # If b's first row repeats a's header, it's a continuation header — skip it
    if a.rows and b.rows and _is_header_row(b.rows[0], a.columns):
        b.rows = b.rows[1:]

    return True


def _signatures_match(sig_a: str, sig_b: str, threshold: float = 0.6) -> bool:
    """Compare two column signatures for similarity."""
    parts_a = [p for p in sig_a.split("|") if p]
    parts_b = [p for p in sig_b.split("|") if p]
    if not parts_a or not parts_b:
        return False
    matches = sum(1 for x, y in zip(parts_a, parts_b) if x == y)
    return matches / max(len(parts_a), len(parts_b)) >= threshold


def _is_header_row(row: list[str], header: list[str], threshold: float = 0.7) -> bool:
    """Check if a row is a repeated header."""
    if len(row) != len(header):
        return False
    matches = sum(
        1 for a, b in zip(row, header) if a.strip().lower() == b.strip().lower()
    )
    return matches / len(header) >= threshold
