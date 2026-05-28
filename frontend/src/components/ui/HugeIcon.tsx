import { forwardRef, type ComponentPropsWithoutRef, type ElementRef } from "react";
import { cn } from "@/lib/utils";
import { HugeiconsIcon, type IconSvgElement } from "@hugeicons/react";

interface HugeIconProps extends Omit<ComponentPropsWithoutRef<"svg">, "size" | "color"> {
  icon: IconSvgElement;
  size?: number | string;
  color?: string;
  strokeWidth?: number;
  absoluteStrokeWidth?: boolean;
}

/**
 * HugeIcon — project-styled wrapper around @hugeicons/react.
 *
 * Usage:
 * ```tsx
 * import { Home01Icon } from "@hugeicons/core-free-icons";
 * import { HugeIcon } from "@/components/ui/HugeIcon";
 *
 * <HugeIcon icon={Home01Icon} size={20} />
 * <HugeIcon icon={Home01Icon} className="text-primary" />
 * ```
 */
const HugeIcon = forwardRef<ElementRef<"svg">, HugeIconProps>(
  ({ icon, size = 24, color, strokeWidth = 2, absoluteStrokeWidth, className, ...props }, ref) => {
    return (
      <HugeiconsIcon
        ref={ref}
        icon={icon}
        size={size}
        color={color}
        strokeWidth={strokeWidth}
        absoluteStrokeWidth={absoluteStrokeWidth}
        className={cn(className)}
        {...props}
      />
    );
  }
);

HugeIcon.displayName = "HugeIcon";

export { HugeIcon };
export type { HugeIconProps };
