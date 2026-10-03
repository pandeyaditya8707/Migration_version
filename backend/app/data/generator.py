import json
import os
import random


def generate_sample_dataset(count: int = 1000, output_path: str = "backend/app/data/sample_records.json"):
    # Fix seed for reproducible, deterministic test runs
    random.seed(42)

    first_names = [
        "James",
        "Mary",
        "Robert",
        "Patricia",
        "John",
        "Jennifer",
        "Michael",
        "Linda",
        "David",
        "Elizabeth",
        "William",
        "Barbara",
        "Richard",
        "Susan",
        "Joseph",
        "Jessica",
        "Thomas",
        "Sarah",
        "Charles",
        "Karen",
    ]
    last_names = [
        "Smith",
        "Johnson",
        "Williams",
        "Brown",
        "Jones",
        "Garcia",
        "Miller",
        "Davis",
        "Rodriguez",
        "Martinez",
        "Hernandez",
        "Lopez",
        "Gonzalez",
        "Wilson",
        "Anderson",
        "Thomas",
        "Taylor",
        "Moore",
        "Jackson",
        "Martin",
    ]
    domains = ["gmail.com", "yahoo.com", "outlook.com", "enterprise.org", "acme-corp.com", "techbiz.io"]
    countries = [
        "USA",
        "United States",
        "US",
        "Canada",
        "CA",
        "United Kingdom",
        "UK",
        "GB",
        "Australia",
        "AU",
        "Germany",
        "DE",
    ]

    records = []

    for i in range(1, count + 1):
        account_id = f"LEGACY-CUST-{i:05d}"
        fn = random.choice(first_names)
        ln = random.choice(last_names)
        dom = random.choice(domains)

        # Decide whether to introduce a dirty edge-case
        anomaly_type = None
        if i % 25 == 0:
            anomaly_type = "BAD_DATE"
        elif i % 33 == 0:
            anomaly_type = "BAD_PHONE"
        elif i % 40 == 0:
            anomaly_type = "BAD_EMAIL"
        elif i % 50 == 0:
            anomaly_type = "UNKNOWN_STATUS"
        elif i % 70 == 0:
            anomaly_type = "EMPTY_NAME"

        # 1. Full name raw
        if anomaly_type == "EMPTY_NAME":
            full_name = ""
        elif i % 3 == 0:
            full_name = f"{ln}, {fn}"
        elif i % 5 == 0:
            full_name = f"  {fn}   {ln}  "
        else:
            full_name = f"{fn} {ln}"

        # 2. Email
        if anomaly_type == "BAD_EMAIL":
            email = f"{fn.lower()}.{ln.lower()}-no-at-sign"
        elif i % 4 == 0:
            email = f" {fn.upper()}.{ln}@{dom} "
        else:
            email = f"{fn.lower()}.{ln.lower()}@{dom}"

        # 3. Phone
        if anomaly_type == "BAD_PHONE":
            phone = "911"
        elif i % 4 == 0:
            phone = f"({random.randint(200, 999)}) {random.randint(100, 999)}-{random.randint(1000, 9999)}"
        elif i % 6 == 0:
            phone = f"{random.randint(200, 999)}.{random.randint(100, 999)}.{random.randint(1000, 9999)}"
        else:
            phone = f"+1{random.randint(2000000000, 9999999999)}"

        # 4. Signup Date
        if anomaly_type == "BAD_DATE":
            date_str = "INVALID_TIMESTAMP"
        elif i % 3 == 0:
            date_str = f"{random.randint(1, 12):02d}/{random.randint(1, 28):02d}/202{random.randint(0, 4)}"
        elif i % 5 == 0:
            date_str = f"202{random.randint(0, 4)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
        else:
            date_str = f"202{random.randint(0, 4)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}T10:30:00Z"

        # 5. Status code: 1=ACTIVE, 0=INACTIVE, 9=TERMINATED, X=UNKNOWN
        if anomaly_type == "UNKNOWN_STATUS":
            status_cd = "X"
        elif i % 7 == 0:
            status_cd = "9"
        elif i % 11 == 0:
            status_cd = "0"
        else:
            status_cd = "1"

        # 6. Balance due
        if i % 15 == 0:
            balance_str = f"(${random.randint(10, 500)}.{random.randint(0, 99):02d})"
        elif i % 8 == 0:
            balance_str = f"${random.randint(50, 5000)}.{random.randint(0, 99):02d}"
        else:
            balance_str = f"{random.randint(0, 3000)}.{random.randint(0, 99):02d}"

        # 7. Risk flag
        if i % 10 == 0:
            risk = "HIGH"
        elif i % 6 == 0:
            risk = "Y"
        elif i % 4 == 0:
            risk = "LOW"
        else:
            risk = "N"

        # 8. Country
        country = random.choice(countries)

        rec = {
            "legacy_account_id": account_id,
            "full_name_raw": full_name,
            "email_address": email,
            "phone_raw": phone,
            "signup_date_str": date_str,
            "account_status_code": status_cd,
            "balance_due_str": balance_str,
            "risk_flag": risk,
            "country_code_raw": country,
        }
        records.append(rec)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)

    print(f"Successfully generated {len(records)} sample records in {output_path}")


if __name__ == "__main__":
    generate_sample_dataset()
