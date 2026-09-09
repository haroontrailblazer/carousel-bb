/**
 * The two list queries, defined once so a page and a prefetch cannot disagree.
 *
 * Both are backed by a snapshot in localStorage. The database this console
 * talks to is remote - a round trip measures 0.6s to 2s from a laptop - and a
 * list of fifty rows is one round trip whatever we do to the SQL. So the fix
 * is not to make the request faster, it is to stop waiting on it before
 * showing anything: paint what this browser saw last, revalidate immediately,
 * replace when the answer lands.
 *
 * The screens say so out loud - see `isRemembered` - rather than passing off
 * a remembered list as a fresh one.
 */

import { get } from "@/lib/api"
import type { QueueResponse, RunSummary } from "@/lib/types"

export type RunsResponse = {
  items: RunSummary[]
  next_cursor?: string | null
}

export const RUNS_KEY = ["runs", "recent"] as const
export const QUEUE_KEY = ["queue"] as const

/** Bumped when a shape changes, so an old snapshot is ignored, not rendered. */
const SNAPSHOT_VERSION = "v1"

function snapshotKey(name: string): string {
  return `carousel-snapshot-${SNAPSHOT_VERSION}-${name}`
}

/**
 * Remembered responses are UNTRUSTED input: another tab, an older build or a
 * curious person could have written anything into that key. Everything here
 * checks the shape it depends on before handing it to a component that will
 * call `.map` on it.
 */
function readSnapshot<T extends { items: unknown[] }>(name: string): T | undefined {
  try {
    const raw = localStorage.getItem(snapshotKey(name))
    if (!raw) return undefined
    const parsed = JSON.parse(raw) as T
    if (!parsed || !Array.isArray(parsed.items)) return undefined
    return parsed
  } catch {
    return undefined
  }
}

function writeSnapshot(name: string, value: unknown): void {
  try {
    localStorage.setItem(snapshotKey(name), JSON.stringify(value))
  } catch {
    /* private mode or a full quota: the list still loads, just not instantly */
  }
}

/**
 * True while what is on screen is the remembered snapshot rather than an
 * answer from this session.
 *
 * `initialDataUpdatedAt: 0` is what makes this knowable: a snapshot is dated
 * to the epoch, so React Query treats it as stale (refetching at once) and
 * any real response moves the timestamp.
 */
export function isRemembered(query: {
  dataUpdatedAt: number
  isFetching: boolean
}): boolean {
  return query.dataUpdatedAt === 0 && query.isFetching
}

/** Recent tasks, filtered in the browser. See useRuns in routes/history.tsx. */
export function runsQuery() {
  return {
    queryKey: RUNS_KEY,
    queryFn: async () => {
      const data = await get<RunsResponse>("/api/runs?limit=50")
      writeSnapshot("runs", data)
      return data
    },
    initialData: () => readSnapshot<RunsResponse>("runs"),
    initialDataUpdatedAt: 0,
    staleTime: 10_000,
  }
}

/** Stories waiting in the newsroom, plus whether a feed check is running. */
export function queueQuery() {
  return {
    queryKey: QUEUE_KEY,
    queryFn: async () => {
      const data = await get<QueueResponse>("/api/queue")
      writeSnapshot("queue", data)
      return data
    },
    initialData: () => readSnapshot<QueueResponse>("queue"),
    initialDataUpdatedAt: 0,
    staleTime: 10_000,
  }
}
