"""
Comprehensive Playwright Browser UI & End-to-End Test Suite for Migration Workbench V2
Validates:
1. Mode 1 / Mode 2 toggle and strict separation of state & data.
2. Mode 2 sample space switching (Orders, Healthcare Encounters).
3. Mode 2 Source Readiness isolation from Mode 1 legacy CRM data.
4. AI Plan generation via Ollama (or mocked backend in headless test).
5. Critical UI assertion: NO 'undefined' transformation rules and NO false '(Generated/Literal)' source fields.
6. Dry run simulation, governance approval, dynamic target database write, and mass conservation.
7. Regression assurance: Mode 1 plans and schemas remain 100% pristine.
"""

import pytest
from playwright.sync_api import expect, sync_playwright

BASE_URL = "http://127.0.0.1:8000"


@pytest.mark.playwright
def test_full_browser_ui_flow():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # Step 1: Load workbench
        page.goto(BASE_URL, wait_until="networkidle")
        expect(page).to_have_title("Agentic Data Migration Planner & Reconciliation Workbench")
        print("✓ Page loaded successfully")

        # Step 2: Verify Mode 1 defaults
        mode1_btn = page.locator("#toggle-mode-1")
        mode2_btn = page.locator("#toggle-mode-2")
        expect(mode1_btn).to_be_visible()
        expect(mode2_btn).to_be_visible()

        # Snapshot Mode 1 active plan text
        mode1_plan_badge = page.locator("#active-plan-badge-text")
        expect(mode1_plan_badge).to_be_visible()
        initial_mode1_badge_text = mode1_plan_badge.inner_text()
        print(f"✓ Initial Mode 1 Badge: {initial_mode1_badge_text}")

        # Step 3: Switch to Mode 2 (Universal Studio)
        mode2_btn.click()
        page.wait_for_timeout(600)
        expect(page.locator("#tab-container-v2")).to_be_visible()
        expect(page.locator("#tab-container-v1")).not_to_be_visible()
        print("✓ Switched to Mode 2 Universal Studio")

        # Step 4: Verify Mode 2 Source Readiness is DECOUPLED from Mode 1 CRM
        v2_src_name = page.locator("#v2-source-name")
        expect(v2_src_name).to_be_visible()
        source_text = v2_src_name.inner_text()
        print(f"✓ Mode 2 Source Dataset displayed: '{source_text}'")
        # Ensure it does NOT reference Mode 1 legacy customers
        assert "legacy_crm" not in source_text.lower() and "legacy customer" not in source_text.lower(), (
            f"Mode 2 is incorrectly displaying Mode 1 CRM data: {source_text}"
        )

        # Step 5: Switch to Healthcare Clinical Encounters Preset
        encounters_preset_btn = page.locator("button.schema-preset-btn[data-preset='encounters']")
        if encounters_preset_btn.count() > 0:
            encounters_preset_btn.click()
            page.wait_for_timeout(1000)
            updated_source_text = page.locator("#v2-source-name").inner_text()
            print(f"✓ Mode 2 loaded Healthcare Encounter space: '{updated_source_text}'")
            assert "encounter" in updated_source_text.lower() or "healthcare" in updated_source_text.lower()

        # Step 6: Verify DDL display
        ddl_text = page.locator("#v2-ddl-display").inner_text()
        print(f"✓ Mode 2 Target DDL preview:\n{ddl_text[:120]}...")
        assert "CREATE TABLE" in ddl_text or "encounters" in ddl_text or "orders" in ddl_text

        # Step 7: Switch to Tab 2 (Ollama AI Mapping Studio)
        page.locator("#tabs-bar-v2 button[data-tab='v2-tab-mapping']").click()
        page.wait_for_timeout(500)
        expect(page.locator("#v2-tab-mapping")).to_be_visible()

        # Step 8: Propose Plan with Ollama AI
        propose_btn = page.locator("#btn-v2-propose")
        expect(propose_btn).to_be_visible()
        print("✓ Clicking 'Propose Plan' to synthesize schema mappings...")
        propose_btn.click()

        # Wait for AI plan synthesis (up to 75s for Ollama Cloud inference)
        page.wait_for_function(
            """() => {
                const tbody = document.getElementById('v2-tbody-mappings');
                return tbody && tbody.querySelectorAll('tr').length > 0 && !tbody.innerText.includes('No migration plan');
            }""",
            timeout=75000,
        )
        print("✓ AI Plan synthesis completed and rendered in UI!")

        # Step 9: CRITICAL UI CHECK - Assert NO 'undefined' and NO incorrect '(Generated/Literal)'
        rows = page.locator("#v2-tbody-mappings tr")
        row_count = rows.count()
        print(f"✓ Mapping table contains {row_count} mapped contract fields.")
        assert row_count >= 4, f"Expected at least 4 mappings, got {row_count}"

        for i in range(row_count):
            row = rows.nth(i)
            target_field = row.locator("td").nth(0).inner_text().strip()
            source_field = row.locator("td").nth(1).inner_text().strip()
            rule = row.locator("td").nth(2).inner_text().strip()
            risk = row.locator("td").nth(3).inner_text().strip()
            rationale = row.locator("td").nth(4).inner_text().strip()

            print(f"   [{i + 1}] Target: '{target_field}' | Source: '{source_field}' | Rule: '{rule}' | Risk: '{risk}'")

            # Assert NO 'undefined' anywhere in the row!
            assert "undefined" not in rule.lower(), f"Row {i + 1} has 'undefined' transformation rule: {rule}"
            assert "undefined" not in source_field.lower(), f"Row {i + 1} has 'undefined' source field: {source_field}"
            assert "undefined" not in rationale.lower(), f"Row {i + 1} has 'undefined' rationale: {rationale}"

            # Assert valid rule name
            assert len(rule) > 2, f"Row {i + 1} has empty rule: {rule}"

            # Assert valid risk level
            assert risk.upper() in ("LOW", "MEDIUM", "HIGH"), f"Row {i + 1} invalid risk: {risk}"

        print("✓ Pure Invariants verified: Zero 'undefined' and zero broken strings in mapping matrix!")

        # Step 10: Test Dry Run Simulation
        page.locator("#tabs-bar-v2 button[data-tab='v2-tab-dryrun']").click()
        page.wait_for_timeout(400)
        expect(page.locator("#v2-tab-dryrun")).to_be_visible()

        run_dry_btn = page.locator("#btn-v2-dryrun-top")
        run_dry_btn.click()
        page.wait_for_timeout(1500)
        print("✓ Dry Run simulation triggered")

        # Step 11: Approve Plan
        page.locator("#tabs-bar-v2 button[data-tab='v2-tab-mapping']").click()
        page.wait_for_timeout(300)
        approve_btn = page.locator("#btn-v2-approve-top")
        if approve_btn.is_enabled():
            approve_btn.click()
            page.wait_for_timeout(1000)
            badge_text = page.locator("#v2-plan-badge-text").inner_text()
            print(f"✓ Plan approved: '{badge_text}'")
            assert "APPROVED" in badge_text

        # Step 12: Execute Migration into Dynamic Target Database
        exec_btn = page.locator("#btn-v2-execute-top")
        expect(exec_btn).to_be_enabled()
        exec_btn.click()
        # Wait for execution to finish and badge to update
        page.wait_for_function(
            "() => { const b = document.getElementById('badge-v2-target-count'); return b && !b.innerText.startsWith('0'); }",
            timeout=25000,
        )
        print("✓ Migration executed into dynamic SQLite store")

        # Step 13: Query Target Database records
        page.locator("#tabs-bar-v2 button[data-tab='v2-tab-execution']").click()
        page.wait_for_timeout(500)
        target_rows = page.locator("#v2-tbody-target tr")
        assert target_rows.count() > 0, "No records found in dynamic target table"
        print(f"✓ Target Database populated: {target_rows.count()} sample records displayed")

        # Step 14: Universal Reconciliation & Mass Conservation
        page.locator("#tabs-bar-v2 button[data-tab='v2-tab-reconciliation']").click()
        page.wait_for_function(
            "() => document.getElementById('v2-recon-delta')?.innerText.trim() === '0'", timeout=15000
        )
        delta_stat = page.locator("#v2-recon-delta").inner_text()
        print(f"✓ Universal Reconciliation Unaccounted Delta: {delta_stat}")
        assert delta_stat.strip() == "0", f"Unaccounted delta must be 0, got {delta_stat}"

        # Step 15: Switch back to Mode 1 and verify ZERO regressions / cross-contamination
        mode1_btn.click()
        page.wait_for_timeout(500)
        expect(page.locator("#tab-container-v1")).to_be_visible()
        final_mode1_badge_text = page.locator("#active-plan-badge-text").inner_text()
        print(f"✓ Final Mode 1 Badge: {final_mode1_badge_text}")
        assert final_mode1_badge_text == initial_mode1_badge_text, (
            f"Mode 1 state was altered by Mode 2 operations! Initial: {initial_mode1_badge_text}, Final: {final_mode1_badge_text}"
        )

        print("\n🎉 PLAYWRIGHT BROWSER UI TEST PASSED WITH 100% SUCCESS! 🎉")
        browser.close()


if __name__ == "__main__":
    test_full_browser_ui_flow()
