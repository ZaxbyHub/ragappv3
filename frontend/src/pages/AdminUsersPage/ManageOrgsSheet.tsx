import { useCallback } from "react";
import { Building2, Loader2, Search } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { OrgItem, User } from "./types";

interface ManageOrgsSheetProps {
  open: boolean;
  user: User | null;
  allOrgs: OrgItem[];
  orgMemberships: Map<number, string>;
  isLoading: boolean;
  isSaving: boolean;
  searchQuery: string;
  onSearchChange: (query: string) => void;
  onToggleOrg: (orgId: number) => void;
  onSetOrgRole: (orgId: number, role: string) => void;
  onSave: () => Promise<void>;
  onClose: () => void;
}

export function ManageOrgsSheet({
  open,
  user,
  allOrgs,
  orgMemberships,
  isLoading,
  isSaving,
  searchQuery,
  onSearchChange,
  onToggleOrg,
  onSetOrgRole,
  onSave,
  onClose,
}: ManageOrgsSheetProps) {
  const toggleOrg = useCallback(
    (orgId: number) => {
      onToggleOrg(orgId);
    },
    [onToggleOrg]
  );

  const filteredOrgs = allOrgs.filter((org) => {
    const searchLower = searchQuery.toLowerCase();
    return (
      org.name.toLowerCase().includes(searchLower) ||
      (org.description && org.description.toLowerCase().includes(searchLower))
    );
  });

  return (
    <Sheet open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <SheetContent
        className="sm:max-w-[400px] flex flex-col"
        aria-labelledby="orgs-title"
        aria-describedby="orgs-desc"
      >
        <SheetHeader>
          <SheetTitle id="orgs-title" className="flex items-center gap-2">
            <Building2 className="h-5 w-5" aria-hidden="true" />
            Manage Organizations
          </SheetTitle>
          <SheetDescription id="orgs-desc">
            Manage organization memberships for <strong>{user?.username}</strong>. Select organizations
            to add or remove from this user.
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 flex flex-col py-4 min-h-0">
          <div className="relative mb-4">
            <Search
              className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              placeholder="Search organizations..."
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
              className="pl-10"
              aria-label="Search organizations"
              disabled={isLoading}
            />
          </div>

          <ScrollArea className="flex-1 -mx-6 px-6">
            {isLoading ? (
              <div className="space-y-3">
                {Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="flex items-center gap-3 p-3 rounded-sm border">
                    <Skeleton className="h-4 w-4" />
                    <div className="flex-1 space-y-2">
                      <Skeleton className="h-4 w-32" />
                      <Skeleton className="h-3 w-24" />
                    </div>
                  </div>
                ))}
              </div>
            ) : filteredOrgs.length === 0 ? (
              <div
                className="text-center py-8 text-muted-foreground"
                role="status"
                aria-live="polite"
              >
                {searchQuery ? "No organizations match your search" : "No organizations available"}
              </div>
            ) : (
              <div className="space-y-2 pr-4">
                {filteredOrgs.map((org) => {
                  const isMember = orgMemberships.has(org.id);
                  const role = orgMemberships.get(org.id) ?? "member";
                  return (
                    <div
                      key={org.id}
                      className={`flex items-start space-x-3 rounded-sm border p-3 transition-colors ${isMember ? "border-primary/50 bg-primary/5" : "hover:bg-muted/50"}`}
                    >
                      <Checkbox
                        id={`org-${org.id}`}
                        checked={isMember}
                        onCheckedChange={() => toggleOrg(org.id)}
                        aria-label={`Select ${org.name}`}
                        disabled={isSaving}
                        className="mt-0.5"
                      />
                      <div className="flex-1 min-w-0">
                        <Label htmlFor={`org-${org.id}`} className="font-medium cursor-pointer block">
                          {org.name}
                        </Label>
                        {org.description && (
                          <div className="text-sm text-muted-foreground line-clamp-1">{org.description}</div>
                        )}
                        <div className="mt-1.5">
                          <Select
                            value={role}
                            onValueChange={(v) => onSetOrgRole(org.id, v)}
                            disabled={!isMember || isSaving}
                          >
                            <SelectTrigger className="h-7 w-24 text-xs" aria-label={`Role in ${org.name}`}>
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="member" className="text-xs">Member</SelectItem>
                              <SelectItem value="admin" className="text-xs">Admin</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </ScrollArea>

          <div className="mt-4 text-sm text-muted-foreground">
            {orgMemberships.size} organization{orgMemberships.size !== 1 ? "s" : ""} selected
          </div>
        </div>

        <SheetFooter className="flex-col gap-2 sm:flex-row border-t pt-4">
          <Button
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={isSaving}
            className="w-full sm:w-auto"
          >
            Cancel
          </Button>
          <Button
            onClick={onSave}
            disabled={isSaving || isLoading || !user}
            className="w-full sm:w-auto"
            aria-label="Save organization changes"
          >
            {isSaving ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
                Saving...
              </>
            ) : (
              "Save Changes"
            )}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
