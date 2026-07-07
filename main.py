#!/usr/bin/env python3
"""
Table Extractor CLI

Extract tables from PDF documents (bordered, borderless, multi-page).

Usage:
    python main.py --input ./input --output ./output

Output:
    - <doc_id>_tables.json    (one JSON per PDF)
    - <doc_id>_table_N.csv    (one CSV per table)
    - <doc_id>_tables.xlsx    (optional Excel workbook)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from src.extractor import extract_tables_from_pdf
from src.writer import write_json, write_csv, write_excel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("table_extractor")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract tables from PDFs (bordered, borderless, multi-page).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --input ./input --output ./output
  python main.py --input ./docs --output ./out --format json,csv,xlsx
  python main.py --input ./doc.pdf --output ./out --verbose
        """,
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to input PDF or directory of PDFs",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output directory",
    )
    parser.add_argument(
        "--format", "-f",
        default="json,csv,xlsx",
        help="Output formats: json, csv, xlsx (comma-separated, default: all)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Document ID helper
# ---------------------------------------------------------------------------

def document_id_from_path(path: Path) -> str:
    """Derive a clean document ID from the file path."""
    name = path.stem
    name = name.lstrip("0123456789_-")
    name = name or path.stem
    import re
    name = re.sub(r"[^\w]", "_", name)
    return name[:60] or "document"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Verbose mode enabled")

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    formats = [f.strip().lower() for f in args.format.split(",")]

    # Collect PDFs
    if input_path.is_file():
        pdf_files = [input_path]
    elif input_path.is_dir():
        pdf_files = sorted(input_path.glob("*.pdf"))
        if not pdf_files:
            logger.error(f"No PDF files found in {input_path}")
            sys.exit(1)
    else:
        logger.error(f"Input path does not exist: {input_path}")
        sys.exit(1)

    logger.info(f"Processing {len(pdf_files)} PDF(s)...")

    total_start = time.time()
    success_count = 0
    error_count = 0

    for pdf_path in pdf_files:
        doc_start = time.time()
        doc_id = document_id_from_path(pdf_path)
        logger.info(f"Processing: {pdf_path.name} (id={doc_id})")

        try:
            regions = extract_tables_from_pdf(str(pdf_path))
            logger.info(f"  -> Found {len(regions)} table(s)")

            if not regions:
                logger.warning(f"  No tables extracted from {pdf_path.name}")
                error_count += 1
                continue

            # Preview first table
            for r in regions:
                page_end = getattr(r, "page_end", r.page_num)
                logger.info(
                    f"  Table \'{r.title}\': {r.nrows} rows x {r.ncols} cols (pages {r.page_num}-{page_end})"
                )

            # Write outputs
            if "json" in formats:
                json_path = output_dir / f"{doc_id}_tables.json"
                write_json(regions, json_path, doc_id)

            if "csv" in formats:
                write_csv(regions, output_dir, doc_id)

            if "xlsx" in formats:
                xlsx_path = output_dir / f"{doc_id}_tables.xlsx"
                write_excel(regions, xlsx_path, doc_id)

            elapsed = time.time() - doc_start
            logger.info(f"  \u2713 Done in {elapsed:.1f}s")
            success_count += 1

        except Exception as exc:
            logger.error(f"  \u2717 Failed: {exc}", exc_info=args.verbose)
            error_count += 1

    total_elapsed = time.time() - total_start
    logger.info(
        f"\n{'='*50}\n"
        f"Done. {success_count}/{len(pdf_files)} PDFs processed "
        f"in {total_elapsed:.1f}s total\n"
        f"Output: {output_dir}"
    )

    if error_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
