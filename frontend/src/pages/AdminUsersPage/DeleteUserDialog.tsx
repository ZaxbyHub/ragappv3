import { useState } from "react";
import { UserX, Loader2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import type { User } from "./types";

interface DeleteUserDialogProps {
  open: boolean;
  user: User | null;
  onDelete: (user: User) => Promise<void>;
  onOpenChange: (open: boolean) => void;
  onClose?: () => void;
}

export function DeleteUserDialog({
  open,
  user,
  onDelete,
  onOpenChange,
  onClose,
}: DeleteUserDialogProps) {
  const handleOpenChange = (open: boolean) => {
    onOpenChange(open);
    if (!open) {
      onClose?.();
    }
  };
  const [isDeleting, setIsDeleting] = useState(false);

  const handleDelete = async () => {
    if (!user) return;
    setIsDeleting(true);
    try {
      await onDelete(user);
    } finally {
      setIsDeleting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent aria-labelledby="delete-title" aria-describedby="delete-desc">
        <DialogHeader>
          <DialogTitle id="delete-title" className="flex items-center gap-2">
            <UserX className="w-5 h-5 text-destructive" />
            Delete User
          </DialogTitle>
          <DialogDescription id="delete-desc">
            Are you sure you want to delete <strong>{user?.username}</strong>? This action
            cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={isDeleting}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={handleDelete} disabled={isDeleting}>
            {isDeleting ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Deleting...
              </>
            ) : (
              "Delete User"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
