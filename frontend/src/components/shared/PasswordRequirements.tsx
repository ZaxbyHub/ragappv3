import { cn } from "@/lib/utils";

/**
 * The one statement of the app's password rules (issue #776 / UI-ENH-12,
 * UI-R4-13). The wording must state exactly the three rules the backend
 * enforces in `password_strength_check`
 * (backend/app/services/auth_service.py): at least 8 characters, at least one
 * digit, at least one uppercase letter. Backend contract pinned by
 * backend/tests/test_l05_password_rule_contract.py — change both together.
 *
 * Static mode (no `value`): a single help-text sentence.
 * Live mode (`value`): the same three rules as a ✓/○ checklist driven by the
 *   current input (replaces RegisterPage's hand-inlined list).
 */
interface PasswordRequirementsProps {
  /** id for aria-describedby wiring (e.g. "password-requirements"). */
  id?: string;
  /** Current password input; when provided the checklist renders live state. */
  value?: string;
  className?: string;
}

const RULES = [
  { label: "At least 8 characters", met: (v: string) => v.length >= 8 },
  { label: "At least one digit", met: (v: string) => /\d/.test(v) },
  { label: "At least one uppercase letter", met: (v: string) => /[A-Z]/.test(v) },
] as const;

export function PasswordRequirements({ id, value, className }: PasswordRequirementsProps) {
  if (value === undefined) {
    return (
      <p id={id} className={cn("text-xs text-muted-foreground", className)}>
        At least 8 characters, one digit, and one uppercase letter.
      </p>
    );
  }
  return (
    <ul id={id} className={cn("space-y-1 text-xs text-muted-foreground", className)}>
      {RULES.map((rule) => {
        const met = rule.met(value);
        return (
          <li
            key={rule.label}
            className={cn("flex items-center gap-1.5", met ? "text-success" : "text-muted-foreground")}
          >
            <span aria-hidden="true">{met ? "✓" : "○"}</span>
            <span>{rule.label}</span>
            <span className="sr-only">{met ? "requirement met" : "requirement not met"}</span>
          </li>
        );
      })}
    </ul>
  );
}
