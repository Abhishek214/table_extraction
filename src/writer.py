"""Output writers for table extraction results."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import pandas as pd

from .extractor import TableRegion

logger = logging.getLogger(__name__)


def write_json(
    regions: list[TableRegion],
    output_path: str | Path,
    document_id: str,
) -> None:
    """
    Write extracted tables to a JSON file.

    Schema:
    {
        "document_id": "...",
        "tables": [
            {
                "table_id": 1,
                "page_start": 1,
                "page_end": 1,
                "title": "...",
                "columns": [...],
                "rows": [[...], ...]
            },
            ...
        ]
    }
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "document_id": document_id,
        "tables": [
            {
                "table_id": i + 1,
                "page_start": r.page_num,
                "page_end": r.page_end if hasattr(r, "page_end") and r.page_end else r.page_num,
                "title": r.title,
                "columns": r.columns,
                "rows": r.rows,
            }
            for i, r in enumerate(regions)
        ],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"JSON written to {output_path}")


def write_csv(
    regions: list[TableRegion],
    output_dir: str | Path,
    document_id: str,
) -> list[Path]:
    """
    Write each table to its own CSV file.

    Returns list of written CSV paths.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_paths: list[Path] = []

    for i, region in enumerate(regions):
        table_id = i + 1
        safe_title = _sanitize_filename(region.title)
        csv_name = f"{document_id}_table_{table_id}_{safe_title}.csv"
        csv_path = output_dir / csv_name

        df = pd.DataFrame(region.rows, columns=region.columns)
        df.to_csv(csv_path, index=False, encoding="utf-8")
        csv_paths.append(csv_path)
        logger.info(f"CSV written to {csv_path}")

    return csv_paths


def write_excel(
    regions: list[TableRegion],
    output_path: str | Path,
    document_id: str,
) -> None:
    """
    Write all tables to a single Excel workbook (one sheet per table).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for i, region in enumerate(regions):
            table_id = i + 1
            safe_title = _sanitize_filename(region.title)
            sheet_name = f"Table_{table_id}_{safe_title}"[:31]  # Excel max sheet name

            df = pd.DataFrame(region.rows, columns=region.columns)
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    logger.info(f"Excel workbook written to {output_path}")


def _sanitize_filename(name: str) -> str:
    """Remove/replace characters that are unsafe for filenames."""
    import re
    name = re.sub(r"[^\w\-_. ]+", "_", name)
    name = re.sub(r"[\s]+", "_", name)
    name = name.strip("_.")
    return name[:50] if name else "untitled"
