import * as React from "react"
import { useNavigation } from "react-router"

/**
 * How long a navigation may take before it is worth mentioning.
 *
 * Screens are prefetched on hover, so most navigations resolve in single-digit
 * milliseconds and this bar never appears at all. That is the point: a
 * progress bar that flashes on every click is noise, and worse than none - it
 * makes an instant app look busy. Below the threshold the user gets the new
 * screen, which is better feedback than anything we could draw.
 */
const APPEAR_AFTER_MS = 200

/**
 * A thin bar at the top of the window while a screen is being fetched.
 *
 * Driven by the router rather than by React Query on purpose. `useIsFetching`
 * would have been the obvious source and is the wrong one: the sidebar polls
 * for its status dots every four seconds, so the bar would blink more or less
 * continuously and stop meaning anything. This lights up for exactly one
 * thing - the app is fetching a screen you asked for and cannot show you yet.
 *
 * The fill is indeterminate because the real figure is unknowable: the browser
 * does not report chunk download progress. It eases toward 90% and stops,
 * which is the honest shape - it says "moving, not finished" without ever
 * claiming a completion it cannot know about. The last 10% is the swap itself.
 */
export function RouteProgress() {
  const navigation = useNavigation()
  const busy = navigation.state !== "idle"
  const [visible, setVisible] = React.useState(false)

  React.useEffect(() => {
    if (!busy) {
      setVisible(false)
      return
    }
    const timer = window.setTimeout(() => setVisible(true), APPEAR_AFTER_MS)
    return () => window.clearTimeout(timer)
  }, [busy])

  if (!visible) return null

  return (
    <div
      // aria-hidden, not a live region. A screen reader announces the new
      // page when it arrives, and a bar that narrates every navigation on the
      // way there talks over that.
      aria-hidden
      className="pointer-events-none fixed inset-x-0 top-0 z-[60] h-0.5"
    >
      <div className="route-progress-bar h-full bg-[var(--brand)]" />
    </div>
  )
}
