/**
 * Settings → Overview tab.
 *
 * Read-only health snapshot. Reuses the existing health hook + connection
 * test API. No editable fields here, so dirty tracking is not relevant.
 */
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Loader2 } from "lucide-react";
import type { HealthStatus } from "@/types/health";
import type { ConnectionTestResult } from "@/lib/api";

export interface OverviewTabProps {
  health: HealthStatus;
  connectionResult: ConnectionTestResult | null;
  isTestingConnections: boolean;
  onTestConnections: () => void;
  curatorEnabled: boolean;
  wikiEnabled: boolean;
}

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${
        ok ? "bg-emerald-500" : "bg-destructive"
      }`}
      aria-hidden
    />
  );
}

export function OverviewTab({
  health,
  connectionResult,
  isTestingConnections,
  onTestConnections,
  curatorEnabled,
  wikiEnabled,
}: OverviewTabProps) {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Service health</CardTitle>
            <CardDescription>
              Live readiness for the chat / embedding / reranker stack.
            </CardDescription>
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={onTestConnections}
            disabled={isTestingConnections}
          >
            {isTestingConnections && (
              <Loader2 className="w-4 h-4 mr-1 animate-spin" />
            )}
            Test connections
          </Button>
        </CardHeader>
        <CardContent className="space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-2">
              <StatusDot ok={health.backend} />
              Backend
            </span>
            <span className="text-muted-foreground capitalize">
              {health.backend ? "ok" : "down"}
            </span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-2">
              <StatusDot ok={health.embeddings} />
              Embedding service
            </span>
            <span className="text-muted-foreground capitalize">
              {health.embeddings ? "ok" : "down"}
            </span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-2">
              <StatusDot ok={health.chat} />
              Chat service
            </span>
            <span className="text-muted-foreground capitalize">
              {health.chat ? "ok" : "down"}
            </span>
          </div>
          {connectionResult && (
            <div className="text-xs text-muted-foreground pt-2 border-t">
              Last connection test:{" "}
              {Object.entries(connectionResult)
                .map(([k, v]) => `${k}=${v?.ok ? "ok" : "fail"}`)
                .join(" · ")}
            </div>
          )}
          {health.lastChecked && (
            <p className="text-xs text-muted-foreground">
              Last checked: {health.lastChecked.toLocaleTimeString()}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Wiki / Curator status</CardTitle>
          <CardDescription>
            Quick view of wiki + optional curator enablement.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <div className="flex items-center gap-2">
            <Badge variant={wikiEnabled ? "secondary" : "outline"}>
              Wiki: {wikiEnabled ? "enabled" : "disabled"}
            </Badge>
            <Badge variant={curatorEnabled ? "secondary" : "outline"}>
              Curator: {curatorEnabled ? "enabled" : "disabled"}
            </Badge>
          </div>
          <p className="text-xs text-muted-foreground">
            Configure curator on the Wiki &amp; Curator tab. Curator output
            never becomes an active claim without source-quote verification.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
