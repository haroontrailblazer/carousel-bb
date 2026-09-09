/**
 * Each screen as its own downloadable chunk, in one place.
 *
 * Two things need these importers and they must be the SAME importers, or the
 * point is lost. The router uses them to load a screen when it is navigated
 * to; the sidebar calls them on hover to have that download already finished
 * by the time the click lands. A module resolves once and caches, so the
 * second call is free - but only if it is literally the same `import()`
 * expression, which is why they live here rather than being written out twice.
 *
 * They are in their own module, not in the router, because the sidebar is
 * rendered BY the router: importing one from the other closes a cycle.
 *
 * Splitting these out is what took the first download from a single 830 KB
 * bundle - the whole console, including the trace renderer and the carousel
 * viewer, before the sign-in box could paint - to the shell plus whichever
 * one screen was actually asked for.
 */

/** Route path -> the module that renders it. */
const CHUNKS = {
  "/new": () => import("@/routes/new-run"),
  "/newsroom": () => import("@/routes/newsroom"),
  "/tasks": () => import("@/routes/history"),
  "/profile": () => import("@/routes/profile"),
  "/reset-password": () => import("@/routes/reset-password"),
} as const

export type PrefetchablePath = keyof typeof CHUNKS

export const newRunChunk = CHUNKS["/new"]
export const newsroomChunk = CHUNKS["/newsroom"]
export const historyChunk = CHUNKS["/tasks"]
export const profileChunk = CHUNKS["/profile"]
export const resetPasswordChunk = CHUNKS["/reset-password"]
export const runDetailChunk = () => import("@/routes/run-detail")

/**
 * Start downloading a screen before it is asked for.
 *
 * Deliberately silent on failure: this is speculative work, and a chunk that
 * fails to prefetch will simply be fetched again - and reported properly -
 * when the user actually navigates. An unhandled rejection here would be a
 * console error for something that went right.
 */
export function prefetchRouteChunk(path: string): void {
  const load = CHUNKS[path as PrefetchablePath]
  if (!load) return
  void load().catch(() => undefined)
}

/**
 * Screens most people open next, in the order they usually open them.
 *
 * Not every route: prefetching all of them during the first idle window would
 * put the bundle back together by the back door, which is the thing splitting
 * it was for. These three are the sidebar's nav, so they are one tap from
 * anywhere.
 */
const LIKELY_NEXT: PrefetchablePath[] = ["/tasks", "/newsroom", "/new"]

/**
 * True when the browser or the user has asked us not to spend their data.
 *
 * The Network Information API is Chromium-only and absent everywhere else, so
 * the absence of an opinion is treated as permission - which is the same
 * assumption every other request in the app already makes.
 */
function shouldConserveData(): boolean {
  const connection = (
    navigator as Navigator & {
      connection?: { saveData?: boolean; effectiveType?: string }
    }
  ).connection
  if (!connection) return false
  if (connection.saveData) return true
  return connection.effectiveType === "slow-2g" || connection.effectiveType === "2g"
}

/**
 * Quietly download the screens the user is most likely to open next, once the
 * browser has nothing better to do.
 *
 * Hovering a link already prefetches it, and on a desktop that is enough -
 * `pointerenter` fires a beat before the click. On a phone there is no hover
 * at all: the pointer event and the tap arrive together, so the download
 * starts at the moment the user is already waiting for it. This closes that
 * gap, which is precisely where switching screens felt slowest.
 *
 * `requestIdleCallback` is the whole safety argument. It runs only when the
 * main thread is genuinely free, so this can never compete with the first
 * paint, with the screen the user actually asked for, or with a live run's
 * trace. Where it is unimplemented (Safari, historically) this does nothing
 * rather than guessing at a timeout - the hover path still covers that case,
 * and Safari is a browser on devices where data is most likely to be metered.
 *
 * Returns a cleanup that cancels a callback which has not fired yet.
 */
export function prefetchLikelyRoutes(currentPath: string): () => void {
  const idle = (
    window as Window & {
      requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number
      cancelIdleCallback?: (handle: number) => void
    }
  )
  if (!idle.requestIdleCallback || shouldConserveData()) return () => undefined

  const handle = idle.requestIdleCallback(
    () => {
      for (const path of LIKELY_NEXT) {
        // The screen already on display is already downloaded.
        if (path === currentPath) continue
        prefetchRouteChunk(path)
      }
    },
    // If the machine never goes idle, do it anyway after two seconds - by then
    // the current screen has long since painted.
    { timeout: 2_000 },
  )
  return () => idle.cancelIdleCallback?.(handle)
}
