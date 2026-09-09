"use client"

import * as React from "react"
import * as ProgressPrimitive from "@radix-ui/react-progress"

import { cn } from "@/lib/utils"

const Progress = React.forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof ProgressPrimitive.Root> & {
    "aria-label"?: string
  }
>(({ className, value, max, "aria-label": ariaLabel, ...props }, ref) => (
  <ProgressPrimitive.Root
    ref={ref}
    className={cn(
      "relative h-4 w-full overflow-hidden rounded-full bg-secondary",
      className
    )}
    // Forward value/max to the Radix Root so the progressbar role carries the
    // real numeric state (aria-valuenow, data-state) — UI-051. `value` must be
    // passed through even when 0 so a 0% upload reads as 0, not indeterminate;
    // an absent value keeps the indeterminate state.
    value={value}
    max={max}
    aria-label={ariaLabel}
    {...props}
  >
    <ProgressPrimitive.Indicator
      className="h-full w-full flex-1 bg-primary transition-all"
      style={{ transform: `translateX(-${100 - (value || 0)}%)` }}
    />
  </ProgressPrimitive.Root>
))
Progress.displayName = ProgressPrimitive.Root.displayName

export { Progress }
