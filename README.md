# Generalized PDF Table Extractor

A clean, layout-agnostic table extraction library for PDF documents — purely geometric and structural detection.

## Approach

1. **Bordered tables** — pdfplumber's default extractor uses visible lines and rectangles to locate cells. Works when tables have drawn borders.
2. **Borderless tables** — When no borders are found, the library falls back to word-position clustering. Words that align vertically across multiple rows indicate columns. This handles whitespace-aligned tables without any content-specific heuristics.
3. **Multi-page tables** — Tables that span pages are merged automatically by comparing column signatures and detecting repeated header rows.

All decisions are based on geometry (x, y positions, line detection) and structure (row/column consistency) — never on hardcoded word lists.

## Installation

```bash
pip3 install -r requirements.txt
```

## Usage

### CLI

```bash
# Single PDF
python3 -m main -i document.pdf -o ./output

# Directory of PDFs
python3 -m main -i ./input_dir -o ./output_dir

# Choose output formats (default: all three)
python3 -m main -i doc.pdf -o ./out -f json,csv

# Debug logging
python3 -m main -i doc.pdf -o ./out -v
```

### Python API

```python
from table_extraction import extract_tables

tables = extract_tables("document.pdf")
for t in tables:
    print(t.title)      # e.g. "Table A: Monthly Funnel Summary"
    print(t.columns)    # ['Metric', 'Jan', 'Feb', 'Mar']
    print(t.rows)       # [['Applications', '1,240', ...], ...]
    print(t.page)       # starting page number
```

## Output Formats

| Format | Description |
|--------|-------------|
| JSON   | One file per PDF with full table metadata |
| CSV    | One file per table |
| XLSX   | One workbook per PDF, one sheet per table |

## Project Structure

```
table_extraction/
├── table_extraction/
│   ├── __init__.py      # Public API exports
│   ├── extractor.py      # Core extraction logic
│   ├── writer.py         # Output format writers
│   └── main.py           # CLI entry point
├── requirements.txt
└── README.md
```

## How It Works (Detailed)

### Bordered Table Detection
pdfplumber analyzes each page for geometric lines and rectangles. When it finds a grid-like structure, it extracts cells automatically. This is the fastest and most reliable path — used for documents like doc_01 and doc_03.

### Borderless Table Detection (Fallback)
When the default strategy finds nothing on a page:

1. **Word extraction** — All text words are extracted with their bounding boxes.
2. **Row clustering** — Words are grouped by vertical position (y-coordinate) into rows.
3. **Column detection** — The library finds persistent vertical gaps between words across multiple rows. Large gaps that appear consistently are treated as column separators.
4. **Assignment** — Each word is assigned to the column whose x-range it falls within.
5. **Noise filtering** — Rows with very sparse content or single long text spans are removed (structural heuristic, not keyword-based).

### Multi-Page Merging
After per-page extraction, tables are merged across pages when:
- They have the same number of columns
- Their column headers are sufficiently similar (signature matching)
- The subsequent page's first row is a repeated header (it's stripped)

## Edge Cases Handled

- **Scanned pages** — No extractable text means no tables (graceful empty result)
- **Borderless tables** — Word-position clustering handles whitespace-aligned layouts
- **Empty cells** — Preserved as empty strings in output
- **Multi-page tables** — Automatically merged into single logical tables
- **Narrative text** — Filtered structurally (long text spans, sparse rows) rather than by keyword

## Roadmap

| Current state | Next step | Future (if needed) |
|---|---|---|
| pdfplumber + word clustering (geometric) | Add OCR preprocessor for scanned PDFs (still geometric, just with OCR words instead of text-layer words) | Table Transformer for complex layouts (learned structure) |
