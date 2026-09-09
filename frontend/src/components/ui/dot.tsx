import { cn } from "@/lib/utils"

/**
 * A dot, never a number.
 *
 * The exact count does not change what anyone does next - you open the screen
 * either way - so a dot carries the SIGNAL and the screen carries the detail.
 * The number is still there on hover, for the moment someone actually wants
 * it.
 *
 * `tone` is a phase token family, so a dot means the same thing wherever it
 * appears: blue is working, orange is waiting on a person, red is stopped and
 * going nowhere on its own. The sidebar's nav counts and a chat row's status
 * therefore read as one vocabulary rather than two colour schemes that happen
 * to sit next to each other.
 *
 * `live` adds the halo. It goes on the two states that are asking for
 * attention right now - work in flight, and work blocked on a decision - and
 * never on a state that is simply true, or the animation stops meaning
 * anything.
 */
export function Dot({
  tone,
  label,
  live = false,
  className,
}: {
  tone: string
  label: string
  live?: boolean
  className?: string
}) {
  const colour = `var(--phase-${tone})`
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn("relative flex size-2 shrink-0", className)}
    >
      {live && (
        <span
          aria-hidden
          // Two elements rather than a box-shadow so the glow can be any phase
          // colour without mixing alpha into a custom property.
          className="absolute inset-0 rounded-full animate-dot-ping"
          style={{ background: colour }}
        />
      )}
      <span
        aria-hidden
        className="relative size-2 rounded-full"
        style={{ background: colour }}
      />
    </span>
  )
}
