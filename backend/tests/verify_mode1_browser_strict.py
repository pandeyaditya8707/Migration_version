import asyncio
from playwright.async_api import async_playwright

async def run_strict_mode1_browser_test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        print("1. Loading workbench...")
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")

        # Reset workbench to ensure clean state
        await page.evaluate("() => fetch('/api/reset', { method: 'POST' })")
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(1000)

        # Step 1: Verify Tab 1 (Profiler on 1,000 records)
        src_count_badge = await page.locator("#badge-source-count").inner_text()
        print(f"✓ Source Count Badge: '{src_count_badge}' (1,000 records)")
        assert "1,000" in src_count_badge

        # Step 2: Tab 2 (AI Mapping Studio)
        print("2. Synthesizing Plan...")
        await page.click("#tab-btn-mapping")
        await page.wait_for_timeout(1000)
        await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/mode1_strict_mapping.png")
        print("Captured mode1_strict_mapping.png")

        # Step 3: Tab 3 (Deterministic Dry-Run Simulation on 1,000 records)
        print("3. Executing Deterministic Dry-Run on 1,000 records...")
        await page.click("#tab-btn-dryrun")
        await page.wait_for_timeout(800)
        await page.click("#btn-execute-dry-run-main")
        await page.wait_for_selector("#tbody-quarantine-ledger tr td", timeout=15000)
        await page.wait_for_timeout(1500)

        # STRICT ASSERTION: Auto-Apply button must NOT exist or be visible in Mode 1
        auto_apply_btn = await page.query_selector("#btn-auto-fix-all-mode1")
        assert auto_apply_btn is None or not await auto_apply_btn.is_visible(), "CRITICAL: Auto-apply button must be HIDDEN in Mode 1!"
        print("✓ Verified: Auto-Apply All AI Fixes button is completely HIDDEN in Mode 1.")

        # STRICT ASSERTION: Single row Fix & Re-run button must NOT exist in Mode 1
        fix_rerun_btns = await page.query_selector_all("#tbody-quarantine-ledger button.btn-primary")
        assert len(fix_rerun_btns) == 0, "CRITICAL: No Fix & Re-run button allowed in Mode 1 table rows!"
        print("✓ Verified: Zero Fix & Re-run buttons in Mode 1 Quarantine Ledger rows.")

        # Screenshot Mode 1 dry-run table (clean forensic view)
        await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/mode1_strict_quarantine_ledger_clean.png")
        print("Captured mode1_strict_quarantine_ledger_clean.png")

        # Step 4: Open Forensic Evidence Modal
        print("4. Opening forensic evidence modal in Mode 1...")
        view_evidence_btn = page.locator("#tbody-quarantine-ledger tr button.btn-secondary").first
        await view_evidence_btn.click()
        await page.wait_for_selector("#record-modal.active", timeout=5000)
        await page.wait_for_timeout(800)

        # STRICT ASSERTION: No AI remediation box or fix buttons in Mode 1 modal
        modal_ai_box = await page.query_selector("#btn-modal-apply-fix")
        assert modal_ai_box is None, "CRITICAL: Implement AI Fix button must NOT exist in Mode 1 modal!"
        live_ai_btn = await page.query_selector("#btn-live-ai-diagnose")
        assert live_ai_btn is None, "CRITICAL: Live AI Diagnosis button must NOT exist in Mode 1 modal!"
        print("✓ Verified: Mode 1 modal contains pure forensic audit evidence (zero AI fix buttons).")

        await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/mode1_strict_modal_clean_forensics.png")
        print("Captured mode1_strict_modal_clean_forensics.png")

        # Close modal
        await page.click("#modal-close-btn")
        await page.wait_for_timeout(500)

        # Step 5: Strict Approval Gate & Execution
        print("5. Approving plan and executing migration...")
        approve_btn = page.locator("#btn-approve-plan-top")
        await approve_btn.click()
        await page.wait_for_timeout(1000)

        exec_btn = page.locator("#btn-execute-migration-top")
        await exec_btn.click()
        await page.wait_for_timeout(3000)

        # Step 6: Verify Target Store has 902 populated records
        print("6. Verifying Target Store View...")
        await page.click("#tab-btn-execution")
        await page.wait_for_timeout(1000)
        target_badge = await page.locator("#badge-target-count").inner_text()
        print(f"✓ Target Store Badge: {target_badge}")
        assert "902" in target_badge, f"Expected 902 in target store, got {target_badge}"

        await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/mode1_strict_target_store_902.png")
        print("Captured mode1_strict_target_store_902.png")

        # Step 7: Verify Universal Reconciliation (0 unaccounted delta)
        print("7. Verifying Universal Reconciliation & Audit...")
        await page.click("#tab-btn-reconciliation")
        await page.wait_for_timeout(1500)
        recon_delta = await page.locator("#recon-eq-unaccounted").inner_text()
        recon_target = await page.locator("#recon-eq-target").inner_text()
        recon_quar = await page.locator("#recon-eq-quar").inner_text()
        print(f"✓ Reconciliation Target Accepted: '{recon_target.strip()}' (902)")
        print(f"✓ Reconciliation Quarantined: '{recon_quar.strip()}' (98)")
        print(f"✓ Reconciliation Unaccounted Delta: '{recon_delta.strip()}' (0)")
        assert "0" in recon_delta, f"Expected 0 unaccounted delta, got {recon_delta}"
        assert "902" in recon_target, f"Expected 902 target, got {recon_target}"
        assert "98" in recon_quar, f"Expected 98 quarantined, got {recon_quar}"

        await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/mode1_strict_reconciliation_zero_delta.png")
        print("Captured mode1_strict_reconciliation_zero_delta.png")

        await browser.close()
        print("\n🎉 STRICT MODE 1 BROWSER END-TO-END TEST PASSED 100%! 🎉")

if __name__ == "__main__":
    asyncio.run(run_strict_mode1_browser_test())
