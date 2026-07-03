import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { FileText, Layers, HardDrive, CheckCircle } from "lucide-react";
import { formatFileSize } from "@/lib/formatters";
import type { DocumentStatsResponse } from "@/lib/api";

export function DocumentStatsCards({ stats }: { stats: DocumentStatsResponse }) {
  return (
    <div className="grid gap-4 md:grid-cols-4">
      <Card>
        <CardContent className="pt-6">
          <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center mb-3" aria-hidden="true">
            <FileText className="h-6 w-6 text-muted-foreground" />
          </div>
          <CardHeader className="pt-0 pb-2">
            <CardDescription>Total Documents</CardDescription>
            <CardTitle className="text-3xl">{stats.total_documents}</CardTitle>
          </CardHeader>
        </CardContent>
      </Card>
      <Card>
        <CardContent className="pt-6">
          <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center mb-3" aria-hidden="true">
            <Layers className="h-6 w-6 text-muted-foreground" />
          </div>
          <CardHeader className="pt-0 pb-2">
            <CardDescription>Total Chunks</CardDescription>
            <CardTitle className="text-3xl">{stats.total_chunks}</CardTitle>
          </CardHeader>
        </CardContent>
      </Card>
      <Card>
        <CardContent className="pt-6">
          <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center mb-3" aria-hidden="true">
            <HardDrive className="h-6 w-6 text-muted-foreground" />
          </div>
          <CardHeader className="pt-0 pb-2">
            <CardDescription>Total Size</CardDescription>
            <CardTitle className="text-3xl">{formatFileSize(stats.total_size_bytes)}</CardTitle>
          </CardHeader>
        </CardContent>
      </Card>
      <Card>
        <CardContent className="pt-6">
          <div className="w-12 h-12 rounded-full bg-muted flex items-center justify-center mb-3" aria-hidden="true">
            <CheckCircle className="h-6 w-6 text-muted-foreground" />
          </div>
          <CardHeader className="pt-0 pb-2">
            <CardDescription>Indexed</CardDescription>
            <CardTitle className="text-3xl">{stats.documents_by_status?.indexed || 0}</CardTitle>
          </CardHeader>
        </CardContent>
      </Card>
    </div>
  );
}
