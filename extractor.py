""" 
Table extraction engine with multi-strategy detection. 
 
Approach (simple-first): 
1. pdfplumber default — works great for bordered tables (lines/rects 
present) 
2. pdfplumber text-alignment strategy — fallback for borderless/loose 
tables 
3. Custom whitespace heuristic — for stubborn borderless tables 
4. Header+multi-page merge — logical table deduplication across pages 
""" 
 
from __future__ import annotations 
 
import logging 
import re 
from pathlib import Path 
from typing import Any 
 
import pdfplumber 
 
logger = logging.getLogger(__name__) 
 
 
# 
--------------------------------------------------------------------------
- 
# Internal data shape 
# 
--------------------------------------------------------------------------
- 
 
class TableRegion: 
    """Represents a detected table region from one page.""" 
 
    def __init__( 
        self, 
        title: str, 
        columns: list[str], 
        rows: list[list[str]], 
        page_num: int, 
        bbox: tuple[float, float, float, float] | None = None, 
    ): 
        self.title = title 
        self.columns = columns 
        self.rows = rows 
        self.page_num = page_num 
        self.bbox = bbox 
 
    @property 
    def ncols(self) -> int: 
        return len(self.columns) 
 
    @property 
    def nrows(self) -> int: 
        return len(self.rows) 
 
 
# 
--------------------------------------------------------------------------
- 
# Strategy 1 — pdfplumber default / bordered detection 
# 
--------------------------------------------------------------------------
- 
 
def _extract_pdfplumber_tables(page: pdfplumber.pdf.Page) -> 
list[TableRegion]: 
    """ 
    Try pdfplumber's built-in table detection. 
    Works best when there are visible lines (borders). 
    """ 
    regions: list[TableRegion] = [] 
 
    # Strategy 1: default 
    raw_tables = page.extract_tables() 
    if raw_tables: 
        for raw in raw_tables: 
            if not raw or len(raw) < 2: 
                continue 
            region = _raw_to_region(raw, page.page_number, "default") 
            if region: 
                regions.append(region) 
 
    # Strategy 2: text-alignment based (good for tables with weak/no 
borders) 
    alt_settings = { 
        "vertical_strategy": "text", 
        "horizontal_strategy": "text", 
        "explicit_vertical_lines": [], 
        "explicit_horizontal_lines": [], 
    } 
    alt_tables = page.extract_tables(table_settings=alt_settings) 
    if alt_tables: 
        for raw in alt_tables: 
            if not raw or len(raw) < 2: 
                continue 
            # Deduplicate against what we already found 
            region = _raw_to_region(raw, page.page_number, "text-align") 
            if region and not _region_similar(region, regions): 
                regions.append(region) 
 
    return regions 
 
 
def _raw_to_region(raw: list[list], page_num: int, strategy: str) -> 
TableRegion | None: 
    """Convert raw pdfplumber table output to TableRegion.""" 
    if not raw: 
        return None 
 
    # pdfplumber sometimes returns nested single-cell rows 
    rows = [r for r in raw if r and any(c for c in r if c)] 
    if len(rows) < 2: 
        return None 
 
    # Detect title: first row often has a non-tabular header label 
    # We detect it by checking if row[0] looks like a table caption 
    title = "" 
    columns: list[str] = [] 
    data_rows: list[list[str]] = [] 
 
    # Try to find header row: first row with consistent column count 
    header_row_idx = 0 
    for i, row in enumerate(rows): 
        cleaned = [str(c).strip() if c else "" for c in row] 
        # Skip empty or mostly-empty rows 
        if not any(cleaned): 
            continue 
        header_row_idx = i 
        break 
 
    header_row = [str(c).strip() if c else "" for c in 
rows[header_row_idx]] 
    columns = header_row 
 
    # If the header has very few cells compared to others, it's likely not 
a header 
    # Try looking for the actual header below 
    if len(rows) > header_row_idx + 1: 
        next_row = [str(c).strip() if c else "" for c in 
rows[header_row_idx + 1]] 
        if len(next_row) > len(header_row) * 1.5: 
            # Likely the first row is a title, real header is next 
            title = " ".join(header_row).strip() 
            columns = next_row 
            data_rows = [[str(c).strip() if c else "" for c in r] for r in 
rows[header_row_idx + 2 :]] 
        else: 
            data_rows = [[str(c).strip() if c else "" for c in r] for r in 
rows[header_row_idx + 1 :]] 
    else: 
        data_rows = [] 
 
    # Try to extract title from text above the table 
    # (handled in higher-level function) 
 
    return TableRegion( 
        title=title or "Untitled Table", 
        columns=columns, 
        rows=data_rows, 
        page_num=page_num, 
    ) 
 
 
def _region_similar(new: TableRegion, existing: list[TableRegion], 
threshold: float = 0.7) -> bool: 
    """Check if new region is similar to any existing region (likely 
duplicate).""" 
    for ex in existing: 
        if ex.page_num != new.page_num: 
            continue 
        if ex.ncols == new.ncols: 
            # Check column similarity 
            matches = sum(1 for a, b in zip(ex.columns, new.columns) if 
a.strip() == b.strip()) 
            if matches / max(len(ex.columns), 1) >= threshold: 
                return True 
    return False 
 
 
# 
--------------------------------------------------------------------------
- 
# Strategy 3 — Borderless / whitespace-aligned detection 
# 
--------------------------------------------------------------------------
- 
 
def _extract_borderless_tables(page: pdfplumber.pdf.Page) -> 
list[TableRegion]: 
    """ 
    Detect tables that have no visible borders. 
    Uses word position analysis to find column boundaries. 
    """ 
    words = page.extract_words() 
    if not words: 
        return [] 
 
    # Find table title and locate the table region 
    # Look for "Table X:" or similar patterns near words 
    all_text = " ".join(w["text"] for w in words) 
    title_match = re.search(r"(Table\s+[A-Z0-9_]+[^\w]*:[^\n]*)", 
all_text, re.IGNORECASE) 
    title = title_match.group(1).strip() if title_match else "Untitled 
Table" 
 
    # If title already captured in normal extraction, skip 
    if _has_pdfplumber_table_near_title(page, title): 
        return [] 
 
    # Group words by y-coordinate (row detection) 
    rows_by_y = _group_words_by_row(words) 
 
    # Detect column boundaries from consistent word x-positions 
    col_boundaries = _detect_column_boundaries(rows_by_y) 
 
    if not col_boundaries: 
        return [] 
 
    # Build table rows from words aligned to column boundaries 
    table_rows = _build_table_from_boundaries(rows_by_y, col_boundaries) 
 
    if len(table_rows) < 2: 
        return [] 
 
    # First row is likely the header 
    header = table_rows[0] 
    data_rows = table_rows[1:] 
 
    return [ 
        TableRegion( 
            title=title, 
            columns=header, 
            rows=data_rows, 
            page_num=page.page_number, 
        ) 
    ] 
 
 
def _has_pdfplumber_table_near_title(page: pdfplumber.pdf.Page, title: 
str) -> bool: 
    """Check if pdfplumber already detected a table in the title area.""" 
    tables = page.extract_tables() 
    for raw in tables: 
        if raw: 
            first_cell = str(raw[0][0]).strip() if raw and raw[0] else "" 
            if first_cell and title.startswith(first_cell[:10]): 
                return True 
    return False 
 
 
def _group_words_by_row( 
    words: list[dict], 
    y_tolerance: float = 5.0, 
) -> list[list[dict]]: 
    """Group words into rows based on y-coordinate (baseline 
alignment).""" 
    if not words: 
        return [] 
 
    rows: list[dict[int, list[dict]]] = [] 
    for w in words: 
        placed = False 
        for row_y, row_words in rows: 
            if abs(w["top"] - row_y) <= y_tolerance: 
                row_words.append(w) 
                placed = True 
                break 
        if not placed: 
            rows.append({w["top"]: [w]}) 
 
    # Convert to list of lists 
    result = [] 
    for row_dict in rows: 
        for row_y, row_words in row_dict.items(): 
            # Sort words left to right 
            row_words.sort(key=lambda w: w["x0"]) 
            result.append((row_y, row_words)) 
    result.sort(key=lambda x: x[0]) 
    return [rw[1] for rw in result] 
 
 
def _detect_column_boundaries( 
    rows: list[list[dict]], 
    min_col_width: float = 20.0, 
) -> list[tuple[float, float]]: 
    """ 
    Detect column boundaries from consistent word x-positions. 
    Returns list of (left, right) boundaries for each column. 
    """ 
    if not rows: 
        return [] 
 
    # Collect all word edges 
    all_edges: list[float] = [] 
    for row in rows: 
        for w in row: 
            all_edges.append(w["x0"])  # left edge 
            all_edges.append(w["x1"])  # right edge 
 
    if not all_edges: 
        return [] 
 
    # Cluster similar x positions (within tolerance) 
    edges = sorted(set(all_edges)) 
    if len(edges) < 2: 
        return [] 
 
    # Find column gaps — large gaps indicate column boundaries 
    boundaries = [] 
    gaps: list[tuple[float, float, float]] = []  # (gap_size, left, right) 
 
    for i in range(len(edges) - 1): 
        gap = edges[i + 1] - edges[i] 
        if gap >= min_col_width * 0.5:  # significant gap 
            gaps.append((gap, edges[i], edges[i + 1])) 
 
    # Sort by gap size (prefer larger gaps as column separators) 
    gaps.sort(reverse=True) 
 
    # Take the largest N-1 gaps for N columns (but limit to reasonable) 
    # Heuristic: if we have 4-6 columns, take top 3-5 gaps 
    if len(gaps) >= 3: 
        selected_gaps = gaps[:4]  # take up to 4 gaps for 5 columns 
    else: 
        selected_gaps = gaps 
 
    # Sort by position and build boundaries 
    selected_gaps.sort(key=lambda x: x[1]) 
    for _, left, right in selected_gaps: 
        boundaries.append((left, right)) 
 
    return boundaries 
 
 
def _build_table_from_boundaries( 
    rows: list[list[dict]], 
    boundaries: list[tuple[float, float]], 
) -> list[list[str]]: 
    """Assign words to columns and build table rows.""" 
    if not boundaries or not rows: 
        return [] 
 
    table_rows: list[list[str]] = [] 
 
    for row_words in rows: 
        row_cells: list[str] = [""] * len(boundaries) 
        for w in row_words: 
            for col_idx, (left, right) in enumerate(boundaries): 
                if left - 5 <= w["x0"] <= right + 5: 
                    if row_cells[col_idx]: 
                        row_cells[col_idx] += " " + w["text"] 
                    else: 
                        row_cells[col_idx] = w["text"] 
                    break 
        # Only include row if it has at least 2 non-empty cells 
        non_empty = sum(1 for c in row_cells if c.strip()) 
        if non_empty >= 2: 
            table_rows.append(row_cells) 
 
    return table_rows 
 
 
# 
--------------------------------------------------------------------------
- 
# Strategy 4 — Legacy Camelot fallback (optional) 
# 
--------------------------------------------------------------------------
- 
 
def _try_camelot_fallback(pdf_path: str) -> list[TableRegion]: 
    """ 
    Attempt extraction using Camelot if installed. 
    Camelot is better for borderless tables in some cases. 
    Falls back gracefully if not available. 
    """ 
    try: 
        import camelot 
 
        regions: list[TableRegion] = [] 
        # Try 'lattice' first (for bordered), then 'stream' (borderless) 
        for flavor in ["stream", "lattice"]: 
            try: 
                tables = camelot.read_pdf(pdf_path, pages="all", 
flavor=flavor) 
                for tbl in tables: 
                    if tbl.df.empty: 
                        continue 
                    data = tbl.df.values.tolist() 
                    if len(data) < 2: 
                        continue 
                    region = TableRegion( 
                        title="Camelot Table", 
                        columns=[str(c) for c in data[0]], 
                        rows=[row for row in data[1:]], 
                        page_num=1, 
                    ) 
                    regions.append(region) 
                break  # only need one flavor to work 
            except Exception: 
                continue 
        return regions 
    except ImportError: 
        return [] 
 
 
# 
--------------------------------------------------------------------------
- 
# Title extraction from page text 
# 
--------------------------------------------------------------------------
- 
 
def _find_table_titles( 
    page: pdfplumber.pdf.Page, 
    region: TableRegion, 
) -> str: 
    """ 
    Find the most likely table title from text near the table. 
    Looks for 'Table X:' patterns or bold header text above the region. 
    """ 
    text = page.extract_text() 
    if not text: 
        return region.title 
 
    # Look for table caption pattern 
    lines = text.split("\n") 
    for i, line in enumerate(lines): 
        if re.match(r"Table\s+[A-Z0-9_]*\s*:", line, re.IGNORECASE): 
            # Check if this caption is near the region 
            return line.strip() 
 
    return region.title 
 
 
# 
--------------------------------------------------------------------------
- 
# Multi-page table merging 
# 
--------------------------------------------------------------------------
- 
 
def _is_continuation_header( 
    row: list[str], 
    header: list[str], 
    tolerance: float = 0.8, 
) -> bool: 
    """Check if a row is a continuation of a multi-page table header.""" 
    if len(row) != len(header): 
        return False 
    matches = sum( 
        1 for a, b in zip(row, header) if a.strip().lower() == 
b.strip().lower() 
    ) 
    return matches / len(header) >= tolerance 
 
 
def _is_continuation_data( 
    row: list[str], 
    prev_row: list[str], 
    ncols: int, 
) -> bool: 
    """Check if row continues data from previous page (no new header).""" 
    if len(row) < 2 or len(prev_row) < 2: 
        return False 
    # Data rows typically don't repeat column names 
    for cell in row: 
        cell_lower = cell.strip().lower() 
        if any( 
            cell_lower == h.strip().lower() for h in ["vendor", "invoice", 
"date", "amount", "status", "metric", "jan", "feb", "mar", "txn date", 
"reference", "description", "debit", "credit", "balance"] 
        ): 
            return False 
    return True 
 
 
def merge_multi_page_tables( 
    regions: list[TableRegion], 
) -> list[TableRegion]: 
    """ 
    Merge tables that span multiple pages into single logical tables. 
    """ 
    if not regions: 
        return [] 
 
    # Sort by page number 
    sorted_regions = sorted(regions, key=lambda r: r.page_num) 
 
    merged: list[TableRegion] = [] 
    current: TableRegion | None = None 
 
    for region in sorted_regions: 
        if current is None: 
            current = TableRegion( 
                title=region.title, 
                columns=region.columns, 
                rows=list(region.rows), 
                page_num=region.page_num, 
            ) 
            merged.append(current) 
            continue 
 
        # Check if this is a continuation 
        is_cont = False 
 
        # Case 1: Same number of columns and first row matches header 
        if len(region.columns) == len(current.columns): 
            if _is_continuation_header(region.rows[0] if region.rows else 
[], current.columns): 
                # It's a header row from the next page — skip it and add 
data 
                data_rows = region.rows[1:] if len(region.rows) > 1 else 
[] 
                current.rows.extend(data_rows) 
                is_cont = True 
 
        # Case 2: Similar column count and looks like data continuation 
        if not is_cont and len(region.columns) == len(current.columns): 
            if region.rows and current.rows: 
                if _is_continuation_data(region.rows[0], current.rows[-1], 
len(current.columns)): 
                    current.rows.extend(region.rows) 
                    is_cont = True 
 
        # Case 3: Multi-page row continuation (same structure) 
        if not is_cont and any(len(region.columns) == len(c) for c in 
[current.columns]): 
            # Check if first row of new region looks like continuation 
            if region.rows and _is_continuation_data(region.rows[0], 
current.rows[-1], len(current.columns)): 
                current.rows.extend(region.rows) 
                is_cont = True 
 
        if not is_cont: 
            # New table 
            current = region 
            merged.append(current) 
 
    # Remove duplicates within merged tables 
    final: list[TableRegion] = [] 
    for region in merged: 
        # Deduplicate consecutive identical rows 
        unique_rows: list[list[str]] = [] 
        for row in region.rows: 
            if not unique_rows or row != unique_rows[-1]: 
                unique_rows.append(row) 
        region.rows = unique_rows 
        final.append(region) 
 
    return final 
 
 
# 
--------------------------------------------------------------------------
- 
# Main extraction function 
# 
--------------------------------------------------------------------------
- 
 
def extract_tables_from_pdf(pdf_path: str) -> list[TableRegion]: 
    """ 
    Extract tables from a PDF using multi-strategy detection. 
 
    Strategy order: 
    1. pdfplumber default (bordered tables) 
    2. pdfplumber text-alignment (weak-bordered tables) 
    3. Custom borderless heuristic (whitespace-aligned) 
    4. Camelot fallback (optional, if installed) 
 
    Returns a list of TableRegion objects. 
    """ 
    regions: list[TableRegion] = [] 
 
    with pdfplumber.open(pdf_path) as pdf: 
        for page in pdf.pages: 
            page_regions: list[TableRegion] = [] 
 
            # Strategy 1 & 2: pdfplumber 
            plumb_regions = _extract_pdfplumber_tables(page) 
            if plumb_regions: 
                page_regions.extend(plumb_regions) 
                logger.debug( 
                    f"Page {page.page_number}: pdfplumber found 
{len(plumb_regions)} table(s)" 
                ) 
 
            # Strategy 3: Borderless detection if pdfplumber found nothing 
useful 
            if not page_regions or all(r.nrows < 2 for r in page_regions): 
                borderless = _extract_borderless_tables(page) 
                if borderless: 
                    for b in borderless: 
                        if not any(_region_similar(b, page_regions)): 
                            page_regions.append(b) 
                    logger.debug( 
                        f"Page {page.page_number}: borderless found 
{len(borderless)} table(s)" 
                    ) 
 
            # Strategy 4: Camelot fallback (if installed) 
            if not page_regions: 
                camelot_results = _try_camelot_fallback(pdf_path) 
                if camelot_results: 
                    page_regions.extend(camelot_results) 
                    logger.debug( 
                        f"Page {page.page_number}: Camelot found 
{len(camelot_results)} table(s)" 
                    ) 
 
            # Refine titles 
            for region in page_regions: 
                region.title = _find_table_titles(page, region) 
                if not region.title or region.title == "Untitled Table": 
                    region.title = f"Table from page {region.page_num}" 
 
            regions.extend(page_regions) 
 
    # Merge multi-page tables 
    merged = merge_multi_page_tables(regions) 
    logger.info(f"Extracted {len(merged)} logical table(s) from 
{pdf_path}") 
 
    return merged 
 
