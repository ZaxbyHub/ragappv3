import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Document, ExtractionDiagnostics } from "@/lib/api";

/** Diagnostics shape the panel understands (mirrors ExtractionDiagnostics). */
interface ParseQualityFacts {
  pages_total: number;
  pages_with_text: number;
  low_content_pages: number[];
  ocr_used: boolean;
  tables_detected: number;
  captions_detected?: number;
  extraction_version: string;
}

function isDiagnostics(value: unknown): value is ParseQualityFacts {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<ParseQualityFacts>;
  return typeof candidate.pages_total === "number" && typeof candidate.extraction_version === "string";
}

/**
 * Resolve the extraction diagnostics a document payload carries. The detail
 * payload exposes them under `extraction`; the status payload and older
 * shapes carry `extraction_diagnostics` (top level or inside `metadata`).
 * All four carrier shapes are accepted so the panel renders whichever
 * endpoint produced the document.
 */
function resolveDiagnostics(doc: Document): ParseQualityFacts | ExtractionDiagnostics | null {
  const direct = doc as Document & { extraction_diagnostics?: unknown };
  const metadata = (doc.metadata ?? {}) as Record<string, unknown>;
  const candidates = [
    doc.extraction,
    direct.extraction_diagnostics,
    metadata.extraction,
    metadata.extraction_diagnostics,
  ];
  for (const candidate of candidates) {
    if (isDiagnostics(candidate)) return candidate;
  }
  return null;
}

/**
 * Parse-quality card for the document detail page (issue #514 /
 * PRODUCT-ENH-06): surfaces extraction facts — page coverage, OCR use,
 * low-content pages, detected tables/captions, and the extraction pipeline
 * version — so an omission (e.g. a scanned page that yielded no text) is
 * visible even when embedding succeeded. Renders extraction facts only;
 * chunk/embedding counts stay in the Details card.
 */
export function ParseQualityPanel({ doc }: { doc: Document }) {
  const diagnostics = resolveDiagnostics(doc);
  if (!diagnostics) return null;

  const {
    pages_total,
    pages_with_text,
    low_content_pages,
    ocr_used,
    tables_detected,
    captions_detected,
    extraction_version,
  } = diagnostics;

  return (
    <Card>
      <CardHeader className="pb-2 pt-3 px-4">
        <CardTitle className="text-sm">Parse Quality</CardTitle>
      </CardHeader>
      <CardContent className="px-4 pb-3 grid grid-cols-2 gap-3 text-sm">
        <div>
          <p className="text-muted-foreground">Pages with text</p>
          <p className="tabular-nums">
            {pages_with_text} / {pages_total}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">OCR</p>
          <p>{ocr_used ? "Used" : "Not used"}</p>
        </div>
        <div>
          <p className="text-muted-foreground">Low-content pages</p>
          <p className="tabular-nums">
            {low_content_pages.length > 0 ? low_content_pages.join(", ") : "None"}
          </p>
        </div>
        <div>
          <p className="text-muted-foreground">Tables detected</p>
          <p className="tabular-nums">{tables_detected}</p>
        </div>
        {captions_detected != null && (
          <div>
            <p className="text-muted-foreground">Captions detected</p>
            <p className="tabular-nums">{captions_detected}</p>
          </div>
        )}
        <div>
          <p className="text-muted-foreground">Extraction version</p>
          <p>{extraction_version}</p>
        </div>
      </CardContent>
    </Card>
  );
}
