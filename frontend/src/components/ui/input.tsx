import * as React from "react"

import { cn } from "@/lib/utils"

// eslint-disable-next-line @typescript-eslint/no-empty-object-type
export interface InputProps
  extends React.InputHTMLAttributes<HTMLInputElement> {}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          // placeholder uses /80 opacity rather than the bare muted-foreground
          // token so contrast stays above WCAG AA in both themes (otherwise
          // light-mode placeholder slips to ~4.2:1 and dark-mode to ~3:1).
          "flex h-10 w-full rounded-sm border border-input bg-card px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground/80 focus-visible:outline-none focus-visible:border-primary/50 disabled:cursor-not-allowed disabled:opacity-50 transition-colors duration-150 ease-in-out",
          className
        )}
        ref={ref}
        {...props}
      />
    )
  }
)
Input.displayName = "Input"

export { Input }
