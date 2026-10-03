from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from ..models.schemas import TransformationRuleSpec

# ISO 3166-1 alpha-2 dictionary for common mappings
COUNTRY_LOOKUP = {
    "US": "US",
    "USA": "US",
    "UNITED STATES": "US",
    "UNITED STATES OF AMERICA": "US",
    "UNTIED STATES": "US",
    "U.S.A.": "US",
    "U.S.": "US",
    "CA": "CA",
    "CAN": "CA",
    "CANADA": "CA",
    "GB": "GB",
    "GBR": "GB",
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB",
    "IN": "IN",
    "IND": "IN",
    "INDIA": "IN",
    "DE": "DE",
    "DEU": "DE",
    "GERMANY": "DE",
    "FR": "FR",
    "FRA": "FR",
    "FRANCE": "FR",
    "AU": "AU",
    "AUS": "AU",
    "AUSTRALIA": "AU",
    "JP": "JP",
    "JPN": "JP",
    "JAPAN": "JP",
}

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
E164_REGEX = re.compile(r"^\+[1-9]\d{1,14}$")


class TransformError(Exception):
    pass


class TransformationRegistry:
    """Deterministic, pure transformation functions with zero external side-effects."""

    @staticmethod
    def direct_copy(val: Any, params: dict[str, Any]) -> tuple[Any, str | None]:
        if val is None or val == "":
            default = params.get("default")
            if default is not None:
                return default, None
            if params.get("required", False):
                return None, "Required field is missing or empty"
            return None, None
        return val, None

    @staticmethod
    def trim_clean(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        if val is None:
            if params.get("required", False):
                return None, "Required field is missing"
            return None, None
        s = str(val).strip()
        # Collapse multiple internal spaces
        s = re.sub(r"\s+", " ", s)
        if not s and params.get("required", False):
            return None, "Field is empty after trimming"
        return s if s else None, None

    @staticmethod
    def concat_ws(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        # val can be a list of values from multiple source fields
        values = val if isinstance(val, list) else [val]
        sep = params.get("separator", " ")
        cleaned = []
        for v in values:
            if v is not None and str(v).strip():
                cleaned.append(str(v).strip())
        if not cleaned:
            if params.get("required", False):
                return None, "No non-empty components found to concatenate"
            return None, None
        return sep.join(cleaned), None

    @staticmethod
    def split_name(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        """Splits full name like 'Doe, John' or 'John Doe' into first or last name."""
        fallback = params.get("default") or params.get("fallback")
        if val is None or not str(val).strip():
            if fallback:
                return fallback, None
            if params.get("required", False):
                return None, "Name is missing or empty"
            return None, None

        name_str = str(val).strip()
        part = params.get("part", "first").lower()  # "first" or "last"

        # Check for "Last, First" format
        if "," in name_str:
            parts = [p.strip() for p in name_str.split(",", 1)]
            last = parts[0]
            first = parts[1] if len(parts) > 1 else ""
        else:
            parts = name_str.split()
            if len(parts) == 1:
                first = parts[0]
                last = ""
            else:
                first = parts[0]
                last = " ".join(parts[1:])

        if part == "first":
            if not first:
                if fallback:
                    return fallback, None
                if params.get("required", False):
                    return None, f"First name could not be extracted from '{val}'"
            return first if first else fallback, None
        elif part == "last":
            if not last:
                if fallback:
                    return fallback, None
                if params.get("required", False):
                    return None, f"Last name could not be extracted from '{val}'"
            return last if last else fallback, None
        else:
            return None, f"Invalid split_name part param '{part}'"

    @staticmethod
    def date_to_iso8601(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        """Parses various date formats to ISO-8601 UTC string: YYYY-MM-DDTHH:MM:SSZ."""
        fallback = params.get("fallback") or params.get("default")
        if val is None or not str(val).strip():
            if fallback:
                return fallback, None
            if params.get("required", False):
                return None, "Required date field is missing"
            return None, None

        val_str = str(val).strip()

        # Handle numeric epoch timestamp
        if val_str.isdigit() or (val_str.startswith("-") and val_str[1:].isdigit()):
            try:
                epoch = int(val_str)
                # Check if in milliseconds
                if abs(epoch) > 1e11:
                    epoch = epoch / 1000.0
                dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), None
            except Exception as e:
                if fallback:
                    return fallback, None
                return None, f"Invalid epoch timestamp '{val_str}': {e}"

        date_formats = [
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%d/%m/%Y",
            "%m-%d-%Y",
            "%d-%m-%Y",
            "%y-%m-%d",
            "%d-%m-%y",
            "%m-%d-%y",
            "%m/%d/%y",
            "%d/%m/%y",
            "%B %d, %Y",
            "%b %d, %Y",
            "%Y/%m/%d",
        ]

        for fmt in date_formats:
            try:
                parsed = datetime.strptime(val_str, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.strftime("%Y-%m-%dT%H:%M:%SZ"), None
            except ValueError:
                continue

        if fallback:
            return fallback, None
        return None, f"Cannot parse '{val_str}' as a valid date. Expected format like YYYY-MM-DD or MM/DD/YYYY"

    @staticmethod
    def phone_to_e164(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        """Converts phone strings to E.164 (+1XXXXXXXXXX)."""
        fallback = params.get("fallback") or params.get("default")
        null_allowed = (
            params.get("on_invalid") == "null" or params.get("allow_null_on_invalid") or params.get("null_on_invalid")
        )

        if val is None or not str(val).strip():
            if null_allowed:
                return None, None
            if fallback:
                return fallback, None
            if params.get("required", False):
                return None, "Phone number is required"
            return None, None

        raw = str(val).strip()
        default_country_code = params.get("default_country_code", "1")  # US default

        # Keep leading + if present, strip other punctuation
        has_plus = raw.startswith("+")
        digits = re.sub(r"\D", "", raw)

        if not digits:
            if null_allowed:
                return None, None
            if fallback:
                return fallback, None
            if params.get("required", False):
                return None, f"Phone number '{raw}' contains no digits"
            return None, None

        if len(digits) < 10:
            if null_allowed:
                return None, None
            if fallback:
                return fallback, None
            return None, f"Phone number '{raw}' is too short ({len(digits)} digits; min 10 required)"
        if len(digits) > 15:
            if null_allowed:
                return None, None
            if fallback:
                return fallback, None
            return None, f"Phone number '{raw}' is too long ({len(digits)} digits; max 15 allowed)"

        if has_plus:
            e164 = f"+{digits}"
        elif len(digits) == 10:
            e164 = f"+{default_country_code}{digits}"
        elif len(digits) == 11 and digits.startswith("1"):
            e164 = f"+{digits}"
        else:
            e164 = f"+{digits}"

        if not E164_REGEX.match(e164):
            if null_allowed:
                return None, None
            if fallback:
                return fallback, None
            return None, f"Generated phone '{e164}' does not meet E.164 format"

        return e164, None

    @staticmethod
    def email_normalize(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        """Lowercases, trims, and validates standard email structure."""
        fallback = params.get("fallback") or params.get("default")
        null_allowed = (
            params.get("on_invalid") == "null" or params.get("allow_null_on_invalid") or params.get("null_on_invalid")
        )

        if val is None or not str(val).strip():
            if fallback:
                return fallback, None
            if null_allowed:
                return None, None
            if params.get("required", False):
                return None, "Email address is required"
            return None, None

        cleaned = str(val).strip().lower()
        if not EMAIL_REGEX.match(cleaned):
            if fallback:
                return fallback, None
            if null_allowed:
                return None, None
            return None, f"Invalid email format: '{val}'"

        return cleaned, None

    @staticmethod
    def clean_currency_to_float(val: Any, params: dict[str, Any]) -> tuple[float | None, str | None]:
        """Parses currency strings like '$1,249.50', '(50.00)' -> -50.00."""
        if val is None or str(val).strip() == "":
            default = params.get("default", 0.0)
            if params.get("required", False) and default is None:
                return None, "Currency field is required"
            return default, None

        s = str(val).strip()
        # Accounting negative in parentheses: (123.45) -> -123.45
        is_negative = False
        if s.startswith("(") and s.endswith(")"):
            is_negative = True
            s = s[1:-1].strip()
        elif s.startswith("-"):
            is_negative = True
            s = s[1:].strip()

        # Remove currency symbols and commas
        clean_num = re.sub(r"[^\d.]", "", s)
        if not clean_num:
            default = params.get("default")
            if default is not None:
                return float(default), None
            return None, f"Unable to parse numeric amount from '{val}'"

        try:
            num = float(clean_num)
            if is_negative:
                num = -num
            return round(num, 2), None
        except ValueError:
            return None, f"Invalid numeric currency string '{val}'"

    @staticmethod
    def enum_lookup(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        """Maps legacy codes to modern enum strings."""
        mapping = params.get("mapping", {})
        fallback = params.get("fallback")
        case_sensitive = params.get("case_sensitive", False)

        if val is None or str(val).strip() == "":
            if fallback is not None:
                return fallback, None
            if params.get("required", False):
                return None, "Value is required for enum mapping"
            return None, None

        key = str(val).strip()
        if not case_sensitive:
            # Case-insensitive search
            lookup_lower = {str(k).lower(): v for k, v in mapping.items()}
            mapped = lookup_lower.get(key.lower())
        else:
            mapped = mapping.get(key)

        if mapped is not None:
            return mapped, None

        if fallback is not None:
            return fallback, None

        return None, f"Unrecognized enum code '{val}'. Allowed source keys: {list(mapping.keys())}"

    @staticmethod
    def country_to_iso2(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        """Standardizes country representations to ISO 3166-1 alpha-2."""
        if val is None or not str(val).strip():
            default = params.get("default", "US")
            if params.get("required", False) and not default:
                return None, "Country code is required"
            return default, None

        raw = str(val).strip().upper()
        cleaned = raw.replace(".", "").strip()
        if raw in COUNTRY_LOOKUP:
            return COUNTRY_LOOKUP[raw], None
        if cleaned in COUNTRY_LOOKUP:
            return COUNTRY_LOOKUP[cleaned], None

        if len(cleaned) == 2 and cleaned.isalpha():
            return cleaned, None

        fallback = params.get("fallback")
        if fallback:
            return fallback, None

        return None, f"Unrecognized country '{val}'. Must be valid 2-letter ISO or recognized name."

    @staticmethod
    def coalesce_val(val: Any, params: dict[str, Any]) -> tuple[Any, str | None]:
        """Fallback to default if value is null or empty."""
        default = params.get("default")
        if val is None or str(val).strip() in ("", "NULL", "none", "nan"):
            return default, None
        return val, None

    @staticmethod
    def uuid_v5_from_key(val: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
        """Generates deterministic UUIDv5 from natural key string."""
        if val is None or not str(val).strip():
            if params.get("required", False):
                return None, "Natural key is missing for UUID generation"
            return str(uuid.uuid4()), None

        namespace_str = params.get("namespace", "data-migration.internal")
        ns = uuid.uuid5(uuid.NAMESPACE_DNS, namespace_str)
        generated_uuid = str(uuid.uuid5(ns, str(val).strip()))
        return generated_uuid, None


# Map transformation rule IDs to callable handlers
TRANSFORM_HANDLERS = {
    "DIRECT_COPY": TransformationRegistry.direct_copy,
    "TRIM_CLEAN": TransformationRegistry.trim_clean,
    "CONCAT_WS": TransformationRegistry.concat_ws,
    "SPLIT_NAME": TransformationRegistry.split_name,
    "DATE_TO_ISO8601": TransformationRegistry.date_to_iso8601,
    "PHONE_TO_E164": TransformationRegistry.phone_to_e164,
    "EMAIL_NORMALIZE": TransformationRegistry.email_normalize,
    "CLEAN_CURRENCY_TO_FLOAT": TransformationRegistry.clean_currency_to_float,
    "ENUM_LOOKUP": TransformationRegistry.enum_lookup,
    "COUNTRY_TO_ISO2": TransformationRegistry.country_to_iso2,
    "COALESCE_VAL": TransformationRegistry.coalesce_val,
    "UUID_V5_FROM_KEY": TransformationRegistry.uuid_v5_from_key,
}

# Catalog of available rules for AI agent and UI
SUPPORTED_RULES: list[TransformationRuleSpec] = [
    TransformationRuleSpec(
        rule_id="DIRECT_COPY",
        name="Direct Pass-Through",
        description="Copies source field directly with optional null fallback",
        optional_params=["default", "required"],
        example_input="'ABC-123'",
        example_output="'ABC-123'",
    ),
    TransformationRuleSpec(
        rule_id="TRIM_CLEAN",
        name="Trim Whitespace",
        description="Strips leading, trailing, and redundant consecutive spaces",
        optional_params=["required"],
        example_input="'  John   Doe '",
        example_output="'John Doe'",
    ),
    TransformationRuleSpec(
        rule_id="SPLIT_NAME",
        name="Split Name to First/Last",
        description="Intelligently parses 'Last, First' or 'First Last' strings into first or last name components",
        required_params=["part"],
        optional_params=["required"],
        example_input="'Doe, Jane', part='first'",
        example_output="'Jane'",
    ),
    TransformationRuleSpec(
        rule_id="CONCAT_WS",
        name="Concatenate With Separator",
        description="Joins multiple source fields using a designated delimiter",
        optional_params=["separator", "required"],
        example_input="['John', 'Doe'], separator=' '",
        example_output="'John Doe'",
    ),
    TransformationRuleSpec(
        rule_id="DATE_TO_ISO8601",
        name="Normalize Date to ISO-8601",
        description="Parses heterogeneous formats (MM/DD/YYYY, YYYY-MM-DD, Epoch) to UTC ISO-8601 string",
        optional_params=["required"],
        example_input="'12/25/2023'",
        example_output="'2023-12-25T00:00:00Z'",
    ),
    TransformationRuleSpec(
        rule_id="PHONE_TO_E164",
        name="Normalize Phone to E.164",
        description="Strips non-numeric symbols and formats into international standard +1XXXXXXXXXX",
        optional_params=["default_country_code", "required"],
        example_input="'(555) 234-5678'",
        example_output="'+15552345678'",
    ),
    TransformationRuleSpec(
        rule_id="EMAIL_NORMALIZE",
        name="Normalize & Validate Email",
        description="Lowercases, trims, and validates strict RFC email syntax",
        optional_params=["required"],
        example_input="' John.Doe@GMAIL.com '",
        example_output="'john.doe@gmail.com'",
    ),
    TransformationRuleSpec(
        rule_id="CLEAN_CURRENCY_TO_FLOAT",
        name="Currency to Float",
        description="Strips symbols ($, €, commas) and converts to 2-decimal float, handling accounting negatives '(25.00)'",
        optional_params=["default", "required"],
        example_input="'$1,420.50'",
        example_output="1420.50",
    ),
    TransformationRuleSpec(
        rule_id="ENUM_LOOKUP",
        name="Categorical Enum Mapping",
        description="Maps legacy integer/string codes to modern enum strings with optional fallback",
        required_params=["mapping"],
        optional_params=["fallback", "case_sensitive", "required"],
        example_input="'1', mapping={'1': 'ACTIVE', '0': 'INACTIVE'}",
        example_output="'ACTIVE'",
    ),
    TransformationRuleSpec(
        rule_id="COUNTRY_TO_ISO2",
        name="Country to ISO 2-Letter",
        description="Standardizes full names and 3-letter codes to standard 2-letter uppercase ISO (e.g. USA -> US)",
        optional_params=["default", "fallback", "required"],
        example_input="'United States'",
        example_output="'US'",
    ),
    TransformationRuleSpec(
        rule_id="COALESCE_VAL",
        name="Coalesce Default",
        description="Substitutes default value when source is null, empty string, or 'NULL'",
        required_params=["default"],
        example_input="null, default='STANDARD'",
        example_output="'STANDARD'",
    ),
    TransformationRuleSpec(
        rule_id="UUID_V5_FROM_KEY",
        name="Deterministic UUIDv5",
        description="Creates collision-resistant, reproducible UUIDv5 from a natural identifier key",
        optional_params=["namespace", "required"],
        example_input="'LEGACY-CUST-10492'",
        example_output="'c0a80101-0000-5000-8000-000000000001'",
    ),
]


def apply_transformation(rule_id: str, val: Any, params: dict[str, Any]) -> tuple[Any, str | None]:
    """Applies a transformation rule and returns (result, error_message)."""
    handler = TRANSFORM_HANDLERS.get(rule_id)
    if not handler:
        return None, f"Unsupported transformation rule '{rule_id}'"
    try:
        return handler(val, params)
    except Exception as exc:
        return None, f"Execution failure in transform '{rule_id}': {exc!s}"
