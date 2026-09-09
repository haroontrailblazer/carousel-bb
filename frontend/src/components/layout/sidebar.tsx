import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"
import { NavLink, useLocation } from "react-router"
import {
  Layers,
  Newspaper,
  PanelLeftClose,
  Sparkles,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { Dot } from "@/components/ui/dot"
import { BrandLogo } from "@/components/layout/brand-logo"
import { ChatList } from "@/components/layout/chat-list"
import { UserMenu } from "@/components/layout/user-menu"
import { usePulse } from "@/hooks/use-pulse"
import { queueQuery, runsQuery } from "@/lib/queries"
import { prefetchRouteChunk } from "@/lib/route-chunks"
import { cn } from "@/lib/utils"

const NAV = [
  { to: "/new", label: "New carousel", icon: Sparkles, end: true },
  { to: "/newsroom", label: "Newsroom", icon: Newspaper, end: true },
  { to: "/tasks", label: "Tasks", icon: Layers, end: false },
] as const


function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <span className="flex items-center gap-2.5">
      {/* 2.84625rem = 45.54px. Two deliberate steps up from the original
          size-9 (36px): +15%, then +10% on that. Arbitrary values rather than
          Tailwind's 4px scale, whose steps here would be +11% and +22% - the
          scale simply has no rung at the sizes that were asked for. */}
      <BrandLogo className="size-[2.84625rem]" />
      {!compact && (
        <span className="min-w-0">
          <span className="block truncate text-sm font-semibold leading-tight">
            Carousel Factory
          </span>
          <span className="block truncate text-xs text-[var(--muted-foreground)]">
            News to Instagram
          </span>
        </span>
      )}
    </span>
  )
}

/**
 * Up to three dots on Tasks: work in flight, work waiting on a decision, and
 * work that has stopped. Left to right in pipeline order.
 *
 * Cancelled is deliberately not counted - somebody meant that, and a standing
 * red dot for a decision already taken is noise that trains people to ignore
 * the dot that matters.
 */
function TaskDots() {
  const { data } = usePulse()
  const running = data?.running ?? 0
  const review = data?.awaiting_review ?? 0
  const stopped = data?.stopped ?? 0
  if (!running && !review && !stopped) return null
  return (
    <span className="ml-auto flex shrink-0 items-center gap-1.5">
      {running > 0 && (
        <Dot tone="generate" live label={`${running} task(s) working now`} />
      )}
      {review > 0 && (
        <Dot
          tone="review"
          live
          label={`${review} task(s) waiting for your review`}
        />
      )}
      {stopped > 0 && (
        <Dot tone="failed" label={`${stopped} task(s) failed or interrupted`} />
      )}
    </span>
  )
}

/**
 * One dot for the newsroom: stories are waiting, and it glows while the feeds
 * are actually being checked.
 */
function QueueDot() {
  const { data } = usePulse()
  const queued = data?.queued ?? 0
  const fetching = !!data?.fetching
  if (!queued && !fetching) return null
  return (
    <span className="ml-auto flex shrink-0 items-center">
      <Dot
        tone="generate"
        live={fetching}
        label={
          fetching
            ? "Checking your feeds for new stories"
            : `${queued} story(ies) waiting in the newsroom`
        }
      />
    </span>
  )
}

export function SidebarContent({
  onNavigate,
  onClose,
}: {
  onNavigate?: () => void
  /** Drawer only. When given, the close control sits in the brand row. */
  onClose?: () => void
}) {
  const queryClient = useQueryClient()
  const location = useLocation()

  /**
   * "New carousel" is only current when it really is a NEW one.
   *
   * `/new` is two screens: the empty composer, and - with `?run=` - an
   * existing chat. NavLink decides `isActive` from the path alone, so opening
   * a chat from the list lit up both the chat's own row AND "New carousel",
   * which says the user is in two places at once and, worse, that the thing
   * they are reading is unsaved.
   */
  const composingNew =
    location.pathname === "/new" && !new URLSearchParams(location.search).has("run")

  /**
   * Warm a screen before it is asked for - both halves of it.
   *
   * Hovering a link is the one moment we know a screen is about to be opened,
   * and there are now two things it needs: the JavaScript that draws it, and
   * the list it draws. Screens are downloaded on demand since the bundle was
   * split, so fetching only the data would have traded one visible wait for
   * another.
   *
   * Both are safe to call on every hover. `prefetchQuery` respects staleTime,
   * so a fresh list is not re-fetched because the pointer crossed the link,
   * and a module resolves once - the second `import()` of a chunk already in
   * memory costs nothing.
   */
  const prefetch = React.useCallback(
    (to: string) => {
      prefetchRouteChunk(to)
      if (to === "/tasks") void queryClient.prefetchQuery(runsQuery())
      if (to === "/newsroom") void queryClient.prefetchQuery(queueQuery())
    },
    [queryClient],
  )


  return (
    <div className="flex h-full flex-col gap-1 p-3">
      {/* Brand row, ruled off from the navigation below it - the same rule
          the footer uses above the theme switch. On the phone the drawer's
          close control lives on this row rather than on a row of its own,
          so the mark and the name occupy the space that row was wasting. */}
      <div className="mb-2 flex items-center gap-2 border-b border-[var(--border)] px-2 py-3">
        <span className="flex min-w-0 flex-1">
          <BrandMark />
        </span>
        {onClose && (
          <Button
            variant="ghost"
            size="icon"
            className="shrink-0"
            onClick={onClose}
          >
            <PanelLeftClose className="size-4" />
            <span className="sr-only">Close menu</span>
          </Button>
        )}
      </div>

      <nav className="flex shrink-0 flex-col gap-0.5">
        {NAV.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            // Hand the swap to the browser's View Transitions API: it
            // cross-fades the old screen into the new one on the compositor,
            // and does nothing at all where it is unsupported.
            viewTransition
            onClick={onNavigate}
            // The list is already on its way by the time the click lands.
            // Pointer-enter, not mouse-enter, so a phone gets it too: it
            // fires on touch just before the tap.
            onPointerEnter={() => prefetch(to)}
            onFocus={() => prefetch(to)}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-2.5 rounded-[var(--radius-md)] px-2.5 py-2 text-sm font-medium transition-colors",
                (to === "/new" ? composingNew : isActive)
                  ? "bg-[var(--muted)] text-[var(--foreground)]"
                  : "text-[var(--muted-foreground)] hover:bg-[var(--muted)] hover:text-[var(--foreground)]",
              )
            }
          >
            <Icon className="size-4 shrink-0" />
            <span className="truncate">{label}</span>
            {to === "/tasks" && <TaskDots />}
            {to === "/newsroom" && <QueueDot />}
          </NavLink>
        ))}
      </nav>

      {/* Every task, as a chat. It reads the same cache entry Tasks reads, so
          it costs no request of its own - and a rename or a new task shows in
          both places at once because there is only one copy of the answer. */}
      <ChatList onNavigate={onNavigate} />

      {/* The theme control moved to Profile -> Appearance, where the two
          options are shown as a choice rather than a toggle whose label has
          to describe the state you are NOT in. The sidebar keeps navigation
          and the account.

          `mt-auto` is gone: the chat list above is the flexible element now,
          so the footer is pushed down by a list that grows into the space
          rather than by a spacer. */}
      <div className="shrink-0 border-t border-[var(--border)] pt-2">
        <UserMenu onNavigate={onNavigate} />
      </div>
    </div>
  )
}

/** Mobile drawer. Rendered only when open so it cannot trap focus when hidden. */
export function SidebarDrawer({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const location = useLocation()

  // Close on navigation - otherwise tapping a link on a phone leaves the
  // drawer covering the page you just asked for.
  React.useEffect(() => {
    onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname])

  React.useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 md:hidden">
      <button
        type="button"
        aria-label="Close menu"
        onClick={onClose}
        className="animate-backdrop-in absolute inset-0 bg-black/40"
      />
      <div className="animate-drawer-in absolute inset-y-0 left-0 w-64 overflow-y-auto border-r border-[var(--border)] bg-[var(--card)] shadow-[var(--shadow-lift)]">
        <SidebarContent onNavigate={onClose} onClose={onClose} />
      </div>
    </div>
  )
}
