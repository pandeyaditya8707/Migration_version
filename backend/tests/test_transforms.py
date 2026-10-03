from app.engine.transforms import apply_transformation


def test_trim_clean():
    res, err = apply_transformation("TRIM_CLEAN", "   Alice   Smith   ", {})
    assert err is None
    assert res == "Alice Smith"


def test_split_name():
    # Last, First
    res, err = apply_transformation("SPLIT_NAME", "Pandey, Aditya", {"part": "first"})
    assert err is None and res == "Aditya"

    res, err = apply_transformation("SPLIT_NAME", "Pandey, Aditya", {"part": "last"})
    assert err is None and res == "Pandey"

    # First Last
    res, err = apply_transformation("SPLIT_NAME", "John Doe", {"part": "first"})
    assert err is None and res == "John"

    res, err = apply_transformation("SPLIT_NAME", "John Doe", {"part": "last"})
    assert err is None and res == "Doe"


def test_date_to_iso8601():
    res, err = apply_transformation("DATE_TO_ISO8601", "12/25/2023", {})
    assert err is None
    assert res == "2023-12-25T00:00:00Z"

    res, err = apply_transformation("DATE_TO_ISO8601", "2024-01-15", {})
    assert err is None
    assert res == "2024-01-15T00:00:00Z"

    # Invalid date
    res, err = apply_transformation("DATE_TO_ISO8601", "INVALID_DATE_STRING", {})
    assert res is None
    assert "Cannot parse" in err


def test_phone_to_e164():
    res, err = apply_transformation("PHONE_TO_E164", "(555) 234-5678", {"default_country_code": "1"})
    assert err is None
    assert res == "+15552345678"

    # Too short
    res, err = apply_transformation("PHONE_TO_E164", "123", {})
    assert res is None
    assert "too short" in err


def test_currency_to_float():
    res, err = apply_transformation("CLEAN_CURRENCY_TO_FLOAT", "$1,450.75", {})
    assert err is None
    assert res == 1450.75

    # Accounting negative
    res, err = apply_transformation("CLEAN_CURRENCY_TO_FLOAT", "(25.50)", {})
    assert err is None
    assert res == -25.50


def test_enum_lookup():
    mapping = {"1": "ACTIVE", "0": "INACTIVE", "9": "TERMINATED"}
    res, err = apply_transformation("ENUM_LOOKUP", "1", {"mapping": mapping})
    assert err is None and res == "ACTIVE"

    # Unknown with fallback
    res, err = apply_transformation("ENUM_LOOKUP", "X", {"mapping": mapping, "fallback": "SUSPENDED"})
    assert err is None and res == "SUSPENDED"

    # Unknown without fallback
    res, err = apply_transformation("ENUM_LOOKUP", "UNKNOWN_CODE", {"mapping": mapping})
    assert res is None
    assert "Unrecognized enum code" in err


def test_uuid_v5_determinism():
    res1, err1 = apply_transformation("UUID_V5_FROM_KEY", "LEGACY-CUST-100", {})
    res2, err2 = apply_transformation("UUID_V5_FROM_KEY", "LEGACY-CUST-100", {})
    assert err1 is None and err2 is None
    assert res1 == res2
    assert len(res1) == 36
