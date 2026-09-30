"""CSV/XLSX source reader with tolerant column detection."""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterator

REQUIRED = {
    "Organization Name": ("organization name", "organisation name", "company name", "entity name"),
    "Domain Name": ("domain name", "domain", "website", "website url", "url"),
}
OPTIONAL = {
    "Organization ID": ("organization id", "organisation id", "company id", "entity id"),
    "Country": ("country", "country code", "jurisdiction"),
    "Sales Territory Name": ("sales territory name", "territory name"),
    "Sales Territory ID": ("sales territory id", "territory id"),
}


def _key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def detect_columns(headers: list[str], organization_column: str | None = None,
                   domain_column: str | None = None) -> dict[str, str]:
    if len(headers) != len(set(headers)):
        raise ValueError("Duplicate source column names would lose data; rename them before processing")
    by_key = {_key(h): h for h in headers}
    result: dict[str, str] = {}
    explicit = {"Organization Name": organization_column, "Domain Name": domain_column}
    for target, aliases in {**REQUIRED, **OPTIONAL}.items():
        chosen = explicit.get(target)
        if chosen:
            if chosen not in headers:
                raise ValueError(f"Configured column not found: {chosen}")
            result[target] = chosen
        else:
            for alias in aliases:
                if alias in by_key:
                    result[target] = by_key[alias]
                    break
        if target in REQUIRED and target not in result:
            raise ValueError(f"Required column missing: {target}; found {headers}")
    return result


def iter_input(path: str | Path, organization_column: str | None = None,
               domain_column: str | None = None) -> Iterator[tuple[int, dict, dict]]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            headers = reader.fieldnames or []
            mapping = detect_columns(headers, organization_column, domain_column)
            for row_number, original in enumerate(reader, 2):
                canonical = {target: original.get(source) for target, source in mapping.items()}
                yield row_number, original, canonical
    elif path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = book.active
            rows = sheet.values
            headers = [str(x or "") for x in next(rows)]
            mapping = detect_columns(headers, organization_column, domain_column)
            for row_number, values in enumerate(rows, 2):
                original = dict(zip(headers, values))
                canonical = {target: original.get(source) for target, source in mapping.items()}
                yield row_number, original, canonical
        finally:
            book.close()
    else:
        raise ValueError("Input must be CSV or XLSX")
