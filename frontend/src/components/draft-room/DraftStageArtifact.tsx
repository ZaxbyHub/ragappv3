import { Fragment } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { type DraftStage, type DraftStageName } from "@/lib/api/draftRoom";
import { FACT_STATUS_LABELS, STAGE_LABELS } from "@/components/draft-room/labels";

export interface DraftStageArtifactProps {
  stage: DraftStage | null;
}

const STAGE_STATUS_LABEL: Record<string, string> = {
  pending: "Pending",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  skipped: "Skipped",
  cancelled: "Cancelled",
};

// ---------------------------------------------------------------------------
// Defensive, non-throwing readers. Backend artifacts are validated Pydantic
// models on the way in, but this component treats every field as `unknown`
// so a shape drift or a hand-crafted/replayed payload degrades to a safe
// placeholder per field instead of throwing or rendering "[object Object]".
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown, fallback = "—"): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function strArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function safeJsonStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "Unable to render this artifact as JSON.";
  }
}

function JsonDisclosure({ artifact }: { artifact: unknown }) {
  return (
    <details className="mt-3 text-xs">
      <summary className="cursor-pointer text-muted-foreground">View JSON</summary>
      <pre className="mt-1 max-h-80 overflow-x-auto overflow-y-auto rounded-sm bg-muted p-2">
        {safeJsonStringify(artifact)}
      </pre>
    </details>
  );
}

function UnknownArtifact({ artifact }: { artifact: unknown }) {
  return (
    <div className="space-y-2">
      <p className="text-sm text-muted-foreground">This stage returned an unrecognised artifact.</p>
      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

// -- Intake -----------------------------------------------------------------

function IntakeArtifactView({ artifact }: { artifact: unknown }) {
  if (!isRecord(artifact) || typeof artifact.brief_hash !== "string" || !Array.isArray(artifact.inputs)) {
    return <UnknownArtifact artifact={artifact} />;
  }
  const inputs = recordArray(artifact.inputs);
  const warnings = strArray(artifact.warnings);

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        Brief hash: <span className="font-mono">{str(artifact.brief_hash)}</span>
      </p>
      <ul className="space-y-1">
        {inputs.map((input, index) => (
          <li key={index} className="rounded-sm border border-border p-2 text-xs">
            Input #{num(input.input_id)} · {str(input.role)} · {num(input.character_count)} characters
          </li>
        ))}
      </ul>
      {warnings.length > 0 && (
        <ul className="list-disc space-y-1 pl-4 text-xs text-warning">
          {warnings.map((warning, index) => (
            <li key={index}>{warning}</li>
          ))}
        </ul>
      )}
      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

// -- Research -----------------------------------------------------------------

function ResearchArtifactView({ artifact }: { artifact: unknown }) {
  if (
    !isRecord(artifact) ||
    !Array.isArray(artifact.facets) ||
    !Array.isArray(artifact.evidence) ||
    typeof artifact.retrieval_status !== "string" ||
    !Array.isArray(artifact.contradictions) ||
    !Array.isArray(artifact.gaps)
  ) {
    return <UnknownArtifact artifact={artifact} />;
  }

  const facets = recordArray(artifact.facets);
  const evidence = recordArray(artifact.evidence);
  const contradictions = recordArray(artifact.contradictions);
  const gaps = recordArray(artifact.gaps);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">Retrieval: {str(artifact.retrieval_status)}</Badge>
        {artifact.source_only === true && <Badge variant="outline">Source-only run</Badge>}
      </div>

      {facets.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold text-foreground">Research facets</h4>
          <ul className="space-y-1">
            {facets.map((facet, index) => (
              <li key={index} className="rounded-sm border border-border p-2 text-xs">
                <p className="font-medium">{str(facet.query)}</p>
                <p className="text-muted-foreground">{str(facet.rationale)}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {evidence.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold text-foreground">Evidence collected</h4>
          <ul className="space-y-1">
            {evidence.map((item, index) => (
              <li key={index} className="rounded-sm border border-border p-2 text-xs">
                <p className="font-medium">
                  [{str(item.label)}] {str(item.title)}
                </p>
                <p className="overflow-x-auto font-mono text-muted-foreground">{str(item.passage)}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {contradictions.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold text-foreground">Contradictions</h4>
          <ul className="space-y-1">
            {contradictions.map((item, index) => (
              <li key={index} className="rounded-sm border border-warning/50 bg-warning/10 p-2 text-xs">
                <p className="font-medium">{str(item.proposition)}</p>
                <p className="text-muted-foreground">
                  [{str(item.evidence_label_a)}] vs [{str(item.evidence_label_b)}]: {str(item.explanation)}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {gaps.length > 0 && (
        <div>
          <h4 className="mb-1 text-xs font-semibold text-foreground">Gaps</h4>
          <ul className="space-y-1">
            {gaps.map((item, index) => (
              <li key={index} className="rounded-sm border border-border p-2 text-xs">
                <p>{str(item.description)}</p>
                <p className="text-muted-foreground">{str(item.impact)}</p>
                {item.blocks_drafting === true && <Badge variant="outline">Blocks drafting</Badge>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

// -- Outline -----------------------------------------------------------------

function OutlineArtifactView({ artifact }: { artifact: unknown }) {
  if (!isRecord(artifact) || typeof artifact.mode !== "string" || !Array.isArray(artifact.sections) || !isRecord(artifact.critic)) {
    return <UnknownArtifact artifact={artifact} />;
  }
  const sections = recordArray(artifact.sections);
  const critic = artifact.critic;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline">Mode: {str(artifact.mode)}</Badge>
        <Badge variant="outline">Critic verdict: {str(critic.verdict)}</Badge>
      </div>
      {strArray(critic.findings).length > 0 && (
        <ul className="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
          {strArray(critic.findings).map((finding, index) => (
            <li key={index}>{finding}</li>
          ))}
        </ul>
      )}

      <ol className="space-y-2">
        {sections.map((section, index) => (
          <li key={index} className="rounded-sm border border-border p-2 text-xs">
            <p className="font-medium text-foreground">
              {index + 1}. {str(section.heading)} ({num(section.target_words)} words)
            </p>
            <p className="text-muted-foreground">{str(section.purpose)}</p>
            {strArray(section.evidence_labels).length > 0 && (
              <p className="mt-1">
                <span className="font-medium">Mapped evidence: </span>
                {strArray(section.evidence_labels).join(", ")}
              </p>
            )}
            {strArray(section.must_preserve).length > 0 && (
              <p className="mt-1">
                <span className="font-medium">Must-keep facts: </span>
                {strArray(section.must_preserve).join("; ")}
              </p>
            )}
          </li>
        ))}
      </ol>

      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

// -- Draft -----------------------------------------------------------------

function DraftArtifactView({ artifact }: { artifact: unknown }) {
  if (!isRecord(artifact) || !Array.isArray(artifact.sections)) {
    return <UnknownArtifact artifact={artifact} />;
  }
  const sections = recordArray(artifact.sections);

  return (
    <div className="space-y-3">
      <ul className="space-y-2">
        {sections.map((section, index) => {
          const audit = isRecord(section.model_call_audit) ? section.model_call_audit : {};
          const preservedResults = recordArray(section.preserved_span_results);
          const preservedCount = preservedResults.filter((result) => result.preserved === true).length;
          return (
            <li key={index} className="rounded-sm border border-border p-2 text-xs">
              <p className="font-medium text-foreground">Section {str(section.section_id)}</p>
              <p className="text-muted-foreground">{str(section.markdown, "").length} characters drafted</p>
              {strArray(section.evidence_labels_used).length > 0 && (
                <p>Evidence used: {strArray(section.evidence_labels_used).join(", ")}</p>
              )}
              {preservedResults.length > 0 && (
                <p>
                  Preserved spans: {preservedCount}/{preservedResults.length}
                </p>
              )}
              <p className="text-muted-foreground">
                Model: {str(audit.model)} (temperature {num(audit.temperature)})
              </p>
            </li>
          );
        })}
      </ul>
      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

// -- Lint -----------------------------------------------------------------

function LintArtifactView({ artifact }: { artifact: unknown }) {
  if (!isRecord(artifact) || typeof artifact.rule_version !== "string" || !Array.isArray(artifact.findings)) {
    return <UnknownArtifact artifact={artifact} />;
  }
  const findings = recordArray(artifact.findings);

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">Rule version: {str(artifact.rule_version)}</p>
      {findings.length === 0 ? (
        <p className="text-sm text-muted-foreground">No lint findings.</p>
      ) : (
        <ul className="space-y-2">
          {findings.map((finding, index) => (
            <li key={index} className="rounded-sm border border-border p-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline">{str(finding.severity)}</Badge>
                <Badge variant="secondary">{str(finding.disposition)}</Badge>
                <span className="text-muted-foreground">
                  {str(finding.rule_id)} · {str(finding.section_id)}
                </span>
              </div>
              <p className="mt-1">{str(finding.message)}</p>
              <p className="overflow-x-auto font-mono text-muted-foreground">{str(finding.excerpt)}</p>
            </li>
          ))}
        </ul>
      )}
      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

// -- Copy / Standards (share the same edit-record shape) --------------------

function CopyStandardsArtifactView({ artifact }: { artifact: unknown }) {
  if (!isRecord(artifact) || !Array.isArray(artifact.edits) || !Array.isArray(artifact.findings)) {
    return <UnknownArtifact artifact={artifact} />;
  }
  const edits = recordArray(artifact.edits);
  const findings = strArray(artifact.findings);

  return (
    <div className="space-y-3">
      {edits.length === 0 ? (
        <p className="text-sm text-muted-foreground">No edits were made.</p>
      ) : (
        <ul className="space-y-2">
          {edits.map((edit, index) => (
            <li key={index} className="rounded-sm border border-border p-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline">{str(edit.category)}</Badge>
                <Badge
                  variant="outline"
                  className={
                    edit.semantic_change === true
                      ? "border-warning/50 bg-warning/10 text-warning"
                      : "border-success/50 bg-success/10 text-success"
                  }
                >
                  {edit.semantic_change === true ? "Semantic change" : "No semantic change"}
                </Badge>
                <span className="text-muted-foreground">{str(edit.section_id)}</span>
              </div>
              <p className="mt-1 text-muted-foreground">{str(edit.rationale)}</p>
              <p className="overflow-x-auto font-mono">
                <span className="text-muted-foreground">Before: </span>
                {str(edit.before_excerpt)}
              </p>
              <p className="overflow-x-auto font-mono">
                <span className="text-muted-foreground">After: </span>
                {str(edit.after_excerpt)}
              </p>
            </li>
          ))}
        </ul>
      )}
      {findings.length > 0 && (
        <ul className="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
          {findings.map((finding, index) => (
            <li key={index}>{finding}</li>
          ))}
        </ul>
      )}
      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

// -- Fact -----------------------------------------------------------------

function FactArtifactView({ artifact }: { artifact: unknown }) {
  if (!isRecord(artifact) || !Array.isArray(artifact.claims) || !Array.isArray(artifact.findings)) {
    return <UnknownArtifact artifact={artifact} />;
  }
  const claims = recordArray(artifact.claims);
  const findings = strArray(artifact.findings);
  const countsByStatus = new Map<string, number>();
  for (const claim of claims) {
    const status = str(claim.status, "unknown");
    countsByStatus.set(status, (countsByStatus.get(status) ?? 0) + 1);
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">{claims.length} claim(s) checked</p>
      <div className="flex flex-wrap gap-1.5">
        {Array.from(countsByStatus.entries()).map(([status, count]) => (
          <Badge key={status} variant="outline">
            {status}: {count}
          </Badge>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">{findings.length} finding(s)</p>
      {findings.length > 0 && (
        <ul className="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
          {findings.map((finding, index) => (
            <li key={index}>{finding}</li>
          ))}
        </ul>
      )}
      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

// -- Assemble -----------------------------------------------------------------

function AssembleArtifactView({ artifact }: { artifact: unknown }) {
  if (
    !isRecord(artifact) ||
    typeof artifact.revision_id !== "number" ||
    typeof artifact.candidate_sha256 !== "string" ||
    typeof artifact.fact_status !== "string"
  ) {
    return <UnknownArtifact artifact={artifact} />;
  }
  const qaSummary = isRecord(artifact.qa_summary) ? artifact.qa_summary : {};

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        Revision #{artifact.revision_id} · {FACT_STATUS_LABELS[artifact.fact_status] ?? artifact.fact_status}
      </p>
      <p className="font-mono text-xs text-muted-foreground">{artifact.candidate_sha256.slice(0, 16)}…</p>
      {Object.keys(qaSummary).length > 0 && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-muted-foreground">
          {Object.entries(qaSummary).map(([key, value]) => (
            <Fragment key={key}>
              <dt>{key}</dt>
              <dd>{String(value)}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      <JsonDisclosure artifact={artifact} />
    </div>
  );
}

function renderArtifactBody(stageName: DraftStageName, artifact: unknown) {
  switch (stageName) {
    case "intake":
      return <IntakeArtifactView artifact={artifact} />;
    case "research":
      return <ResearchArtifactView artifact={artifact} />;
    case "outline":
      return <OutlineArtifactView artifact={artifact} />;
    case "draft":
      return <DraftArtifactView artifact={artifact} />;
    case "lint":
      return <LintArtifactView artifact={artifact} />;
    case "copy":
    case "standards":
      return <CopyStandardsArtifactView artifact={artifact} />;
    case "fact":
      return <FactArtifactView artifact={artifact} />;
    case "assemble":
      return <AssembleArtifactView artifact={artifact} />;
    default:
      return <UnknownArtifact artifact={artifact} />;
  }
}

export function DraftStageArtifact({ stage }: DraftStageArtifactProps) {
  if (stage == null) {
    return <p className="text-sm text-muted-foreground">No stage selected.</p>;
  }

  const stageLabel = STAGE_LABELS[stage.stage] ?? stage.stage;
  const statusLabel = STAGE_STATUS_LABEL[stage.status] ?? stage.status;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-foreground">{stageLabel}</h3>
        <Badge variant="outline">{statusLabel}</Badge>
        <span className="text-xs text-muted-foreground">Attempt {stage.attempt}</span>
      </div>

      {stage.status === "failed" && (
        <Alert variant="destructive">
          <AlertTitle>{stage.error_code ?? "Stage failed"}</AlertTitle>
          {stage.error_message != null && <AlertDescription>{stage.error_message}</AlertDescription>}
        </Alert>
      )}

      {renderArtifactBody(stage.stage, stage.artifact)}
    </div>
  );
}
