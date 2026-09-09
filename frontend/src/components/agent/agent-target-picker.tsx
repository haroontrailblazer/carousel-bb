import * as React from "react"

import { AGENT_LABELS, REWORKABLE_AGENTS } from "@/lib/pipeline"
import { cn } from "@/lib/utils"

/**
 * Everything the composer needs to know about "/" targeting, in one hook.
 *
 * The interaction is the one people already have muscle memory for: type "/"
 * at the start of an empty message and a short list appears; keep typing to
 * narrow it; Enter or click to choose. What it produces is not text - it is a
 * target chip beside the field - because the message and the address are two
 * different things and merging them into one string is how "[cta] make it
 * shorter" ends up as literal feedback the agents have to interpret.
 *
 * Only ever offered while a task is waiting on review. That is the one moment
 * the pipeline is paused on `await_human_review` with a call to answer, so it
 * is the only moment naming an agent can do anything. Offering it afterwards
 * would be a menu that quietly does nothing.
 */
export function useTargetPicker({
  value,
  onChange,
  onPick,
  enabled,
}: {
  value: string
  onChange: (next: string) => void
  onPick: (agent: string) => void
  enabled: boolean
}) {
  // "/" only counts at the very start. Mid-sentence a slash is a slash - URLs,
  // dates and "and/or" are all things people type into feedback.
  const query = enabled && value.startsWith("/") ? value.slice(1).toLowerCase() : null
  const open = query !== null

  const matches = React.useMemo(() => {
    if (query === null) return []
    return REWORKABLE_AGENTS.filter(
      (agent) =>
        agent.slash.startsWith(query) ||
        AGENT_LABELS[agent.name]?.toLowerCase().includes(query),
    )
  }, [query])

  const [cursor, setCursor] = React.useState(0)
  React.useEffect(() => setCursor(0), [query])
  const active = matches.length ? matches[Math.min(cursor, matches.length - 1)] : null

  const choose = React.useCallback(
    (name: string) => {
      onPick(name)
      // The "/cta" was an address, not a message. Clearing it leaves the field
      // ready for what the person actually wants to say.
      onChange("")
    },
    [onPick, onChange],
  )

  /** Returns true when the key was consumed by the menu. */
  const onKeyDown = React.useCallback(
    (event: React.KeyboardEvent): boolean => {
      if (!open || !matches.length) return false
      if (event.key === "ArrowDown") {
        event.preventDefault()
        setCursor((c) => (c + 1) % matches.length)
        return true
      }
      if (event.key === "ArrowUp") {
        event.preventDefault()
        setCursor((c) => (c - 1 + matches.length) % matches.length)
        return true
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault()
        if (active) choose(active.name)
        return true
      }
      if (event.key === "Escape") {
        event.preventDefault()
        onChange("")
        return true
      }
      return false
    },
    [open, matches, active, choose, onChange],
  )

  return { open, matches, active, choose, onKeyDown }
}

/**
 * Shared shell for the menu and its empty state.
 *
 * Four fifths of the composer's width, centred over it. Spanning the bar edge
 * to edge made the menu read as part of the bar rather than as a list sitting
 * above it; inset a little on both sides, it reads as its own surface while
 * still being obviously anchored to the field it came from.
 *
 * A percentage rather than a fixed width, so it stays in proportion on a phone
 * instead of nearly filling a 390px screen. Centring is
 * `left-1/2 + -translate-x-1/2`, which needs no knowledge of the width.
 */
const MENU_BOX =
  "absolute bottom-full left-1/2 z-30 mb-2 w-4/5 -translate-x-1/2 " +
  "overflow-hidden rounded-[14px] border border-[var(--border)] " +
  "bg-[var(--card)] shadow-[var(--shadow-lift)]"

/**
 * The list itself, floated above the composer.
 *
 * Above rather than below: the composer is docked to the bottom of the
 * viewport, so a menu under it would open off-screen.
 */
export function TargetMenu({
  matches,
  active,
  onChoose,
}: {
  matches: readonly { name: string; slash: string; hint: string }[]
  active: { name: string } | null
  onChoose: (name: string) => void
}) {
  if (!matches.length) {
    return (
      <div className={cn(MENU_BOX, "p-3 text-[13px] text-[var(--muted-foreground)]")}>
        No agent by that name. Try /cover, /copy, /design or /cta.
      </div>
    )
  }

  return (
    <div
      role="listbox"
      aria-label="Send this to a specific agent"
      className={cn(MENU_BOX, "p-1")}
    >
      {matches.map((agent) => (
        <button
          key={agent.name}
          type="button"
          role="option"
          aria-selected={active?.name === agent.name}
          // Mouse down, not click: the field's blur would otherwise close this
          // before the click landed.
          onMouseDown={(event) => {
            event.preventDefault()
            onChoose(agent.name)
          }}
          className={cn(
            "flex w-full items-baseline gap-2.5 rounded-[10px] px-2.5 py-2 text-left",
            active?.name === agent.name && "bg-[var(--muted)]",
          )}
        >
          <span className="font-mono text-[12px] text-[var(--muted-foreground)]">
            /{agent.slash}
          </span>
          <span className="shrink-0 text-[13px] font-medium">
            {AGENT_LABELS[agent.name]}
          </span>
          {/* Dropped on a phone rather than truncated. Four fifths of a 390px
              screen leaves the hint about two words, and "Slide count, hoo…"
              is not a hint - the slash command and the agent's name already
              fit, and they are what the row is for. */}
          <span className="ml-auto hidden truncate text-[11px] text-[var(--muted-foreground)] sm:block">
            {agent.hint}
          </span>
        </button>
      ))}
    </div>
  )
}

/** The chosen agent, shown in the bar so the message has a visible address. */
export function TargetChip({
  name,
  onClear,
}: {
  name: string
  onClear: () => void
}) {
  return (
    <span className="ml-1 flex shrink-0 items-center gap-1.5 rounded-full bg-[var(--brand-soft)] py-1 pl-2.5 pr-1.5 text-[12px] font-medium">
      {AGENT_LABELS[name] ?? name}
      <button
        type="button"
        onClick={onClear}
        aria-label={`Stop sending this to ${AGENT_LABELS[name] ?? name}`}
        className="grid size-4 place-items-center rounded-full text-[var(--muted-foreground)] hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
      >
        <span aria-hidden className="text-[13px] leading-none">
          ×
        </span>
      </button>
    </span>
  )
}
