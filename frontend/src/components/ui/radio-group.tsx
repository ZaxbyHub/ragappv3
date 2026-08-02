import * as React from "react"

import { cn } from "@/lib/utils"

interface RadioGroupContextValue {
  value?: string
  onValueChange?: (value: string) => void
  name?: string
  disabled?: boolean
}

const RadioGroupContext = React.createContext<RadioGroupContextValue | undefined>(
  undefined
)

const useRadioGroup = () => {
  const context = React.useContext(RadioGroupContext)
  if (!context) {
    throw new Error("RadioGroupItem must be used within a RadioGroup")
  }
  return context
}

export interface RadioGroupProps
  extends React.HTMLAttributes<HTMLDivElement> {
  value?: string
  defaultValue?: string
  onValueChange?: (value: string) => void
  name?: string
  disabled?: boolean
  orientation?: "horizontal" | "vertical"
}

const RadioGroup = React.forwardRef<HTMLDivElement, RadioGroupProps>(
  (
    {
      className,
      value,
      defaultValue,
      onValueChange,
      name,
      disabled = false,
      orientation = "horizontal",
      children,
      ...props
    },
    ref
  ) => {
    // Generate a stable name using useId if not provided
    const generatedId = React.useId()
    const finalName = name || generatedId

    // Handle controlled vs uncontrolled
    const [internalValue, setInternalValue] = React.useState(defaultValue || "")
    const isControlled = value !== undefined
    const currentValue = isControlled ? value : internalValue

    const handleValueChange = React.useCallback(
      (newValue: string) => {
        if (!isControlled) {
          setInternalValue(newValue)
        }
        onValueChange?.(newValue)
      },
      [isControlled, onValueChange]
    )

    const contextValue: RadioGroupContextValue = {
      value: currentValue,
      onValueChange: handleValueChange,
      name: finalName,
      disabled,
    }

    return (
      <RadioGroupContext.Provider value={contextValue}>
        <div
          ref={ref}
          role="radiogroup"
          className={cn(
            "flex",
            orientation === "vertical" ? "flex-col" : "flex-row",
            className
          )}
          {...props}
        >
          {children}
        </div>
      </RadioGroupContext.Provider>
    )
  }
)
RadioGroup.displayName = "RadioGroup"

export interface RadioGroupItemProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
  value: string
  id?: string
  disabled?: boolean
}

const RadioGroupItem = React.forwardRef<
  HTMLInputElement,
  RadioGroupItemProps
>(({ className, value, id, disabled: itemDisabled, ...props }, ref) => {
  const context = useRadioGroup()
  const finalId = id || `radio-${context.name}-${value}`
  const isDisabled = itemDisabled !== undefined ? itemDisabled : context.disabled

  return (
    <div className="flex items-center">
      <input
        ref={ref}
        type="radio"
        id={finalId}
        name={context.name}
        value={value}
        checked={context.value === value}
        onChange={() => context.onValueChange?.(value)}
        disabled={isDisabled}
        className={cn(
          "sr-only peer",
          className
        )}
        {...props}
      />
      <div
        className={cn(
          "size-4 rounded-full border-2 border-primary ring-offset-background transition-all",
          "peer-checked:border-primary peer-checked:bg-primary",
          "peer-focus-visible:outline-none peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-focus-visible:ring-offset-2",
          "peer-disabled:opacity-50 peer-disabled:cursor-not-allowed",
          "relative"
        )}
      >
        <div
          className={cn(
            "absolute inset-0 rounded-full bg-background transition-all pointer-events-none",
            "peer-checked:inset-1"
          )}
        />
      </div>
    </div>
  )
})
RadioGroupItem.displayName = "RadioGroupItem"

export { RadioGroup, RadioGroupItem }
