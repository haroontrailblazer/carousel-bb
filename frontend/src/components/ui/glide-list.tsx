import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * A list whose hover highlight slides between rows instead of blinking on and
 * off under each one.
 *
 * One element moves; the rows themselves have no background at all. That is
 * the whole trick, and it is also why this is cheap: moving a single box is a
 * `transform` on one node, where per-row `hover:bg` is a paint on whichever
 * row the pointer is over and another on the one it just left. Running down a
 * list of forty chats, the difference is one animation against eighty.
 *
 * The highlight is `translate`d and scaled rather than positioned with `top`
 * and `height`, so the browser can run it on the compositor. Animating `top`
 * would put layout on the main thread for every frame of every pointer move -
 * the exact cost this is meant to avoid.
 *
 * Rows opt in with `data-glide-row`. Anything else in the list (a header, a
 * divider, an empty state) is simply not marked and is skipped.
 */
export function GlideList({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  const container = React.useRef<HTMLDivElement>(null)
  const [box, setBox] = React.useState<{ y: number; h: number } | null>(null)

  const track = React.useCallback((event: React.SyntheticEvent) => {
    const host = container.current
    if (!host) return
    const row = (event.target as Element).closest?.("[data-glide-row]")
    if (!(row instanceof HTMLElement) || !host.contains(row)) {
      setBox(null)
      return
    }
    setBox({ y: row.offsetTop, h: row.offsetHeight })
  }, [])

  const clear = React.useCallback(() => setBox(null), [])

  return (
    <div
      ref={container}
      className={cn("relative", className)}
      onPointerMove={track}
      onPointerLeave={clear}
      // Keyboard users get the same highlight. Focus events do not bubble,
      // but their `focusin`-flavoured React counterpart does, which is what
      // makes one handler here enough for every row.
      onFocus={track}
      onBlur={clear}
    >
      {box && (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 rounded-[var(--radius-md)] bg-[var(--muted)]"
          style={{
            height: box.h,
            transform: `translateY(${box.y}px)`,
            // Only the movement is animated. Height is set directly, because
            // a row that is a different size is a different row, and easing
            // between two heights reads as the highlight stretching.
            transition: "transform 180ms cubic-bezier(0.16, 1, 0.3, 1)",
          }}
        />
      )}
      {children}
    </div>
  )
}
