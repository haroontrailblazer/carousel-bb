import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * A status chip in one of the phase colour families.
 *
 * The colour rule, which the CSS comments in index.css also spell out because
 * it keeps getting violated: chip TEXT uses the `-fg` step on the `-soft`
 * background (5.2:1 to 6.7:1). The solid `--phase-*` colours are for dots,
 * rails and borders only - putting white on #E56D24 gives 3.21:1 and fails AA.
 */
export function Chip({
  tone = "generate",
  dot = false,
  pulse = false,
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & {
  tone?: string
  dot?: boolean
  pulse?: boolean
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[var(--radius-pill)] px-2.5 py-1 " +
          "text-xs font-medium leading-none",
        className,
      )}
      style={{
        backgroundColor: `var(--phase-${tone}-soft)`,
        color: `var(--phase-${tone}-fg)`,
      }}
      {...props}
    >
      {dot && (
        <span
          aria-hidden
          className={cn("size-1.5 rounded-full", pulse && "animate-pip-pulse")}
          style={{ backgroundColor: `var(--phase-${tone})` }}
        />
      )}
      {children}
    </span>
  )
}

/** A neutral chip for metadata (source, counts) that carries no status. */
export function MutedChip({
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[var(--radius-pill)] " +
          "bg-[var(--muted)] px-2.5 py-1 text-xs font-medium leading-none " +
          "text-[var(--muted-foreground)]",
        className,
      )}
      {...props}
    />
  )
}
