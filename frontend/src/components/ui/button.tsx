import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * Buttons are pills, per the beautifului look.
 *
 * The `brand` variant is the lime one and it is deliberately scarce: it marks
 * the two moments that matter (approve, generate). Use it everywhere and it
 * becomes wallpaper.
 *
 * Note `text-brand-foreground` on the brand variant - that is INK, never
 * white. White on #B8EF43 is 1.36:1 and unreadable; ink is 13.2:1.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap text-sm font-medium " +
    "transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] " +
    "focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--background)] " +
    "disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0 select-none",
  {
    variants: {
      variant: {
        brand:
          "bg-[var(--brand)] text-[var(--brand-foreground)] hover:bg-[var(--brand-hover)] font-semibold shadow-sm",
        default:
          "bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] hover:bg-[var(--muted)]",
        secondary:
          "bg-[var(--secondary)] text-[var(--secondary-foreground)] hover:opacity-90",
        destructive:
          "bg-[var(--destructive)] text-[var(--destructive-foreground)] hover:opacity-90",
        ghost: "text-[var(--foreground)] hover:bg-[var(--muted)]",
        link: "text-[var(--link)] underline-offset-4 hover:underline",
      },
      size: {
        sm: "h-8 px-3 rounded-[var(--radius-pill)]",
        default: "h-10 px-5 rounded-[var(--radius-pill)]",
        lg: "h-12 px-7 text-base rounded-[var(--radius-pill)]",
        icon: "size-9 rounded-[var(--radius-pill)]",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button"
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size, className }))}
        {...props}
      />
    )
  },
)
Button.displayName = "Button"

export { buttonVariants }
