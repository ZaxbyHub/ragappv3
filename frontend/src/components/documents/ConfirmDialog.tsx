import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

export interface ConfirmDialogState {
  open: boolean;
  title: string;
  description: string;
  onConfirm: () => void;
  variant?: "destructive" | "default";
}

interface ConfirmDialogProps {
  state: ConfirmDialogState;
  onOpenChange: (open: boolean) => void;
}

export function ConfirmDialog({ state, onOpenChange }: ConfirmDialogProps) {
  return (
    <Dialog open={state.open} onOpenChange={onOpenChange}>
      <DialogContent aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-desc">
        <DialogHeader>
          <DialogTitle id="confirm-dialog-title">{state.title}</DialogTitle>
          <DialogDescription id="confirm-dialog-desc">{state.description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant={state.variant === "destructive" ? "destructive" : "default"}
            onClick={() => {
              onOpenChange(false);
              state.onConfirm();
            }}
          >
            Confirm
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
