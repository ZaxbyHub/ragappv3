import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Issue #867 census guard (AC8): unnamed interactive controls must not
// reappear after the button-name re-arm. This is a SOURCE-CENSUS structural
// guard in the App.render-timeout-guard.test.ts lineage (readFileSync +
// lightweight scanning, no DOM): axe only measures rendered default states,
// so a control that is unnamed only in a non-default state (loading spinner
// branch, empty-selection combobox, childless tree node) is invisible to the
// e2e matrix but still an axe button-name violation when that state renders.
// The three predicate classes below pin the named-state contract statically.
//
// RED at the pre-fix base (bf33d57f: the #867 worklist — FolderTree leaf
// chevron, MemoryPage search, WikiPageDetail delete, WikiJobsPanel refresh)
// and GREEN after the fix labels them; the failure message IS the fix's
// worklist (file:line per offender).
//
// AMENDED (checkpoint CHECK_WRONG, plan-critic round-1 finding 4): the
// SelectTrigger leg originally flagged every placeholder-capable trigger
// without aria-label, conflating placeholder presence with a missing
// accessible name. Nine of ten flagged triggers are named at runtime by an
// associated <Label htmlFor> (accname step 2D — the critic's
// computeAccessibleName replay returned the label text, not the placeholder),
// and the tenth pins a concrete parent value (WikiJobsPanel.tsx:
// value={statusFilter || "all"}) so its placeholder state is unreachable.
// The amended Select leg exempts those shapes and is GREEN at the base; the
// RED-at-base classes (icon-only Buttons, FolderTree chevron) are unchanged.
//
// Deliberate scope limits, so the predicates stay discriminating instead of
// brittle (each class documents what it does NOT try to catch):
//  - SelectTrigger: only the genuinely-unnamed placeholder shape — a
//    SelectValue placeholder prop (reachable unnamed state candidate) with
//    NONE of these name sources: aria-label/aria-labelledby on the block, an
//    associated <Label htmlFor> in the same file targeting the trigger's id
//    (matched by identical attribute-value text, so variable ids like
//    htmlFor={fromSelectId}/id={fromSelectId} associate), inline text
//    children, or a parent <Select value={... || "lit"/"lit"}> that pins a
//    concrete value so the placeholder never renders. Triggers that render a
//    concrete selected value are NOT flagged: their accessible name comes
//    from contents, and a blanket aria-label there breaks WCAG 2.5.3
//    Label-in-Name (03-localization-log constraint).
//  - Button: only icon-only shapes (no visible text in any branch, an
//    icon-ish component tag present). Buttons whose conditional fallback is a
//    text string ("Delete User" etc.) are named via contents and are NOT
//    offenders. components/ui/pagination.tsx's disabled ellipsis button is
//    aria-hidden="true" and therefore outside the a11y tree — excluded by the
//    aria-hidden leg, no file-list denylist needed.
//  - plain <button>: only the can-render-empty shape (children whose
//    conditional fallback is null — the FolderTree leaf-chevron class).

const SRC_ROOT = "src";

/** All non-test, non-fb .tsx component files under src/ (relative paths). */
function collectTsxFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...collectTsxFiles(full));
    } else if (
      entry.isFile() &&
      entry.name.endsWith(".tsx") &&
      !/\.(test|fb)\./.test(entry.name)
    ) {
      out.push(full);
    }
  }
  return out;
}

/**
 * Blank every comment (block + line) with spaces, preserving offsets and
 * newlines, so structural scans cannot match markup inside commented-out
 * code. String literals are KEPT (their contents are legitimate analysis
 * input); a closing tag inside a string or comment is filtered by scanning
 * for it in this blanked view while slicing text from the original.
 */
function blankComments(source: string): string {
  const chars = source.split("");
  let i = 0;
  let inString: string | null = null;
  while (i < chars.length) {
    const ch = chars[i];
    const next = chars[i + 1];
    if (inString) {
      if (ch === "\\") {
        i += 2;
        continue;
      }
      if (ch === inString) inString = null;
      i += 1;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inString = ch;
      i += 1;
      continue;
    }
    if (ch === "/" && next === "*") {
      const end = source.indexOf("*/", i + 2);
      const stop = end === -1 ? chars.length : end + 2;
      for (let j = i; j < stop; j++) if (chars[j] !== "\n") chars[j] = " ";
      i = stop;
      continue;
    }
    if (ch === "/" && next === "/") {
      const end = source.indexOf("\n", i);
      const stop = end === -1 ? chars.length : end;
      for (let j = i; j < stop; j++) chars[j] = " ";
      i = stop;
      continue;
    }
    i += 1;
  }
  return chars.join("");
}

interface ElementBlock {
  /** 1-based line of the opening tag. */
  line: number;
  /** 0-based index of the opening tag in the file source. */
  start: number;
  /** Opening tag source, `<Tag ...>` or `<Tag ... />`. */
  openTag: string;
  /** Source between the opening tag and the closing tag ("" when self-closing). */
  inner: string;
  selfClosing: boolean;
}

/**
 * Scan the end of an opening tag beginning at `start`: string-aware and
 * brace-aware (a ">" inside an attribute string, an arrow function's `=>` or
 * a generic inside an attribute-value expression can never end the tag).
 * Returns null when no balanced end exists (fail-safe: callers skip).
 */
function scanOpenTag(source: string, start: number): { openEnd: number; selfClosing: boolean } | null {
  let i = start;
  let inString: string | null = null;
  let depth = 0;
  while (i < source.length) {
    const ch = source[i];
    if (inString) {
      if (ch === "\\") {
        i += 2;
      } else {
        if (ch === inString) inString = null;
        i += 1;
      }
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inString = ch;
      i += 1;
      continue;
    }
    if (ch === "{") {
      depth += 1;
      i += 1;
      continue;
    }
    if (ch === "}") {
      depth -= 1;
      i += 1;
      continue;
    }
    if (ch === ">" && depth === 0) {
      let k = i - 1;
      while (k >= start && /\s/.test(source[k])) k -= 1;
      return { openEnd: i + 1, selfClosing: source[k] === "/" };
    }
    i += 1;
  }
  return null;
}

/**
 * Every `<tag ...>` element block in the file. The opening tag ends at the
 * scanOpenTag boundary; the closing tag is searched in the comment-blanked
 * view so markup inside strings or comments cannot truncate a block.
 * Unbalanced blocks are skipped (fail-safe: they match no child predicate).
 */
function findElementBlocks(source: string, blanked: string, tag: string): ElementBlock[] {
  const blocks: ElementBlock[] = [];
  const openRe = new RegExp(`<${tag}(?=[\\s/>])`, "g");
  let m: RegExpExecArray | null;
  while ((m = openRe.exec(blanked)) !== null) {
    const start = m.index;
    const scanned = scanOpenTag(source, start);
    if (scanned === null) continue;
    const { openEnd, selfClosing } = scanned;
    if (selfClosing) {
      blocks.push({ line: lineOf(source, start), start, openTag: source.slice(start, openEnd), inner: "", selfClosing: true });
      continue;
    }
    const closeTag = `</${tag}>`;
    const closeIdx = blanked.indexOf(closeTag, openEnd);
    if (closeIdx === -1) continue;
    blocks.push({
      line: lineOf(source, start),
      start,
      openTag: source.slice(start, openEnd),
      inner: blanked.slice(openEnd, closeIdx),
      selfClosing: false,
    });
    openRe.lastIndex = openEnd;
  }
  return blocks;
}

function lineOf(source: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index; i++) if (source[i] === "\n") line += 1;
  return line;
}

/**
 * Parse an attribute value starting at index `i` (which must sit on a quote
 * or an opening brace). The brace scanner is string-aware, so template
 * literals with interpolation (`{`draft-${draftId}-retry-stage`}`) close at
 * their true end rather than at the first `}` inside `${...}`. Returns the
 * raw inner text, or null when unbalanced.
 */
function parseAttrValueAt(text: string, i: number): string | null {
  const ch = text[i];
  if (ch === '"' || ch === "'") {
    const end = text.indexOf(ch, i + 1);
    return end === -1 ? null : text.slice(i + 1, end);
  }
  if (ch === "{") {
    let depth = 0;
    let inString: string | null = null;
    for (let j = i; j < text.length; j++) {
      const c = text[j];
      if (inString) {
        if (c === "\\") j += 1;
        else if (c === inString) inString = null;
        continue;
      }
      if (c === '"' || c === "'" || c === "`") {
        inString = c;
        continue;
      }
      if (c === "{") depth += 1;
      else if (c === "}") {
        depth -= 1;
        if (depth === 0) return text.slice(i + 1, j);
      }
    }
    return null;
  }
  return null;
}

/**
 * Extract an attribute's RAW value text (inside quotes or braces, whichever
 * it is) from an opening tag — e.g. `id="create-role"` -> `create-role`,
 * `id={fromSelectId}` -> `fromSelectId`,
 * `id={`draft-${draftId}-retry-stage`}` -> `draft-${draftId}-retry-stage`
 * (backticks kept; both sides of a comparison carry them). Returns null when
 * the attribute is absent. Comparing raw value texts makes label association
 * work for literal, variable, AND template ids, because both sides
 * (`<Label htmlFor={X}>` / `<SelectTrigger id={X}>`) carry the identical
 * source token.
 */
/**
 * True when openTag carries `name` with a LIVE value: the attribute exists
 * and its value is neither the empty string nor the literal `{undefined}`
 * (React drops both, so the attribute names nothing at runtime).
 */
function hasLiveAttribute(openTag: string, name: string): boolean {
  const value = attrValueText(openTag, name);
  if (value === null) return false;
  const v = value.trim();
  return v !== "" && v !== "undefined";
}

function attrValueText(openTag: string, name: string): string | null {
  const re = new RegExp(`(?:^|[\\s"])${name}\\s*=`, "g");
  const m = re.exec(openTag);
  if (m === null) return null;
  let i = m.index + m[0].length;
  while (i < openTag.length && /\s/.test(openTag[i])) i += 1;
  return parseAttrValueAt(openTag, i);
}

/** Last regex match index in `text` (for backward searches); -1 when none. */
function lastMatch(text: string, source: string): number {
  const re = new RegExp(source, "g");
  let last = -1;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) last = m.index;
  return last;
}

/**
 * Every label-target value text in the (comment-blanked) file: the value of
 * each htmlFor/for attribute on any element. `<Label htmlFor="x">` (shadcn,
 * renders a real <label for="x">), plain `<label htmlFor={x}>` and raw
 * for="x" are all collected, so association is detected regardless of which
 * label flavor a file uses. Values parse with the same balanced scanner as
 * trigger ids (template ids associate exactly). Scanned on the blanked view
 * so commented-out labels never exempt a trigger.
 */
function labelTargets(blanked: string): Set<string> {
  const targets = new Set<string>();
  const re = /[\s<](?:htmlFor|for)\s*=/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(blanked)) !== null) {
    let i = m.index + m[0].length;
    while (i < blanked.length && /\s/.test(blanked[i])) i += 1;
    const value = parseAttrValueAt(blanked, i);
    if (value !== null) targets.add(value);
  }
  return targets;
}

/**
 * True when the <Select ...> element ENCLOSING the trigger at `triggerStart`
 * pins a concrete value: its value attribute carries a non-empty string
 * literal fallback (`value={x || "all"}` / `value={x ?? "all"}`), so the
 * SelectValue placeholder never renders and the trigger's name always comes
 * from the selected item's contents. Walks backward past closed sibling
 * Selects (a </Select> between the candidate open and the trigger proves the
 * candidate is not the parent). Conservative by design: ternaries and
 * undefined fallbacks do NOT exempt (their placeholder state stays reachable).
 */
function parentSelectPinsValue(source: string, blanked: string, triggerStart: number): boolean {
  let searchEnd = triggerStart;
  while (searchEnd > 0) {
    const openIdx = lastMatch(blanked.slice(0, searchEnd), "<Select(?=[\\s/>])");
    if (openIdx === -1) return false;
    const closeIdx = blanked.indexOf("</Select>", openIdx);
    if (closeIdx !== -1 && closeIdx < triggerStart) {
      // That Select closed before our trigger: a completed sibling. Keep
      // looking at earlier candidates.
      searchEnd = openIdx;
      continue;
    }
    const scanned = scanOpenTag(source, openIdx);
    if (scanned === null) return false;
    const value = attrValueText(source.slice(openIdx, scanned.openEnd), "value");
    if (value === null) return false;
    return (
      /\|\|\s*["'][^"']+["']/.test(value) || /\?\?\s*["'][^"']+["']/.test(value) || /\|\|\s*`[^`]+`/.test(value) || /\?\?\s*`[^`]+`/.test(value)
    );
  }
  return false;
}

interface InnerAnalysis {
  /** Text outside tags and outside top-level {...} expressions, whitespace-trimmed. */
  staticText: string;
  /** Top-level {...} expression contents with tags stripped out. */
  expressionsStripped: string[];
  /** True when any tag anywhere in the inner content is an icon-ish component (uppercase or member-expression name). */
  hasIconTag: boolean;
}

/** Analyze an element's inner JSX for text vs icon content. */
function analyzeInner(inner: string): InnerAnalysis {
  // Walk the inner source recording: characters outside tags and top-level
  // braces (static text), top-level brace-expression spans, and every tag name.
  let depth = 0; // brace depth at top level of the inner content
  let inString: string | null = null;
  let inTag = false;
  let tagName = "";
  const staticChars: string[] = [];
  const expressions: string[] = [];
  const exprChars: string[] = [];
  const tagNames: string[] = [];
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i];
    const next = inner[i + 1];
    if (inString) {
      if (!inTag && depth > 0) exprChars.push(ch);
      else if (!inTag && depth === 0) staticChars.push(ch);
      if (ch === "\\") i += 1;
      else if (ch === inString) inString = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inString = ch;
      if (!inTag && depth > 0) exprChars.push(ch);
      else if (!inTag && depth === 0) staticChars.push(ch);
      continue;
    }
    if (inTag) {
      if (ch === ">") {
        if (tagName) tagNames.push(tagName);
        inTag = false;
        tagName = "";
      } else if (/\s/.test(ch)) {
        if (tagName) {
          tagNames.push(tagName);
          tagName = "";
        }
      } else {
        tagName += ch;
      }
      continue;
    }
    if (ch === "<") {
      inTag = true;
      tagName = "";
      // Skip the "/" of a closing tag and the tag name's leading chars are
      // accumulated above; a name like "/div" normalizes to "div".
      if (next === "/") i += 1;
      continue;
    }
    if (ch === "{") {
      depth += 1;
      continue;
    }
    if (ch === "}") {
      depth -= 1;
      if (depth === 0) {
        expressions.push(exprChars.join(""));
        exprChars.length = 0;
      }
      continue;
    }
    if (depth > 0) exprChars.push(ch);
    else staticChars.push(ch);
  }
  // Closing-tag "/" was folded into names ("div" from "</div"); strip any
  // residual "/" and drop empties.
  const names = tagNames.map((n) => n.replace(/^\//, "")).filter(Boolean);
  return {
    staticText: staticChars.join("").trim(),
    expressionsStripped: expressions.map(stripTags),
    hasIconTag: names.some((n) => /^[A-Z]/.test(n) || n.includes(".")),
  };
}

/** Remove `<...>` spans from a string (for looking at expression text only). */
function stripTags(text: string): string {
  return text.replace(/<[^>]*>/g, " ");
}

/**
 * Remove balanced `(...)` groups that contain a `?` — a nested ternary's
 * condition identifiers are structure, not rendered text (e.g. the FolderTree
 * chevron's `(isOpen ? <ChevronDown/> : <ChevronRight/>)` branch).
 */
function stripNestedConditionalGroups(text: string): string {
  let out = "";
  let group = "";
  let depth = 0;
  for (const ch of text) {
    if (ch === "(") {
      depth += 1;
      if (depth === 1) {
        group = "";
        continue;
      }
    } else if (ch === ")") {
      depth -= 1;
      if (depth === 0) {
        if (!group.includes("?")) out += `(${group})`;
        group = "";
        continue;
      }
    }
    if (depth > 0) group += ch;
    else out += ch;
  }
  return out;
}

/**
 * True when a tag-stripped expression renders text a user can read. A
 * top-level ternary splits at its first `?`: everything before it is the
 * condition (identifiers are fine there), everything after is RENDERED
 * content — letters there (bare JSX text inside a fragment, like
 * `Deleting...`, or a quoted fallback like `"Delete User"`) mean the control
 * is named via contents in that branch. Nested ternary conditions inside
 * parens are structural and stripped first. An expression with no `?`
 * renders its whole value, so any letter (a dynamic `{result.title}`,
 * `{children}`) counts as text. Icon class names never reach here: they live
 * inside tags, which were stripped.
 */
function expressionRendersText(exprStripped: string): boolean {
  const q = exprStripped.indexOf("?");
  const valuePart = q === -1 ? exprStripped : exprStripped.slice(q + 1);
  // `null` / `undefined` / boolean literals are value keywords, not text.
  const content = stripNestedConditionalGroups(valuePart)
    .replace(/\b(?:null|undefined|true|false)\b/g, " ");
  return /[A-Za-z]/.test(content);
}

/**
 * The inner content renders text a user can read: literal JSX text outside
 * expressions, or an expression whose rendered branch contains text (see
 * expressionRendersText).
 */
function rendersVisibleText(inner: string): boolean {
  const a = analyzeInner(inner);
  if (a.staticText.length > 0) return true;
  return a.expressionsStripped.some(expressionRendersText);
}

function hasIconComponent(inner: string): boolean {
  // Inline `<svg>` (lowercase, not a component tag) is still an icon — an
  // icon-only shape by the same rule the capitalised-component check covers.
  return analyzeInner(inner).hasIconTag || /<svg[\s>/]/.test(inner);
}

interface Offender {
  file: string;
  line: number;
  openTagExcerpt: string;
}

const files = collectTsxFiles(SRC_ROOT);

describe("unnamed-controls source census (issue #867 AC8)", () => {
  it("placeholder-capable SelectTriggers carry an accessible name", () => {
    const offenders: Offender[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      const blanked = blankComments(source);
      const labels = labelTargets(blanked);
      for (const block of findElementBlocks(source, blanked, "SelectTrigger")) {
        const whole = block.openTag + block.inner;
        // Candidate: a SelectValue placeholder prop = a state whose trigger
        // could render unnamed — but only when NO other name source exists.
        if (!whole.includes("placeholder=")) continue;
        // Exemption 1 — named directly: aria-label / aria-labelledby on the
        // trigger's OWN open tag (a descendant's aria-label does not name
        // the combobox role), and only when the value is live (non-empty,
        // not literally {undefined}).
        if (hasLiveAttribute(block.openTag, "aria-label")) continue;
        if (hasLiveAttribute(block.openTag, "aria-labelledby")) continue;
        // Exemption 2 — named by an associated label (accname step 2D; the
        // plan critic's runtime computeAccessibleName replay confirmed the
        // label wins over the placeholder): a <Label htmlFor>/<label for> in
        // this file targets the trigger's id (identical attribute-value
        // text, so literal, variable, and template ids all associate).
        const idText = attrValueText(block.openTag, "id");
        if (idText !== null && labels.has(idText)) continue;
        // Exemption 3 — named by inline contents: literal text children.
        if (rendersVisibleText(block.inner)) continue;
        // Exemption 4 — placeholder unreachable: the parent <Select> pins a
        // concrete value (`value={x || "lit"}`), so the trigger always shows
        // the selected item's text instead of the placeholder.
        if (parentSelectPinsValue(source, blanked, block.start)) continue;
        offenders.push(offender(file, block));
      }
    }
    expect(
      offenders,
      `SelectTrigger blocks with no name source in their placeholder state ` +
        `(add aria-label mirroring the visible field label, or an associated Label): ${render(offenders)}`
    ).toEqual([]);
  });

  it("icon-only Buttons carry an accessible name or are aria-hidden", () => {
    const offenders: Offender[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      const blanked = blankComments(source);
      for (const block of findElementBlocks(source, blanked, "Button")) {
        // ui/pagination.tsx's disabled ellipsis button carries aria-hidden on
        // the opening tag and is excluded by that same leg (no denylist).
        if (hasLiveAttribute(block.openTag, "aria-label")) continue;
        if (hasLiveAttribute(block.openTag, "aria-labelledby")) continue;
        // ui/pagination.tsx's disabled ellipsis button carries aria-hidden on
        // the opening tag and is excluded by that same leg (no denylist).
        // aria-hidden only exempts when literally "true" — "false" leaves
        // the control in the accessibility tree.
        if (attrValueText(block.openTag, "aria-hidden") === "true") continue;
        // A title attribute is an accessible-name source (accname step 2F),
        // so a title-bearing control is not a button-name offender — but
        // only a non-empty literal title names anything; title="" or
        // title={undefined} does not.
        const title = attrValueText(block.openTag, "title");
        if (title !== null && title.trim() !== "" && title !== "undefined") continue;
        // Generic wrappers (LoadingButton, EmptyState-style components) spread
        // props or render caller-provided children — their accessible name is
        // supplied at the call site, so this static census cannot classify
        // them. Call-site buttons are themselves scanned by this same pass.
        if (block.openTag.includes("{...")) continue;
        if (/\bchildren\b/.test(block.inner)) continue;
        // Long bodies are by construction not the compact icon-only shape.
        if (block.inner.length > 400) continue;
        if (rendersVisibleText(block.inner)) continue;
        if (!hasIconComponent(block.inner)) continue;
        offenders.push(offender(file, block));
      }
    }
    expect(
      offenders,
      `icon-only Button blocks without a name ` +
        `(add aria-label, or aria-hidden when decorative): ${render(offenders)}`
    ).toEqual([]);
  });

  it("predicate hardening fixtures (#1022 review): comments are not names, svg is an icon, presence exemptions require live values", () => {
    const cases: Array<{ name: string; src: string; offender: boolean }> = [
      { name: "JSX comment is not an accessible name", offender: true,
        src: '<Button onClick={x}>{/* TODO: label me */}<Trash2 size={16} /></Button>' },
      { name: "inline svg counts as an icon", offender: true,
        src: '<Button onClick={x}><svg viewBox="0 0 2 2" /></Button>' },
      { name: "aria-hidden=false does not exempt", offender: true,
        src: '<Button onClick={x} aria-hidden="false"><Trash2 size={16} /></Button>' },
      { name: "empty title does not exempt", offender: true,
        src: '<Button onClick={x} title=""><Trash2 size={16} /></Button>' },
      { name: "aria-label={undefined} does not exempt", offender: true,
        src: '<Button onClick={x} aria-label={undefined}><Trash2 size={16} /></Button>' },
      { name: "live title still exempts", offender: false,
        src: '<Button onClick={x} title="Delete row"><Trash2 size={16} /></Button>' },
      { name: "live aria-label still exempts", offender: false,
        src: '<Button onClick={x} aria-label="Delete row"><Trash2 size={16} /></Button>' },
    ];
    for (const c of cases) {
      const blanked = blankComments(c.src);
      const blocks = findElementBlocks(c.src, blanked, "Button");
      expect(blocks.length, c.name).toBe(1);
      const block = blocks[0];
      if (hasLiveAttribute(block.openTag, "aria-label")) continue;
      if (hasLiveAttribute(block.openTag, "aria-labelledby")) continue;
      if (attrValueText(block.openTag, "aria-hidden") === "true") continue;
      const title = attrValueText(block.openTag, "title");
      if (title !== null && title.trim() !== "" && title !== "undefined") continue;
      const isOffender =
        rendersVisibleText(block.inner) === false && hasIconComponent(block.inner);
      expect(isOffender, c.name).toBe(c.offender);
    }
  });

  it("plain buttons cannot render as unnamed empty controls", () => {
    const offenders: Offender[] = [];
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      const blanked = blankComments(source);
      for (const block of findElementBlocks(source, blanked, "button")) {
        // The pinned anti-pattern: a conditional label whose fallback branch
        // is `undefined` (unnamed in that state) — the FolderTree
        // leaf-chevron shape at the pre-fix base. Buttons with a static label
        // (present or absent) are other classes' concern; this one exists so
        // the "label can silently vanish" regression cannot return.
        const attr = block.openTag.match(/aria-label=\{([^}]*)\}/);
        const labelUndefinedConditional =
          attr !== null && attr[1].includes(": undefined");
        if (!labelUndefinedConditional) continue;
        // Children can render nothing: an empty body, or a conditional whose
        // fallback branch is null.
        const rendersNothing =
          block.inner.trim() === "" || block.inner.includes(": null");
        if (!rendersNothing) continue;
        if (rendersVisibleText(block.inner)) continue;
        offenders.push(offender(file, block));
      }
    }
    expect(
      offenders,
      `plain <button> blocks that can render empty and unnamed ` +
        `(the FolderTree leaf-chevron class — render a non-interactive spacer instead): ${render(offenders)}`
    ).toEqual([]);
  });
});

function offender(file: string, block: ElementBlock): Offender {
  return {
    file,
    line: block.line,
    openTagExcerpt:
      block.openTag
        .replace(/\s+/g, " ")
        .slice(0, 90) + (block.openTag.length > 90 ? "…" : ""),
  };
}

function render(offenders: Offender[]): string {
  return offenders.map((o) => `${o.file}:${o.line} (${o.openTagExcerpt})`).join("; ");
}
