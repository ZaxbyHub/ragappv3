# Frontend testing gotchas (Vitest + jsdom)

Reusable patterns for this repo's frontend tests. Reach for these instead of
re-deriving them — each one cost a debugging cycle to discover.

Test runner: **Vitest** (`vitest run`), environment **jsdom**, setup file
`frontend/src/test/setup.ts` (mocks `localStorage`, `window.confirm`,
`Element.prototype.scrollTo`). Library: `@testing-library/react` +
`@testing-library/jest-dom`.

## 1. Components using `<Link>` / router hooks need a Router

Any component that renders `<Link>` or calls `useNavigate`/`useParams` throws
without a router context. Wrap renders in `MemoryRouter`. To keep call sites
clean across a whole file, alias the RTL `render`:

```tsx
import { render as rtlRender, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const render: typeof rtlRender = (ui, options) =>
  rtlRender(ui, { wrapper: MemoryRouter, ...options });
```

`DocumentTable` (links filenames to `/documents/:id`) is the canonical case —
see `src/pages/DocumentsPage.test.tsx` and the virtualization test files.

## 2. Radix `Select` (shadcn `ui/select`) cannot be opened in jsdom

Radix Select relies on pointer-capture / `scrollIntoView` APIs jsdom doesn't
implement, so `fireEvent.click` on the trigger won't reveal options and
`userEvent.selectOptions` doesn't apply (it's not a native `<select>`). Two
working approaches in this repo:

**(a) Mock the primitive with a context** when you need to assert the
`onValueChange` wiring (e.g. testing that a filter emits the right value):

```tsx
vi.mock("@/components/ui/select", async () => {
  const React = await import("react");
  const Ctx = React.createContext<(v: string) => void>(() => {});
  return {
    Select: ({ onValueChange, children }: any) =>
      React.createElement(Ctx.Provider, { value: onValueChange }, children),
    SelectTrigger: ({ children, "aria-label": al }: any) =>
      React.createElement("div", { role: "group", "aria-label": al }, children),
    SelectValue: ({ placeholder }: any) => React.createElement("span", null, placeholder),
    SelectContent: ({ children }: any) => React.createElement("div", null, children),
    SelectItem: ({ value, children }: any) => {
      const onValueChange = React.useContext(Ctx);
      return React.createElement("button", { onClick: () => onValueChange(value) }, children);
    },
  };
});
```

Then `fireEvent.click(screen.getByRole("button", { name: "<item label>" }))`
fires the real `onValueChange` path. See `src/tests/documents-organization.test.tsx`
(TagFilter test). **Only mock the module in files where the component under
test is the sole `ui/select` consumer**, or the mock will reshape other
rendered components.

**(b) Render-stub the primitive** when you only need the trigger present and
don't assert selection — `src/pages/VaultsPage.test.tsx` mocks each part to a
plain `<div>`/`<button>`.

## 3. Virtualized lists (`@tanstack/react-virtual`) hide off-screen rows

`useVirtualizer` only renders the visible window, so rows you want to assert on
may not be in the DOM. Mock it to render all items:

```tsx
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: vi.fn(({ count, estimateSize }: any) => {
    const size = estimateSize?.() ?? 72;
    return {
      getVirtualItems: () =>
        Array.from({ length: count }, (_, i) => ({ index: i, start: i * size, size, key: i })),
      getTotalSize: () => count * size,
      measureElement: () => {},
    };
  }),
}));
```

See `src/tests/documents-organization.test.tsx` and the DocumentsPage
virtualization suites.

## 4. `vi.mock` is hoisted — factories can't close over outer variables

`vi.mock(path, factory)` is hoisted above imports. The factory may not
reference module-scope variables; use a dynamic `await import("react")` inside
the factory (as above) rather than a top-level `import React`.

## 5. `window.matchMedia`, `vi.stubEnv` ordering, and the renderer-mock anti-pattern

**`window.matchMedia` is not in jsdom.** Any component that transitively
imports `useThemeStore` will crash with `window.matchMedia is not a function`
because that store calls `matchMedia` at module-load time. The global
`frontend/src/test/setup.ts` does NOT stub it. Add a `beforeAll` stub
whenever your test renders a component that imports the theme store:

```tsx
beforeAll(() => {
  if (!window.matchMedia) {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: (query: string) => ({
        matches: false, media: query, onchange: null,
        addEventListener: () => {}, removeEventListener: () => {},
        addListener: () => {}, removeListener: () => {},
        dispatchEvent: () => false,
      }),
    });
  }
});
```

Affected: `NavigationRail`, `PageShell`, and anything that renders the app
shell — because `NavigationRail` imports `useThemeStore` directly.

**`vi.stubEnv` must be at the top level (module scope), before any `import()`.**
Vite replaces `import.meta.env.*` at module-evaluation time. Calling
`vi.stubEnv` in `beforeAll` is too late if the test then does
`const App = await import("../App")` — the module was already evaluated with
the original env values. Place `vi.stubEnv` calls at the top of the test file:

```ts
// TOP OF FILE — before any imports or describe blocks
vi.stubEnv("VITE_APP_BASENAME", "/meridian");

describe("...", () => {
  it("...", async () => {
    const App = (await import("../App")).default; // gets the stubbed env
  });
});
```

**Anti-pattern: don't mock third-party renderers to silence crashes.** If a
component crashes at render and you mock the crashing sub-component to
`() => null` just to make the test pass, you mask a real production bug. The
correct fix is to pass valid inputs to the sub-component. For DOM assertions
that don't require SVG identity, assert on stable structure instead —
aria-labels, roles, text content, or `querySelector("svg")`.

The canonical mistake in this repo: `NavigationRail.test.tsx` previously
contained `vi.mock("@hugeicons/react", () => ({ HugeiconsIcon: () => null }))`.
The comment claimed "icons resolve to undefined under vitest" — they actually
resolve correctly; the real problem was a wrong type discriminator passing a
lucide `forwardRef` object to `HugeiconsIcon`, which spread it and crashed.
The mock hid this crash from CI for the entire PR #140 lifetime.
