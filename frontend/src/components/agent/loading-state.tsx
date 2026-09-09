import * as React from "react"

export type LoadingVariant = "Drive" | "Dots" | "Orbit" | "Surfer"

const chevron = Array.from({ length: 9 }, (_, index) => {
  const row = Math.floor(index / 3)
  const column = index % 3
  return (column + Math.abs(row - 1)) * 90
})

const ORBIT_ORDER = [0, 1, 2, 5, 8, 7, 6, 3]
const orbit = Array.from({ length: 9 }, (_, index) => {
  const position = ORBIT_ORDER.indexOf(index)
  return position === -1 ? null : position * 110
})

const PATTERNS: Record<Exclude<LoadingVariant, "Surfer">, {
  delays: (number | null)[]
  duration: number
  round: boolean
}> = {
  Drive: { delays: chevron, duration: 650, round: false },
  Dots: { delays: chevron, duration: 650, round: true },
  Orbit: { delays: orbit, duration: 950, round: false },
}

function LoaderGrid({
  delays,
  duration,
  round,
  running,
}: {
  delays: (number | null)[]
  duration: number
  round: boolean
  running: boolean
}) {
  return (
    <span aria-hidden className="agent-loading-grid">
      {delays.map((delay, index) => (
        <span
          key={index}
          className={round ? "rounded-full" : "rounded-[1px]"}
          style={{
            opacity: delay === null ? 0.07 : 0.15,
            animation: running && delay !== null
              ? `pixel-on ${duration}ms ease-in-out ${delay}ms infinite`
              : "none",
          }}
        />
      ))}
    </span>
  )
}

/**
 * Elapsed time, measured from an absolute instant rather than from mount.
 *
 * It used to count ticks into local state starting at 0, which meant the
 * number was really "how long this component has existed". Switching to
 * another tab and back unmounts and remounts it, so a task that had been
 * running for four minutes came back reading 0.1s - the only thing on the
 * page that was wrong, because everything else is derived from server state.
 *
 * Anchoring to `startedAt` makes the value a fact about the RUN, so it
 * survives remounts, route changes and reloads. The interval now only decides
 * how often the display refreshes; it no longer holds the measurement.
 * Without an anchor (the pre-run "connecting" loader has no task yet) it
 * falls back to mount time, which for that case is the correct origin.
 */
function useElapsed(running: boolean, startedAt?: string | null) {
  const origin = React.useMemo(() => {
    if (startedAt) {
      const parsed = new Date(startedAt).getTime()
      if (!Number.isNaN(parsed)) return parsed
    }
    return Date.now()
  }, [startedAt])

  const [now, setNow] = React.useState(() => Date.now())

  React.useEffect(() => {
    if (!running) return
    // Re-read the clock rather than incrementing: a background tab throttles
    // timers, and an incrementing counter would silently lose that time.
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 100)
    return () => window.clearInterval(timer)
  }, [running, origin])

  const total = Math.max(0, (now - origin) / 1000)
  if (total < 60) return `${total.toFixed(1)}s`
  return `${Math.floor(total / 60)}m ${(total % 60).toFixed(1)}s`
}

export function LoadingState({
  label,
  variant = "Drive",
  videoSrc = "/subway-surfers.mp4",
  running = true,
  outcome = "complete",
  startedAt,
}: {
  label?: string
  variant?: LoadingVariant
  videoSrc?: string
  running?: boolean
  outcome?: string
  /** When the task began. Without it the timer measures this component's age. */
  startedAt?: string | null
}) {
  const elapsed = useElapsed(running, startedAt)
  const surfer = variant === "Surfer"
  const resolvedLabel = label ?? (surfer ? "Subway surfing" : "Churning")
  const [videoOk, setVideoOk] = React.useState(true)
  const pattern = surfer ? PATTERNS.Drive : PATTERNS[variant]

  const labelElement = (
    <span className={running ? "agent-loading-label" : "text-[13px] font-medium text-[var(--muted-foreground)]"}>
      {resolvedLabel}
    </span>
  )
  const elapsedElement = (
    <span className="font-mono text-[12px] tabular-nums text-[var(--muted-foreground)]">
      {running ? elapsed : outcome}
    </span>
  )

  if (surfer) {
    return (
      <div role="status" className="flex w-fit flex-col items-start">
        <div className="flex items-center gap-2.5">
          <LoaderGrid {...pattern} running={running} />
          {labelElement}
          {elapsedElement}
        </div>

        <div className="agent-loading-video mt-2 w-56 overflow-hidden rounded-[10px] shadow-[var(--shadow-lift)]">
          <div className="relative aspect-video w-full bg-[var(--foreground)]">
            {videoOk ? (
              <video
                src={videoSrc}
                autoPlay={running}
                muted
                loop
                playsInline
                onError={() => setVideoOk(false)}
                className="h-full w-full object-cover"
              />
            ) : (
              <div className="flex h-full w-full flex-col items-center justify-center gap-1.5 text-[var(--background)]">
                <LoaderGrid {...PATTERNS.Drive} running={running} />
                <span className="px-3 text-center font-mono text-[10px] opacity-65">
                  Video unavailable
                </span>
              </div>
            )}
          </div>
        </div>
      </div>
    )
  }

  return (
    <div role="status" className="flex w-fit max-w-full items-center gap-2.5" aria-live="polite">
      <LoaderGrid {...pattern} running={running} />
      {labelElement}
      {elapsedElement}
    </div>
  )
}
