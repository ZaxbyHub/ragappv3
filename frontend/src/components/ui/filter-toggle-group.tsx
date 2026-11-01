"use client"

import * as React from "react"

import { cn } from "@/lib/utils"

// role="group" of aria-pressed filter buttons — the accessible replacement for
// trigger-only Radix Tabs filter bars (#865). Those bars rendered TabsTrigger
// elements with no TabsContent, so every trigger's aria-controls pointed at a
// panel id that never existed in the DOM (axe aria-valid-attr-value, critical).
// A control that re-filters a single list is a toggle group per the ARIA
// Authoring Practices, not a tab set; the visual treatment mirrors the Tabs
// strip it replaces (same bg-muted surface, active pill via aria-pressed
// variants instead of Radix data-state). Keyboard model: plain buttons — every
// filter is its own tab stop (no roving arrows).
const FilterToggleGroup = React.forwardRef<
  React.ElementRef<"div">,
  React.ComponentPropsWithoutRef<"div">
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    role="group"
    className={cn(
      // bg-muted surface: matches the Tabs strip (#777/UI-R4-11 note — the
      // muted surface keeps inactive labels readable in every theme).
      "inline-flex h-10 items-center justify-center rounded-sm bg-muted p-1 text-muted-foreground",
      className
    )}
    {...props}
  />
))
FilterToggleGroup.displayName = "FilterToggleGroup"

const FilterToggle = React.forwardRef<
  React.ElementRef<"button">,
  React.ComponentPropsWithoutRef<"button"> & {
    pressed: boolean
  }
>(({ className, pressed, ...props }, ref) => (
  <button
    type="button"
    ref={ref}
    aria-pressed={pressed}
    className={cn(
      "inline-flex items-center justify-center whitespace-nowrap rounded-sm px-3 py-1.5 text-sm font-medium ring-offset-background transition-all focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
      // hover keeps the accent-foreground-on-accent pair; the pressed state
      // reproduces the Tabs active pill (background + inset foreground ring)
      // keyed on aria-pressed instead of Radix data-state (#865).
      "hover:bg-accent hover:text-accent-foreground",
      "aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-xs aria-pressed:ring-2 aria-pressed:ring-inset aria-pressed:ring-foreground",
      className
    )}
    {...props}
  />
))
FilterToggle.displayName = "FilterToggle"

export { FilterToggleGroup, FilterToggle }
