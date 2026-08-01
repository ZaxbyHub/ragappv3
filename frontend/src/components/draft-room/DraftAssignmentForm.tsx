import { useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listAccessibleVaults } from "@/lib/api";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { labelVariants } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import {
  DRAFT_MODES,
  DRAFT_TIERS,
  type DraftBrief,
  type DraftInput,
  type DraftMode,
  type DraftRoomCapabilities,
  type DraftTier,
} from "@/lib/api/draftRoom";
import {
  DRAFTING_PRIORITY_LABELS,
  INPUT_ROLE_LABELS,
  MODE_DESCRIPTIONS,
  MODE_LABELS,
  PIECE_TYPE_LABELS,
  TIER_DESCRIPTIONS,
  TIER_LABELS,
  TRANSFORMATION_STRENGTH_LABELS,
} from "@/components/draft-room/labels";

// The backend's `_PIECE_TYPES` / `_TRANSFORMATION_STRENGTHS` tuples
// (backend/app/api/routes/draft_room.py) are also exposed live via
// `DraftRoomCapabilities.piece_types` / `.transformation_strengths` — prefer
// those when available and fall back to these mirrors only when `capabilities`
// hasn't loaded yet. `drafting_priority` has no capabilities field at all, so
// this list is the only source of truth on the client. Display text for all
// three enums comes from labels.ts's `*_LABELS` maps.
const FALLBACK_PIECE_TYPES = ["article", "report", "brief", "press_release", "other"];
const FALLBACK_TRANSFORMATION_STRENGTHS = ["light", "moderate", "substantial"];
const DRAFTING_PRIORITIES = ["manuscript", "vault", "balanced"];

const MAX_LIST_ITEMS = 50;
const MAX_LIST_ITEM_LENGTH = 500;

/** Splits one-per-line textarea text into a trimmed, non-empty, capped string[]. */
function linesToList(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .slice(0, MAX_LIST_ITEMS);
}

function countNonBlankLines(text: string): number {
  return text.split("\n").filter((line) => line.trim().length > 0).length;
}

export interface DraftAssignmentFormValue {
  title: string;
  vault_id: number | null;
  mode: DraftMode;
  tier: DraftTier;
  brief: DraftBrief;
}

export interface DraftAssignmentFormProps {
  value: DraftAssignmentFormValue;
  onChange(next: DraftAssignmentFormValue): void;
  /** Field-name -> message. Keys are dotted for brief fields, e.g. "brief.audience". */
  errors?: Record<string, string>;
  /** Hide title/vault/mode when the form is reused to edit an existing project's brief. */
  variant?: "create" | "edit";
  disabled?: boolean;
  idPrefix?: string;
  capabilities?: DraftRoomCapabilities;
  /** Available only in the edit variant, after uploads exist. Powers the "Primary source" picker. */
  inputs?: DraftInput[];
}

/** Ordered field keys this form renders, for callers that need to focus the first invalid one. */
export const DRAFT_ASSIGNMENT_FIELD_ORDER = [
  "title",
  "vault_id",
  "mode",
  "brief.piece_type",
  "brief.audience",
  "brief.purpose",
  "brief.tone",
  "brief.target_words",
  "brief.transformation_strength",
  "brief.drafting_priority",
  "brief.must_include",
  "brief.must_avoid",
  "brief.additional_instructions",
] as const;
export type DraftAssignmentFieldKey = (typeof DRAFT_ASSIGNMENT_FIELD_ORDER)[number];

export function draftAssignmentFieldId(idPrefix: string, field: string): string {
  return `${idPrefix}-${field.replace(".", "-")}`;
}

/**
 * Focuses the first field named in `errors`, in `DRAFT_ASSIGNMENT_FIELD_ORDER`.
 * Every field this form renders wraps its control in `[data-field="<key>"]`, so
 * this works uniformly across inputs, radio groups, and selects.
 */
export function focusFirstInvalidDraftAssignmentField(
  container: HTMLElement | null,
  errors: Record<string, string>
): void {
  if (!container) return;
  for (const key of DRAFT_ASSIGNMENT_FIELD_ORDER) {
    if (!(key in errors)) continue;
    const scope = container.querySelector<HTMLElement>(`[data-field="${key}"]`);
    if (!scope) continue;
    const target = scope.matches("input,textarea,select,button")
      ? scope
      : scope.querySelector<HTMLElement>("input,textarea,select,button,[tabindex]");
    target?.focus();
    return;
  }
}

function defaultDraftBrief(): DraftBrief {
  return {
    piece_type: FALLBACK_PIECE_TYPES[0],
    audience: "",
    purpose: "",
    tone: "clear and direct",
    target_words: 800,
    transformation_strength: FALLBACK_TRANSFORMATION_STRENGTHS[1],
    primary_input_id: null,
    must_include: [],
    must_avoid: [],
    preserve_quotes: true,
    preserve_numbers: true,
    preserve_uncertainty: true,
    drafting_priority: "balanced",
    additional_instructions: "",
  };
}

/** Sensible starting value for a brand-new drafting project. */
export function createDefaultDraftAssignmentFormValue(
  defaultVaultId?: number | null
): DraftAssignmentFormValue {
  return {
    title: "",
    vault_id: defaultVaultId ?? null,
    mode: "compose",
    tier: "standard",
    brief: defaultDraftBrief(),
  };
}

/**
 * Client-side mirror of `DraftBrief` / `DraftCreateRequest` validation in
 * backend/app/api/routes/draft_room.py (~L330-391). Keep the numeric bounds in
 * sync if the backend contract changes.
 */
export function validateDraftAssignmentForm(
  value: DraftAssignmentFormValue,
  variant: "create" | "edit" = "create"
): Record<string, string> {
  const errors: Record<string, string> = {};

  if (variant !== "edit") {
    const title = value.title.trim();
    if (title.length < 1 || title.length > 300) {
      errors.title = "Title must be 1-300 characters.";
    }
    if (value.vault_id == null) {
      errors.vault_id = "Choose a vault.";
    }
    if (!DRAFT_MODES.includes(value.mode)) {
      errors.mode = "Choose a project mode.";
    }
  }

  const { brief } = value;

  if (brief.audience.trim().length < 1 || brief.audience.length > 500) {
    errors["brief.audience"] = "Audience must be 1-500 characters.";
  }
  if (brief.purpose.trim().length < 1 || brief.purpose.length > 1000) {
    errors["brief.purpose"] = "Purpose must be 1-1000 characters.";
  }
  if (brief.tone.trim().length < 1 || brief.tone.length > 500) {
    errors["brief.tone"] = "Tone must be 1-500 characters.";
  }
  if (!Number.isFinite(brief.target_words) || brief.target_words < 100 || brief.target_words > 20000) {
    errors["brief.target_words"] = "Target length must be between 100 and 20,000 words.";
  }
  if (brief.must_include.length > MAX_LIST_ITEMS || brief.must_include.some((item) => item.length < 1 || item.length > MAX_LIST_ITEM_LENGTH)) {
    errors["brief.must_include"] = `Each line must be 1-${MAX_LIST_ITEM_LENGTH} characters, up to ${MAX_LIST_ITEMS} lines.`;
  }
  if (brief.must_avoid.length > MAX_LIST_ITEMS || brief.must_avoid.some((item) => item.length < 1 || item.length > MAX_LIST_ITEM_LENGTH)) {
    errors["brief.must_avoid"] = `Each line must be 1-${MAX_LIST_ITEM_LENGTH} characters, up to ${MAX_LIST_ITEMS} lines.`;
  }
  if (brief.additional_instructions.length > 4000) {
    errors["brief.additional_instructions"] = "Additional instructions must be 4000 characters or fewer.";
  }

  return errors;
}

function FieldError({ id, message }: { id: string; message?: string }): JSX.Element | null {
  if (!message) return null;
  return (
    <p id={id} className="mt-1 text-sm text-destructive">
      {message}
    </p>
  );
}

function FieldHelp({ id, children }: { id: string; children: React.ReactNode }): JSX.Element {
  return (
    <p id={id} className="mt-1 text-sm text-muted-foreground">
      {children}
    </p>
  );
}

function describedBy(...ids: Array<string | undefined | false>): string | undefined {
  const joined = ids.filter((id): id is string => Boolean(id)).join(" ");
  return joined.length > 0 ? joined : undefined;
}

export function DraftAssignmentForm(props: DraftAssignmentFormProps): JSX.Element {
  const { value, onChange, errors = {}, variant = "create", disabled = false, capabilities, inputs } = props;
  const autoId = useId();
  const idPrefix = props.idPrefix ?? autoId;
  const isEdit = variant === "edit";

  const pieceTypes = capabilities?.piece_types && capabilities.piece_types.length > 0
    ? capabilities.piece_types
    : FALLBACK_PIECE_TYPES;
  const transformationStrengths =
    capabilities?.transformation_strengths && capabilities.transformation_strengths.length > 0
      ? capabilities.transformation_strengths
      : FALLBACK_TRANSFORMATION_STRENGTHS;
  const modes = capabilities?.modes && capabilities.modes.length > 0 ? capabilities.modes : DRAFT_MODES;
  const tiers = capabilities?.tiers && capabilities.tiers.length > 0 ? capabilities.tiers : DRAFT_TIERS;

  const vaultsQuery = useQuery({
    queryKey: ["vaults", "accessible"],
    queryFn: () => listAccessibleVaults(),
    enabled: !isEdit,
  });
  const vaults = vaultsQuery.data?.vaults ?? [];

  const [mustIncludeText, setMustIncludeText] = useState(() => value.brief.must_include.join("\n"));
  const [mustAvoidText, setMustAvoidText] = useState(() => value.brief.must_avoid.join("\n"));

  function updateBrief(partial: Partial<DraftBrief>): void {
    onChange({ ...value, brief: { ...value.brief, ...partial } });
  }

  const mustIncludeCount = useMemo(() => countNonBlankLines(mustIncludeText), [mustIncludeText]);
  const mustAvoidCount = useMemo(() => countNonBlankLines(mustAvoidText), [mustAvoidText]);

  const titleId = draftAssignmentFieldId(idPrefix, "title");
  const vaultId = draftAssignmentFieldId(idPrefix, "vault_id");
  const modeLabelId = draftAssignmentFieldId(idPrefix, "mode-label");
  const tierId = draftAssignmentFieldId(idPrefix, "tier");
  const primaryInputId = draftAssignmentFieldId(idPrefix, "brief-primary_input_id");
  const pieceTypeId = draftAssignmentFieldId(idPrefix, "brief-piece_type");
  const audienceId = draftAssignmentFieldId(idPrefix, "brief-audience");
  const purposeId = draftAssignmentFieldId(idPrefix, "brief-purpose");
  const toneId = draftAssignmentFieldId(idPrefix, "brief-tone");
  const targetWordsId = draftAssignmentFieldId(idPrefix, "brief-target_words");
  const transformationStrengthId = draftAssignmentFieldId(idPrefix, "brief-transformation_strength");
  const draftingPriorityId = draftAssignmentFieldId(idPrefix, "brief-drafting_priority");
  const mustIncludeId = draftAssignmentFieldId(idPrefix, "brief-must_include");
  const mustAvoidId = draftAssignmentFieldId(idPrefix, "brief-must_avoid");
  const additionalInstructionsId = draftAssignmentFieldId(idPrefix, "brief-additional_instructions");

  return (
    <div className="space-y-6">
      {!isEdit && (
        <>
          <div data-field="title">
            <Label htmlFor={titleId}>Project title</Label>
            <Input
              id={titleId}
              value={value.title}
              disabled={disabled}
              aria-invalid={Boolean(errors.title)}
              aria-describedby={describedBy(errors.title && `${titleId}-error`)}
              onChange={(e) => onChange({ ...value, title: e.target.value })}
              maxLength={300}
            />
            <FieldError id={`${titleId}-error`} message={errors.title} />
          </div>

          <div data-field="vault_id">
            <Label htmlFor={vaultId}>Vault</Label>
            {vaultsQuery.isLoading ? (
              <p className="text-sm text-muted-foreground">Loading vaults…</p>
            ) : vaultsQuery.isError ? (
              <Alert variant="destructive">
                <AlertDescription>Could not load your vaults. Try again.</AlertDescription>
              </Alert>
            ) : vaults.length === 0 ? (
              <Alert variant="warning">
                <AlertDescription>
                  You don&apos;t have read access to any vault yet. Ask an admin to grant you vault
                  access before creating a drafting project.
                </AlertDescription>
              </Alert>
            ) : (
              <>
                <Select
                  value={value.vault_id != null ? String(value.vault_id) : undefined}
                  onValueChange={(v) => onChange({ ...value, vault_id: Number(v) })}
                  disabled={disabled}
                >
                  <SelectTrigger
                    id={vaultId}
                    aria-invalid={Boolean(errors.vault_id)}
                    aria-describedby={describedBy(errors.vault_id && `${vaultId}-error`)}
                  >
                    <SelectValue placeholder="Choose a vault" />
                  </SelectTrigger>
                  <SelectContent>
                    {vaults.map((vault) => (
                      <SelectItem key={vault.id} value={String(vault.id)}>
                        {vault.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <FieldError id={`${vaultId}-error`} message={errors.vault_id} />
              </>
            )}
          </div>

          <div data-field="mode">
            <span id={modeLabelId} className={cn(labelVariants(), "block")}>
              Project mode
            </span>
            <RadioGroup
              orientation="vertical"
              value={value.mode}
              onValueChange={(v) => onChange({ ...value, mode: v as DraftMode })}
              disabled={disabled}
              aria-labelledby={modeLabelId}
              aria-invalid={Boolean(errors.mode)}
              aria-describedby={describedBy(errors.mode && `${idPrefix}-mode-error`)}
              className="gap-3"
            >
              {modes.map((mode) => {
                const optionId = `${idPrefix}-mode-${mode}`;
                return (
                  <div key={mode} className="flex items-start gap-2">
                    <RadioGroupItem id={optionId} value={mode} className="mt-1" />
                    <label htmlFor={optionId} className="cursor-pointer">
                      <span className="block text-sm font-medium">{MODE_LABELS[mode] ?? mode}</span>
                      <span className="block text-sm text-muted-foreground">
                        {MODE_DESCRIPTIONS[mode] ?? ""}
                      </span>
                    </label>
                  </div>
                );
              })}
            </RadioGroup>
            <FieldError id={`${idPrefix}-mode-error`} message={errors.mode} />
          </div>
        </>
      )}

      {isEdit && inputs && inputs.length > 0 && (
        <div data-field="brief.primary_input_id">
          <Label htmlFor={primaryInputId}>Primary source</Label>
          <Select
            value={value.brief.primary_input_id != null ? String(value.brief.primary_input_id) : "none"}
            onValueChange={(v) => updateBrief({ primary_input_id: v === "none" ? null : Number(v) })}
            disabled={disabled}
          >
            <SelectTrigger id={primaryInputId}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">None</SelectItem>
              {inputs.map((input) => (
                <SelectItem key={input.id} value={String(input.id)}>
                  {input.original_name} ({INPUT_ROLE_LABELS[input.role] ?? input.role})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}

      <div data-field="brief.piece_type">
        <Label htmlFor={pieceTypeId}>Piece type</Label>
        <Select
          value={value.brief.piece_type}
          onValueChange={(v) => updateBrief({ piece_type: v })}
          disabled={disabled}
        >
          <SelectTrigger
            id={pieceTypeId}
            aria-invalid={Boolean(errors["brief.piece_type"])}
            aria-describedby={describedBy(errors["brief.piece_type"] && `${pieceTypeId}-error`)}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {pieceTypes.map((type) => (
              <SelectItem key={type} value={type}>
                {PIECE_TYPE_LABELS[type] ?? type}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FieldError id={`${pieceTypeId}-error`} message={errors["brief.piece_type"]} />
      </div>

      <div data-field="brief.audience">
        <Label htmlFor={audienceId}>Audience</Label>
        <Textarea
          id={audienceId}
          value={value.brief.audience}
          disabled={disabled}
          maxLength={500}
          aria-invalid={Boolean(errors["brief.audience"])}
          aria-describedby={describedBy(errors["brief.audience"] && `${audienceId}-error`)}
          onChange={(e) => updateBrief({ audience: e.target.value })}
        />
        <FieldError id={`${audienceId}-error`} message={errors["brief.audience"]} />
      </div>

      <div data-field="brief.purpose">
        <Label htmlFor={purposeId}>Purpose</Label>
        <Textarea
          id={purposeId}
          value={value.brief.purpose}
          disabled={disabled}
          maxLength={1000}
          aria-invalid={Boolean(errors["brief.purpose"])}
          aria-describedby={describedBy(errors["brief.purpose"] && `${purposeId}-error`)}
          onChange={(e) => updateBrief({ purpose: e.target.value })}
        />
        <FieldError id={`${purposeId}-error`} message={errors["brief.purpose"]} />
      </div>

      <div data-field="brief.tone">
        <Label htmlFor={toneId}>Tone</Label>
        <Input
          id={toneId}
          value={value.brief.tone}
          disabled={disabled}
          maxLength={500}
          aria-invalid={Boolean(errors["brief.tone"])}
          aria-describedby={describedBy(errors["brief.tone"] && `${toneId}-error`)}
          onChange={(e) => updateBrief({ tone: e.target.value })}
        />
        <FieldError id={`${toneId}-error`} message={errors["brief.tone"]} />
      </div>

      <div data-field="brief.target_words">
        <Label htmlFor={targetWordsId}>Target length (words)</Label>
        <Input
          id={targetWordsId}
          type="number"
          min={100}
          max={20000}
          value={value.brief.target_words}
          disabled={disabled}
          aria-invalid={Boolean(errors["brief.target_words"])}
          aria-describedby={describedBy(errors["brief.target_words"] && `${targetWordsId}-error`)}
          onChange={(e) => updateBrief({ target_words: e.target.value === "" ? 0 : Number(e.target.value) })}
        />
        <FieldError id={`${targetWordsId}-error`} message={errors["brief.target_words"]} />
      </div>

      <div data-field="brief.transformation_strength">
        <Label htmlFor={transformationStrengthId}>Transformation strength</Label>
        <Select
          value={value.brief.transformation_strength}
          onValueChange={(v) => updateBrief({ transformation_strength: v })}
          disabled={disabled}
        >
          <SelectTrigger
            id={transformationStrengthId}
            aria-invalid={Boolean(errors["brief.transformation_strength"])}
            aria-describedby={describedBy(
              errors["brief.transformation_strength"] && `${transformationStrengthId}-error`
            )}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {transformationStrengths.map((strength) => (
              <SelectItem key={strength} value={strength}>
                {TRANSFORMATION_STRENGTH_LABELS[strength] ?? strength}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FieldError
          id={`${transformationStrengthId}-error`}
          message={errors["brief.transformation_strength"]}
        />
      </div>

      <div data-field="brief.drafting_priority">
        <Label htmlFor={draftingPriorityId}>Drafting priority</Label>
        <Select
          value={value.brief.drafting_priority}
          onValueChange={(v) => updateBrief({ drafting_priority: v })}
          disabled={disabled}
        >
          <SelectTrigger
            id={draftingPriorityId}
            aria-invalid={Boolean(errors["brief.drafting_priority"])}
            aria-describedby={describedBy(
              errors["brief.drafting_priority"] && `${draftingPriorityId}-error`
            )}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {DRAFTING_PRIORITIES.map((priority) => (
              <SelectItem key={priority} value={priority}>
                {DRAFTING_PRIORITY_LABELS[priority] ?? priority}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FieldError id={`${draftingPriorityId}-error`} message={errors["brief.drafting_priority"]} />
      </div>

      <div data-field="brief.must_include">
        <Label htmlFor={mustIncludeId}>Must include (one per line)</Label>
        <Textarea
          id={mustIncludeId}
          value={mustIncludeText}
          disabled={disabled}
          rows={4}
          aria-invalid={Boolean(errors["brief.must_include"])}
          aria-describedby={describedBy(
            `${mustIncludeId}-count`,
            errors["brief.must_include"] && `${mustIncludeId}-error`
          )}
          onChange={(e) => {
            const text = e.target.value;
            setMustIncludeText(text);
            updateBrief({ must_include: linesToList(text) });
          }}
        />
        <FieldHelp id={`${mustIncludeId}-count`}>
          {mustIncludeCount}/{MAX_LIST_ITEMS} items
          {mustIncludeCount > MAX_LIST_ITEMS ? ` — only the first ${MAX_LIST_ITEMS} are kept` : ""}
        </FieldHelp>
        <FieldError id={`${mustIncludeId}-error`} message={errors["brief.must_include"]} />
      </div>

      <div data-field="brief.must_avoid">
        <Label htmlFor={mustAvoidId}>Must avoid (one per line)</Label>
        <Textarea
          id={mustAvoidId}
          value={mustAvoidText}
          disabled={disabled}
          rows={4}
          aria-invalid={Boolean(errors["brief.must_avoid"])}
          aria-describedby={describedBy(
            `${mustAvoidId}-count`,
            errors["brief.must_avoid"] && `${mustAvoidId}-error`
          )}
          onChange={(e) => {
            const text = e.target.value;
            setMustAvoidText(text);
            updateBrief({ must_avoid: linesToList(text) });
          }}
        />
        <FieldHelp id={`${mustAvoidId}-count`}>
          {mustAvoidCount}/{MAX_LIST_ITEMS} items
          {mustAvoidCount > MAX_LIST_ITEMS ? ` — only the first ${MAX_LIST_ITEMS} are kept` : ""}
        </FieldHelp>
        <FieldError id={`${mustAvoidId}-error`} message={errors["brief.must_avoid"]} />
      </div>

      <fieldset className="space-y-2" disabled={disabled}>
        <legend className={cn(labelVariants(), "mb-1")}>Preserve</legend>
        <div className="flex items-center gap-2">
          <Checkbox
            id={`${idPrefix}-preserve_quotes`}
            checked={value.brief.preserve_quotes}
            onCheckedChange={(checked) => updateBrief({ preserve_quotes: checked === true })}
          />
          <Label htmlFor={`${idPrefix}-preserve_quotes`} className="mb-0">
            Preserve quotes
          </Label>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id={`${idPrefix}-preserve_numbers`}
            checked={value.brief.preserve_numbers}
            onCheckedChange={(checked) => updateBrief({ preserve_numbers: checked === true })}
          />
          <Label htmlFor={`${idPrefix}-preserve_numbers`} className="mb-0">
            Preserve numbers
          </Label>
        </div>
        <div className="flex items-center gap-2">
          <Checkbox
            id={`${idPrefix}-preserve_uncertainty`}
            checked={value.brief.preserve_uncertainty}
            onCheckedChange={(checked) => updateBrief({ preserve_uncertainty: checked === true })}
          />
          <Label htmlFor={`${idPrefix}-preserve_uncertainty`} className="mb-0">
            Preserve uncertainty
          </Label>
        </div>
      </fieldset>

      <div data-field="brief.additional_instructions">
        <Label htmlFor={additionalInstructionsId}>Additional instructions</Label>
        <Textarea
          id={additionalInstructionsId}
          value={value.brief.additional_instructions}
          disabled={disabled}
          rows={3}
          maxLength={4000}
          aria-invalid={Boolean(errors["brief.additional_instructions"])}
          aria-describedby={describedBy(
            errors["brief.additional_instructions"] && `${additionalInstructionsId}-error`
          )}
          onChange={(e) => updateBrief({ additional_instructions: e.target.value })}
        />
        <FieldError
          id={`${additionalInstructionsId}-error`}
          message={errors["brief.additional_instructions"]}
        />
      </div>

      <div data-field="tier">
        <Label htmlFor={tierId}>Quality tier</Label>
        <Select
          value={value.tier}
          onValueChange={(v) => onChange({ ...value, tier: v as DraftTier })}
          disabled={disabled}
        >
          <SelectTrigger id={tierId} aria-describedby={`${tierId}-help`}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {tiers.map((tier) => (
              <SelectItem key={tier} value={tier}>
                {TIER_LABELS[tier] ?? tier}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FieldHelp id={`${tierId}-help`}>{TIER_DESCRIPTIONS[value.tier] ?? ""}</FieldHelp>
      </div>
    </div>
  );
}
