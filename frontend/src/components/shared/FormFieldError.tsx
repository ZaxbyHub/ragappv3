import { AlertCircle } from "lucide-react";

interface FormFieldErrorProps {
  children?: React.ReactNode;
  id?: string;
}

export function FormFieldError({ children, id }: FormFieldErrorProps) {
  if (!children) return null;
  return (
    <p
      id={id}
      className="text-xs text-destructive mt-1 flex items-center gap-1"
      role="alert"
      // assertive so submit-blocking errors interrupt screen-reader output;
      // atomic so the entire error is re-announced on change rather than diff
      aria-live="assertive"
      aria-atomic="true"
    >
      <AlertCircle className="h-3 w-3 shrink-0" aria-hidden="true" />
      {children}
    </p>
  );
}
