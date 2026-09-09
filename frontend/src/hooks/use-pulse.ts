import { useQuery } from "@tanstack/react-query"

import { get } from "@/lib/api"
import type { Pulse } from "@/lib/types"

/** Shared by every dot in the sidebar, and invalidated after every mutation. */
export const PULSE_KEY = ["pulse"] as const

const STORAGE_KEY = "carousel-pulse"

/**
 * The last dots this browser saw.
 *
 * A reload used to show no dots at all for a second or two - the session had
 * to resolve, then a fifty-row list had to come back from a remote database
 * before anything could be counted. Painting the last known state instantly
 * and correcting it when the answer arrives is the difference between "the
 * sidebar tells me what is happening" and "the sidebar catches up eventually".
 *
 * These are four integers about a pipeline every signed-in user shares, so
 * there is nothing here worth protecting from the next person at this
 * browser.
 */
function remembered(): Pulse | undefined {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return undefined
    const parsed = JSON.parse(raw) as Partial<Pulse>
    const num = (v: unknown) => (typeof v === "number" && v >= 0 ? v : 0)
    return {
      running: num(parsed.running),
      awaiting_review: num(parsed.awaiting_review),
      stopped: num(parsed.stopped),
      queued: num(parsed.queued),
      fetching: parsed.fetching === true,
    }
  } catch {
    // Private mode, blocked storage, or something else wrote this key. The
    // dots simply arrive with the network instead.
    return undefined
  }
}

function remember(pulse: Pulse): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pulse))
  } catch {
    /* not worth a word to the user */
  }
}

/**
 * The state of everything, as five numbers.
 *
 * One small query rather than counting the history and queue lists in the
 * browser: those are the console's two heaviest responses, and the dots are
 * the first thing on screen. This one can be polled hard and re-asked after
 * every action without costing anything.
 */
export function usePulse() {
  return useQuery({
    queryKey: PULSE_KEY,
    queryFn: async () => {
      const pulse = await get<Pulse>("/api/pulse")
      remember(pulse)
      return pulse
    },
    // Seeded from the last visit and immediately marked ancient, so the dots
    // paint on the first frame AND a fresh count is already on its way.
    initialData: remembered,
    initialDataUpdatedAt: 0,
    staleTime: 0,
    // Fast while anything is moving - this is what makes a task starting show
    // up as a dot rather than as something you notice later. Idle, it is
    // still worth asking: a decision can be made from Telegram and a cron
    // fetch can start without anyone touching this screen.
    refetchInterval: (query) => {
      const data = query.state.data
      return data && (data.running > 0 || data.fetching) ? 4_000 : 20_000
    },
    refetchIntervalInBackground: false,
  })
}
