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
    """Use pdfplumber's line/rectangle based extraction.

    pdfplumber's grid-finder treats any rects/lines as candidate table
    borders. Decorative page furniture -- full-width accent bars, header/
    footer stripes, background logo curves -- forms a grid too, and gets
    reported as a "table" spanning (or exceeding) the whole page. A real
    table occupies a bounded sub-region of the page's content area, so we
    use the found table's bbox relative to the page as a structural filter
    before ever looking at the cell text.
    """
    found: list[TableRegion] = []
    for table in page.find_tables():
        if not _is_plausible_table_region(table, page):
            continue
        raw = table.extract()
        region = _plumb_table_to_region(raw, page.page_number)
        if region and _is_valid(region):
            found.append(region)
    return found


def _is_plausible_table_region(
    table: "pdfplumber.table.Table",
    page: pdfplumber.page.Page,
    max_area_ratio: float = 0.6,
) -> bool:
    """
    Reject grids formed by decorative page furniture rather than an actual
    table: such grids span nearly the entire page (often bleeding past the
    page's own bounding box, since background shapes are drawn edge-to-edge
    or larger). A genuine table occupies a bounded region of page content.
    """
    x0, top, x1, bottom = table.bbox
    page_area = page.width * page.height
    if page_area <= 0:
        return False

    # Bbox extending outside the page's own bounds is a strong signal that
    # this "grid" came from background/decorative geometry, not table rules.
    if x0 < page.bbox[0] - 1 or top < page.bbox[1] - 1:
        return False
    if x1 > page.bbox[2] + 1 or bottom > page.bbox[3] + 1:
        return False

    area_ratio = (x1 - x0) * (bottom - top) / page_area
    if area_ratio > max_area_ratio:
        return False

    return True


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
        1. Group words into rows (physical lines) by y-coordinate.
        2. Find a table title line → restrict to rows below it, if present.
        3. First non-noise row = header. Merge close header words (e.g.
           "Invoice" + "#") into single column headers.
        4. Use header word centers as column anchors.
        5. Assign each data word to the nearest column anchor.
        6. Filter structurally invalid rows and reject content that reads
           like prose/bullets/code rather than tabular data.
    """
    words = page.extract_words(
        keep_blank_chars=False,
        x_tolerance=3,
        y_tolerance=3,
    )
    if not words:
        return []

    # 1. Group words into rows (physical lines) by y-coordinate
    rows = _words_to_rows(words)
    if len(rows) < 3:
        return []

    # 2. Restrict to table region (below title line if one exists)
    rows = _restrict_to_table_region(rows)
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
    if not _looks_like_header(header):
        return []

    # 4. Assign data rows to columns using nearest-anchor matching
    candidate_rows: list[tuple[list[str], float]] = []
    for row_words in rows[header_row_idx + 1 :]:
        row = _assign_words_to_anchors(row_words, col_anchors, max_dist=70)
        y = row_words[0]["top"] if row_words else 0
        candidate_rows.append((row, y))

    # 5. Trim footer by detecting abnormal row spacing
    candidate_rows = _trim_footer_by_spacing(candidate_rows)

    # Reject candidates whose "rows" are really titles/headings that just
    # happen to x-align across a couple of lines. Genuine tabular data is
    # set in one uniform body-text size with regular line pitch; headings
    # and cover-page text mix large/varying font sizes. This is a font-size
    # consistency check, not a keyword rule, so it generalizes across
    # domains and catches false positives structurally. Checked on the
    # rows that actually survive footer-trimming, so a stray oversized
    # footer heading (trimmed anyway) doesn't cause a false rejection.
    row_span = [rows[header_row_idx]] + [
        row_words
        for row_words in rows[header_row_idx + 1 : header_row_idx + 1 + len(candidate_rows)]
    ]
    if not _has_uniform_text_size(row_span):
        return []

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

    # 7. Reject if the cell content reads like prose/bullets/code rather
    #    than short atomic table values (dates, names, amounts, statuses).
    if not _looks_tabular([header] + data):
        return []

    region = TableRegion(title="", columns=header, rows=data, page=page.page_number)
    if not _is_valid(region):
        return []

    return [region]


def _restrict_to_table_region(
    rows: list[list[dict[str, Any]]],
) -> list[list[dict[str, Any]]]:
    """
    Keep only rows that fall inside the likely table region.
    If a physical line matching a title pattern ("Table X: ...") is found,
    only rows below it are kept. Otherwise all rows are returned (the caller
    will try its best).

    Matching is done per physical line (not on the whole page joined into
    one string) so an incidental mention of the word "table" inside running
    prose — e.g. "... the Table Extraction Challenge ..." — can't be
    mistaken for a title and drag in everything below it on the page.
    """
    for i, row in enumerate(rows):
        line = " ".join(w["text"] for w in row).strip()
        if re.match(r"^Table\s+\S+.*:", line, re.IGNORECASE):
            return rows[i + 1 :]
    return rows


def _looks_like_header(header: list[str]) -> bool:
    """
    Reject header candidates that are structurally not column titles —
    e.g. a bullet marker ("•", "-", "*") standing alone as a "column".
    """
    for cell in header:
        text = cell.strip()
        if len(text) <= 1 and not text.isalnum():
            return False
    return True


def _looks_tabular(
    rows: list[list[str]],
    max_avg_words: float = 2.5,
    max_cell_words: int = 8,
) -> bool:
    """
    Distinguish genuine tabular cell values from running prose that happens
    to fall into whitespace-aligned buckets (e.g. wrapped bullet text, or
    "key": "value" lines from a JSON snippet).

    Real table cells — names, dates, amounts, short statuses/descriptions —
    are short and atomic. Wrapped sentences are not. This is a structural
    length check, not a keyword list, so it generalizes across domains.
    """
    word_counts = [len(c.split()) for r in rows for c in r if c.strip()]
    if not word_counts:
        return False
    if max(word_counts) > max_cell_words:
        return False
    if sum(word_counts) / len(word_counts) > max_avg_words:
        return False
    return True


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


def _has_uniform_text_size(
    rows: list[list[dict[str, Any]]],
    max_relative_spread: float = 0.35,
) -> bool:
    """
    Real tabular data is set in a single uniform body-text font size.
    Headings, titles, and cover-page copy vary font size line to line
    (a large title, then smaller subtitles/labels). Measure the spread of
    word heights across the candidate rows; a wide spread means this is
    heterogeneous free-form text, not a table.
    """
    heights = [w["bottom"] - w["top"] for row in rows for w in row]
    if not heights:
        return False
    median_h = sorted(heights)[len(heights) // 2]
    if median_h <= 0:
        return False
    max_h = max(heights)
    min_h = min(heights)
    relative_spread = (max_h - min_h) / median_h
    return relative_spread <= max_relative_spread


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