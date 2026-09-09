import * as React from "react"
import { PanelLeft } from "lucide-react"
import { useLocation } from "react-router"

import { SidebarContent, SidebarDrawer } from "@/components/layout/sidebar"
import { Button } from "@/components/ui/button"
import { prefetchLikelyRoutes } from "@/lib/route-chunks"

/**
 * The signed-in layout: a fixed sidebar on desktop, a drawer on small screens.
 *
 * A sidebar rather than a top bar because the console has a persistent piece
 * of state worth keeping in view - whether anything is waiting on a human. A
 * task at review blocks the whole pipeline until someone decides, so that
 * signal belongs somewhere permanent rather than behind a click. It is a dot,
 * not a number: the count changes nothing about what you do next.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const [menuOpen, setMenuOpen] = React.useState(false)
  const location = useLocation()
  const isAgentWorkspace = location.pathname === "/new"

  // Both task views are viewport workbenches now: Review divides the width
  // between filmstrip, preview and inspector; Trace divides it between agent
  // runs and the selected pass. Key this on the route rather than `?tab=` so
  // the very first frame is already full-width before the default tab is
  // written into the URL.
  const isFittedTask = /^\/tasks\/[^/]+\/?$/.test(location.pathname)

  // Once this screen has settled, quietly fetch the ones next to it in the
  // sidebar. Hover already covers a mouse; nothing covered a thumb, which is
  // where switching screens felt slowest.
  //
  // Keyed on the path so it re-arms after each navigation: by then the
  // likely NEXT screen is a different one.
  React.useEffect(
    () => prefetchLikelyRoutes(location.pathname),
    [location.pathname],
  )

  /**
   * Put the cursor at the top of the new screen after a navigation.
   *
   * A single-page app changes the whole page without the browser knowing a
   * page changed, so nothing moves the focus ring or a screen reader's
   * position - both stay wherever they were, which after clicking a sidebar
   * link is the sidebar. The next Tab then walks the navigation again instead
   * of entering the screen that was just opened, and a screen reader announces
   * nothing at all.
   *
   * `preventScroll` because scroll position is already decided, by
   * ScrollRestoration; focusing must not fight it. The first render is skipped
   * deliberately - taking focus on page load steals it from whatever the
   * browser restored, including the address bar.
   */
  const mainRef = React.useRef<HTMLElement>(null)
  const navigated = React.useRef(false)
  React.useEffect(() => {
    if (!navigated.current) {
      navigated.current = true
      return
    }
    mainRef.current?.focus({ preventScroll: true })
  }, [location.pathname])

  return (
    <div className="min-h-dvh bg-[var(--background)]">
      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-[17rem] border-r border-[var(--border)] bg-[var(--card)] md:block">
        <SidebarContent />
      </aside>

      <SidebarDrawer open={menuOpen} onClose={() => setMenuOpen(false)} />

      <div className="md:pl-[17rem]">
        {/* No top bar on small screens - just the control that opens the
            drawer, floating over the page. The bar was spending 56px of a
            phone's height restating the app's name and mark, which the drawer
            it opens already shows. Fixed rather than sticky so it stays
            reachable once the page is scrolled. */}
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setMenuOpen(true)}
          className="fixed left-2 top-2 z-30 bg-[var(--background)]/80 backdrop-blur md:hidden"
        >
          <PanelLeft className="size-4" />
          <span className="sr-only">Open menu</span>
        </Button>

        <main
          ref={mainRef}
          // -1 so it can be focused programmatically after a navigation but
          // never lands in the Tab order itself.
          tabIndex={-1}
          className={
            (isAgentWorkspace
              ? "agent-main"
              : isFittedTask
                ? "fitted-main flex w-full max-w-none flex-col px-4 pb-8 pt-14 md:px-5 md:py-4"
                : "mx-auto max-w-5xl px-4 pb-8 pt-14 md:px-8 md:py-8") +
            // The focus is a position, not a selection - it should not draw a
            // ring around the entire page.
            " outline-none"
          }
        >
          {children}
        </main>
      </div>
    </div>
  )
}
