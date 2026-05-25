import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatFileSize } from "@/lib/formatters";
import type { DocumentStatsResponse } from "@/lib/api";

export function DocumentStatsCards({ stats }: { stats: DocumentStatsResponse }) {
  return (
    <div className="grid gap-4 md:grid-cols-4">
      <Card>
        <CardHeader className="pb-2">
          <CardDescription>Total Documents</CardDescription>
          <CardTitle className="text-3xl">{stats.total_documents}</CardTitle>
        </CardHeader>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardDescription>Total Chunks</CardDescription>
          <CardTitle className="text-3xl">{stats.total_chunks}</CardTitle>
        </CardHeader>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardDescription>Total Size</CardDescription>
          <CardTitle className="text-3xl">{formatFileSize(stats.total_size_bytes)}</CardTitle>
        </CardHeader>
      </Card>
      <Card>
        <CardHeader className="pb-2">
          <CardDescription>Indexed</CardDescription>
          <CardTitle className="text-3xl">{stats.documents_by_status?.indexed || 0}</CardTitle>
        </CardHeader>
      </Card>
    </div>
  );
}
