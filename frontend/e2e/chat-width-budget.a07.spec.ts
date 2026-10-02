// frontend/e2e/chat-width-budget.a07.spec.ts — A07 width/render-budget
// acceptance checks against the REAL built app (vite preview :4173) with the
// stub backend (:9090). Selectors follow chat-smoke.spec.ts (aria-labels /
// roles read from the component code):
//   * composer textarea  aria-label="Message input"    (Composer.tsx)
//   * send button        aria-label="Send message"     (Composer.tsx)
//   * citation chip      aria-label "Source S1: handbook.pdf" (SourceCitation.tsx)
//   * main column        <main>                        (ChatShell.tsx)
//   * login form         #login-username / #login-password (LoginPage.tsx)

import { test, expect, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/login");
  await page.fill("#login-username", "e2e-user");
  await page.fill("#login-password", "e2e-pass");
  await page.getByRole("button", { name: /sign in/i }).click();
  // "/" redirects to /documents — go to the chat surface explicitly.
  await page.goto("/chat");
  await expect(page.getByLabel("Message input")).toBeVisible({ timeout: 20_000 });
  // The composer refuses to send without an active vault ("Please select a
  // vault before starting a chat"), and a fresh context defaults to "All
  // vaults" — select the stub's concrete vault so the send path is live.
  await page
    .getByRole("button", { name: /all vaults \(\d+ files total\)/i })
    .click();
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

test.describe("chat width budget (A07 AC1-3)", () => {
  test("Send stays inside the composer at 1024", async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await login(page);

    // The composer container is the rounded card that owns the toolbar — the
    // textarea's parent div (verified in Composer.tsx's DOM structure).
    const composerContainer = page.getByLabel("Message input").locator("..");
    const composerBox = await composerContainer.boundingBox();
    const sendBox = await page
      .getByRole("button", { name: "Send message" })
      .boundingBox();

    const inside =
      composerBox !== null &&
      sendBox !== null &&
      sendBox.x >= composerBox.x &&
      sendBox.y >= composerBox.y &&
      sendBox.x + sendBox.width <= composerBox.x + composerBox.width &&
      sendBox.y + sendBox.height <= composerBox.y + composerBox.height;
    expect(inside).toBe(true);
  });

  test("768 px leaves a usable chat column", async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    await login(page);

    // Fresh load with the default rails: the main transcript column must
    // keep a usable width. Two <main> elements exist (the app shell's
    // #main-content wrapper and the chat column); the chat column is the
    // inner one.
    const chatColumn = page.locator('main:not(#main-content)');
    const mainBox = await chatColumn.boundingBox();
    const width = mainBox?.width ?? 0;
    expect(width).toBeGreaterThanOrEqual(360);
  });

  test("phone evidence sheet leaves the composer reachable", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);

    await sendQuestion(page, "Where is the coolant interval documented?");
    // The inline citation chip and the source-card list item share the
    // accessible name "Source S1: handbook.pdf" — take the first (chat-smoke
    // strict-mode note). This is the real user path to the evidence surface.
    const chip = page
      .getByRole("button", { name: /Source S1: handbook\.pdf/i })
      .first();
    await expect(chip).toBeVisible({ timeout: 20_000 });
    await chip.click();

    // On a phone the evidence pane is the bottom sheet (its title is
    // "Evidence"); wait until it is actually open.
    await expect(
      page.getByRole("dialog").filter({ hasText: "Evidence" }).first()
    ).toBeVisible({ timeout: 10_000 });
    // The sheet slides in over ~500ms; visibility of the dialog fires while
    // it is still translating, so the measurement below must wait for the
    // animation to settle or the base tree can race to a false pass
    // (checkpoint amend CHECK_WRONG: the frozen check measured too early).
    await page.waitForTimeout(1_000);

    // The composer's textarea must still be the topmost element at its own
    // center — the sheet must not cover it.
    const hitsTextarea = await page.evaluate(() => {
      const textarea = document.querySelector(
        'textarea[aria-label="Message input"]'
      );
      if (!textarea) return false;
      const rect = textarea.getBoundingClientRect();
      const hit = document.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2
      );
      return hit === textarea || (hit !== null && textarea.contains(hit));
    });
    expect(hitsTextarea).toBe(true);
  });
});
