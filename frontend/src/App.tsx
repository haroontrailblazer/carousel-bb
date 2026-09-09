import {
  MutationCache,
  QueryClient,
  QueryClientProvider,
  focusManager,
} from "@tanstack/react-query"
import { RouterProvider } from "react-router"
import { Toaster } from "sonner"

import { AuthProvider } from "@/hooks/use-auth"
import { PULSE_KEY } from "@/hooks/use-pulse"
import { router } from "@/router"

/**
 * Every successful mutation refreshes the sidebar dots.
 *
 * One rule here instead of a line in each of the dozen places that start,
 * cancel, delete, approve or fetch something - and, more importantly, one
 * that a new mutation cannot forget. Starting a task is meant to light the
 * blue dot on the click, not on the next poll.
 *
 * `queryClient` is referenced before it is declared and that is fine: this
 * only ever runs long after the module has finished evaluating.
 */
const mutationCache = new MutationCache({
  onSuccess: () => {
    void queryClient.invalidateQueries({ queryKey: PULSE_KEY })
  },
})

/**
 * Treat "the user is looking at this tab again" as the moment to revalidate.
 *
 * React Query listens for `visibilitychange` out of the box, which covers a
 * tab switch and nothing else. Two cases it misses are exactly the ones that
 * made this console show stale data:
 *
 *  - `pageshow` with `persisted` set. Going back, or reopening a laptop onto
 *    a page the browser froze into its back/forward cache, restores the whole
 *    JavaScript heap - timers, caches and all - WITHOUT firing
 *    visibilitychange. Every number on screen is then as old as the freeze.
 *  - `focus`. Some mobile browsers return from the app switcher with a focus
 *    event and a visibility state that never changed.
 *
 * This is the fix for the reported bug in its general form: a change made on
 * a phone appears on the laptop when the laptop is next looked at, rather
 * than only after a hard refresh.
 */
focusManager.setEventListener((handleFocus) => {
  if (typeof window === "undefined") return () => undefined

  // Called with NO ARGUMENT, which is load-bearing and was got wrong once.
  //
  // Passing a boolean routes into React Query's `setFocused`, which compares
  // against the focus state it is already holding and does nothing when they
  // match. That silently disables the two cases this exists for: after a
  // bfcache restore the page was never marked unfocused, and a `focus` event
  // often arrives while the document was never hidden - so `handleFocus(true)`
  // is a no-op precisely when the data is most likely to be stale. Calling it
  // with nothing notifies unconditionally and lets `isFocused()` read
  // `document.visibilityState` itself, which is also the only reading that
  // cannot drift out of step with the actual tab.
  const notify = () => handleFocus()
  const onPageShow = (event: PageTransitionEvent) => {
    if (event.persisted) notify()
  }

  window.addEventListener("visibilitychange", notify, false)
  window.addEventListener("focus", notify, false)
  window.addEventListener("pageshow", onPageShow, false)

  return () => {
    window.removeEventListener("visibilitychange", notify)
    window.removeEventListener("focus", notify)
    window.removeEventListener("pageshow", onPageShow)
  }
})

const queryClient = new QueryClient({
  mutationCache,
  defaultOptions: {
    queries: {
      // The console shows live agent runs; a stale snapshot is misleading in a
      // way that a stale product listing is not.
      staleTime: 5_000,
      retry: (failureCount, error) => {
        // Never retry an auth failure: a redirect is already in flight, and
        // retrying just delays it while firing more doomed requests.
        const status = (error as { status?: number })?.status
        if (status === 401 || status === 403 || status === 404) return false
        return failureCount < 2
      },
      refetchOnWindowFocus: true,
      // A laptop that slept through a Wi-Fi change comes back with a cache
      // full of answers from before the gap.
      refetchOnReconnect: true,
    },
  },
})

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
        {/* Top-centre: a carousel takes ~15 minutes, so the confirmation
            that one started is worth putting where the eye already is
            rather than in a corner. */}
        <Toaster
          position="top-center"
          toastOptions={{
            style: {
              background: "var(--card)",
              color: "var(--foreground)",
              border: "1px solid var(--border)",
            },
          }}
        />
      </AuthProvider>
    </QueryClientProvider>
  )
}
