// frontend/e2e/chat-smoke.spec.ts — issue #573 (AC7) acceptance check C7.
//
// Four e2e smoke tests against the REAL built app (vite preview :4173) with
// the stub backend (:9090). Selectors are resilient (aria-labels / roles /
// ids read from the component code — no brittle CSS):
//   * composer textarea  aria-label="Message input"      (Composer.tsx)
//   * stop button        aria-label="Stop generating"    (Composer.tsx)
//   * interrupted banner [data-interrupted-status]       (TranscriptPane.tsx)
//   * citation chip      aria-label "Source S1: handbook.pdf" (SourceCitation.tsx)
//   * evidence pane      aside[aria-label="Details panel"]   (ChatShell.tsx)
//   * login form         #login-username / #login-password   (LoginPage.tsx)

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

/** Find the stub session whose user row contains `needle` (newest wins). */
async function findSessionIdByUserContent(page: Page, needle: string): Promise<number | null> {
  const listRes = await page.request.get("/api/chat/sessions");
  if (!listRes.ok()) return null;
  const { sessions } = (await listRes.json()) as { sessions?: Array<{ id: number }> };
  for (const s of sessions ?? []) {
    const detailRes = await page.request.get(`/api/chat/sessions/${s.id}`);
    if (!detailRes.ok()) continue;
    const detail = (await detailRes.json()) as {
      messages?: Array<{ role: string; content: string; status?: string }>;
    };
    const found = (detail.messages ?? []).some(
      (m) => m.role === "user" && m.content.includes(needle)
    );
    if (found) return s.id;
  }
  return null;
}

test.describe("chat smoke (issue #573 AC7 / C7)", () => {
  test("send: submitting a question streams an assistant answer", async ({ page }) => {
    await login(page);

    await sendQuestion(page, "What did the maintenance manual say about coolant?");

    // The user turn is rendered...
    await expect(
      page.getByText("What did the maintenance manual say about coolant?")
    ).toBeVisible({ timeout: 10_000 });
    // ...the assistant answer streams in...
    await expect(page.getByText(/Here is the streamed answer/i)).toBeVisible({
      timeout: 20_000,
    });
    // ...and the final sources from the done frame render a citation chip.
    await expect(
      page.getByRole("button", { name: /Source S1: handbook\.pdf/i }).first()
    ).toBeVisible({ timeout: 10_000 });
  });

  test("stop: interrupting a slow stream persists the turn as interrupted", async ({ page }) => {
    await login(page);

    // "SLOW" is a stub marker: one content chunk, then the stream stays open.
    await sendQuestion(page, "SLOW: tell me about the coolant interval");

    await expect(
      page.getByText(/coolant interval is 500 hours according to the manual/i)
    ).toBeVisible({ timeout: 20_000 });

    await page.getByRole("button", { name: "Stop generating" }).click();

    // The live turn shows the interrupted state.
    await expect(
      page.locator('[data-interrupted-status="interrupted"]').first()
    ).toBeVisible({ timeout: 10_000 });

    // Durable-turn contract: the interrupted exchange is saved server-side.
    let sessionId: number | null = null;
    await expect
      .poll(
        async () => {
          const id = await findSessionIdByUserContent(page, "SLOW:");
          if (!id) return null;
          const detail = (await (
            await page.request.get(`/api/chat/sessions/${id}`)
          ).json()) as { messages?: Array<{ role: string; status?: string }> };
          const hasInterrupted = (detail.messages ?? []).some(
            (m) => m.role === "assistant" && m.status === "interrupted"
          );
          return hasInterrupted ? id : null;
        },
        { timeout: 15_000 }
      )
      .toBeTruthy();
    sessionId = await findSessionIdByUserContent(page, "SLOW:");
    expect(sessionId).not.toBeNull();

    // Full page load of the session URL: the interrupted turn is still there.
    await page.goto(`/chat/${sessionId}`);
    await expect(
      page.locator('[data-interrupted-status="interrupted"]').first()
    ).toBeVisible({ timeout: 20_000 });
    await expect(
      page.getByText(/coolant interval is 500 hours/i).first()
    ).toBeVisible({ timeout: 10_000 });
  });

  test("reload: a completed exchange restores from the session history", async ({ page }) => {
    await login(page);

    const question = "What does the manual recommend for maintenance?";
    await sendQuestion(page, question);
    await expect(page.getByText(/Here is the streamed answer/i)).toBeVisible({
      timeout: 20_000,
    });

    // Wait for the durable save (persistTurn) to land, then deep-link it.
    let sessionId: number | null = null;
    await expect
      .poll(async () => {
        sessionId = await findSessionIdByUserContent(page, question);
        return sessionId;
      }, { timeout: 15_000 })
      .toBeTruthy();

    await page.goto(`/chat/${sessionId}`);
    // .first(): the follow-up-suggestions region (issue #573 AC1) may render
    // a suggestion button whose text embeds the question's topic.
    await expect(page.getByText(question).first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(/Here is the streamed answer/i)).toBeVisible({
      timeout: 20_000,
    });
    await expect(
      page.getByRole("button", { name: /Source S1: handbook\.pdf/i }).first()
    ).toBeVisible({ timeout: 10_000 });
  });

  test("cite: a source citation chip opens the source card", async ({ page }) => {
    await login(page);

    await sendQuestion(page, "Where is the coolant interval documented?");
    const chip = page.getByRole("button", { name: /Source S1: handbook\.pdf/i }).first();
    await expect(chip).toBeVisible({ timeout: 20_000 });

    await chip.click();

    // The SourceSpanPopover opens with the source's metadata...
    await expect(
      page.getByRole("dialog").filter({ hasText: "handbook.pdf" })
    ).toBeVisible({ timeout: 10_000 });
    // ...and the evidence/details pane opens.
    await expect(page.locator('aside[aria-label="Details panel"]')).toBeVisible({
      timeout: 10_000,
    });
  });
});
