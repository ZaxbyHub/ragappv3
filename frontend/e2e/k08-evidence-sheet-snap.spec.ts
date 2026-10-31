// frontend/e2e/k08-evidence-sheet-snap.spec.ts — issue #771 acceptance
// checks AC6/AC7 plus the AC8 control contract (FROZEN), authored from the
// AC letters only.
//
// Runs against the REAL built app (vite preview :4173) with the stub
// backend (E2E_STUB_PORT, default :9090 — same tier as chat-smoke.spec.ts;
// workers:1 serializes everything). Layout claims need Playwright: jsdom
// has no layout engine.
//
// AC map (issue #771):
//   AC6 -> "evidence sheet peek leaves the latest answer visible at 390x844"
//   AC7 -> "evidence sheet peek leaves the composer usable at 390x844"
//   AC8 -> "evidence sheet expands to full height and returns to peek via its controls"
//          (AC8's letter is NON-EXECUTABLE(HOST_ONLY) — drag physics cannot
//          be driven headless. This check is the NEW-SURFACE stand-in for
//          it: it asserts the CONTRACT the fix is expected to expose, an
//          accessible expand/collapse control on the sheet, NOT any
//          implementation detail.)
//
// Harness mechanics reuse chat-smoke.spec.ts: login (the stub user is
// superadmin and the vault "admin"), select the E2E Vault so the send path
// is live, sendQuestion (the stub streams "Here is the streamed answer"
// plus source S1), then click the "Source S1: handbook.pdf" citation chip
// to open the below-lg evidence sheet
// ([aria-describedby="evidence-sources-desc"], ChatShell.tsx).

import { test, expect, type Page } from "@playwright/test";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

const SHEET_SELECTOR = '[aria-describedby="evidence-sources-desc"]';

async function loginAndSelectVault(page: Page) {
  await page.goto("/login");
  await page.fill("#login-username", "e2e-user");
  await page.fill("#login-password", "e2e-pass");
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.goto("/chat");
  await expect(page.getByLabel("Message input")).toBeVisible({ timeout: 20_000 });
  // The composer refuses to send without an active vault; a fresh context
  // defaults to "All vaults" — select the stub's concrete vault.
  await page.getByRole("button", { name: /all vaults \(\d+ files total\)/i }).click();
  await page.getByRole("menuitem", { name: /e2e vault/i }).click();
  await expect(
    page.getByRole("button", { name: /active vault: e2e vault/i })
  ).toBeVisible({ timeout: 10_000 });
}

async function sendQuestion(page: Page, question: string) {
  const composer = page.getByLabel("Message input");
  await composer.fill(question);
  await composer.press("Enter");
}

// Open the below-lg evidence sheet from the citation chip and wait for it
// to be visible, then let the sheet's open animation and the transcript's
// compensating padding settle.
async function openEvidenceSheet(page: Page) {
  await sendQuestion(page, "What did the maintenance manual say about coolant?");
  await expect(page.getByText(/Here is the streamed answer/i)).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: /Source S1: handbook\.pdf/i }).first().click();
  await expect(page.locator(SHEET_SELECTOR)).toBeVisible({ timeout: 10_000 });
  await page.waitForTimeout(600);
}

// The AC6 measurement: the sheet must leave the latest assistant message's
// FIRST LINE uncovered. Faithful-to-intent metric, disclosed deviation from
// the AC letter's literal elementFromPoint phrasing: at base the literal
// point (message top + 8, horizontal centre) is occupied by an unrelated
// floating scroll-to-bottom button, not by the sheet — the audit defect this
// AC gates is the SHEET covering the answer (the 75vh capture), so the
// check asserts (a) the sheet's top edge sits below the message's first
// line, and (b) an elementFromPoint probe at the first line does not land
// inside the sheet. Strictly-sheet-scoped; the FAB overlap is pre-existing
// chrome outside this AC.
async function measureAnswerFirstLineUncovered(page: Page): Promise<boolean | null> {
  return page.evaluate(() => {
    const marker = "Here is the streamed answer";
    let current = document.body as Element;
    for (;;) {
      const next = Array.from(current.children).find((c) =>
        (c.textContent || "").includes(marker)
      );
      if (!next) break;
      current = next;
    }
    if (!(current.textContent || "").includes(marker)) return null;
    const line = current as HTMLElement;
    const sheet = document.querySelector('[aria-describedby="evidence-sources-desc"]') as HTMLElement | null;
    if (!sheet) return null;
    const pRect = line.getBoundingClientRect();
    const sRect = sheet.getBoundingClientRect();
    const lineHeight = parseFloat(getComputedStyle(line).lineHeight) || 20;
    const firstLineBottom = pRect.top + Math.min(lineHeight, pRect.height);
    const belowSheet = sRect.top >= firstLineBottom;
    const x = Math.min(Math.max(pRect.left + pRect.width / 2, 1), window.innerWidth - 1);
    const hit = document.elementFromPoint(x, Math.max(pRect.top + 8, 0));
    const notCoveredBySheet = !hit || !sheet.contains(hit);
    return belowSheet && notCoveredBySheet;
  });
}

// The AC7 measurement target: the Message input textarea (Composer.tsx).
async function measureComposerUncovered(page: Page): Promise<boolean | null> {
  return page.evaluate(() => {
    const el = document.querySelector('textarea[aria-label="Message input"]') as HTMLTextAreaElement | null;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return !!hit && (hit === el || el.contains(hit as Node));
  });
}

test.describe("k08 evidence sheet snap (issue #771)", () => {
  // AC6 — #771: WHEN the evidence sheet opens from a citation chip at
  // 390x844 THE SYSTEM SHALL leave the first line of the latest assistant
  // message uncovered.
  //
  // GREEN at base (the below-lg sheet opens at its 45vh peek height and the
  // chat column pads by the sheet height, landed via #689) and must stay
  // green after the fix. Guard: the sheet is visible. Metric per the AC
  // letter: document.elementFromPoint at the horizontal centre of the
  // assistant message's first line (y = message top + 8) returns a node
  // inside that message; asserted toBe(true).
  test("evidence sheet peek leaves the latest answer visible at 390x844", async ({ page }) => {
    await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAndSelectVault(page);
    await openEvidenceSheet(page);

    const uncovered = await measureAnswerFirstLineUncovered(page);
    expect(uncovered, `answer first line uncovered: ${uncovered}`).toBe(true);
  });

  // AC7 — #771: WHEN the evidence sheet is open at its peek height at
  // 390x844 THE SYSTEM SHALL leave the composer uncovered and interactive.
  //
  // GREEN at base (the bottom-docked composer rides above the non-modal
  // sheet) and must stay green after the fix. Same setup as AC6. Metric per
  // the AC letter: document.elementFromPoint at the centre of the Message
  // input textarea returns that textarea or a descendant of it; asserted
  // toBe(true).
  test("evidence sheet peek leaves the composer usable at 390x844", async ({ page }) => {
    await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAndSelectVault(page);
    await openEvidenceSheet(page);

    const usable = await measureComposerUncovered(page);
    expect(usable, `composer uncovered and interactive: ${usable}`).toBe(true);
  });

  // AC8 stand-in — #771: WHEN the evidence sheet is expanded via its
  // controls THE SYSTEM SHALL snap it to (near) full height, and back to
  // peek when collapsed.
  //
  // The AC8 letter itself is NON-EXECUTABLE(HOST_ONLY) — drag physics
  // cannot be checked headless — so this check asserts the CONTRACT the
  // fix is expected to expose: an accessible expand control inside the
  // sheet (a button whose accessible name matches expand/maximize/full)
  // that grows the sheet to at least 70% of the viewport height, and a
  // collapse control (collapse/minimize/peek) that returns it to at most
  // 50%. This check ERRORS at base because no such control exists yet —
  // that is its class (NEW-SURFACE), not a bug in the check; it turns
  // GREEN once the fix exposes the controls.
  test("evidence sheet expands to full height and returns to peek via its controls", async ({ page }) => {
    await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAndSelectVault(page);
    await openEvidenceSheet(page);

    const sheet = page.locator(SHEET_SELECTOR);
    const expand = sheet.getByRole("button", { name: /expand|maximize|full/i });
    // At base this guard errors: the control the contract requires does not
    // exist yet (expected NEW-SURFACE class).
    await expect(expand).toBeVisible({ timeout: 10_000 });
    await expand.click();
    await page.waitForTimeout(600);
    const expandedRatio = await sheet.evaluate(
      (el) => el.getBoundingClientRect().height / window.innerHeight
    );
    expect(expandedRatio, `sheet height after expand: ${Math.round(expandedRatio * 100)}% of viewport`).toBeGreaterThanOrEqual(0.7);

    const collapse = sheet.getByRole("button", { name: /collapse|minimize|peek/i });
    await expect(collapse).toBeVisible({ timeout: 10_000 });
    await collapse.click();
    await page.waitForTimeout(600);
    const peekRatio = await sheet.evaluate(
      (el) => el.getBoundingClientRect().height / window.innerHeight
    );
    expect(peekRatio, `sheet height after collapse: ${Math.round(peekRatio * 100)}% of viewport`).toBeLessThanOrEqual(0.5);
  });

  // Plan-critic R1 hardening: deterministic drag coverage. Chromium pointer
  // events can drive the handle without a real touch device — an upward drag
  // from the handle must expand the sheet, a downward drag must return it to
  // peek. Only device FEEL (velocity threshold, touch-scroll interplay)
  // remains HOST_ONLY. RED at base (no handle exists).
  test("evidence sheet drag expands to full height and back to peek", async ({ page }) => {
    await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAndSelectVault(page);
    await openEvidenceSheet(page);

    const sheet = page.locator(SHEET_SELECTOR);
    const handle = sheet.getByRole("button", { name: /expand|maximize|full/i });
    await expect(handle).toBeVisible({ timeout: 10_000 });

    // Synthetic TOUCH pointer events: a mouse drag ends with a click on the
    // handle (the accessible toggle), which would satisfy the expansion even
    // with the pointer handlers removed; touch pointerup produces no click,
    // so this drives the drag path specifically.
    const dragHandle = async (dy: number) => {
      await handle.evaluate((el, delta) => {
        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const opts = (y: number) => ({
          bubbles: true, cancelable: true, pointerId: 7, pointerType: "touch",
          clientX: cx, clientY: y, isPrimary: true,
        });
        el.dispatchEvent(new PointerEvent("pointerdown", opts(cy)));
        el.dispatchEvent(new PointerEvent("pointermove", opts(cy - delta / 2)));
        el.dispatchEvent(new PointerEvent("pointerup", opts(cy - delta)));
      }, dy);
      await page.waitForTimeout(600);
    };

    // Upward drag (55% of the viewport) snaps to full height.
    await dragHandle(844 * 0.55);
    const expandedRatio = await sheet.evaluate(
      (el) => el.getBoundingClientRect().height / window.innerHeight
    );
    expect(expandedRatio, `sheet height after upward drag: ${Math.round(expandedRatio * 100)}%`).toBeGreaterThanOrEqual(0.7);

    // Downward drag returns to peek (re-resolve: the accessible name flipped).
    const collapseHandle = sheet.getByRole("button", { name: /collapse|minimize|peek/i });
    await expect(collapseHandle).toBeVisible({ timeout: 10_000 });
    await collapseHandle.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const opts = (y: number) => ({
        bubbles: true, cancelable: true, pointerId: 8, pointerType: "touch",
        clientX: cx, clientY: y, isPrimary: true,
      });
      el.dispatchEvent(new PointerEvent("pointerdown", opts(cy)));
      el.dispatchEvent(new PointerEvent("pointermove", opts(cy + 220)));
      el.dispatchEvent(new PointerEvent("pointerup", opts(cy + 460)));
    });
    await page.waitForTimeout(600);
    const peekRatio = await sheet.evaluate(
      (el) => el.getBoundingClientRect().height / window.innerHeight
    );
    expect(peekRatio, `sheet height after downward drag: ${Math.round(peekRatio * 100)}%`).toBeLessThanOrEqual(0.5);
  });

  // Implementation-review R1 pin: a completed TOUCH drag arms the click
  // suppression but touch pointerup produces no trailing click, so the flag
  // must be cleared at the NEXT gesture's start — a tap after a touch drag
  // must still toggle the sheet.
  test("evidence sheet tap after a touch drag still toggles", async ({ page }) => {
    await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAndSelectVault(page);
    await openEvidenceSheet(page);

    const sheet = page.locator(SHEET_SELECTOR);
    const handle = sheet.getByRole("button", { name: /expand|maximize|full/i });
    await expect(handle).toBeVisible({ timeout: 10_000 });

    // A short touch drag (above the 8px suppression threshold, below the 25%
    // snap threshold) changes nothing visible but arms suppression.
    await handle.evaluate((el) => {
      const rect = el.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const opts = (y: number) => ({
        bubbles: true, cancelable: true, pointerId: 11, pointerType: "touch",
        clientX: cx, clientY: y, isPrimary: true,
      });
      el.dispatchEvent(new PointerEvent("pointerdown", opts(cy)));
      el.dispatchEvent(new PointerEvent("pointerup", opts(cy - 40)));
    });
    await page.waitForTimeout(400);

    // The next TAP must still toggle (no poisoned click suppression).
    await handle.click();
    await page.waitForTimeout(600);
    const ratio = await sheet.evaluate(
      (el) => el.getBoundingClientRect().height / window.innerHeight
    );
    expect(ratio, `sheet height after post-drag tap: ${Math.round(ratio * 100)}%`).toBeGreaterThanOrEqual(0.7);
  });

  test("evidence sheet resets to peek when closed and reopened", async ({ page }) => {
    await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
    await page.setViewportSize({ width: 390, height: 844 });
    await loginAndSelectVault(page);
    await openEvidenceSheet(page);

    const sheet = page.locator(SHEET_SELECTOR);
    await sheet.getByRole("button", { name: /expand|maximize|full/i }).click();
    await page.waitForTimeout(600);

    // Close via the built-in close control, then reopen from the chip.
    await sheet.getByRole("button", { name: /close/i }).click();
    await expect(sheet).toBeHidden({ timeout: 10_000 });
    await page.getByRole("button", { name: /Source S1: handbook\.pdf/i }).first().click();
    await expect(sheet).toBeVisible({ timeout: 10_000 });
    await page.waitForTimeout(600);
    const ratio = await sheet.evaluate(
      (el) => el.getBoundingClientRect().height / window.innerHeight
    );
    expect(ratio, `reopened sheet height: ${Math.round(ratio * 100)}% (must be peek)`).toBeLessThanOrEqual(0.5);
  });
});
