import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        print("Navigating to app...")
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")

        # Propose a fresh plan (unpatched initial version)
        print("Proposing fresh plan via #btn-propose-new-plan...")
        propose_btn = page.locator("#btn-propose-new-plan")
        if await propose_btn.is_visible():
            await propose_btn.click()
            await page.wait_for_timeout(1800)

        # Tab 3: Dry-Run Simulation
        print("Navigating to dry run...")
        await page.click("#tab-btn-dryrun")
        await page.wait_for_timeout(800)
        run_btn = page.locator("#btn-execute-dry-run-main")
        if await run_btn.is_visible():
            print("Running initial simulation...")
            await run_btn.click()
            await page.wait_for_selector("#tbody-quarantine-ledger tr td", timeout=15000)
            await page.wait_for_timeout(1500)

        # Screenshot before fixes (showing ~98 quarantined records and AI suggestions)
        await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/quarantine_before_fixes.png")
        print("Captured quarantine_before_fixes.png")

        # Open modal on first row
        first_evidence_btn = page.locator("#tbody-quarantine-ledger tr button.btn-secondary").first
        if await first_evidence_btn.is_visible():
            print("Opening forensic evidence modal...")
            await first_evidence_btn.click()
            await page.wait_for_selector("#record-modal.active", timeout=5000)
            await page.wait_for_timeout(800)

            # Test live diagnosis
            live_diag_btn = page.locator("#btn-live-ai-diagnose")
            if await live_diag_btn.is_visible():
                print("Clicking live AI diagnosis...")
                await live_diag_btn.click()
                await page.wait_for_selector("#live-ai-diagnosis-result div", timeout=15000)
                await page.wait_for_timeout(1000)

            await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/modal_with_live_ai_diagnosis.png")
            print("Captured modal_with_live_ai_diagnosis.png")

            # Click implement fix in modal
            modal_fix_btn = page.locator("#btn-modal-apply-fix")
            if await modal_fix_btn.is_visible():
                print("Implementing AI fix from inside modal...")
                await modal_fix_btn.click()
                await page.wait_for_timeout(2500)

        # Screenshot after single fix
        await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/quarantine_after_single_fix.png")
        print("Captured quarantine_after_single_fix.png")

        # Now test Auto-Apply All AI Fixes & Re-run
        auto_all_btn = page.locator("#btn-auto-fix-all-mode1")
        if await auto_all_btn.is_visible():
            print("Clicking Auto-Apply All AI Fixes & Re-run...")
            await auto_all_btn.click()
            await page.wait_for_timeout(3000)

        # Screenshot after all fixes
        await page.screenshot(path="/Users/adityapandey/.gemini/antigravity-ide/brain/36e2b5fc-c9c3-452a-890b-4b210f0ecde8/quarantine_after_all_fixes_zero.png")
        print("Captured quarantine_after_all_fixes_zero.png")

        # Check badge text
        badge = page.locator("#quarantine-count-badge")
        badge_text = await badge.inner_text()
        print(f"Final Quarantine Badge: {badge_text}")

        await browser.close()
        print("Verification completed successfully!")

if __name__ == "__main__":
    asyncio.run(run())
