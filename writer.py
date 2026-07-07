"""Output writers for extracted tables."""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path

import pandas as pd

from extractor import TableRegion

logger = logging.getLogger(__name__)


def write_json(regions: list[TableRegion], path: str | Path, doc_id: str) -> None:
    """Write tables to a single JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "document_id": doc_id,
        "tables": [
            {
                "table_id": i + 1,
                "page_start": r.page,
                "page_end": r.page_end or r.page,
                "title": r.title,
                "columns": r.columns,
                "rows": r.rows,
            }
            for i, r in enumerate(regions)
        ],
    }

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    logger.info("JSON written to %s", path)


def write_csv(
    regions: list[TableRegion], output_dir: str | Path, doc_id: str
) -> list[Path]:
    """Write each table to its own CSV file."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for i, r in enumerate(regions):
        safe = _safe_name(r.title or f"table_{i + 1}")
        name = f"{doc_id}_table_{i + 1}_{safe}.csv"
        dest = output_dir / name

        with open(dest, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(r.columns)
            writer.writerows(r.rows)

        paths.append(dest)
        logger.info("CSV written to %s", dest)

    return paths


def write_excel(regions: list[TableRegion], path: str | Path, doc_id: str) -> None:
    """Write all tables to a single Excel workbook (one sheet per table)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for i, r in enumerate(regions):
            safe = _safe_name(r.title or f"table_{i + 1}")
            sheet = f"T{i + 1}_{safe}"[:31]
            df = pd.DataFrame(r.rows, columns=r.columns)
            df.to_excel(writer, sheet_name=sheet, index=False)

    logger.info("Excel written to %s", path)


def _safe_name(name: str) -> str:
    """Sanitise a string for use in filenames / sheet names."""
    name = re.sub(r"[^\w\-_. ]+", "_", name)
    name = re.sub(r"\s+", "_", name).strip("_.")
    return name[:50] or "untitled"
