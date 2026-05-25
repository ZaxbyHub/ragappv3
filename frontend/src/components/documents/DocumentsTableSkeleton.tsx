import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Checkbox } from "@/components/ui/checkbox";

export function DocumentsTableSkeleton() {
  return (
    <>
      {/* Desktop Table Skeleton (hidden on mobile) */}
      <Card className="hidden sm:block">
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full">
              <caption className="sr-only">Documents List</caption>
              <thead>
                <tr className="border-b bg-muted/50">
                  <th scope="col" className="text-left p-4 font-medium">
                    <Checkbox disabled aria-label="Select all documents" />
                  </th>
                  <th scope="col" className="text-left p-4 font-medium">Filename</th>
                  <th scope="col" className="text-left p-4 font-medium">Status</th>
                  <th scope="col" className="text-left p-4 font-medium">Progress</th>
                  <th scope="col" className="text-left p-4 font-medium">Chunks</th>
                  <th scope="col" className="text-left p-4 font-medium">Size</th>
                  <th scope="col" className="text-left p-4 font-medium">Uploaded</th>
                  <th scope="col" className="text-right p-4 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {[...Array(5)].map((_, i) => (
                  <tr key={i} className="border-b">
                    <td className="p-4">
                      <Checkbox disabled />
                    </td>
                    <td className="p-4">
                      <div className="flex items-center gap-2">
                        <Skeleton className="h-4 w-4" />
                        <Skeleton className="h-4 w-[180px]" />
                      </div>
                    </td>
                    <td className="p-4"><Skeleton className="h-5 w-[80px]" /></td>
                    <td className="p-4"><Skeleton className="h-5 w-[120px]" /></td>
                    <td className="p-4"><Skeleton className="h-4 w-[40px]" /></td>
                    <td className="p-4"><Skeleton className="h-4 w-[60px]" /></td>
                    <td className="p-4"><Skeleton className="h-4 w-[80px]" /></td>
                    <td className="p-4 text-right"><Skeleton className="h-11 w-11 ml-auto" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {/* Mobile Cards Skeleton (hidden on desktop) */}
      <div className="grid grid-cols-1 gap-3 sm:hidden">
        {[...Array(3)].map((_, i) => (
          <Card key={i} className="w-full">
            <CardContent className="p-4">
              <div className="flex items-start justify-between gap-3 mb-3">
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <Skeleton className="h-11 w-11 rounded-md" />
                  <div className="min-w-0">
                    <Skeleton className="h-5 w-32 mb-1" />
                    <Skeleton className="h-4 w-24" />
                  </div>
                </div>
                <Skeleton className="h-11 w-11 rounded-full" />
              </div>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="space-y-1">
                  <Skeleton className="h-4 w-16" />
                  <Skeleton className="h-6 w-20" />
                </div>
                <div className="space-y-1">
                  <Skeleton className="h-4 w-16" />
                  <Skeleton className="h-4 w-16" />
                </div>
                <div className="space-y-1">
                  <Skeleton className="h-4 w-16" />
                  <Skeleton className="h-4 w-20" />
                </div>
                <div className="space-y-1">
                  <Skeleton className="h-4 w-16" />
                  <Skeleton className="h-4 w-12" />
                </div>
              </div>
              <div className="mt-4 flex sm:hidden">
                <Skeleton className="h-11 w-full rounded-md" />
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </>
  );
}
