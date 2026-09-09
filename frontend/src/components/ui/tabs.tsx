import * as React from "react"

import { cn } from "@/lib/utils"

export type TabItem<T extends string> = {
  value: T
  label: string
  icon?: React.ReactNode
  /** Rendered after the label - a count, or a dot that says "look here". */
  badge?: React.ReactNode
}

/**
 * A compact segmented view switch.
 *
 * Tabs, not links. Switching between the trace and the review has to keep the
 * event stream open and the trace scrolled where it was; routing between two
 * screens tore both down and rebuilt them on every switch. That is why the
 * review no longer lives at its own URL.
 *
 * Arrow keys move between the tabs, per the WAI-ARIA tabs pattern - the tab
 * strip is a single tab stop, so Tab still moves to the panel below.
 */
export function Tabs<T extends string>({
  items,
  value,
  onChange,
  label,
  className,
}: {
  items: TabItem<T>[]
  value: T
  onChange: (value: T) => void
  /** Names the strip for screen readers, e.g. "Task views". */
  label: string
  className?: string
}) {
  const refs = React.useRef<Record<string, HTMLButtonElement | null>>({})

  const move = (delta: number) => {
    const i = items.findIndex((item) => item.value === value)
    if (i < 0) return
    const next = items[(i + delta + items.length) % items.length]
    onChange(next.value)
    refs.current[next.value]?.focus()
  }

  return (
    <div
      role="tablist"
      aria-label={label}
      className={cn(
        "inline-flex items-center gap-1 rounded-[var(--radius-pill)] bg-[var(--muted)] p-1",
        className,
      )}
      onKeyDown={(e) => {
        if (e.key === "ArrowRight") {
          e.preventDefault()
          move(1)
        } else if (e.key === "ArrowLeft") {
          e.preventDefault()
          move(-1)
        }
      }}
    >
      {items.map((item) => {
        const selected = item.value === value
        return (
          <button
            key={item.value}
            ref={(el) => {
              refs.current[item.value] = el
            }}
            type="button"
            role="tab"
            id={`tab-${item.value}`}
            aria-selected={selected}
            aria-controls={`panel-${item.value}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(item.value)}
            className={cn(
              "inline-flex h-9 items-center gap-2 rounded-[var(--radius-pill)] px-4 " +
                "text-sm font-medium transition-[color,background-color,box-shadow,transform] duration-200 ease-out " +
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] " +
                "active:scale-[0.98] motion-reduce:transition-none motion-reduce:active:scale-100 " +
                "[&_svg]:size-4 [&_svg]:shrink-0",
              selected
                ? "bg-[var(--card)] text-[var(--foreground)] shadow-[var(--shadow-card)]"
                : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
            )}
          >
            {item.icon}
            {item.label}
            {item.badge}
          </button>
        )
      })}
    </div>
  )
}

/** The panel a tab controls. Kept out of the DOM entirely when not selected. */
export function TabPanel({
  value,
  selected,
  className,
  children,
}: {
  value: string
  selected: boolean
  className?: string
  children: React.ReactNode
}) {
  if (!selected) return null
  return (
    <div
      role="tabpanel"
      id={`panel-${value}`}
      aria-labelledby={`tab-${value}`}
      tabIndex={0}
      className={cn("focus-visible:outline-none", className)}
    >
      {children}
    </div>
  )
}
