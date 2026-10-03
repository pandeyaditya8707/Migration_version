from __future__ import annotations

import csv
import io
import json
import os
import re
from typing import Any

from ..engine.transforms import apply_transformation

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def parse_any_dataset_payload(content_str: str, filename: str = "") -> list[dict[str, Any]]:
    """Extensively parses any JSON (array, object, wrapped list, NDJSON/JSON-Lines)
    or delimited text (CSV, TSV, semicolon, pipe) into a normalized list of dicts.
    """
    raw = content_str.strip()
    if not raw:
        return []

    # Strip UTF-8 BOM if present
    if raw.startswith("\ufeff"):
        raw = raw[1:].strip()

    # 1. Try standard JSON first
    if raw.startswith("{") or raw.startswith("[") or filename.lower().endswith(".json"):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
            elif isinstance(data, dict):
                for key in (
                    "records",
                    "data",
                    "items",
                    "results",
                    "rows",
                    "invoices",
                    "orders",
                    "customers",
                    "entries",
                    "payload",
                ):
                    if key in data and isinstance(data[key], list):
                        return [r for r in data[key] if isinstance(r, dict)]
                return [data]
        except Exception:
            pass

    # 2. Try JSON Lines / NDJSON (one JSON object per line)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if lines and all(item.startswith("{") and item.endswith("}") for item in lines[: min(10, len(lines))]):
        ndjson_records = []
        try:
            for line in lines:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    ndjson_records.append(parsed)
            if ndjson_records:
                return ndjson_records
        except Exception:
            pass

    # 3. Delimited Text Parser (CSV, TSV, Semicolon, Pipe)
    first_line = lines[0] if lines else ""
    delimiter = ","
    if "\t" in first_line:
        delimiter = "\t"
    elif ";" in first_line and first_line.count(";") > first_line.count(","):
        delimiter = ";"
    elif "|" in first_line and first_line.count("|") > first_line.count(","):
        delimiter = "|"

    try:
        reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
        records = [dict(row) for row in reader if any(v is not None and str(v).strip() != "" for v in row.values())]
        if records:
            return records
    except Exception:
        pass

    # Fallback standard comma DictReader
    try:
        reader = csv.DictReader(io.StringIO(raw))
        return [dict(row) for row in reader]
    except Exception as e:
        raise ValueError(f"Could not parse payload as JSON, NDJSON, or CSV: {e}")


def load_source_schema() -> dict[str, Any]:
    path = os.path.join(DATA_DIR, "source_schema.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_target_schema() -> dict[str, Any]:
    path = os.path.join(DATA_DIR, "target_schema.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_sample_records() -> list[dict[str, Any]]:
    path = os.path.join(DATA_DIR, "sample_records.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def infer_schema_from_records(records: list[dict[str, Any]], dataset_name: str = "Uploaded Dataset") -> dict[str, Any]:
    """Dynamically infers field types, names, and nullability from arbitrary records."""
    if not records:
        return {"schema_id": "empty_dataset", "name": dataset_name, "description": "Empty dataset", "fields": []}

    # Collect all unique columns across all records
    all_cols = []
    seen = set()
    for r in records:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    all_cols.append(k)

    fields = []
    for col in all_cols:
        values = [r.get(col) for r in records if r.get(col) is not None and str(r.get(col)).strip() != ""]
        nullable = len(values) < len(records)

        inferred_type = "string"
        if values:
            if all(str(v).isdigit() or (str(v).startswith("-") and str(v)[1:].isdigit()) for v in values):
                inferred_type = "int"
            elif all(re.match(r"^-?\d+(\.\d+)?$", str(v)) for v in values):
                inferred_type = "float"

        fields.append(
            {
                "name": col,
                "data_type": inferred_type,
                "nullable": nullable,
                "description": f"Source column '{col}' ({inferred_type})",
                "constraints": {},
            }
        )

    return {
        "schema_id": f"source_{re.sub(r'[^a-zA-Z0-9_]', '_', dataset_name.lower())}",
        "name": dataset_name,
        "description": f"Inferred schema from {len(records)} records",
        "fields": fields,
    }


class InspectionTools:
    """Standardized inspection and validation tools for the AI migration agent."""

    def __init__(self, records: list[dict[str, Any]] | None = None):
        self.records = records if records is not None else load_sample_records()
        self.source_schema = load_source_schema()
        self.target_schema = load_target_schema()

    def inspect_source_schema(self) -> dict[str, Any]:
        """Returns the source schema structure, field types, and nullability."""
        return self.source_schema

    def inspect_target_schema(self) -> dict[str, Any]:
        """Returns target schema constraints, required fields, and enum definitions."""
        return self.target_schema

    def sample_records(self, n: int = 5) -> list[dict[str, Any]]:
        """Returns n raw source records for inspection."""
        return self.records[:n]

    def profile_source_column(self, column_name: str) -> dict[str, Any]:
        """Profiles a column: null rates, distinct values, anomalies, and sample patterns."""
        total = len(self.records)
        values = [r.get(column_name) for r in self.records]

        null_or_empty_count = sum(1 for v in values if v is None or str(v).strip() == "")
        non_null_values = [v for v in values if v is not None and str(v).strip() != ""]

        distinct_vals = list(set(non_null_values))
        freq_map: dict[str, int] = {}
        for v in non_null_values:
            k = str(v)
            freq_map[k] = freq_map.get(k, 0) + 1

        top_values = sorted(freq_map.items(), key=lambda x: x[1], reverse=True)[:10]

        patterns_detected = []
        # Pattern heuristics
        if any("," in str(v) for v in non_null_values):
            patterns_detected.append("CONTAINS_COMMAS (e.g. 'Last, First' or amounts)")
        if any("/" in str(v) or "-" in str(v) for v in non_null_values):
            patterns_detected.append("DATE_SEPARATORS_DETECTED")
        if any(str(v).startswith("$") or "(" in str(v) for v in non_null_values):
            patterns_detected.append("CURRENCY_SYMBOLS_OR_ACCOUNTING_PARENS")
        if any(re.search(r"\d{3}", str(v)) for v in non_null_values):
            patterns_detected.append("DIGIT_CLUSTERS_DETECTED")

        return {
            "column_name": column_name,
            "total_records": total,
            "non_null_count": len(non_null_values),
            "null_count": null_or_empty_count,
            "null_percentage": round((null_or_empty_count / total) * 100, 2) if total else 0.0,
            "distinct_count": len(distinct_vals),
            "top_frequencies": [{"value": k, "count": count} for k, count in top_values],
            "sample_distinct_values": [str(x) for x in distinct_vals[:8]],
            "patterns_detected": patterns_detected,
        }

    def profile_all_columns(self) -> dict[str, Any]:
        """Profiles every column in the dataset."""
        fields = [f["name"] for f in self.source_schema.get("fields", [])]
        return {f_name: self.profile_source_column(f_name) for f_name in fields}

    def test_rule_on_column(
        self, column_name: str, rule_id: str, params: dict[str, Any], sample_limit: int = 200
    ) -> dict[str, Any]:
        """Validates a candidate transformation rule against a slice of source records."""
        sample_slice = self.records[:sample_limit]
        success_count = 0
        error_count = 0
        errors: list[dict[str, Any]] = []

        for idx, rec in enumerate(sample_slice):
            val = rec.get(column_name)
            res, err = apply_transformation(rule_id, val, params)
            if err:
                error_count += 1
                if len(errors) < 5:
                    errors.append({"row_index": idx, "raw_value": val, "error_message": err})
            else:
                success_count += 1

        return {
            "column_name": column_name,
            "rule_id": rule_id,
            "tested_sample_size": len(sample_slice),
            "success_count": success_count,
            "error_count": error_count,
            "success_rate_percent": round((success_count / len(sample_slice)) * 100, 2) if sample_slice else 0.0,
            "sample_errors": errors,
        }
