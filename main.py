#!/usr/bin/env python3
"""
CLI entry-point for table extraction.

Usage:
    python -m main --input ./input --output ./output
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

from extractor import extract_tables
from writer import write_csv, write_excel, write_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("table_extraction")


def _doc_id(path: Path) -> str:
    """Derive a clean document ID from a file path."""
    name = path.stem
    name = name.lstrip("0123456789_-") or name
    name = re.sub(r"[^\w]", "_", name)
    return name[:60] or "document"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract tables from PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m table_extraction.main -i ./input -o ./output
  python -m table_extraction.main -i doc.pdf -o ./out -f json,csv
        """,
    )
    parser.add_argument("--input", "-i", required=True, help="PDF file or directory")
    parser.add_argument("--output", "-o", required=True, help="Output directory")
    parser.add_argument(
        "--format", "-f", default="json,csv,xlsx",
        help="Output formats (comma-separated). Default: json,csv,xlsx",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    formats = [f.strip().lower() for f in args.format.split(",")]

    # Collect PDFs
    if input_path.is_file():
        pdfs = [input_path]
    elif input_path.is_dir():
        pdfs = sorted(input_path.glob("*.pdf"))
        if not pdfs:
            logger.error("No PDFs found in %s", input_path)
            return 1
    else:
        logger.error("Input path does not exist: %s", input_path)
        return 1

    logger.info("Processing %d PDF(s) …", len(pdfs))
    ok = 0
    fail = 0
    t0 = time.time()

    for pdf in pdfs:
        t1 = time.time()
        doc_id = _doc_id(pdf)
        logger.info("→ %s  (id=%s)", pdf.name, doc_id)

        try:
            tables = extract_tables(str(pdf))
            logger.info("   Found %d table(s)", len(tables))

            if not tables:
                logger.info("   No tables found (writing empty result)")

            for t in tables:
                logger.info(
                    "   '%s': %d rows × %d cols  (page %s)",
                    t.title or "untitled",
                    t.nrows,
                    t.ncols,
                    f"{t.page}-{t.page_end}" if t.page_end and t.page_end != t.page else str(t.page),
                )

            # Always emit the JSON contract, even when no tables were found —
            # downstream consumers expect one file per input document.
            if "json" in formats:
                write_json(tables, output_dir / f"{doc_id}_tables.json", doc_id)
            if "csv" in formats:
                write_csv(tables, output_dir, doc_id)
            if "xlsx" in formats and tables:
                write_excel(tables, output_dir / f"{doc_id}_tables.xlsx", doc_id)

            logger.info("   ✓ Done in %.1fs", time.time() - t1)
            ok += 1

        except Exception as exc:
            logger.error("   ✗ Failed: %s", exc, exc_info=args.verbose)
            fail += 1

    logger.info(
        "\n%s\nDone. %d/%d succeeded in %.1fs total. Output: %s",
        "=" * 50,
        ok,
        len(pdfs),
        time.time() - t0,
        output_dir,
    )
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())