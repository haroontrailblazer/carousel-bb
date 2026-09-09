import { cn } from "@/lib/utils"

/**
 * The one waiting indicator, so every wait in the app looks like the same
 * wait.
 *
 * It was written out inline in three places with three slightly different
 * sizes, which read as three different kinds of loading rather than one.
 * `animate-spin-slow` is already disabled under prefers-reduced-motion in
 * index.css, so a still ring is the reduced-motion state - visible, but not
 * moving.
 */
export function Spinner({
  className,
  label = "Loading",
}: {
  className?: string
  /** Announced to screen readers. Pass null for a purely decorative one. */
  label?: string | null
}) {
  return (
    <div
      className={cn(
        "size-6 shrink-0 animate-spin-slow rounded-full border-2",
        "border-[var(--border)] border-t-[var(--brand)]",
        className,
      )}
      role={label ? "status" : undefined}
      aria-label={label ?? undefined}
      aria-hidden={label ? undefined : true}
    />
  )
}
