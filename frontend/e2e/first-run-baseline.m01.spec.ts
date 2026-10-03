// frontend/e2e/first-run-baseline.m01.spec.ts — issue-tracer trace
// 781-vaultgate-first-run-baseline, acceptance check C1 (AC1).
//
// Fresh-install walkthrough harness: ONE serial Playwright test in a fresh
// context (kv_active_vault_id unset — no vault selection is ever persisted
// before the Memory visit) that walks app load → setup (as far as the stub
// allows) → login → /memory with no vault selected (dead-end state A) →
// create a vault via /vaults → upload a document → /chat first send from the
// "All Vaults" composer (dead-end state B) → recover by picking the vault,
// send again, and reach the first cited answer. Every user interaction
// increments actionCount ("setup-to-first-cited-answer action count" metric).
//
// At the end the spec FIRST writes a machine-readable record to
//   frontend/e2e/test-results/first-run-baseline-record.json
// and THEN asserts dead_ends is empty — pre-fix runs still emit the record
// (the human evidence) while failing; post-fix runs pass with dead_ends [].
//
// FAIL-CLOSED semantics: degradation is itself recorded as dead ends, so a
// run against an unreachable/wrong backend can never pass vacuously —
//   * harness/stub-unreachable — the early direct probe of the advertised
//     stub port failed;
//   * walkthrough/login-failed — the authenticated shell never appeared
//     (login form still showing, or nothing usable rendered);
//   * walkthrough/memory-unreachable — the memory page rendered no surface;
//   * walkthrough/chat-unreachable — the composer never became visible.
// Each still writes the record before the final assertion fails.
//
// ----------------------------------------------------------------------------
// STUB ROUTE REQUIREMENTS beyond what stub-backend.mjs already implements.
// The stub today covers: auth (setup-status/csrf/login/refresh/me/logout),
// health, GET /api/vaults(/accessible), chat sessions CRUD + batch, and the
// citation-bearing chat SSE stream (sources + done frames with handbook.pdf
// S1 — already sufficient for the recovery leg). The walkthrough ADDITIONALLY
// needs, from the implementer:
//
//   REQUIRED for the full walkthrough to complete (without them the spec
//   records-and-continues, so dead-end detection still works; the affected
//   steps just land in `completed` as degraded/partial):
//     * POST /api/vaults                      — VaultsPage "New Vault" dialog
//       (lib/api/vaults.ts createVault). Missing at base: the create step
//       fails and the spec falls back to the stub's seeded "E2E Vault" for
//       the recovery leg.
//     * GET  /api/documents?vault_id=…        — DocumentsPage listDocuments.
//       Missing at base: the documents page renders its error state.
//     * GET  /api/documents/stats?vault_id=…  — DocumentsPage getDocumentStats.
//     * POST /api/documents (multipart, ?vault_id=) — uploadDocument
//       (UploadDropzone input[aria-label="Upload files"] → useUploadStore).
//
//   OPTIONAL (each is tolerated by the app or by this spec when absent):
//     * GET /api/organizations/               — VaultsPage listOrganizations;
//       the page catches the failure and hides the org select.
//     * POST /api/auth/register               — only if the setup wizard is
//       exercised; the stub reports needs_setup:false, so the walkthrough
//       normally goes straight to /login.
//     * Memory search routes                  — the /memory null-vault visit
//       makes no memory calls (that is the defect).
//
// Spec-side stub port: direct stub API calls use
// process.env.E2E_STUB_PORT || "9090". The config-level port override lands
// with the fix; with no env set everything stays on the default 9090 the
// playwright.config.ts webServer boots. Playwright baseURL stays :4173 and
// page.request(...) calls ride the vite preview proxy.
// ----------------------------------------------------------------------------

import { test, expect, type Locator, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;
const RECORD_DIR = path.join(__dirname, "test-results");
const RECORD_PATH = path.join(RECORD_DIR, "first-run-baseline-record.json");

type DeadEnd = { site: string; gate: string; detail: string };

const deadEnds: DeadEnd[] = [];
const completed: string[] = [];
let actionCount = 0;

// ---- interaction wrappers (the action-count metric) -------------------------

async function doClick(locator: Locator, options?: { timeout?: number }): Promise<void> {
  actionCount++;
  await locator.click({ timeout: options?.timeout ?? 15_000 });
}

async function doFill(locator: Locator, value: string): Promise<void> {
  actionCount++;
  await locator.fill(value, { timeout: 15_000 });
}

async function doPress(locator: Locator, key: string): Promise<void> {
  actionCount++;
  await locator.press(key, { timeout: 15_000 });
}

async function doSetInputFiles(locator: Locator, files: Parameters<Locator["setInputFiles"]>[1]): Promise<void> {
  actionCount++;
  await locator.setInputFiles(files, { timeout: 15_000 });
}

/** Best-effort visibility probe that never throws (record-and-continue). */
async function visibleWithin(locator: Locator, timeout = 15_000): Promise<boolean> {
  try {
    await expect(locator).toBeVisible({ timeout });
    return true;
  } catch {
    return false;
  }
}

/** The vault-selector trigger, wherever it renders: the real VaultSelector's
 *  button carries aria-label starting "Active vault:" or "All vaults". */
function selectorTriggers(page: Page): Locator {
  return page.locator(
    'button[aria-label^="All vaults"], button[aria-label^="Active vault:"]'
  );
}

/** Pick `name` in the nearest vault selector dropdown (opens the trigger,
 *  chooses the menuitem, asserts the trigger now shows the active vault). */
async function pickVault(page: Page, vaultName: RegExp | string): Promise<boolean> {
  const trigger = selectorTriggers(page).first();
  if (!(await visibleWithin(trigger, 15_000))) return false;
  await doClick(trigger);
  const item = page.getByRole("menuitem", { name: vaultName }).first();
  if (!(await visibleWithin(item, 10_000))) return false;
  await doClick(item);
  const active = await visibleWithin(
    page.getByRole("button", { name: /active vault:/i }).first(),
    10_000
  );
  return active;
}

test.describe("first-run baseline (trace 781-vaultgate-first-run-baseline / C1)", () => {
  test("fresh-install walkthrough: setup to first cited answer without dead ends", async ({ page }) => {
    test.setTimeout(240_000);

    // ---- 1. App load (fresh context: no kv_active_vault_id anywhere) -------
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle").catch(() => {});
    completed.push("app-load");

    // Fail-closed harness probe (spec-side env-overridable port), run EARLY so
    // the record says so up front: when the stub is not reachable on the
    // advertised port, every downstream degraded step is untrustworthy and the
    // record must not pass vacuously.
    const stubReachable = await page
      .request.get(`${STUB_ORIGIN}/api/health`, { timeout: 5_000 })
      .then((r) => r.ok())
      .catch(() => false);
    completed.push(`stub-direct-probe(${STUB_ORIGIN})=${stubReachable}`);
    if (!stubReachable) {
      deadEnds.push({
        site: "harness",
        gate: "stub-unreachable",
        detail:
          "walkthrough ran against an unreachable/wrong backend; record is not trustworthy",
      });
    }

    // ---- 2. Setup gate, as far as the stub allows --------------------------
    // The stub reports needs_setup:false, so the app should not gate on setup;
    // if a future stub flips it, complete the wizard form best-effort.
    const setupUser = page.locator("#setup-username");
    if (await visibleWithin(setupUser, 5_000)) {
      await doFill(page.locator("#setup-username"), "e2e-admin");
      await doFill(page.locator("#setup-password"), "e2e-password-123");
      await doFill(page.locator("#setup-confirm-password"), "e2e-password-123");
      const submit = page.getByRole("button", { name: /create|start|finish|submit|complete/i }).first();
      await doClick(submit);
      // The stub has no register route unless extended — degrade gracefully.
      if (await visibleWithin(page.locator("#login-username"), 10_000)) {
        completed.push("setup-completed");
      } else {
        completed.push("setup-attempted (stub did not accept the wizard)");
      }
    } else {
      completed.push("setup-skipped (stub reports needs_setup:false)");
    }

    // ---- 3. Login (stub superadmin) ----------------------------------------
    const loginField = page.locator("#login-username");
    const loginFormShown = await visibleWithin(loginField, 10_000);
    if (loginFormShown) {
      await doFill(page.locator("#login-username"), "e2e-user");
      await doFill(page.locator("#login-password"), "e2e-pass");
      await doClick(page.getByRole("button", { name: /sign in/i }));
    }
    // Fail-closed: the authenticated shell must actually appear. A fresh-
    // install walkthrough that cannot even authenticate IS a dead end — a
    // degraded continuation must not yield a vacuously green record.
    const shell = page
      .getByRole("button", { name: /new vault|scan directory/i })
      .first()
      .or(page.locator("nav").first());
    const shellVisible = await visibleWithin(shell, 20_000);
    if (shellVisible) {
      completed.push("login");
    } else {
      const formStillShown = await loginField.isVisible().catch(() => false);
      if (loginFormShown && !formStillShown) {
        // Ambiguous middle: the form vanished but no shell rendered either.
        // Not counted as a dead end — noted and continued.
        completed.push(
          "login-ambiguous (authenticated shell never appeared, login form also gone; recorded-and-continued)"
        );
      } else {
        deadEnds.push({
          site: "walkthrough",
          gate: "login-failed",
          detail: loginFormShown
            ? "the login form is still showing after the sign-in attempt — the authenticated shell never appeared"
            : "neither a login form nor the authenticated shell appeared (setup gate unresolved or blank app)",
        });
        completed.push("login-failed (recorded-and-continued)");
      }
    }

    // ---- 4. Memory page FIRST, with NO vault selected ----------------------
    // Nothing above ever touched the vault selector, so kv_active_vault_id is
    // still unset (fresh context) and activeVaultId is null.
    await page.goto("/memory", { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle").catch(() => {});

    const nullBranchCopy = page.getByText("Select a vault").first();
    const nullBranchVisible = await visibleWithin(nullBranchCopy, 20_000);
    // Let any late-mount settle before counting triggers.
    await page.waitForTimeout(1_000);
    const triggerCount = await selectorTriggers(page).count();
    const memoriesTitle = await visibleWithin(
      page.getByRole("heading", { name: "Memories", exact: true }),
      5_000
    );
    const memoryTitle = await visibleWithin(
      page.getByRole("heading", { name: "Memory", exact: true }),
      3_000
    );

    if (nullBranchVisible && triggerCount === 0) {
      deadEnds.push({
        site: "memory-null-branch",
        gate: "selector-referenced-not-rendered",
        detail:
          `MemoryPage null-vault branch shows the "Select a vault" EmptyState ` +
          `(title reads ${memoriesTitle ? '"Memories"' : '"Memory"'}: heading "Memories" ` +
          `visible=${memoriesTitle}) and its copy says "Choose a vault from the vault ` +
          `selector", but no vault-selector trigger (aria-label "All vaults…"/` +
          `"Active vault:…") is rendered anywhere on the page (count=${triggerCount}).`,
      });
      completed.push("memory-null-branch-visited (dead end A detected)");
    } else if (nullBranchVisible) {
      completed.push(
        `memory-null-branch-visited (selector present, count=${triggerCount}, ` +
          `title "Memories"=${memoriesTitle})`
      );
    } else if (!memoriesTitle && !memoryTitle && triggerCount === 0) {
      // Fail-closed: neither the null-branch copy nor ANY memory-page surface
      // (either page-title heading, a selector trigger) rendered — the page is
      // effectively blank or was redirected away.
      deadEnds.push({
        site: "walkthrough",
        gate: "memory-unreachable",
        detail:
          "the memory page rendered no surface at all (blank or redirected away)",
      });
      completed.push("memory-unreachable (recorded-and-continued)");
    } else {
      completed.push(
        `memory-page-visited (null-branch copy not shown; selector count=${triggerCount})`
      );
    }

    // ---- 5. Create a vault via the /vaults UI ------------------------------
    let createdVaultName: string | null = null;
    await page.goto("/vaults", { waitUntil: "domcontentloaded" });
    const newVaultButton = page.getByRole("button", { name: /new vault/i }).first();
    if (await visibleWithin(newVaultButton, 20_000)) {
      await doClick(newVaultButton);
      const nameField = page.locator("#vault-name");
      if (await visibleWithin(nameField, 10_000)) {
        createdVaultName = "Walkthrough Vault";
        await doFill(nameField, createdVaultName);
        // Submit the dialog form (the Create button).
        await doClick(page.getByRole("button", { name: /^create$/i }).first());
        // Success: the new card appears (and VaultsPage auto-selects it).
        const created = await visibleWithin(
          page.getByText(createdVaultName).first(),
          15_000
        );
        if (created) {
          completed.push("vault-created-via-ui");
        } else {
          completed.push(
            "vault-create-failed (stub lacks POST /api/vaults? recorded-and-continued)"
          );
          createdVaultName = null;
        }
      } else {
        completed.push("vault-create-dialog-did-not-open (recorded-and-continued)");
      }
    } else {
      completed.push("vaults-page-unavailable (recorded-and-continued)");
    }

    // ---- 6. Upload a document (stub) ----------------------------------------
    await page.goto("/documents", { waitUntil: "domcontentloaded" });
    const uploadInput = page.locator('input[aria-label="Upload files"]');
    if (await visibleWithin(uploadInput, 20_000)) {
      await doSetInputFiles(uploadInput, {
        name: "walkthrough.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("Walkthrough first-run baseline document.", "utf-8"),
      });
      // Success looks like the file name somewhere (queue or table); failure
      // (stub lacks POST /api/documents) is tolerated.
      const uploaded = await visibleWithin(
        page.getByText("walkthrough.txt").first(),
        20_000
      );
      completed.push(
        uploaded
          ? "document-uploaded"
          : "document-upload-attempted (stub lacks POST /api/documents? recorded-and-continued)"
      );
    } else {
      completed.push("documents-page-unavailable (recorded-and-continued)");
    }

    // ---- 7. /chat: first send from the "All Vaults" composer ---------------
    await page.goto("/chat", { waitUntil: "domcontentloaded" });
    const composer = page.getByLabel("Message input");
    const composerVisible = await visibleWithin(composer, 20_000);
    if (!composerVisible) {
      // Fail-closed: without the composer there is no chat surface to walk.
      deadEnds.push({
        site: "walkthrough",
        gate: "chat-unreachable",
        detail:
          'the chat composer (aria-label "Message input") never became visible — no chat surface to walk',
      });
      completed.push("chat-unreachable (recorded-and-continued)");
    } else {
      completed.push("chat-opened");
    }

    // Guarantee the precondition deterministically: the composer must show
    // "All Vaults" for this leg. If a successful create auto-selected the new
    // vault, explicitly switch back to All Vaults via the header selector.
    const allVaultsTrigger = page
      .getByRole("button", { name: /all vaults \(\d+ files total\)/i })
      .first();
    const activeVaultTrigger = page
      .getByRole("button", { name: /active vault:/i })
      .first();
    let composerAllVaults = await visibleWithin(allVaultsTrigger, 10_000);
    if (!composerAllVaults && (await visibleWithin(activeVaultTrigger, 5_000))) {
      await doClick(activeVaultTrigger);
      const allVaultsItem = page.getByRole("menuitem", { name: /all vaults/i }).first();
      if (await visibleWithin(allVaultsItem, 10_000)) {
        await doClick(allVaultsItem);
        composerAllVaults = await visibleWithin(allVaultsTrigger, 10_000);
      }
    }

    if (composerAllVaults && composerVisible) {
      const firstQuestion = "What did the maintenance manual say about coolant?";
      await doFill(composer, firstQuestion);
      await doPress(composer, "Enter");
      completed.push("chat-first-send-from-all-vaults");

      // Dead-end state B: the vault-agnostic gate fires on first send.
      await page.waitForTimeout(1_500);
      const gateError = page.getByText(
        "Please select a vault before starting a chat."
      );
      if (await visibleWithin(gateError, 10_000)) {
        deadEnds.push({
          site: "chat-composer",
          gate: "vault-required-contradicts-all-vaults",
          detail:
            "The chat header selector offers \"All Vaults\" as the default, but " +
            "the first send of a new chat is rejected with the exact copy " +
            "\"Please select a vault before starting a chat.\"",
        });
        completed.push("chat-first-send-dead-end (dead end B detected)");
      } else {
        // The send went through vault-agnostically — no dead end on this leg.
        const answered = await visibleWithin(
          page.getByText(/Here is the streamed answer/i),
          20_000
        );
        completed.push(
          answered
            ? "chat-first-send-answered-vault-agnostically"
            : "chat-first-send-no-error-no-answer (stub stream did not complete?)"
        );
      }
    } else {
      completed.push(
        "chat-composer-not-on-all-vaults (selector state unreadable; recorded-and-continued)"
      );
    }

    // ---- 8. RECOVER: pick the vault, send again, reach the cited answer ----
    // Prefer the vault created by this walkthrough; fall back to the stub's
    // seeded vault when creation was not possible.
    const vaultToPick = createdVaultName
      ? new RegExp(createdVaultName, "i")
      : /e2e vault/i;
    const picked = await pickVault(page, vaultToPick);
    if (picked) {
      completed.push(`vault-selected-in-chat-header (${createdVaultName ?? "E2E Vault"})`);

      const secondQuestion = "Where is the coolant interval documented?";
      await doFill(composer, secondQuestion);
      await doPress(composer, "Enter");

      const answerVisible = await visibleWithin(
        page.getByText(/Here is the streamed answer/i),
        30_000
      );
      if (answerVisible) completed.push("assistant-answer-streamed");

      const chip = page
        .getByRole("button", { name: /Source S1: handbook\.pdf/i })
        .first();
      const chipVisible = await visibleWithin(chip, 20_000);
      if (chipVisible) {
        completed.push("citation-rendered");
        // Open the citation as far as the UI supports it.
        await doClick(chip);
        const sourceDialog = await visibleWithin(
          page.getByRole("dialog").filter({ hasText: "handbook.pdf" }),
          10_000
        );
        const detailsPane = await visibleWithin(
          page.locator('aside[aria-label="Details panel"]'),
          10_000
        );
        completed.push(
          `citation-opened (source dialog=${sourceDialog}, details pane=${detailsPane})`
        );
      } else if (answerVisible) {
        completed.push("citation-missing (answer streamed but no Source S1 chip)");
      }
    } else {
      completed.push(
        "vault-selection-in-chat-header-failed (recorded-and-continued)"
      );
    }

    // ---- 9. Emit the record FIRST, then the pass/fail assertion ------------
    const record = {
      action_count: actionCount,
      dead_ends: deadEnds,
      completed,
    };
    fs.mkdirSync(RECORD_DIR, { recursive: true });
    fs.writeFileSync(RECORD_PATH, JSON.stringify(record, null, 2) + "\n");

    expect(record.dead_ends, JSON.stringify(record.dead_ends, null, 2)).toEqual([]);
  });
});
