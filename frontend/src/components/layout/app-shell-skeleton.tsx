import { BrandLogo } from "@/components/layout/brand-logo"
import {
  RouteSkeleton,
  isFullHeightRoute,
} from "@/components/layout/route-skeleton"
import { Skeleton } from "@/components/ui/skeleton"

/**
 * The app, drawn but not yet filled in.
 *
 * Shown while the session is still being confirmed, in place of a spinner
 * centred on an otherwise empty page. Both wait exactly as long; this one
 * spends the wait telling the user the app arrived and is laying itself out,
 * rather than that something is happening somewhere.
 *
 * The rule this has to obey: **it runs no hooks and issues no requests.** It
 * is deliberately not the real `AppShell` with placeholder content, because
 * the real shell mounts the sidebar, and the sidebar asks who is signed in and
 * what the pipeline is doing. Firing those before the session is confirmed
 * means a burst of requests that 401 when it turns out nobody is signed in -
 * and a 401 is a hard navigation to the sign-in screen, so a soft redirect
 * would have become a full page reload. Static markup cannot do that.
 *
 * It is only rendered when this browser has been signed in before (see
 * `hadSession` in use-auth). A first-time visitor gets the spinner: showing
 * them a console they are about to be redirected away from would be a worse
 * lie than showing them nothing.
 *
 * The middle of it is the placeholder for THE SCREEN BEING OPENED, not a
 * generic one. It used to draw a task list whatever the URL said, so
 * reloading the browser on a chat showed a list of cards, which then vanished
 * and was replaced by the chat's own placeholder - the app appeared to load
 * the wrong page first. `RouteSkeleton` is the same component the screen
 * itself renders while its data lands, so now the shape goes up once and
 * stays until the real thing replaces it.
 */
export function AppShellSkeleton({
  pathname = "/",
  search = "",
}: {
  pathname?: string
  search?: string
}) {
  // The chat and the composer own the viewport; every other screen sits in a
  // padded column. Getting this wrong is not cosmetic - it is the difference
  // between the composer being centred and it jumping down the page when the
  // real screen mounts.
  const full = isFullHeightRoute(pathname)
  return (
    <div className="min-h-dvh bg-[var(--background)]">
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-[17rem] border-r border-[var(--border)] bg-[var(--card)] md:block">
        <div className="flex h-full flex-col gap-1 p-3">
          {/* The brand row is real. It needs nothing from the network and it
              is the part that makes the wait read as this app rather than as
              a generic loading screen. */}
          <div className="mb-2 flex items-center gap-2.5 border-b border-[var(--border)] px-2 py-3">
            <BrandLogo className="size-[2.84625rem]" />
            <span className="min-w-0">
              <span className="block truncate text-sm font-semibold leading-tight">
                Carousel Factory
              </span>
              <span className="block truncate text-xs text-[var(--muted-foreground)]">
                News to Instagram
              </span>
            </span>
          </div>

          <div className="flex flex-col gap-0.5">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>

          <div className="mt-auto border-t border-[var(--border)] pt-2">
            <Skeleton className="h-11" />
          </div>
        </div>
      </aside>

      <div className="md:pl-[17rem]">
        {/* The same two wrappers `AppShell` picks between, so the screen does
            not change container the moment it mounts. */}
        <main
          className={
            full
              ? "agent-main"
              : "mx-auto max-w-5xl px-4 pb-8 pt-14 md:px-8 md:py-8"
          }
        >
          <RouteSkeleton pathname={pathname} search={search} />
        </main>
      </div>
    </div>
  )
}
