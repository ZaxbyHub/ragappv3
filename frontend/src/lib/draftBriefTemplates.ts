import type { DraftBrief } from "@/lib/api/draftRoom";

/**
 * Brief templates for the Draft Room assignment form (issue #517 AC14).
 *
 * A template stores the BRIEF and nothing else — never evidence, fact-check,
 * approval, or readiness state (those are established per project, per run)
 * and never vault or title metadata (the form's own fields stay untouched
 * when a template is applied). Templates live in localStorage under a
 * namespaced key so they survive navigation without a server round trip.
 */
export const DRAFT_BRIEF_TEMPLATES_STORAGE_KEY = "ragapp.v1.draft-brief-templates";

/** Keeps the stored list bounded; the most recent template stays last. */
const MAX_TEMPLATES = 10;

export interface DraftBriefTemplate {
  id: string;
  name: string;
  brief: DraftBrief;
}

/** Session cache of the stored list; null until storage is read once. */
let sessionTemplates: DraftBriefTemplate[] | null = null;

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

/** Reads the stored templates, newest last. Never throws. */
export function listDraftBriefTemplates(): DraftBriefTemplate[] {
  // Session-level cache: read storage once, then serve from memory. Storage
  // can also be a silent no-op (private mode, embedded webviews, stubbed test
  // environments), so the in-memory copy — not the storage write — is what
  // keeps save-then-apply working within a session.
  if (sessionTemplates == null) {
    let stored: string | null = null;
    try {
      stored = window.localStorage.getItem(DRAFT_BRIEF_TEMPLATES_STORAGE_KEY);
    } catch {
      // localStorage can be unavailable (private mode, quota, sandbox) —
      // degrade to "no templates" rather than breaking the form.
    }
    sessionTemplates = parseStoredTemplates(stored);
  }
  return sessionTemplates;
}

function persist(templates: DraftBriefTemplate[]): DraftBriefTemplate[] {
  sessionTemplates = templates;
  try {
    window.localStorage.setItem(DRAFT_BRIEF_TEMPLATES_STORAGE_KEY, JSON.stringify(templates));
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
 * Saves a brief as a template, replacing any earlier template with the same
 * name and moving it to the newest slot. Returns the updated list (newest
 * last) so callers can refresh their state from the return value.
 */
export function saveDraftBriefTemplate(name: string, brief: DraftBrief): DraftBriefTemplate[] {
  const trimmedName = name.trim() || "Untitled brief";
  const withoutSameName = listDraftBriefTemplates().filter(
    (template) => template.name !== trimmedName
  );
  const template: DraftBriefTemplate = { id: generateTemplateId(), name: trimmedName, brief };
  return persist([...withoutSameName, template].slice(-MAX_TEMPLATES));
}

/** The most recently saved template, or null when none exists. */
export function latestDraftBriefTemplate(): DraftBriefTemplate | null {
  const templates = listDraftBriefTemplates();
  return templates.length > 0 ? templates[templates.length - 1] : null;
}
