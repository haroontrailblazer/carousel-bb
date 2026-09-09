import * as React from "react"

const STORAGE_KEY = "carousel-rail-open"

/**
 * Whether the asset rail is showing, and whether it may show at all yet.
 *
 * Two separate facts, deliberately, because they answer to different things:
 *
 *  - **the preference** is the user's, it persists, and closing the rail on
 *    one task must leave it closed on the next - a panel that reopens every
 *    time you navigate is not one you have closed;
 *  - **the gate** is the chat's, and it is temporary: while the conversation
 *    is still a skeleton the rail is held shut whatever the preference says,
 *    so the loading chat has the full width and the rail arrives INTO it
 *    rather than out of a gap that was reserved all along.
 *
 * The rail element stays mounted throughout either way. A grid track cannot
 * animate to the width of something that is not there, and unmounting it
 * would also throw away its scroll position every time it was closed.
 */
export function useRailPanel(gate: boolean) {
  const [preferred, setPreferred] = React.useState(() => {
    try {
      // Open unless explicitly closed: someone who has never touched the
      // control should see their carousel being built.
      return localStorage.getItem(STORAGE_KEY) !== "closed"
    } catch {
      return true
    }
  })

  const toggle = React.useCallback(() => {
    setPreferred((open) => {
      const next = !open
      try {
        localStorage.setItem(STORAGE_KEY, next ? "open" : "closed")
      } catch {
        /* private mode: it stays open for this session only */
      }
      return next
    })
  }, [])

  return { open: gate && preferred, preferred, toggle }
}
