// frontend/src/components/chat/ShareAction.tsx
// Issue #573 (AC5): share the conversation as an authenticated session link.
// DESIGN DECISION (recorded per the issue's trust-boundary mandate): this is
// an authenticated-only link — every chat API requires an authenticated user,
// so the URL only renders the conversation for signed-in users of this
// deployment, and deleting the session revokes it. No public/unauthenticated
// surface and no database change. The URL is basename-aware (appPath from
// @/lib/paths) so subpath deployments (/knowledgevault, /meridian) produce
// correct links — unlike the existing "Export chat" action, which downloads
// a Markdown file; this action copies a URL to the clipboard.

import { useState } from "react";
import { Share2, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { appPath } from "@/lib/paths";
import { toast } from "sonner";

interface ShareActionProps {
  sessionId: string;
}

export function ShareAction({ sessionId }: ShareActionProps) {
  const [copied, setCopied] = useState(false);

  const handleShare = async () => {
    const url = `${window.location.origin}${appPath(`/chat/${sessionId}`)}`;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      toast.success("Conversation link copied to clipboard");
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Couldn't copy the conversation link");
    }
  };

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={handleShare}
      disabled={!sessionId}
      aria-label="Share conversation link"
    >
      {copied ? (
        <Check className="h-5 w-5" aria-hidden="true" />
      ) : (
        <Share2 className="h-5 w-5" aria-hidden="true" />
      )}
    </Button>
  );
}
