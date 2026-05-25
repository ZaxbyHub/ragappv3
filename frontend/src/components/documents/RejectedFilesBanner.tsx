import { Button } from "@/components/ui/button";
import { AlertCircle } from "lucide-react";

interface RejectedFilesBannerProps {
  files: string[];
  onDismiss: () => void;
}

export function RejectedFilesBanner({ files, onDismiss }: RejectedFilesBannerProps) {
  if (files.length === 0) return null;
  return (
    <div
      className="rounded-lg border border-warning/30 bg-warning/10 p-4 text-warning-foreground"
      role="status"
      aria-live="polite"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <AlertCircle className="h-5 w-5 text-warning" aria-hidden="true" />
          <span className="font-medium text-foreground">
            {files.length === 1 ? "1 file was rejected" : `${files.length} files were rejected`}
          </span>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={onDismiss}
          aria-label="Dismiss rejected files list"
          className="h-7 text-xs"
        >
          Dismiss
        </Button>
      </div>
      <ul className="list-disc space-y-1 pl-5 text-sm text-foreground/80">
        {files.map((file, index) => (
          <li key={index}>{file}</li>
        ))}
      </ul>
    </div>
  );
}
