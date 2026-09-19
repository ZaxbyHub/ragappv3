import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, X } from "lucide-react";
import { Button } from "@/components/ui/button";

const DISMISS_KEY = "unconfigured-chat-banner-dismissed";

interface UnconfiguredChatBannerProps {
  /** Server-computed signal (safe for every role). False = chat unconfigured. */
  chatConfigured: boolean;
}

/**
 * First-login banner (issue #622, AC2): shown while chat models are
 * unconfigured so the operator gets proactive guidance instead of
 * discovering the 409 on the first message. Prop-driven (the mount site in
 * PageShell owns the getSettings fetch and renders nothing on fetch
 * failure); dismissal persists for the browser session.
 */
export default function UnconfiguredChatBanner({
  chatConfigured,
}: UnconfiguredChatBannerProps) {
  const [dismissed, setDismissed] = useState(
    () => sessionStorage.getItem(DISMISS_KEY) === "1"
  );

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === DISMISS_KEY && event.newValue === "1") {
        setDismissed(true);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  if (chatConfigured || dismissed) {
    return null;
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center gap-3 border-b border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-100"
    >
      <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
      <p className="flex-1">
        Chat is not configured yet. Pick your model endpoints to start
        chatting —{" "}
        <Link to="/settings" className="font-medium underline underline-offset-2">
          Settings → Models
        </Link>
      </p>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => {
          sessionStorage.setItem(DISMISS_KEY, "1");
          setDismissed(true);
        }}
        aria-label="Dismiss"
      >
        <X className="h-4 w-4" aria-hidden="true" />
        Dismiss
      </Button>
    </div>
  );
}
