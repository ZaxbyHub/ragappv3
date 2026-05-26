import {
  FileText,
  FileType,
  Sheet,
  FileCode,
  FileJson,
  Presentation,
  File,
} from "lucide-react";

/**
 * Returns a colored Lucide icon component for the given filename based on
 * extension. Covers every extension in the backend's allowed_extensions set
 * (config.py); anything unrecognized falls back to the generic File icon.
 * Always use via JSX: <FileIcon filename={doc.filename} className="h-4 w-4" />
 */
export function FileIcon({ filename, className }: { filename: string | null | undefined; className?: string }) {
  const ext = (filename ?? '').split('.').pop()?.toLowerCase() ?? '';

  if (ext === 'pdf') {
    return <FileType className={className} style={{ color: '#ef4444' }} aria-hidden="true" />;
  }
  if (ext === 'docx' || ext === 'doc') {
    return <FileType className={className} style={{ color: '#3b82f6' }} aria-hidden="true" />;
  }
  if (ext === 'pptx' || ext === 'ppt') {
    return <Presentation className={className} style={{ color: '#f97316' }} aria-hidden="true" />;
  }
  if (ext === 'md' || ext === 'mdx') {
    return <FileText className={className} style={{ color: '#14b8a6' }} aria-hidden="true" />;
  }
  if (ext === 'xlsx' || ext === 'xls' || ext === 'csv') {
    return <Sheet className={className} style={{ color: '#22c55e' }} aria-hidden="true" />;
  }
  if (ext === 'json') {
    return <FileJson className={className} style={{ color: '#eab308' }} aria-hidden="true" />;
  }
  if (
    ext === 'py' ||
    ext === 'js' ||
    ext === 'ts' ||
    ext === 'html' ||
    ext === 'css' ||
    ext === 'xml' ||
    ext === 'yaml' ||
    ext === 'yml' ||
    ext === 'sql'
  ) {
    return <FileCode className={className} style={{ color: '#8b5cf6' }} aria-hidden="true" />;
  }
  if (ext === 'txt' || ext === 'log') {
    return <FileText className={className} style={{ color: undefined }} aria-hidden="true" />;
  }
  return <File className={className} aria-hidden="true" />;
}
