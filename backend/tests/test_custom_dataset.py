import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client():
    return TestClient(app)

def test_custom_csv_upload_and_migration(client):
    csv_content = """id,name,email,created,status_raw,amount
101,"Alice Wonder",alice@example.com,2023-01-10,1,$120.00
102,"Bob Builder",bob-at-example.com,2023-02-15,0,$85.50
103,"Charlie Brown",charlie@peanuts.org,INVALID_DATE,9,(20.00)
104,"Diana Prince",diana@amazon.net,2023-03-20,1,$500.00
"""
    # 1. Upload custom CSV
    files = {"file": ("my_users.csv", csv_content, "text/csv")}
    res_upload = client.post("/api/upload/source", files=files)
    assert res_upload.status_code == 200
    up_data = res_upload.json()
    assert up_data["total_records"] == 4
    assert up_data["schema"]["name"] == "my_users.csv"

    # 2. Check inferred source schema
    res_src = client.get("/api/schemas/source")
    assert res_src.status_code == 200
    src_data = res_src.json()
    assert src_data["total_sample_records"] == 4
    col_names = [f["name"] for f in src_data["schema"]["fields"]]
    assert "email" in col_names
    assert "amount" in col_names

    plan_ver = up_data["plan"]["version"]

    # 3. Check dynamically generated Plan
    res_plan = client.get(f"/api/plans/{plan_ver}")
    assert res_plan.status_code == 200
    plan = res_plan.json()
    assert len(plan["field_mappings"]) > 0

    # 4. Dry-run custom dataset
    res_dry = client.post(f"/api/plans/{plan_ver}/dry-run")
    assert res_dry.status_code == 200
    dry = res_dry.json()
    assert dry["total_source_records"] == 4
    # Bob has bad email, Charlie has invalid date -> quarantined!
    assert dry["rejected_count"] >= 1
    assert dry["accepted_count"] + dry["rejected_count"] == 4

    # 5. Approve plan
    res_appr = client.post(f"/api/plans/{plan_ver}/approve", json="Custom Engineer")
    assert res_appr.status_code == 200

    # 6. Execute custom migration
    res_exec = client.post(f"/api/plans/{plan_ver}/execute", json={"plan_version": plan_ver, "executed_by": "Custom Engineer"})
    assert res_exec.status_code == 200
    assert res_exec.json()["status"] == "SUCCESS"

    # 7. Verify Target CSV export
    res_export = client.get("/api/export/target?format=csv")
    assert res_export.status_code == 200
    assert "text/csv" in res_export.headers["content-type"]
    assert "Alice" in res_export.text or "diana@amazon.net" in res_export.text

    # 8. Verify Quarantine CSV export
    res_q_export = client.get("/api/export/quarantine")
    assert res_q_export.status_code == 200
    assert "text/csv" in res_q_export.headers["content-type"]
    assert "errors_detected" in res_q_export.text
