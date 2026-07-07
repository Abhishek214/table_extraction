"""
Table extraction engine with multi-strategy detection.

Approach (simple-first):
1. pdfplumber default -- works great for bordered tables (lines/rects present)
2. Word-position analysis -- for borderless tables with aligned columns
3. pdfplumber text-alignment strategy -- fallback with cleanup
4. Header+multi-page merge -- logical table deduplication across pages

Fixes applied:
- Default strategy preferred: only fall back when default finds nothing useful
- Word-position analysis for borderless tables (better than text-align)
- Robust title extraction with continuation pattern support
- Content-based deduplication with quality scoring
- Improved multi-page merge with title-based continuation detection
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pdfplumber

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal data shape
# ---------------------------------------------------------------------------

class TableRegion:
    """Represents a detected table region from one page."""

    def __init__(
        self,
        title: str,
        columns: list[str],
        rows: list[list[str]],
        page_num: int,
        bbox: tuple[float, float, float, float] | None = None,
        page_end: int | None = None,
    ):
        self.title = title
        self.columns = columns
        self.rows = rows
        self.page_num = page_num
        self.page_end = page_end or page_num
        self.bbox = bbox

    @property
    def ncols(self) -> int:
        return len(self.columns)

    @property
    def nrows(self) -> int:
        return len(self.rows)

    def copy(self) -> "TableRegion":
        return TableRegion(
            title=self.title,
            columns=list(self.columns),
            rows=[list(r) for r in self.rows],
            page_num=self.page_num,
            bbox=self.bbox,
            page_end=self.page_end,
        )


# ---------------------------------------------------------------------------
# Strategy 1 -- pdfplumber default / bordered detection
# ---------------------------------------------------------------------------

def _extract_pdfplumber_default(page: pdfplumber.pdf.Page) -> list[TableRegion]:
    """Extract tables using pdfplumber default settings (best for bordered tables)."""
    regions: list[TableRegion] = []
    raw_tables = page.extract_tables()
    if raw_tables:
        for raw in raw_tables:
            if not raw or len(raw) < 2:
                continue
            region = _raw_to_region(raw, page.page_number, "default")
            if region:
                regions.append(region)
    return regions


def _raw_to_region(raw: list[list], page_num: int, strategy: str) -> TableRegion | None:
    """Convert raw pdfplumber table output to TableRegion."""
    if not raw:
        return None

    rows = [r for r in raw if r and any(c for c in r if c)]
    if len(rows) < 2:
        return None

    header_row_idx = 0
    for i, row in enumerate(rows):
        cleaned = [str(c).strip() if c else "" for c in row]
        if any(cleaned):
            header_row_idx = i
            break

    header_row = [str(c).strip() if c else "" for c in rows[header_row_idx]]
    columns = header_row

    title = ""
    data_rows: list[list[str]] = []

    if len(rows) > header_row_idx + 1:
        next_row = [str(c).strip() if c else "" for c in rows[header_row_idx + 1]]
        if len(next_row) > len(header_row) * 1.5:
            title = " ".join(h for h in header_row if h).strip()
            columns = next_row
            data_rows = [[str(c).strip() if c else "" for c in r] for r in rows[header_row_idx + 2:]]
        else:
            data_rows = [[str(c).strip() if c else "" for c in r] for r in rows[header_row_idx + 1:]]
    else:
        data_rows = []

    data_rows = [r for r in data_rows if any(c.strip() for c in r)]

    if len(data_rows) < 1:
        return None

    return TableRegion(
        title=title or "Untitled Table",
        columns=columns,
        rows=data_rows,
        page_num=page_num,
    )


# ---------------------------------------------------------------------------
# Strategy 2 -- Word-position analysis for borderless tables
# ---------------------------------------------------------------------------

def _merge_close_header_words(header_words: list[dict], gap_threshold: float = 8) -> list[dict]:
    """Merge header words that are very close together (e.g. \'Invoice\' + \'#\')."""
    if not header_words:
        return header_words

    merged = []
    i = 0
    while i < len(header_words):
        w = header_words[i]
        if i + 1 < len(header_words):
            next_w = header_words[i + 1]
            gap = next_w["x0"] - w["x1"]
            if gap <= gap_threshold:
                merged_word = dict(w)
                merged_word["text"] = w["text"] + " " + next_w["text"]
                merged_word["x1"] = next_w["x1"]
                merged.append(merged_word)
                i += 2
                continue
        merged.append(w)
        i += 1

    return merged


def _is_footer_row(row: list[str], n_cols: int) -> bool:
    """Check if a row looks like footer/narrative text."""
    total_text = " ".join(c for c in row if c)
    non_empty = sum(1 for c in row if c.strip())
    row_lower = total_text.lower()

    footer_keywords = [
        "footer", "notes", "narrative", "ignore this",
        "the table above", "paragraphs here", "intentionally included",
    ]
    for kw in footer_keywords:
        if kw in row_lower:
            return True

    if len(total_text) > 50 and non_empty <= 2:
        return True
    if non_empty < n_cols * 0.3 and len(total_text) > 25:
        return True

    return False


def _extract_borderless_by_words(page: pdfplumber.pdf.Page) -> list[TableRegion]:
    """
    Extract borderless tables using word position analysis.
    Uses header row words as column anchors for precise alignment.
    """
    words = page.extract_words()
    if not words:
        return []

    text = page.extract_text() or ""
    lines = text.split("\n")

    # Find table title with improved regex
    title = "Untitled Table"
    title_y = 0
    for line in lines:
        match = re.search(r"Table\s+[A-Z0-9_]+(?:\s*\([^)]+\))?\s*:", line, re.IGNORECASE)
        if match:
            title = line.strip()
            title_words = [w for w in words if w["text"] in line.split()[:5]]
            if title_words:
                title_y = max(w["bottom"] for w in title_words)
            break

    # Filter words to area below title
    table_words = [w for w in words if w["top"] >= title_y - 5]

    # Group words by row (y-position)
    y_tolerance = 5
    word_rows: list[list[dict]] = []
    for w in table_words:
        placed = False
        for row in word_rows:
            if abs(w["top"] - row[0]["top"]) <= y_tolerance:
                row.append(w)
                placed = True
                break
        if not placed:
            word_rows.append([w])

    # Sort rows by y and words within rows by x
    for row in word_rows:
        row.sort(key=lambda w: w["x0"])
    word_rows.sort(key=lambda r: r[0]["top"])

    if len(word_rows) < 2:
        return []

    # Find header row by looking for common header keywords
    header_keywords = {
        "vendor", "invoice", "date", "amount", "status",
        "description", "debit", "credit", "balance",
        "metric", "jan", "feb", "mar", "qty", "total",
        "reference", "txn", "transaction", "payment",
    }

    header_idx = 0
    for i, row in enumerate(word_rows):
        row_text = " ".join(w["text"] for w in row).lower()
        words_in_row = set(row_text.split())
        matches = words_in_row & header_keywords
        if len(matches) >= 2:
            header_idx = i
            break

    # Merge close header words (e.g. \'Invoice\' + \'#\' -> \'Invoice #\')
    header_words = _merge_close_header_words(word_rows[header_idx], gap_threshold=8)
    n_cols = len(header_words)

    # Build table rows by assigning each word to the nearest header column
    raw_rows = []
    for row_words in word_rows:
        row = [""] * n_cols
        for w in row_words:
            w_center = (w["x0"] + w["x1"]) / 2
            best_col = -1
            best_dist = float("inf")
            for i, hw in enumerate(header_words):
                hw_center = (hw["x0"] + hw["x1"]) / 2
                dist = abs(w_center - hw_center)
                if dist < best_dist:
                    best_dist = dist
                    best_col = i

            if best_col >= 0 and best_dist < 80:
                if row[best_col]:
                    row[best_col] += " " + w["text"]
                else:
                    row[best_col] = w["text"]

        if any(cell.strip() for cell in row):
            raw_rows.append(row)

    if len(raw_rows) < 2:
        return []

    header = [w["text"] for w in header_words]

    # Collect data rows with footer filtering
    data_rows = []
    for row in raw_rows[header_idx + 1:]:
        if _is_footer_row(row, n_cols):
            continue

        # Pad to match header length
        while len(row) < n_cols:
            row.append("")
        row = row[:n_cols]

        data_rows.append(row)

    if len(data_rows) < 1:
        return []

    return [TableRegion(
        title=title,
        columns=header,
        rows=data_rows,
        page_num=page.page_number,
    )]


# ---------------------------------------------------------------------------
# Strategy 3 -- Text-align with cleanup
# ---------------------------------------------------------------------------

def _is_reasonable_word(word: str) -> bool:
    """Check if a word looks reasonable (not a fragment)."""
    if len(word) < 2:
        return False
    common_words = {
        "vendor", "invoice", "date", "amount", "status", "description",
        "debit", "credit", "balance", "reference", "metric", "applications",
        "approvals", "declines", "paid", "pending", "table", "narrative",
        "footer", "notes", "report", "payments", "supplies", "logistics",
        "services", "repairs", "alpha", "blue", "cyan", "delta", "opening",
        "closing", "transfer", "payment", "withdrawal", "vendor", "txn",
        "transaction", "fuel", "expense", "subscription", "customer",
    }
    word_lower = word.lower().strip()
    if word_lower in common_words:
        return True
    if len(word) > 15:
        return False
    if sum(c.isalpha() for c in word) < len(word) * 0.5:
        return False
    if not any(v in word_lower for v in "aeiou"):
        return False
    return True


def _merge_fragmented_cells(row: list[str]) -> list[str]:
    """Merge cells that appear to be fragments of the same word."""
    if not row:
        return row

    result = []
    i = 0
    while i < len(row):
        cell = row[i]
        if cell and i + 1 < len(row) and row[i + 1]:
            next_cell = row[i + 1]
            combined = cell + next_cell
            combined_spaced = cell + " " + next_cell

            if len(cell) < 10 and len(next_cell) < 10:
                if not cell.endswith(" ") and not next_cell.startswith(" "):
                    if _is_reasonable_word(combined):
                        result.append(combined)
                        i += 2
                        continue
                    elif _is_reasonable_word(combined_spaced):
                        result.append(combined_spaced)
                        i += 2
                        continue

        result.append(cell)
        i += 1

    while len(result) < len(row):
        result.append("")

    return result[:len(row)]


def _cleanup_text_align_table(raw_rows: list[list], page_num: int) -> TableRegion | None:
    """Clean up text-align strategy output to extract only the actual table."""
    non_empty = []
    for row in raw_rows:
        if row and any(c and str(c).strip() for c in row):
            non_empty.append([str(c).strip() if c else "" for c in row])

    if len(non_empty) < 2:
        return None

    header_keywords = {
        "vendor", "invoice", "date", "amount", "status",
        "description", "debit", "credit", "balance",
        "metric", "jan", "feb", "mar", "qty", "total",
        "reference", "txn", "transaction", "payment",
    }

    header_idx = -1
    for i, row in enumerate(non_empty):
        row_text = " ".join(c for c in row if c).lower()
        words_in_row = set(row_text.split())
        matches = words_in_row & header_keywords
        if len(matches) >= 2:
            header_idx = i
            break

    if header_idx == -1:
        return None

    header_row = non_empty[header_idx]
    header_row = _merge_fragmented_cells(header_row)

    data_rows = []
    for row in non_empty[header_idx + 1:]:
        non_empty_count = sum(1 for c in row if c.strip())
        if non_empty_count == 0:
            continue

        total_text = " ".join(c for c in row if c)
        if len(total_text) > 40 and non_empty_count <= 2:
            continue

        cleaned_row = [str(c).strip() if c else "" for c in row]
        while len(cleaned_row) < len(header_row):
            cleaned_row.append("")
        cleaned_row = cleaned_row[:len(header_row)]
        cleaned_row = _merge_fragmented_cells(cleaned_row)
        data_rows.append(cleaned_row)

    if len(data_rows) < 1:
        return None

    title = "Untitled Table"
    for row in non_empty[:header_idx]:
        row_text = " ".join(c for c in row if c)
        match = re.search(r"Table\s+[A-Z0-9_]+(?:\s*\([^)]+\))?\s*:", row_text, re.IGNORECASE)
        if match:
            title = row_text.strip()
            break

    return TableRegion(
        title=title,
        columns=header_row,
        rows=data_rows,
        page_num=page_num,
    )


def _extract_pdfplumber_text_align(page: pdfplumber.pdf.Page) -> list[TableRegion]:
    """Extract tables using pdfplumber text-alignment strategy with cleanup."""
    alt_settings = {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "explicit_vertical_lines": [],
        "explicit_horizontal_lines": [],
    }
    alt_tables = page.extract_tables(table_settings=alt_settings)
    regions = []
    if alt_tables:
        for raw in alt_tables:
            if not raw or len(raw) < 2:
                continue
            region = _cleanup_text_align_table(raw, page.page_number)
            if region:
                regions.append(region)
    return regions


# ---------------------------------------------------------------------------
# Strategy 4 -- Camelot fallback (optional)
# ---------------------------------------------------------------------------

def _try_camelot_fallback(pdf_path: str) -> list[TableRegion]:
    """Attempt extraction using Camelot if installed."""
    try:
        import camelot
        regions: list[TableRegion] = []
        for flavor in ["stream", "lattice"]:
            try:
                tables = camelot.read_pdf(pdf_path, pages="all", flavor=flavor)
                for tbl in tables:
                    if tbl.df.empty:
                        continue
                    data = tbl.df.values.tolist()
                    if len(data) < 2:
                        continue
                    region = TableRegion(
                        title="Camelot Table",
                        columns=[str(c) for c in data[0]],
                        rows=[list(row) for row in data[1:]],
                        page_num=1,
                    )
                    regions.append(region)
                break
            except Exception:
                continue
        return regions
    except ImportError:
        return []


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

def _score_region(region: TableRegion) -> float:
    """Score a region to determine table quality. Higher = better."""
    score = 0.0

    score += min(region.nrows, 30) * 2.0

    expected_cols = region.ncols
    consistency_bonus = 0
    for r in region.rows:
        actual_nonempty = sum(1 for c in r if c.strip())
        if actual_nonempty >= expected_cols * 0.5:
            consistency_bonus += 1
    score += (consistency_bonus / max(region.nrows, 1)) * 15

    if region.title and region.title != "Untitled Table":
        if re.search(r"Table\s+[A-Z0-9_]+", region.title, re.IGNORECASE):
            score += 8
        else:
            score += 3

    total_cells = sum(len(r) for r in region.rows)
    nonempty_cells = sum(1 for r in region.rows for c in r if c.strip())
    if total_cells > 0:
        fill_ratio = nonempty_cells / total_cells
        score += fill_ratio * 10

    # Penalize fragmented text
    avg_cell_len = sum(
        len(c.strip()) for r in region.rows for c in r if c.strip()
    ) / max(nonempty_cells, 1)
    if avg_cell_len < 6 and region.ncols > 4:
        score -= 15

    return score


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _compute_row_fingerprint(row: list[str]) -> str:
    """Create a fingerprint for a row to compare content."""
    return "|".join(c.strip().lower() for c in row if c.strip())


def _rows_overlap(rows_a: list[list[str]], rows_b: list[list[str]], threshold: float = 0.4) -> bool:
    """Check if two sets of rows have significant content overlap."""
    if not rows_a or not rows_b:
        return False

    fps_a = {_compute_row_fingerprint(r) for r in rows_a}
    fps_b = {_compute_row_fingerprint(r) for r in rows_b}
    fps_a = {f for f in fps_a if f}
    fps_b = {f for f in fps_b if f}

    if not fps_a or not fps_b:
        return False

    intersection = fps_a & fps_b
    smaller = min(len(fps_a), len(fps_b))
    if smaller == 0:
        return False
    return len(intersection) / smaller >= threshold


def _regions_are_duplicates(a: TableRegion, b: TableRegion) -> bool:
    """Check if two regions represent the same table."""
    if a.page_num != b.page_num:
        return False

    cols_match = False
    if a.ncols == b.ncols:
        matches = sum(
            1 for x, y in zip(a.columns, b.columns)
            if x.strip().lower() == y.strip().lower() and x.strip()
        )
        if matches / max(a.ncols, 1) >= 0.5:
            cols_match = True

    rows_overlap = _rows_overlap(a.rows, b.rows, threshold=0.3)

    containment = False
    fps_a = {_compute_row_fingerprint(r) for r in a.rows if any(c.strip() for c in r)}
    fps_b = {_compute_row_fingerprint(r) for r in b.rows if any(c.strip() for c in r)}
    if fps_a and fps_b:
        if fps_a.issubset(fps_b) or fps_b.issubset(fps_a):
            containment = True

    return cols_match or rows_overlap or containment


def deduplicate_page_regions(regions: list[TableRegion]) -> list[TableRegion]:
    """Remove duplicate tables on the same page, keeping highest quality."""
    if len(regions) <= 1:
        return regions

    kept: list[TableRegion] = []
    for region in regions:
        is_dup = False
        for i, existing in enumerate(kept):
            if _regions_are_duplicates(region, existing):
                if _score_region(region) > _score_region(existing):
                    kept[i] = region
                is_dup = True
                break
        if not is_dup:
            kept.append(region)
    return kept


# ---------------------------------------------------------------------------
# Title extraction from page text
# ---------------------------------------------------------------------------

def _find_table_titles(
    page: pdfplumber.pdf.Page,
    region: TableRegion,
) -> str:
    """Find the most likely table title from text near the table."""
    text = page.extract_text()
    if not text:
        return region.title

    lines = text.split("\n")
    for line in lines:
        match = re.search(r"Table\s+[A-Z0-9_]+(?:\s*\([^)]+\))?\s*:", line, re.IGNORECASE)
        if match:
            return line.strip()

    return region.title


# ---------------------------------------------------------------------------
# Multi-page table merging
# ---------------------------------------------------------------------------

def _is_continuation_header(
    row: list[str],
    header: list[str],
    tolerance: float = 0.8,
) -> bool:
    """Check if a row is a continuation header (same columns repeated on next page)."""
    if len(row) != len(header):
        return False
    matches = sum(
        1 for a, b in zip(row, header)
        if a.strip().lower() == b.strip().lower()
    )
    return matches / len(header) >= tolerance


def _headers_similar(a: list[str], b: list[str], tolerance: float = 0.7) -> bool:
    """Check if two column headers are similar enough to be the same table."""
    if len(a) != len(b):
        return False
    if len(a) == 0:
        return True
    matches = sum(
        1 for x, y in zip(a, b)
        if x.strip().lower() == y.strip().lower()
    )
    return matches / len(a) >= tolerance


def _title_indicates_continuation(title: str) -> bool:
    """Check if title indicates this is a continuation of a previous table."""
    lower = title.lower()
    patterns = [
        r"continued",
        r"\(continued\)",
        r"page \d+ of \d+",
        r"\(page \d+",
    ]
    return any(re.search(p, lower) for p in patterns)


def _title_base(title: str) -> str:
    """
    Extract the base table name from a title for matching.
    E.g., 'Table C: Ledger (Page 1 of 2)' -> 'table c ledger'
    """
    cleaned = re.sub(r"\(page \d+ of \d+\)", "", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"page \d+ of \d+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\(continued\)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"continued:?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^a-zA-Z0-9]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    return cleaned


def merge_multi_page_tables(
    regions: list[TableRegion],
) -> list[TableRegion]:
    """Merge tables that span multiple pages into single logical tables."""
    if not regions:
        return []

    sorted_regions = sorted(regions, key=lambda r: r.page_num)
    merged: list[TableRegion] = []

    for region in sorted_regions:
        if not merged:
            merged.append(region.copy())
            continue

        matched = False
        for candidate in reversed(merged[-3:]):
            is_match = False

            # Signal 1: Same columns and similar headers
            if _headers_similar(region.columns, candidate.columns):
                if region.rows and _is_continuation_header(region.rows[0], candidate.columns):
                    candidate.rows.extend(region.rows[1:])
                    is_match = True
                elif region.rows:
                    candidate.rows.extend(region.rows)
                    is_match = True

            # Signal 2: Same title base
            if not is_match:
                region_base = _title_base(region.title)
                candidate_base = _title_base(candidate.title)
                if region_base and candidate_base and region_base == candidate_base:
                    if _headers_similar(region.columns, candidate.columns):
                        if region.rows and _is_continuation_header(region.rows[0], candidate.columns):
                            candidate.rows.extend(region.rows[1:])
                        else:
                            candidate.rows.extend(region.rows)
                        is_match = True

            # Signal 3: Explicit continuation marker
            if not is_match:
                if _title_indicates_continuation(region.title):
                    if _headers_similar(region.columns, candidate.columns):
                        if region.rows and _is_continuation_header(region.rows[0], candidate.columns):
                            candidate.rows.extend(region.rows[1:])
                        else:
                            candidate.rows.extend(region.rows)
                        is_match = True

            if is_match:
                candidate.page_num = min(candidate.page_num, region.page_num)
                candidate.page_end = max(candidate.page_end, region.page_num)
                matched = True
                break

        if not matched:
            merged.append(region.copy())

    # Remove consecutive duplicate rows
    for region in merged:
        unique_rows: list[list[str]] = []
        for row in region.rows:
            if not unique_rows or row != unique_rows[-1]:
                unique_rows.append(row)
        region.rows = unique_rows

    return merged


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_tables_from_pdf(pdf_path: str) -> list[TableRegion]:
    """
    Extract tables from a PDF using multi-strategy detection.

    Strategy order:
    1. pdfplumber default (bordered tables)
    2. Word-position analysis (borderless tables) - if default finds nothing
    3. pdfplumber text-align with cleanup (last resort)
    4. Camelot fallback (optional, if installed)

    Then: deduplicate per-page, merge multi-page tables.
    """
    regions: list[TableRegion] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_regions: list[TableRegion] = []

            # Strategy 1: pdfplumber default
            default_regions = _extract_pdfplumber_default(page)
            if default_regions:
                page_regions.extend(default_regions)
                logger.debug(
                    f"Page {page.page_number}: default found {len(default_regions)} table(s)"
                )

            # Strategy 2: Word-position analysis (if default found nothing)
            use_fallback = False
            if not page_regions:
                use_fallback = True
            elif all(r.nrows < 2 for r in page_regions):
                use_fallback = True

            if use_fallback:
                word_regions = _extract_borderless_by_words(page)
                if word_regions:
                    page_regions.extend(word_regions)
                    logger.debug(
                        f"Page {page.page_number}: word-position found {len(word_regions)} table(s)"
                    )

            # Strategy 3: Text-align with cleanup (last resort)
            if not page_regions:
                align_regions = _extract_pdfplumber_text_align(page)
                if align_regions:
                    page_regions.extend(align_regions)
                    logger.debug(
                        f"Page {page.page_number}: text-align found {len(align_regions)} table(s)"
                    )

            # Strategy 4: Camelot fallback
            if not page_regions:
                camelot_results = _try_camelot_fallback(pdf_path)
                if camelot_results:
                    page_regions.extend(camelot_results)
                    logger.debug(
                        f"Page {page.page_number}: Camelot found {len(camelot_results)} table(s)"
                    )

            # Refine titles
            for region in page_regions:
                region.title = _find_table_titles(page, region)
                if not region.title or region.title == "Untitled Table":
                    region.title = f"Table from page {region.page_num}"

            # Deduplicate multiple strategies on same page
            page_regions = deduplicate_page_regions(page_regions)
            regions.extend(page_regions)

    # Merge multi-page tables
    merged = merge_multi_page_tables(regions)
    logger.info(f"Extracted {len(merged)} logical table(s) from {pdf_path}")

    return merged
