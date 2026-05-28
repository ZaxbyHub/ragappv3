import * as React from "react"

import { cn } from "@/lib/utils"

// eslint-disable-next-line @typescript-eslint/no-empty-object-type
export interface TextareaProps
  extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {}

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, ...props }, ref) => {
    return (
      <textarea
        className={cn(
          // placeholder uses /80 opacity rather than the bare muted-foreground
          // token so contrast stays above WCAG AA in both themes (otherwise
          // light-mode placeholder slips to ~4.2:1 and dark-mode to ~3:1).
          "flex min-h-[80px] w-full rounded-sm border border-input bg-card px-3 py-2 text-sm placeholder:text-muted-foreground/80 focus-visible:outline-none focus-visible:border-primary/50 disabled:cursor-not-allowed disabled:opacity-50 transition-colors duration-150",
          className
        )}
        ref={ref}
        {...props}
      />
    )
  }
)
Textarea.displayName = "Textarea"

export { Textarea }
