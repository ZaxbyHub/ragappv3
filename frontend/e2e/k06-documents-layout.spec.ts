// frontend/e2e/k06-documents-layout.spec.ts — issue #769 acceptance checks
// (FROZEN), authored from the AC letters only.
//
// Runs against the REAL built app (vite preview :4173) with the stub backend
// (E2E_STUB_PORT, default :9090 — same tier as chat-smoke.spec.ts and
// a11y-matrix.spec.ts; workers:1 serializes everything). Layout claims need
// Playwright: jsdom has no layout engine.
//
// AC map (issue #769):
//   AC1 -> "documents header controls stay inside a 390px viewport"
//   AC2 -> "documents header controls stay inside a 320px viewport"
//   AC3 -> "document list keeps usable width beside the folder tree at 390px"
//   AC4 -> "document list keeps usable width beside the folder tree at 320px"
//   AC5 -> "document detail actions stay inside a 390px viewport"
//   AC6 -> "table row actions are visible without horizontal scroll at 1366px"
//   AC7 -> "row actions are visible without horizontal scroll in the 640 to 767px band"
//   AC8 -> "documents content width is capped at 3840px"
//   (AC9/AC10 of #769 are pinned elsewhere: DocumentsPage.virtualization
//   vitest and the chat-smoke spec — not re-authored here.)
//
// Setup contract shared by the /documents tests: POST /_e2e/a11y-seed (the
// stub's seed makes /api/tags and /api/folders answer 200 with the
// "Manuals" folder instead of 404), the documents LIST is fulfilled with
// page.route (regex /\/api\/documents(\?.*)?$/ — end-anchored so it never
// captures /api/documents/1, /api/documents/stats or /api/documents/1/raw,
// which the stub keeps serving), and /api/settings comes from the stub with
// chat_configured: true so the UnconfiguredChatBanner never renders.
// Delete affordances are vault-gated (canMutateDocuments needs a concrete
// vault the user administers): after login the spec selects the stub's
// E2E Vault exactly like chat-smoke.spec.ts:26-33 does.

import { test, expect, type Page } from "@playwright/test";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

async function login(page: Page) {
  await page.goto("/login");
  await page.fill("#login-username", "e2e-user");
  await page.fill("#login-password", "e2e-pass");
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.endsWith("/login"), { timeout: 20_000 });
}

async function seed(page: Page) {
  const res = await page.request.post(`${STUB_ORIGIN}/_e2e/a11y-seed`, { data: {} });
  if (!res.ok()) throw new Error(`stub control /_e2e/a11y-seed failed: ${res.status()} ${await res.text()}`);
}

// The stub's E2E Vault carries current_user_permission "admin"; document
// delete actions are vault-gated (a fresh context defaults to "All vaults",
// which selects no concrete vault and hides every Delete affordance).
async function selectE2eVault(page: Page) {
  await page.getByRole("button", { name: /all vaults \(\d+ files total\)/i }).click();
  await page.getByRole("menuitem", { name: /e2e vault/i }).click();
  await expect(
    page.getByRole("button", { name: /active vault: e2e vault/i })
  ).toBeVisible({ timeout: 10_000 });
}

function docRow(id: number, filename: string) {
  const now = new Date().toISOString();
  return {
    id: String(id),
    filename,
    vault_id: 1,
    content_type: "application/pdf",
    size: 2048,
    created_at: now,
    processed_at: now,
    error_message: null,
    metadata: { status: "indexed", chunk_count: 7, phase: "done" },
  };
}

const THREE_DOCS = [
  docRow(1, "handbook-a11y.pdf"),
  docRow(2, "service-manual.pdf"),
  docRow(3, "parts-list.pdf"),
];

// Shared setup for the /documents list tests: seed, fulfil the list with the
// three fixture rows, pin the viewport, log in, open /documents and select
// the E2E Vault so the header's Delete-All and the row Delete affordances
// render (vault-gated).
async function setupDocumentsList(page: Page, width: number, height: number) {
  await seed(page);
  await page.route(/\/api\/documents(\?.*)?$/, async (route) => {
    await route.fulfill({ json: { documents: THREE_DOCS, total: THREE_DOCS.length } });
  });
  await page.setViewportSize({ width, height });
  await login(page);
  await page.goto("/documents");
  await selectE2eVault(page);
}

test.describe("k06 documents layout (issue #769)", () => {
  // AC1 — #769: WHEN /documents renders at 390x844 THE SYSTEM SHALL keep
  // every visible header control, including Scan Directory and Delete All in
  // Vault, inside the viewport's right edge.
  //
  // GREEN at base (the header row landed flex-wrap via PageTitleHeader,
  // issue #776's shared wrap rule) and must stay green after the fix.
  // Metric per the AC letter: the count of visible button/[role=combobox]
  // elements inside the header block (walk up from the "Documents" h1 to the
  // nearest flex-wrap container) whose getBoundingClientRect().right exceeds
  // window.innerWidth + 1, asserted toBe(0). Guard: Scan Directory visible.
  test("documents header controls stay inside a 390px viewport", async ({ page }) => {
    await setupDocumentsList(page, 390, 844);
    await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("button", { name: /scan directory/i })).toBeVisible();

    const offenders = await page.evaluate(() => {
      const h1 = Array.from(document.querySelectorAll("h1")).find(
        (h) => h.textContent?.trim() === "Documents"
      );
      if (!h1) return -1;
      let header = h1 as HTMLElement;
      while (header.parentElement && !header.classList.contains("flex-wrap")) {
        header = header.parentElement;
      }
      const ctrls = header.querySelectorAll('button, [role="combobox"]');
      let count = 0;
      for (const el of Array.from(ctrls)) {
        const style = window.getComputedStyle(el);
        if (style.visibility === "hidden" || style.display === "none") continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (r.right > window.innerWidth + 1) count++;
      }
      return count;
    });
    expect(offenders, `header controls past the viewport right edge: ${offenders}`).toBe(0);
  });

  // AC2 — #769: same header contract at the narrowest matrix width, 320x640.
  // GREEN at base and must stay green after the fix. Same guard and metric
  // as AC1.
  test("documents header controls stay inside a 320px viewport", async ({ page }) => {
    await setupDocumentsList(page, 320, 640);
    await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("button", { name: /scan directory/i })).toBeVisible();

    const offenders = await page.evaluate(() => {
      const h1 = Array.from(document.querySelectorAll("h1")).find(
        (h) => h.textContent?.trim() === "Documents"
      );
      if (!h1) return -1;
      let header = h1 as HTMLElement;
      while (header.parentElement && !header.classList.contains("flex-wrap")) {
        header = header.parentElement;
      }
      const ctrls = header.querySelectorAll('button, [role="combobox"]');
      let count = 0;
      for (const el of Array.from(ctrls)) {
        const style = window.getComputedStyle(el);
        if (style.visibility === "hidden" || style.display === "none") continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        if (r.right > window.innerWidth + 1) count++;
      }
      return count;
    });
    expect(offenders, `header controls past the viewport right edge: ${offenders}`).toBe(0);
  });

  // AC3 — #769: WHEN /documents renders at 390x844 with folders present THE
  // SYSTEM SHALL render the first document card at least 300 CSS px wide.
  //
  // GREEN at base (the folder tree stacks above the list below lg, issue
  // #778's reflow pass) and must stay green after the fix. Guard: the first
  // document's filename is visible. Metric per the AC letter: the
  // getBoundingClientRect().width of the first visible document card — the
  // card element containing the first document's filename link — asserted
  // >= 300. The metric does not depend on the folder/list row surviving the
  // fix: stacking, collapsing or sheeting the tree all pass. The seed
  // provides the one mocked folder ("Manuals").
  test("document list keeps usable width beside the folder tree at 390px", async ({ page }) => {
    await setupDocumentsList(page, 390, 844);
    // `>> visible=true` skips the desktop table row link, which is display:none below sm (a11y-matrix.spec.ts documents the same trap).
    await expect(page.locator("text=handbook-a11y.pdf >> visible=true").first()).toBeVisible({ timeout: 20_000 });

    const width = await page.evaluate((filename: string) => {
      const visible = (el: Element): boolean => {
        for (let n: Element | null = el; n; n = n.parentElement) {
          const s = window.getComputedStyle(n);
          if (s.visibility === "hidden" || s.display === "none") return false;
        }
        return true;
      };
      const links = Array.from(document.querySelectorAll("#main-content a")).filter(
        (a) => (a.textContent || "").trim() === filename && visible(a)
      );
      const link = links[0];
      if (!link) return -1;
      let card = link as HTMLElement;
      while (card.parentElement && !/card/.test(card.className)) card = card.parentElement;
      return Math.round(card.getBoundingClientRect().width);
    }, "handbook-a11y.pdf");
    expect(width, `first document card width: ${width}px`).toBeGreaterThanOrEqual(300);
  });

  // AC4 — #769: same folder-tree contract at 320x640, floor 240 CSS px.
  // GREEN at base and must stay green after the fix. Same guard and metric
  // as AC3 with the 240px floor (the audit measured about 8px for the list
  // at 320px before the tree stacked).
  test("document list keeps usable width beside the folder tree at 320px", async ({ page }) => {
    await setupDocumentsList(page, 320, 640);
    // `>> visible=true` skips the desktop table row link, which is display:none below sm (a11y-matrix.spec.ts documents the same trap).
    await expect(page.locator("text=handbook-a11y.pdf >> visible=true").first()).toBeVisible({ timeout: 20_000 });

    const width = await page.evaluate((filename: string) => {
      const visible = (el: Element): boolean => {
        for (let n: Element | null = el; n; n = n.parentElement) {
          const s = window.getComputedStyle(n);
          if (s.visibility === "hidden" || s.display === "none") return false;
        }
        return true;
      };
      const links = Array.from(document.querySelectorAll("#main-content a")).filter(
        (a) => (a.textContent || "").trim() === filename && visible(a)
      );
      const link = links[0];
      if (!link) return -1;
      let card = link as HTMLElement;
      while (card.parentElement && !/card/.test(card.className)) card = card.parentElement;
      return Math.round(card.getBoundingClientRect().width);
    }, "handbook-a11y.pdf");
    expect(width, `first document card width: ${width}px`).toBeGreaterThanOrEqual(240);
  });

  // AC5 — #769: WHEN a document detail page renders at 390x844 THE SYSTEM
  // SHALL keep "Ask about this document", "Download" and the Tags "Save"
  // button inside the viewport's right edge.
  //
  // GREEN at base (the detail column landed w-full min-w-0, issue #778's
  // reflow pass) and must stay green after the fix. Setup: /api/documents/1
  // and the vault's tag list are fulfilled with page.route; the raw preview
  // stays on the stub (seeded). Guard: all three buttons attached and
  // visible. Metric per the AC letter: the count of those three buttons
  // whose getBoundingClientRect().right > window.innerWidth + 1, toBe(0).
  test("document detail actions stay inside a 390px viewport", async ({ page }) => {
    await seed(page);
    await page.route(/\/api\/documents\/1(\?.*)?$/, async (route) => {
      await route.fulfill({ json: docRow(1, "handbook-a11y.pdf") });
    });
    await page.route(/\/api\/tags(\?.*)?$/, async (route) => {
      const now = new Date().toISOString();
      await route.fulfill({
        json: {
          tags: [{ id: 1, vault_id: 1, name: "maintenance", color: "#2563eb", created_at: now, updated_at: now, document_count: 1 }],
        },
      });
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);
    await page.goto("/documents/1");

    const ask = page.getByRole("button", { name: "Ask about this document", exact: true });
    const download = page.getByRole("button", { name: "Download", exact: true });
    const save = page.getByRole("button", { name: "Save", exact: true });
    await expect(ask).toBeAttached({ timeout: 20_000 });
    await expect(download).toBeAttached();
    await expect(save).toBeAttached();
    await expect(ask).toBeVisible();
    await expect(download).toBeVisible();
    await expect(save).toBeVisible();

    const offenders = await page.evaluate(() => {
      const wanted = ["Ask about this document", "Download", "Save"];
      const buttons = Array.from(document.querySelectorAll("#main-content button"));
      let count = 0;
      for (const name of wanted) {
        const btn = buttons.find((b) => (b.textContent || "").trim() === name);
        if (!btn) continue;
        const r = btn.getBoundingClientRect();
        if (r.right > window.innerWidth + 1) count++;
      }
      return count;
    });
    expect(offenders, `detail action buttons past the viewport right edge: ${offenders}`).toBe(0);
  });

  // AC6 — #769: WHEN /documents renders its table at 1366x768 THE SYSTEM
  // SHALL show the first row's Delete action inside both the table's scroll
  // box and the viewport, without any horizontal scrolling.
  //
  // RED at base (the Actions column sits about 496px past the scroll box's
  // right edge at 1366px) and GREEN after the fix. Guard: the first row's
  // Delete document button (aria-label "Delete document",
  // DocumentTable.tsx) is attached. Metric per the AC letter:
  // deleteBtn.right - Math.min(scrollBox.right, window.innerWidth) at
  // scrollLeft 0, asserted <= 0, where scrollBox is the button's nearest
  // ancestor with overflow-x auto or scroll. A sticky Actions column or a
  // narrower layout both pass. Vault selected so canMutateDocuments is true
  // (stub user is superadmin and admin of the E2E Vault).
  test("table row actions are visible without horizontal scroll at 1366px", async ({ page }) => {
    await setupDocumentsList(page, 1366, 768);
    await expect(
      page.getByRole("button", { name: "Delete document" }).first()
    ).toBeAttached({ timeout: 20_000 });

    const over = await page.evaluate(() => {
      const btns = Array.from(
        document.querySelectorAll('button[aria-label="Delete document"]')
      ).filter((b) => {
        const style = window.getComputedStyle(b);
        return style.visibility !== "hidden" && style.display !== "none" && b.getBoundingClientRect().width > 0;
      });
      const btn = btns[0];
      if (!btn) return null;
      const rect = btn.getBoundingClientRect();
      let limit = window.innerWidth;
      for (let n = btn as HTMLElement; n; n = n.parentElement) {
        const ox = window.getComputedStyle(n).overflowX;
        if (ox === "auto" || ox === "scroll") {
          limit = Math.min(limit, n.getBoundingClientRect().right);
          break;
        }
      }
      return Math.round(rect.right - limit);
    });
    expect(over as number, `first-row Delete clipped by ${over}px at 1366x768`).toBeLessThanOrEqual(0);

    // Plan-critic R1: geometry alone does not prove usability — a sticky
    // column painted inside the box but covered by scrolled content would
    // still pass the rect check. Require the Delete control to be
    // HIT-TESTABLE: the element at its centre must be the button or a
    // descendant of it. scrollIntoViewIfNeeded first models what a real
    // click does (Playwright clicks auto-scroll); without it the first row
    // can sit at the page fold and the centre point falls below the fold.
    await page.getByRole("button", { name: "Delete document" }).first().scrollIntoViewIfNeeded();
    const hit = await page.evaluate(() => {
      const btns = Array.from(
        document.querySelectorAll('button[aria-label="Delete document"]')
      ).filter((b) => {
        const style = window.getComputedStyle(b);
        return style.visibility !== "hidden" && style.display !== "none" && b.getBoundingClientRect().width > 0;
      });
      const btn = btns[0];
      if (!btn) return null;
      const r = btn.getBoundingClientRect();
      const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return !!el && (el === btn || btn.contains(el));
    });
    expect(hit, `first-row Delete hit-testable at its centre: ${hit}`).toBe(true);
  });

  // AC7 — #769: WHEN /documents renders in the 640-767px band (700x900
  // viewport) THE SYSTEM SHALL show the first document's Delete action
  // inside the viewport without horizontal scrolling.
  //
  // RED at base (the table took over from the cards at sm and hid its
  // Actions column about 642px past the scroll box at 700px) and GREEN
  // after the fix (the card list covers the band below md). Same metric as
  // AC6, applied to whichever Delete control is visible — a table-row
  // button OR, in the card branch, the Delete item inside the card's
  // Actions overflow menu (the issue's own suggested approach sanctions
  // per-row overflow menus for secondary actions, so the card exposes
  // Delete through its "Actions for <filename>" dropdown; a menu item that
  // must be reached past the viewport edge would defeat the AC, so the
  // opened item is measured by the same right-edge rule).
  test("row actions are visible without horizontal scroll in the 640 to 767px band", async ({ page }) => {
    await setupDocumentsList(page, 700, 900);

    // Card branch (below md): open the first card's Actions menu and
    // measure its Delete item; table branch (>= md): the direct button.
    const directDelete = page.getByRole("button", { name: "Delete document" }).first();
    const inCardBranch = !(await directDelete.isVisible().catch(() => false));
    if (inCardBranch) {
      const firstCard = page.locator('[aria-label^="Document: "]').first();
      await expect(firstCard).toBeVisible({ timeout: 20_000 });
      await firstCard.getByRole("button", { name: /^Actions for / }).click();
    } else {
      await expect(directDelete).toBeAttached({ timeout: 20_000 });
    }

    const over = await page.evaluate(() => {
      const direct = Array.from(
        document.querySelectorAll('button[aria-label="Delete document"]')
      ).filter((b) => {
        const style = window.getComputedStyle(b);
        return style.visibility !== "hidden" && style.display !== "none" && b.getBoundingClientRect().width > 0;
      });
      // Card branch: the opened menu's Delete item carries
      // aria-label="Delete <filename>".
      const menuItem = Array.from(
        document.querySelectorAll('[role="menuitem"][aria-label^="Delete "]')
      ).filter((el) => {
        const style = window.getComputedStyle(el);
        return style.visibility !== "hidden" && style.display !== "none" && el.getBoundingClientRect().width > 0;
      });
      const btn = (direct[0] || menuItem[0]) as HTMLElement | undefined;
      if (!btn) return null;
      const rect = btn.getBoundingClientRect();
      let limit = window.innerWidth;
      for (let n = btn; n; n = n.parentElement) {
        const ox = window.getComputedStyle(n).overflowX;
        if (ox === "auto" || ox === "scroll") {
          limit = Math.min(limit, n.getBoundingClientRect().right);
          break;
        }
      }
      return Math.round(rect.right - limit);
    });
    expect(over as number, `Delete clipped by ${over}px at 700x900`).toBeLessThanOrEqual(0);
  });

  // AC8 — #769: WHEN /documents renders at 3840x2160 THE SYSTEM SHALL keep
  // every visible text-bearing element inside #main-content at most 2000
  // CSS px wide.
  //
  // GREEN at base (PageShell's non-chat wrapper carries a 1536px cap) and
  // must stay green after the fix. The stub serves /api/settings with
  // chat_configured: true, so the UnconfiguredChatBanner (inside
  // #main-content but outside the capped wrapper) does not render. Guard:
  // the "Documents" h1 is visible. Metric per the AC letter: the maximum
  // getBoundingClientRect().width over visible descendants of #main-content
  // that directly contain a non-whitespace text node (TreeWalker), asserted
  // <= 2000. The 2000px ceiling is well below the 3,160px baseline and
  // above any max-w-screen-* token, so the fixer can choose the token.
  test("documents content width is capped at 3840px", async ({ page }) => {
    await setupDocumentsList(page, 3840, 2160);
    await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible({ timeout: 20_000 });
    await page.waitForTimeout(500);

    const widest = await page.evaluate(() => {
      let max = 0;
      const root = document.getElementById("main-content");
      if (!root) return -1;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const seen = new Set<Element>();
      for (; walker.nextNode(); ) {
        const t = walker.currentNode as Text;
        if (!t.textContent?.trim()) continue;
        const el = t.parentElement;
        if (!el || seen.has(el)) continue;
        seen.add(el);
        const style = window.getComputedStyle(el);
        if (style.visibility === "hidden" || style.display === "none") continue;
        const r = el.getBoundingClientRect();
        if (r.width > max) max = r.width;
      }
      return Math.round(max);
    });
    expect(widest, `widest text-bearing element: ${widest}px`).toBeLessThanOrEqual(2000);
  });
});
