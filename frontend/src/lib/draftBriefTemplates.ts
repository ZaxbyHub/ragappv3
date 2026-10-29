import type { DraftBrief } from "@/lib/api/draftRoom";

/**
 * Brief templates for the Draft Room assignment form (issue #517 AC14).
 *
 * A template stores the BRIEF and nothing else — never evidence, fact-check,
 * approval, or readiness state (those are established per project, per run)
 * and never vault or title metadata (the form's own fields stay untouched
 * when a template is applied). Templates live in localStorage so they
 * survive navigation without a server round trip.
 *
 * Issue #1036 AC2: template text is user-authored content, so every key is
 * PRINCIPAL-SCOPED (`DRAFT_BRIEF_TEMPLATES_KEY_PREFIX` + principal id) and
 * the in-memory cache is per-principal — the identity-switch reset
 * (`resetChatStateForIdentitySwitch`) wipes every key under the prefix, and
 * any residue a degraded storage surface leaves behind is scoped to its own
 * principal, so the next principal can never read it. The legacy un-scoped
 * `DRAFT_BRIEF_TEMPLATES_STORAGE_KEY` namespace is NEVER read (its payload
 * is unowned — copying it would hand one principal's templates to another);
 * the boundary removes it best-effort and it is inert residue if removal
 * fails.
 */
export const DRAFT_BRIEF_TEMPLATES_KEY_PREFIX = "ragapp.v1.draft-brief-templates:";
/** Legacy pre-scoping namespace. Never read; removed best-effort at the identity boundary. */
export const DRAFT_BRIEF_TEMPLATES_STORAGE_KEY = "ragapp.v1.draft-brief-templates";

/** Keeps the stored list bounded; the most recent template stays last. */
const MAX_TEMPLATES = 10;

export interface DraftBriefTemplate {
  id: string;
  name: string;
  brief: DraftBrief;
}

export type PrincipalId = string | number;

/** Per-principal session caches of the stored lists; a principal is absent until its storage is read once. */
const sessionTemplates = new Map<PrincipalId, DraftBriefTemplate[]>();

function templateKey(principalId: PrincipalId): string {
  return `${DRAFT_BRIEF_TEMPLATES_KEY_PREFIX}${principalId}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isBrief(value: unknown): value is DraftBrief {
  if (!isRecord(value)) return false;
  return (
    typeof value.piece_type === "string" &&
    typeof value.audience === "string" &&
    typeof value.purpose === "string" &&
    typeof value.tone === "string" &&
    typeof value.target_words === "number" &&
    Array.isArray(value.must_include) &&
    Array.isArray(value.must_avoid)
  );
}

function isTemplate(value: unknown): value is DraftBriefTemplate {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.name === "string" &&
    isBrief(value.brief)
  );
}

function parseStoredTemplates(raw: string | null): DraftBriefTemplate[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  return parsed.filter(isTemplate);
}

/** Reads the principal's stored templates, newest last. Never throws. */
export function listDraftBriefTemplates(principalId: PrincipalId): DraftBriefTemplate[] {
  let cached = sessionTemplates.get(principalId);
  if (cached === undefined) {
    let stored: string | null = null;
    try {
      stored = window.localStorage.getItem(templateKey(principalId));
    } catch {
      // localStorage can be unavailable (private mode, quota, sandbox) —
      // degrade to "no templates" rather than breaking the form.
    }
    cached = parseStoredTemplates(stored);
    sessionTemplates.set(principalId, cached);
  }
  return cached;
}

function persist(
  principalId: PrincipalId,
  templates: DraftBriefTemplate[]
): DraftBriefTemplate[] {
  sessionTemplates.set(principalId, templates);
  try {
    window.localStorage.setItem(templateKey(principalId), JSON.stringify(templates));
  } catch {
    // Storage failed; the in-memory copy above still reflects the save.
  }
  return templates;
}

function generateTemplateId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `brief-template-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Saves a brief as a template under the given principal, replacing any
 * earlier template with the same name and moving it to the newest slot.
 * Returns the updated list (newest last) so callers can refresh their state
 * from the return value.
 */
export function saveDraftBriefTemplate(
  name: string,
  brief: DraftBrief,
  principalId: PrincipalId
): DraftBriefTemplate[] {
  const trimmedName = name.trim() || "Untitled brief";
  const withoutSameName = listDraftBriefTemplates(principalId).filter(
    (template) => template.name !== trimmedName
  );
  const template: DraftBriefTemplate = { id: generateTemplateId(), name: trimmedName, brief };
  return persist(principalId, [...withoutSameName, template].slice(-MAX_TEMPLATES));
}

/** The principal's most recently saved template, or null when none exists. */
export function latestDraftBriefTemplate(principalId: PrincipalId): DraftBriefTemplate | null {
  const templates = listDraftBriefTemplates(principalId);
  return templates.length > 0 ? templates[templates.length - 1] : null;
}

/**
 * Identity-boundary wipe (issue #1036 AC2): removes every principal-scoped
 * template key plus the legacy un-scoped namespace, and clears the
 * per-principal caches. Best-effort per key — a throwing `removeItem` must
 * not abort the remaining keys — mirroring clearAllChatDrafts. Returns the
 * number of failed removals, or -1 when key enumeration itself failed (the
 * wipe's completeness is then unknown).
 */
export function clearAllDraftBriefTemplates(): number {
  sessionTemplates.clear();
  let keys: string[];
  try {
    const all: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i);
      if (key !== null) all.push(key);
    }
    keys = all;
  } catch {
    return -1;
  }
  const doomed = keys.filter(
    (key) =>
      key.startsWith(DRAFT_BRIEF_TEMPLATES_KEY_PREFIX) ||
      key === DRAFT_BRIEF_TEMPLATES_STORAGE_KEY
  );
  let failures = 0;
  for (const key of doomed) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      failures += 1;
    }
  }
  return failures;
}

/**
 * Clears the session caches so the next `listDraftBriefTemplates` re-reads
 * storage. Exists purely for test isolation — within a session the
 * in-memory copy is authoritative by design, so production code never needs
 * this.
 */
export function resetDraftBriefTemplatesCacheForTests(): void {
  sessionTemplates.clear();
}
