import { useMemo } from "react";
import { Users, Loader2, Search } from "lucide-react";
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
import type { Group, User } from "./types";

interface ManageGroupsSheetProps {
  open: boolean;
  user: User | null;
  allGroups: Group[];
  selectedGroupIds: number[];
  isLoading: boolean;
  isSaving: boolean;
  searchQuery: string;
  onSearchChange: (query: string) => void;
  onToggleGroup: (groupId: number) => void;
  onSave: () => Promise<void>;
  onClose: () => void;
}

export function ManageGroupsSheet({
  open,
  user,
  allGroups,
  selectedGroupIds,
  isLoading,
  isSaving,
  searchQuery,
  onSearchChange,
  onToggleGroup,
  onSave,
  onClose,
}: ManageGroupsSheetProps) {
  const filteredGroups = useMemo(() => {
    const searchLower = searchQuery.toLowerCase();
    return allGroups.filter((group) =>
      group.name.toLowerCase().includes(searchLower) ||
      (group.description && group.description.toLowerCase().includes(searchLower))
    );
  }, [allGroups, searchQuery]);

  return (
    <Sheet open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <SheetContent
        className="sm:max-w-[400px] flex flex-col"
        aria-labelledby="groups-title"
        aria-describedby="groups-desc"
      >
        <SheetHeader>
          <SheetTitle id="groups-title" className="flex items-center gap-2">
            <Users className="h-5 w-5" aria-hidden="true" />
            Manage Groups
          </SheetTitle>
          <SheetDescription id="groups-desc">
            Manage group memberships for <strong>{user?.username}</strong>. Select groups
            to add or remove from this user.
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 flex flex-col py-4 min-h-0">
          {/* Search Input */}
          <div className="relative mb-4">
            <Search
              className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              placeholder="Search groups..."
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
              className="pl-10"
              aria-label="Search groups"
              disabled={isLoading}
            />
          </div>

          {/* Groups List */}
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
            ) : filteredGroups.length === 0 ? (
              <div
                className="text-center py-8 text-muted-foreground"
                role="status"
                aria-live="polite"
              >
                {searchQuery ? "No groups match your search" : "No groups available"}
              </div>
            ) : (
              <div className="space-y-2 pr-4">
                {filteredGroups.map((group) => (
                  <div
                    key={group.id}
                    className="flex items-start space-x-3 rounded-sm border p-3 hover:bg-muted/50 transition-colors"
                  >
                      <Checkbox
                      id={`group-${group.id}`}
                      checked={selectedGroupIds.includes(group.id)}
                      onCheckedChange={() => onToggleGroup(group.id)}
                      aria-label={`Select ${group.name}`}
                      disabled={isSaving}
                    />
                    <Label
                      htmlFor={`group-${group.id}`}
                      className="flex-1 cursor-pointer space-y-1"
                    >
                      <div className="font-medium">{group.name}</div>
                      {group.description && (
                        <div className="text-sm text-muted-foreground">{group.description}</div>
                      )}
                    </Label>
                  </div>
                ))}
              </div>
            )}
          </ScrollArea>

          {/* Selected Count */}
          <div className="mt-4 text-sm text-muted-foreground">
            {selectedGroupIds.length} group{selectedGroupIds.length !== 1 ? "s" : ""} selected
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
            aria-label="Save group changes"
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
