import * as React from "react"

import { cn } from "@/lib/utils"

const field =
  "w-full rounded-[var(--radius-md)] border border-[var(--input)] bg-[var(--card)] " +
  "px-3 py-2 text-sm text-[var(--foreground)] placeholder:text-[var(--muted-foreground)] " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] " +
  "focus-visible:border-transparent disabled:opacity-50"

export const Input = React.forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input ref={ref} className={cn(field, "h-10", className)} {...props} />
))
Input.displayName = "Input"

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea ref={ref} className={cn(field, "resize-none", className)} {...props} />
))
Textarea.displayName = "Textarea"

export function Label({
  className,
  ...props
}: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn("text-sm font-medium text-[var(--foreground)]", className)}
      {...props}
    />
  )
}
