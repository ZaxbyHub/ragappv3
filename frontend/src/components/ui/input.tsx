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
          // Placeholder uses the BARE muted-foreground token: an /NN opacity
          // modifier can only lower contrast against the card surface (an
          // /80 override measured 3.56:1 in light theme, #777/UI-R3-03 —
          // the previous comment here claimed the opposite). The bare token
          // measures 5.86:1 light / 6.75:1 dark, pinned by the token
          // contrast guardrail in src/index.css.contrast.test.ts.
          "flex h-10 w-full rounded-sm border border-input bg-card px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-hidden focus-visible:border-primary/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 transition-colors duration-150 ease-in-out",
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
