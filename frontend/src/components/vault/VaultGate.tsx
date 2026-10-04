// frontend/src/components/vault/VaultGate.tsx
// Issue #781 (UI-R2-03 + UI-R1-05, defect class C17): the shared control for
// vault-gated null states. One component renders the selector the page's own
// copy points at, plus the create/open-vaults action — instead of every page
// re-implementing ad hoc guidance. Navigation is click-triggered only; the
// gate never redirects on mount.
//
// Import surface is deliberately minimal (VaultSelector, ui/button,
// react-router-dom): the frozen acceptance fixtures mock exactly these, so
// any wider import fails loudly in the checks rather than silently in prod.

import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { VaultSelector } from "@/components/vault/VaultSelector";

interface VaultGateProps {
  /** Optional "why you're seeing this" line rendered above the controls. */
  reason?: string;
}

// Issue #782: tolerate router-less mounts (bare unit-test renders — Wiki's
// null-branch acceptance check renders the page with no Router). react-router
// throws only AFTER its internal useContext, so catching inside a named hook
// keeps hook order stable across renders (same shape as WikiPage's
// useOptionalSearchParams, #515 AC39). Outside a Router the Open Vaults click
// is inert; production always renders the gate inside the app Router.
function useOptionalNavigate(): ((to: string) => void) | null {
  try {
    return useNavigate() as unknown as (to: string) => void;
  } catch {
    return null;
  }
}

export function VaultGate({ reason }: VaultGateProps) {
  const navigate = useOptionalNavigate();

  return (
    <div className="flex flex-col items-center gap-3" data-testid="vault-gate">
      {reason && (
        <p className="text-sm text-muted-foreground">{reason}</p>
      )}
      <div className="flex flex-wrap items-center justify-center gap-2">
        <VaultSelector />
        <Button
          variant="outline"
          onClick={() => {
            if (navigate) {
              navigate("/vaults");
            }
          }}
        >
          Open Vaults
        </Button>
      </div>
    </div>
  );
}
