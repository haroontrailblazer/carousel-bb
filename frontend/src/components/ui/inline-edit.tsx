import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * A field that replaces a piece of text while it is being renamed.
 *
 * The parent owns the display state and decides what the trigger is; this
 * owns the part that is easy to get subtly wrong, and it is written once
 * because two copies of these rules will drift:
 *
 *  - **Blur commits.** Clicking away from a rename means you finished, not
 *    that you changed your mind. Discarding on blur loses work for anyone who
 *    types a name and then clicks the thing they were naming.
 *  - **Escape cancels, and must run before blur.** `preventDefault` then
 *    `onCancel` - if the field simply loses focus instead, the blur handler
 *    commits the text Escape was meant to throw away.
 *  - **The confirm control listens for `mousedown`, not `click`.** Blur fires
 *    first on a click, which commits and unmounts this field, so the button's
 *    own handler never runs. Pressing it feels broken exactly once and then
 *    the user stops using it.
 *  - **Select, do not just focus.** Renaming is usually replacing.
 *
 * An empty value is a legitimate result and is passed through: for a task it
 * means "drop my name and use the generated one again".
 */
export function InlineEdit({
  value,
  placeholder,
  label,
  onCommit,
  onCancel,
  className,
}: {
  value: string
  placeholder?: string
  /** Accessible name; the visible text is the value being edited. */
  label: string
  onCommit: (next: string) => void
  onCancel: () => void
  className?: string
}) {
  const [draft, setDraft] = React.useState(value)
  const input = React.useRef<HTMLInputElement>(null)
  // Guards the double-commit: Escape unmounts this, and React fires the blur
  // on the way out.
  const settled = React.useRef(false)

  React.useEffect(() => {
    const frame = requestAnimationFrame(() => input.current?.select())
    return () => cancelAnimationFrame(frame)
  }, [])

  const commit = React.useCallback(() => {
    if (settled.current) return
    settled.current = true
    onCommit(draft)
  }, [draft, onCommit])

  const cancel = React.useCallback(() => {
    if (settled.current) return
    settled.current = true
    onCancel()
  }, [onCancel])

  return (
    <input
      ref={input}
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault()
          commit()
        }
        if (event.key === "Escape") {
          event.preventDefault()
          cancel()
        }
      }}
      onBlur={commit}
      aria-label={label}
      placeholder={placeholder}
      spellCheck={false}
      className={cn(
        "min-w-0 flex-1 bg-transparent outline-none",
        "placeholder:text-[var(--muted-foreground)]",
        className,
      )}
    />
  )
}
