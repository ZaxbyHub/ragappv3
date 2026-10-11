import { Badge } from "@/components/ui/badge";
import { Server, Cpu, MessageCircle } from "lucide-react";
import type { HealthStatus } from "@/types/health";

export function ConnectionStatusBadges({ health }: { health: HealthStatus }) {
  const getBadgeClass = (isUp: boolean) => {
    if (health.loading) return "bg-muted text-muted-foreground";
    return isUp ? "bg-success hover:bg-success/80" : "bg-destructive hover:bg-destructive/80";
  };

  const getBadgeLabel = (label: string) => {
    return health.loading ? "Checking" : label;
  };

  return (
    // Issue #779 (UI-R3-06): the row must be able to wrap — at 320px the
    // three badges overflow off the LEFT edge under Settings' items-end
    // header stacking, clipping the first badge's icon, and leftward
    // overflow is invisible to scrollWidth-based capture metrics.
    // justify-end keeps the wrapped badges right-anchored so wrapping never
    // introduces a rightward overflow instead.
    <div className="flex flex-wrap items-center justify-end gap-2">
      <Badge variant="default" className={getBadgeClass(health.backend)}>
        <Server className="w-3 h-3 mr-1" />
        {getBadgeLabel("Backend")}
      </Badge>
      <Badge variant="default" className={getBadgeClass(health.embeddings)}>
        <Cpu className="w-3 h-3 mr-1" />
        {getBadgeLabel("Embeddings")}
      </Badge>
      <Badge variant="default" className={getBadgeClass(health.chat)}>
        <MessageCircle className="w-3 h-3 mr-1" />
        {getBadgeLabel("Chat")}
      </Badge>
    </div>
  );
}
