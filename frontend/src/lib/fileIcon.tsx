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
  const cls = className ?? '';

  if (ext === 'pdf') {
    return <FileType className={`${cls} text-filetype-pdf`} aria-hidden="true" />;
  }
  if (ext === 'docx' || ext === 'doc') {
    return <FileType className={`${cls} text-filetype-docx`} aria-hidden="true" />;
  }
  if (ext === 'pptx' || ext === 'ppt') {
    return <Presentation className={`${cls} text-filetype-pptx`} aria-hidden="true" />;
  }
  if (ext === 'md' || ext === 'mdx') {
    return <FileText className={`${cls} text-filetype-md`} aria-hidden="true" />;
  }
  if (ext === 'xlsx' || ext === 'xls' || ext === 'csv') {
    return <Sheet className={`${cls} text-filetype-xlsx`} aria-hidden="true" />;
  }
  if (ext === 'json') {
    return <FileJson className={`${cls} text-filetype-json`} aria-hidden="true" />;
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
    return <FileCode className={`${cls} text-filetype-code`} aria-hidden="true" />;
  }
  if (ext === 'txt' || ext === 'log') {
    return <FileText className={cls} aria-hidden="true" />;
  }
  return <File className={cls} aria-hidden="true" />;
}
