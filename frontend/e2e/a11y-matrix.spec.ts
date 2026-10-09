// frontend/e2e/a11y-matrix.spec.ts — issue #778 acceptance gate (FROZEN).
//
// Route x viewport x theme accessibility matrix against the REAL built app
// (vite preview :4173) + the stub backend (E2E_STUB_PORT, default :9090 —
// same tier as chat-smoke.spec.ts; workers:1 serializes everything).
// Four gates, one spec:
//
//   1. "no serious or critical axe violations" — axe-core (injected from
//      ../node_modules/axe-core/axe.min.js, default config) across every
//      route x BOTH viewports x BOTH themes; zero serious/critical.
//   2. "no interactive element overflows the viewport" — at 320x568 every
//      visible interactive element must sit fully inside the horizontal
//      viewport unless it is table content (descendant of a <table>) whose
//      horizontal scroll a container owns — the WCAG 1.4.10 two-dimensional
//      exception. DISCLOSED DEVIATION from the AC2 letter (generic
//      scrollable-ancestor exemption): the letter's generic exemption is
//      vacuous on this app (PageShell's content wrapper is overflow-auto on
//      16 of 18 shell routes), so this probe is STRICTER than the letter —
//      non-table horizontal scrollers are flagged.
//   3. "offender probe exempts scroll containers" — SELF-TEST of the probe
//      from gate 2 (page.setContent, no app, no stub route) so the
//      scroll-container exemption can never silently rot. Reuses the SAME
//      serialized probe as gate 2 (single definition, no divergent copies).
//   2b/2c. Two companion self-tests pin the probe's edge semantics: a wide
//      button in a NON-table scroller stays an offender ("offender probe
//      flags non-table scrollers as offenders") and a letter-listed element
//      with tabindex="-1" stays in scope ("offender probe keeps tabindex
//      minus one letter elements in scope").
//   4. "exactly one main landmark per route" — OUTERMOST main landmarks:
//      exactly 1 on the 18 MainAppShell routes, 0 on the shell-less routes.
//      Outermost semantics is deliberate: ChatShell renders a nested <main>
//      inside PageShell's <main> (issue #779, out of scope here) and
//      AdminGroupsPage renders div[role="main"] — neither may fail this gate.
//
// Viewports: 320x568 (small phone) and 640x360 (the 200% zoom of
// 1280x720 — WCAG 1.4.10 reflow). Themes: light + dark ("kv-theme").
//
// SEEDING CONTRACT — the implementer makes stub-backend.mjs serve these;
// this spec only CALLS them:
//   - POST /_e2e/a11y-seed {} — idempotent; makes available: chat session
//     id 1 containing a user message "What did the maintenance manual say
//     about coolant?" and an assistant message "The coolant interval is
//     500 hours." (the seed posts this user message into session 1 via the
//     same addMessage path POST /api/chat/sessions/1/messages uses);
//     document "handbook-a11y.pdf" (id "1", vault 1, metadata.status
//     "indexed", size 2048); tags [{id:1,name:"maintenance"}]; folders
//     [{id:1,name:"Manuals"}]; memory row content "Coolant intervals run
//     every 500 hours."; wiki page {id:"coolant-maintenance",title:"Coolant
//     Maintenance Wiki"} (plus /wiki/lint and /wiki/claims empty
//     findings/claims arrays); kms entry {id:1,title:"Coolant Schedule
//     Entry",tags:["guide"]}; canvas artifact uid "cav_a11y_fixture_0"
//     titled "A11Y Fixture Canvas" with one version content "// fixture";
//     draft-room draft {id:1,title:"A11Y Fixture Draft",status:"ready"};
//     unified search q=coolant returns one result titled
//     "handbook-a11y.pdf".
//   - POST /_e2e/a11y-reset {} — restores the stub to its unseeded state.
//   - GET /api/settings, GET /api/users/?skip=0&limit=20, GET /api/groups,
//     GET /api/auth/sessions are STATIC fixtures the implementer adds
//     (always served with the pinned rows; do not gate on the seed call).
//     GET /api/organizations/ is always SERVED but returns [] until the a11y
//     seed runs (first-run-baseline.m01.spec.ts reads this endpoint and its
//     /vaults surface must not see fixture rows) — the seed adds e2e-org.
//     Pinned rows the sentinels require (plan-critic round 1, MINOR 1):
//     /api/users/ -> { users: [ { id: 1, username: "e2e-user",
//     full_name: "E2E User", role: "superadmin", is_active: true,
//     created_at: <iso> } ], total: 1 } (+ echo skip/limit/q shape);
//     /api/groups -> { groups: [ { id: 1, name: "e2e-group",
//     description: "a11y fixture group", created_at: <iso>, org_id: null,
//     organization_name: null } ], total: 1, page: 1, per_page: 20 };
//     /api/organizations/ -> [] UNTIL the a11y seed runs (empty array keeps
//     first-run-baseline.m01.spec.ts's /vaults walkthrough surface unchanged
//     — it tolerates the endpoint being absent, not extra rows), then
//     [ { id: 1, name: "e2e-org", description: "a11y fixture org",
//     member_count: 1, vault_count: 1, created_at: <iso> } ];
//     /api/auth/sessions -> { sessions: [ { id: "s1", user_id: 1,
//     user_agent: "e2e", ip_address: "127.0.0.1", created_at: <iso>,
//     expires_at: <iso>, is_current: true } ] }.
//   - POST /_e2e/user-flags {"must_change_password":true|false} — flips
//     the stub login/me user flag.
//   - POST /_e2e/setup-mode {"needs_setup":true|false} — already in the
//     stub (issue #782).
//
// The spec calls POST /_e2e/a11y-seed ONCE per worker (module-level flag)
// after the first login; POST /_e2e/a11y-reset is NOT required at end (the
// stub is per-process and CI runs it fresh), but afterAll calls it anyway
// for local-run hygiene — guarded with try/catch so a restarted stub can
// never fail the suite from afterAll.

import {
  test,
  expect,
  request as playwrightRequest,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import path from "node:path";
import fs from "node:fs";

const STUB_PORT = process.env.E2E_STUB_PORT || "9090";
const STUB_ORIGIN = `http://localhost:${STUB_PORT}`;

const AXE_PATH = path.resolve(__dirname, "../node_modules/axe-core/axe.min.js");
if (!fs.existsSync(AXE_PATH)) {
  throw new Error(
    `axe-core not found at ${AXE_PATH} — install frontend dependencies before running this spec`
  );
}

// 320x568 (small phone) and 640x360 (200% zoom of 1280x720). Written as
// literals: scripts/check_l07_a11y_gate_wired.py greps for 320/568/640/360.
const VIEWPORTS = [
  { width: 320, height: 568 },
  { width: 640, height: 360 },
] as const;

const THEMES = ["light", "dark"] as const;
type Theme = (typeof THEMES)[number];

interface RouteRow {
  path: string;
  auth: "anon" | "login";
  kind: "shell" | "shellless";
  sentinel: { selector: string };
}

// Sentinel citations are file:line in the current page components — re-read
// the component if a refactor moves the marker. All sentinels are resolved
// via page.locator(selector).first() so CSS, text= and role= engines all
// work with one string shape.
const ROUTES: RouteRow[] = [
  {
    path: "/login",
    auth: "anon",
    kind: "shellless",
    sentinel: { selector: "#login-username" }, // LoginPage.tsx:171
  },
  {
    path: "/register",
    auth: "anon",
    kind: "shellless",
    sentinel: { selector: "#register-username" }, // RegisterPage.tsx:123
  },
  {
    path: "/setup",
    auth: "anon",
    kind: "shellless",
    // SetupPage.tsx:305 — the step-1 submit button's label is "Create
    // Superadmin Account" (full accessible name used, so the match is safe
    // under both exact and substring role-name semantics).
    sentinel: { selector: "role=button[name='Create Superadmin Account']" },
  },
  {
    path: "/change-password",
    auth: "login",
    kind: "shellless",
    // ChangePasswordRequiredPage.tsx:127 id="new-password" (label "New
    // Password"). NOTE: ProtectedRoute.tsx:82 redirects an UNFLAGGED user
    // away from /change-password, so the /_e2e/user-flags flip must land
    // BEFORE login() — the login response user then carries the flag.
    sentinel: { selector: "#new-password" },
  },
  {
    path: "/chat",
    auth: "login",
    kind: "shell",
    sentinel: { selector: "textarea[aria-label='Message input']" }, // Composer.tsx:618
  },
  {
    path: "/chat/1",
    auth: "login",
    kind: "shell",
    // Session 1's seeded user message, rendered by the transcript
    // (chat-smoke.spec.ts:67-69 getByText precedent). Session 1 + this
    // message exist via POST /_e2e/a11y-seed (seeding contract above).
    sentinel: { selector: "text=What did the maintenance manual say" },
  },
  {
    path: "/chat/1/canvas/cav_a11y_fixture_0",
    auth: "login",
    kind: "shell",
    // CanvasPage.tsx:661 renders PageTitleHeader title={artifact.name};
    // PageTitleHeader.tsx:35 renders it as the page <h1>. The fixture
    // artifact (uid cav_a11y_fixture_0) is titled "A11Y Fixture Canvas".
    sentinel: { selector: "role=heading[name='A11Y Fixture Canvas']" },
  },
  {
    path: "/documents",
    auth: "login",
    kind: "shell",
    // Seeded document row filename — DocumentTable.tsx:268 {doc.filename}.
    // Amendment 5 + review F-18: viewport-stable AND readiness-correct.
    // text=handbook-a11y.pdf matched the desktop table row that is
    // display:none below sm (mobile card list renders instead); the page h1
    // is visible at every width but renders while the list skeleton is still
    // up. `>> visible=true` waits for a VISIBLE seeded-row match at either
    // breakpoint, which only exists once the document list has loaded.
    sentinel: { selector: "text=handbook-a11y.pdf >> visible=true" },
  },
  {
    path: "/documents/1",
    auth: "login",
    kind: "shell",
    // Detail page title — DocumentDetailPage.tsx:283 PageTitleHeader
    // title={doc.filename} renders as the page h1 (visible at every width —
    // amendment 5, same reason as the list route).
    sentinel: { selector: "role=heading[name='handbook-a11y.pdf']" },
  },
  {
    path: "/memory",
    auth: "login",
    kind: "shell",
    // Seeded memory row content — MemoryPage.tsx:316 {memory.content}.
    sentinel: { selector: "text=Coolant intervals run every 500 hours" },
  },
  {
    path: "/vaults",
    auth: "login",
    kind: "shell",
    // Static stub vault name — VaultsPage.tsx:216 CardTitle {vault.name}.
    sentinel: { selector: "text=E2E Vault" },
  },
  {
    path: "/settings",
    auth: "login",
    kind: "shell",
    // SettingsPage.tsx:352 tabTrigger("overview", "Overview") — the first
    // TabsTrigger (role=tab) of the settings surface.
    sentinel: { selector: "role=tab[name='Overview']" },
  },
  {
    path: "/admin/users",
    auth: "login",
    kind: "shell",
    // Static users fixture row — AdminUsersPage.tsx:486 {user.username}.
    sentinel: { selector: "text=e2e-user" },
  },
  {
    path: "/admin/groups",
    auth: "login",
    kind: "shell",
    // Static groups fixture row — GroupTable.tsx:139
    // {row.original.name} (rendered by AdminGroupsPage.tsx:292).
    sentinel: { selector: "text=e2e-group" },
  },
  {
    path: "/admin/organizations",
    auth: "login",
    kind: "shell",
    // Static orgs fixture row — OrgsPage.tsx:385 CardTitle {org.name}.
    sentinel: { selector: "text=e2e-org" },
  },
  {
    path: "/profile",
    auth: "login",
    kind: "shell",
    // ProfilePage.tsx:173-174 PageTitleHeader title="Profile" -> <h1>.
    sentinel: { selector: "role=heading[name='Profile']" },
  },
  {
    path: "/wiki",
    auth: "login",
    kind: "shell",
    // Seeded wiki page title — WikiPageList.tsx:188 {page.title}.
    sentinel: { selector: "text=Coolant Maintenance Wiki" },
  },
  {
    path: "/kms",
    auth: "login",
    kind: "shell",
    // Seeded KMS entry title — KMSPage.tsx:315 {entry.title}.
    sentinel: { selector: "text=Coolant Schedule Entry" },
  },
  {
    path: "/kms/1",
    auth: "login",
    kind: "shell",
    // Seeded KMS entry detail title — KMSDetailPage.tsx:173 {entry.title}.
    sentinel: { selector: "text=Coolant Schedule Entry" },
  },
  {
    path: "/search?q=coolant",
    auth: "login",
    kind: "shell",
    // Unified search result title — SearchPage.tsx:240 {result.title}; the
    // seeded q=coolant result is titled "handbook-a11y.pdf".
    sentinel: { selector: "text=handbook-a11y.pdf" },
  },
  {
    path: "/draft-room",
    auth: "login",
    kind: "shell",
    // Seeded draft title — DraftRoomPage.tsx:73 {draft.title}.
    sentinel: { selector: "text=A11Y Fixture Draft" },
  },
  {
    path: "/draft-room/1",
    auth: "login",
    kind: "shell",
    // Draft detail title — DraftRoomDetailPage.tsx:238 PageTitleHeader
    // title={draft.title}.
    sentinel: { selector: "text=A11Y Fixture Draft" },
  },
  {
    path: "/this-page-does-not-exist",
    auth: "anon",
    kind: "shellless",
    // NotFoundPage.tsx:13 EmptyState title="404".
    sentinel: { selector: "text=404" },
  },
];

interface Offender {
  tag: string;
  label: string;
  left: number;
  right: number;
  innerWidth: number;
}

interface AxeViolation {
  id: string;
  impact: string;
  html: string;
}

// The offender probe, defined ONCE as a serialized function so the gate
// (test 2) and the self-test (test 3) execute byte-identical logic. The two
// call sites invoke it as an IIFE string — Playwright evaluates string
// arguments as expressions and does NOT auto-call a function expression
// (plan-critic round 1 BLOCKER 1, replay-proven on @playwright/test 1.63).
//
// Offender = visible, non-zero-size interactive element (selector matches the
// AC2 letter: a, button, input, select, textarea, [role=button], tabindex>=0),
// not inside an aria-hidden="true" subtree (walk starts at the element
// itself), NOT exempt by the WCAG 1.4.10 two-dimensional-content exception —
// i.e. NOT a descendant of a <table> that a scroll container (computed
// overflow-x auto|scroll on the table or any ancestor between the table and
// the viewport) provides horizontal scrolling for. A generic overflow
// ancestor does NOT exempt: PageShell's content wrapper is overflow-auto on
// 16 of 18 shell routes, so a generic exemption would swallow exactly the
// reflow failures this gate exists to catch (plan-critic round 1 BLOCKER 2,
// measured). Exempt content is data-table content, per the audit review the
// issue cites ("data tables wrapped in a scrolling container").
// Offender condition: rect.left < -1 || rect.right > window.innerWidth + 1.
// Known deviation from the AC2 letter, disclosed: opacity:0 and
// content-visibility are not part of the visibility filter.
const OFFENDER_PROBE_SRC = `
  () => {
    const vw = window.innerWidth;
    const nodes = Array.from(
      document.querySelectorAll(
        'a, button, input, select, textarea, [role="button"], [tabindex]'
      )
    );
    const offenders = [];
    for (const el of nodes) {
      const tabIndexAttr = el.getAttribute("tabindex");
      // AC2 letter element list: a, button, input, select, textarea and
      // [role=button] ALWAYS qualify — a tabindex attribute on a
      // letter-listed element never disqualifies it (tabindex="-1" buttons
      // stay in scope). Any OTHER element qualifies only with a valid
      // non-negative integer tabindex ([tabindex>=0]; invalid strings and
      // "" are not focusable cues).
      if (
        el.tagName.toLowerCase() !== "a" &&
        el.tagName.toLowerCase() !== "button" &&
        el.tagName.toLowerCase() !== "input" &&
        el.tagName.toLowerCase() !== "select" &&
        el.tagName.toLowerCase() !== "textarea" &&
        el.getAttribute("role") !== "button" &&
        (tabIndexAttr === null || !/^[0-9]+$/.test(tabIndexAttr))
      ) {
        continue;
      }
      const style = window.getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none") continue;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      let hidden = false;
      for (let node = el; node !== null; node = node.parentElement) {
        if (node.getAttribute && node.getAttribute("aria-hidden") === "true") {
          hidden = true;
          break;
        }
      }
      if (hidden) continue;
      // WCAG 1.4.10 two-dimensional-content exemption: an element inside a
      // <table> whose horizontal overflow a scroll container owns.
      const table = el.closest("table");
      if (table) {
        let scrollOwned = false;
        for (let node = table; node !== null; node = node.parentElement) {
          const overflowX = window.getComputedStyle(node).overflowX;
          if (overflowX === "auto" || overflowX === "scroll") {
            scrollOwned = true;
            break;
          }
        }
        if (scrollOwned) continue;
      }
      if (rect.left < -1 || rect.right > vw + 1) {
        offenders.push({
          tag: el.tagName.toLowerCase(),
          label: (el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 60),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          innerWidth: vw,
        });
      }
    }
    return offenders;
  }
`;

// ---- helpers -----------------------------------------------------------------

async function setTheme(page: Page, theme: Theme) {
  // kv-theme is the zustand persist key (probe-a11y.spec.ts precedent):
  // the store reads { state: { theme }, version: 0 } at boot. Init scripts
  // accumulate and run in insertion order, so the LAST setTheme call wins
  // on the next navigation — theme switches mid-test are safe.
  await page.addInitScript((t) => {
    localStorage.setItem("kv-theme", JSON.stringify({ state: { theme: t }, version: 0 }));
  }, theme);
}

async function login(page: Page) {
  // Copied from chat-smoke.spec.ts (LoginPage.tsx selectors).
  await page.goto("/login");
  await page.fill("#login-username", "e2e-user");
  await page.fill("#login-password", "e2e-pass");
  await page.getByRole("button", { name: /sign in/i }).click();
  // Post-login navigation lands on "/" (-> /documents), or on
  // /change-password when the must_change_password flag is set.
  await page.waitForURL((url) => !url.pathname.endsWith("/login"), { timeout: 20_000 });
}

async function postControl(page: Page, routePath: string, data: Record<string, unknown>) {
  const res = await page.request.post(`${STUB_ORIGIN}${routePath}`, { data });
  if (!res.ok()) {
    throw new Error(`stub control ${routePath} failed: ${res.status()} ${await res.text()}`);
  }
}

// Page-independent control context (issue #778 review PRR-037): the toggle
// release in a test's finally must survive the page/context teardown that a
// 60s test timeout triggers — page.request dies with the context, this
// standalone context does not.
let controlApi: APIRequestContext | null = null;
async function controlContext(): Promise<APIRequestContext> {
  if (!controlApi) {
    controlApi = await playwrightRequest.newContext({ baseURL: STUB_ORIGIN });
  }
  return controlApi;
}

async function postControlStandalone(routePath: string, data: Record<string, unknown>) {
  const api = await controlContext();
  const res = await api.post(routePath, { data });
  if (!res.ok()) {
    throw new Error(`stub control ${routePath} failed: ${res.status()} ${await res.text()}`);
  }
}

// POST /_e2e/a11y-seed once per worker (module-level guard), AFTER login.
let a11ySeeded = false;
async function ensureA11ySeed(page: Page) {
  if (a11ySeeded) return;
  await postControl(page, "/_e2e/a11y-seed", {});
  a11ySeeded = true;
}

// Per-test auth prep. For /change-password the flag flip must precede
// login() (the login response user carries it — and every subsequent full
// page load re-fetches /api/auth/me, so the flag holds across reloads).
// Every booted context also pins the active vault to the stub's seed vault:
// a fresh context starts in "All Vaults" mode (useVaultStore treats a null
// activeVaultId as a VALID all-vaults selection and never auto-selects
// vaults[0]), and the VaultGate pages rolled out by #782 (/memory, /wiki,
// /kms) render their "Select a vault" state instead of the populated
// surface the sentinels gate on (amendment 4, CHECK_WRONG — the original
// roster assumed vault auto-selection that does not exist).
async function prepareRoute(page: Page, route: RouteRow) {
  await page.addInitScript(() => {
    localStorage.setItem("kv_active_vault_id", "1");
  });
  if (route.auth === "anon") return;
  if (route.path === "/change-password") {
    await postControl(page, "/_e2e/user-flags", { must_change_password: true });
  }
  await login(page);
  await ensureA11ySeed(page);
}

// Navigate to the route, wait for its sentinel, then settle. /setup toggles
// setup-mode around EVERY visit (each goto re-boots the app and re-reads
// /api/auth/setup-status) and resets it in the finally so later routes —
// and later iterations — never see setup mode.
async function visitRoute(page: Page, route: RouteRow) {
  if (route.path === "/setup") {
    await postControl(page, "/_e2e/setup-mode", { needs_setup: true });
  }
  try {
    await page.goto(route.path);
    await expect(page.locator(route.sentinel.selector).first()).toBeVisible({ timeout: 20_000 });
    // Settle: React Query refetches and layout effects can still be in
    // flight, and the app polls /api/health forever so networkidle never
    // settles — a fixed beat is the only bounded option.
    await page.waitForTimeout(800);
  } finally {
    if (route.path === "/setup") {
      await postControl(page, "/_e2e/setup-mode", { needs_setup: false });
    }
  }
}

// End-of-test toggle release (belt and suspenders on top of visitRoute's
// per-visit finally — post-fix wiring must never leak a flag to later rows).
async function releaseRouteToggles(_page: Page, route: RouteRow) {
  // Uses the standalone context on purpose (PRR-037): this runs in finally
  // blocks that race teardown after a test timeout.
  if (route.path === "/change-password") {
    await postControlStandalone("/_e2e/user-flags", { must_change_password: false });
  }
  if (route.path === "/setup") {
    await postControlStandalone("/_e2e/setup-mode", { needs_setup: false });
  }
}

async function runAxe(page: Page): Promise<AxeViolation[]> {
  await page.addScriptTag({ path: AXE_PATH });
  return page.evaluate(async (): Promise<AxeViolation[]> => {
    const win = window as unknown as {
      axe?: {
        run: () => Promise<{
          violations: Array<{
            id: string;
            impact: string | null;
            nodes: Array<{ html: string }>;
          }>;
        }>;
      };
    };
    if (!win.axe) {
      throw new Error("window.axe is missing — axe.min.js injection failed");
    }
    // Amendment 5 (AC_CHANGED_BY_USER, user-sanctioned 2026-10-09): the axe
    // gate enforces the full default rule set EXCEPT four rule families the
    // user moved to numbered follow-up issues (Radix Tabs dangling
    // aria-controls on unmounted panels; clickable-card nested interactive
    // content; remaining unnamed controls; chat empty-state list semantics).
    // color-contrast and every other serious/critical rule stay enforced —
    // at full matrix run 2 those other rules found only the four tracked-out
    // classes after the body base-coat fix.
    const results = await win.axe.run({
      rules: {
        // Each disabled family is owned by an open follow-up issue whose AC1
        // re-enables it (issue #778 review F-05 — keep the refs beside the
        // rules so the waiver never outlives its issues).
        "aria-valid-attr-value": { enabled: false }, // #865
        "nested-interactive": { enabled: false }, // #866
        "button-name": { enabled: false }, // #867
        "aria-required-children": { enabled: false }, // #868
      },
    });
    return results.violations
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .map((v) => ({
        id: v.id,
        impact: v.impact ?? "unknown",
        html: v.nodes
          .slice(0, 3)
          .map((n) => n.html)
          .join(" | "),
      }));
  });
}

// Below-fold pass: a single scan at the natural scroll position certifies
// only the first viewport (lazily-measured contrast and off-screen reflow
// slip through). Scroll every scroll container to its bottom and settle.
async function scrollAllToBottom(page: Page) {
  await page.evaluate(() => {
    const scrollers = [
      document.scrollingElement,
      ...Array.from(
        document.querySelectorAll(
          'main, [class*="overflow-auto"], [class*="overflow-y-auto"], [data-radix-scroll-area-viewport]'
        )
      ),
    ];
    for (const el of scrollers) {
      if (el instanceof HTMLElement || el instanceof Element) {
        el.scrollTop = (el as HTMLElement).scrollHeight;
      }
    }
  });
  await page.waitForTimeout(400);
}

// axe with a second, scrolled pass; the two violation sets merge.
async function runAxeBothPasses(page: Page): Promise<AxeViolation[]> {
  const top = await runAxe(page);
  await scrollAllToBottom(page);
  const bottom = await runAxe(page);
  const merged = new Map<string, AxeViolation>();
  for (const v of [...top, ...bottom]) {
    const key = `${v.id}:${v.html}`;
    if (!merged.has(key)) merged.set(key, v);
  }
  return Array.from(merged.values());
}

// ---- gates -------------------------------------------------------------------

test.describe("a11y matrix (issue #778 — routes x viewports x themes)", () => {
  // One matrix row per test keeps every row inside the 60s budget
  // (playwright.config.ts timeout: 60_000; workers: 1 serializes the file).
  test.describe.configure({ timeout: 60_000 });

  test.afterAll(async () => {
    // Local-run hygiene only — CI boots the stub fresh. Guarded so a
    // restarted stub (or a control route that never existed) cannot fail
    // the suite from afterAll. Reuses the standalone control context.
    try {
      const api = await controlContext();
      await api.post("/_e2e/setup-mode", { data: { needs_setup: false } });
      await api.post("/_e2e/user-flags", { data: { must_change_password: false } });
      await api.post("/_e2e/a11y-reset", { data: {} });
    } catch {
      // Best-effort reset — see comment above.
    } finally {
      if (controlApi) {
        await controlApi.dispose();
        controlApi = null;
      }
    }
  });

  // Gate 1 — axe serious/critical across the full matrix. Fan-out per
  // route x viewport x theme (one visit per test keeps every row far inside
  // the 60s budget even when a sentinel regresses — plan-critic round 1
  // MINOR 5 — and reports the exact failing combination).
  for (const route of ROUTES) {
    for (const viewport of VIEWPORTS) {
      for (const theme of THEMES) {
        test(`[${route.path}][${viewport.width}x${viewport.height}][${theme}] no serious or critical axe violations`, async ({ page }) => {
          await page.setViewportSize({ width: viewport.width, height: viewport.height });
          await setTheme(page, theme);
          // prepareRoute is INSIDE the try (issue #778 review PRR-002): its
          // /_e2e/user-flags flip precedes login, so a login/seed throw before
          // the try would skip the release and cascade /change-password
          // redirects across every later row.
          try {
            await prepareRoute(page, route);
            await visitRoute(page, route);
            // Dark legs pin the theme store actually applied (issue #778
            // review F-18): a kv-theme regression would otherwise run the
            // dark combination silently against light styles.
            if (theme === "dark") {
              await expect(page.locator("html")).toHaveClass(/dark/, { timeout: 5_000 });
            }
            const violations = await runAxeBothPasses(page);
            const details = violations
              .map((v) => `[${v.impact}] ${v.id}: ${v.html}`)
              .join("; ");
            expect(
              details,
              `${route.path} ${viewport.width}x${viewport.height} ${theme}: axe serious/critical violations`
            ).toBe("");
          } finally {
            await releaseRouteToggles(page, route);
          }
        });
      }
    }
  }

  // Gate 2 — offender probe, ONLY at 320x568 (the reflow-critical width).
  for (const route of ROUTES) {
    test(`[${route.path}] no interactive element overflows the viewport`, async ({ page }) => {
      await page.setViewportSize({ width: 320, height: 568 });
      await setTheme(page, "light");
      try {
        await prepareRoute(page, route);
        await visitRoute(page, route);
        await scrollAllToBottom(page);
        const offenders = (await page.evaluate(
          `(${OFFENDER_PROBE_SRC})()`
        )) as unknown as Offender[];
        const details = offenders
          .map(
            (o) => `<${o.tag}> "${o.label}" left=${o.left} right=${o.right} innerWidth=${o.innerWidth}`
          )
          .join("; ");
        expect(
          details,
          `${route.path} 320x568: interactive elements outside the viewport`
        ).toBe("");
      } finally {
        await releaseRouteToggles(page, route);
      }
    });
  }

  // Gate 3 — probe self-test on a synthetic page (no app, no stub route):
  // the wide button inside the overflow-x:auto container (as table content —
  // the two-dimensional shape the WCAG 1.4.10 exception and the audit review
  // name) is exempt; the uncontained one is the single offender.
  test("offender probe exempts scroll containers", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 568 });
    await page.setContent(
      '<div style="overflow-x:auto;width:300px"><table><tbody><tr><td>' +
        '<button style="width:600px">contained wide button</button>' +
        "</td></tr></tbody></table></div>" +
        '<button style="width:600px">uncontained wide button</button>'
    );
    const offenders = (await page.evaluate(
      `(${OFFENDER_PROBE_SRC})()`
    )) as unknown as Offender[];
    expect(offenders.length, JSON.stringify(offenders)).toBe(1);
    expect(offenders[0]?.label, JSON.stringify(offenders)).toBe("uncontained wide button");
  });

  // Gate 3b — table-scoping discrimination (plan-critic round 2 MAJOR 2): a
  // wide button in a NON-table overflow-x:auto container must be an OFFENDER.
  // Under a reverted generic scroll-ancestor exemption this test goes RED
  // (the button is wrongly exempted) — the round-1 blocker-2 regression class
  // cannot stay green.
  test("offender probe flags non-table scrollers as offenders", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 568 });
    await page.setContent(
      '<div style="overflow-x:auto;width:300px">' +
        '<button style="width:600px">rail button not in a table</button>' +
        "</div>"
    );
    const offenders = (await page.evaluate(
      `(${OFFENDER_PROBE_SRC})()`
    )) as unknown as Offender[];
    expect(offenders.length, JSON.stringify(offenders)).toBe(1);
    expect(offenders[0]?.label, JSON.stringify(offenders)).toBe("rail button not in a table");
  });

  // Gate 3c — tabindex-shape guard (plan-critic round 3 blocker): a
  // letter-listed element carrying tabindex="-1" must REMAIN an offender —
  // the strict tabindex parse gates only the non-letter fallthrough.
  test("offender probe keeps tabindex minus one letter elements in scope", async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 568 });
    await page.setContent(
      '<button tabindex="-1" style="width:600px">mouse only wide button</button>'
    );
    const offenders = (await page.evaluate(
      `(${OFFENDER_PROBE_SRC})()`
    )) as unknown as Offender[];
    expect(offenders.length, JSON.stringify(offenders)).toBe(1);
    expect(offenders[0]?.label, JSON.stringify(offenders)).toBe("mouse only wide button");
  });

  // Gate 4 — exactly one OUTERMOST main landmark on shell routes, zero on
  // shell-less routes (see the header comment for the nested-main note).
  for (const route of ROUTES) {
    test(`[${route.path}] exactly one main landmark per route`, async ({ page }) => {
      await page.setViewportSize({ width: 320, height: 568 });
      await setTheme(page, "light");
      try {
        await prepareRoute(page, route);
        await visitRoute(page, route);
        const landmarks = await page.evaluate(() => {
          const all = Array.from(document.querySelectorAll('main, [role="main"]'));
          const outermost = all.filter((el) => !el.parentElement?.closest('main, [role="main"]'));
          return {
            outermost: outermost.length,
            total: all.length,
            tags: all.map((el) => {
              const role = el.getAttribute("role");
              return el.tagName.toLowerCase() + (role ? `[role=${role}]` : "");
            }),
          };
        });
        const expected = route.kind === "shell" ? 1 : 0;
        expect(
          landmarks.outermost,
          `${route.path}: outermost main landmarks [${landmarks.tags.join(", ")}]` +
            ` (total incl. nested: ${landmarks.total}), expected ${expected} for kind "${route.kind}"`
        ).toBe(expected);
      } finally {
        await releaseRouteToggles(page, route);
      }
    });
  }
});
