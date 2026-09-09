import { cn } from "@/lib/utils"

/**
 * A placeholder shaped like the thing that is coming.
 *
 * The rule this exists to enforce: a skeleton must match the layout it stands
 * in for. The console had a `h-64` grey rectangle standing in for a task
 * detail page and a `h-96` one for a carousel, and both did the thing a
 * skeleton is supposed to prevent - the page settled, then jumped, because
 * what arrived was not the shape that was being held. A block of roughly the
 * right height in roughly the right place costs the same and stops the jump.
 *
 * `aria-hidden` throughout: a screen reader gets the real content when it
 * lands, and reading out a scaffold in the meantime is noise. The screens
 * announce their own waiting state in words where it matters.
 *
 * The shimmer itself lives in `.skeleton` in index.css - a sweep needs a
 * pseudo-element, which utilities cannot express - and that rule carries its
 * own `prefers-reduced-motion` exception, so a new skeleton cannot be left out
 * of it by forgetting to add a class here.
 */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn(
        // `.skeleton` (index.css) draws the block and sweeps a highlight
        // across it. It is a class rather than utilities because the sweep
        // needs a pseudo-element, and it carries its own reduced-motion rule.
        "skeleton rounded-[var(--radius-md)]",
        className,
      )}
    />
  )
}

/**
 * The task-list placeholder, used both by the list itself and by anything
 * that needs to stand in for it while a screen loads.
 */
export function SkeletonRows({
  rows = 4,
  className,
}: {
  rows?: number
  className?: string
}) {
  return (
    <div className={cn("space-y-2", className)}>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton
          key={i}
          // Matches the real row: a Card at p-4 holding a title line and a
          // row of chips.
          className="h-20 rounded-[var(--radius)] border border-[var(--border)]"
        />
      ))}
    </div>
  )
}
