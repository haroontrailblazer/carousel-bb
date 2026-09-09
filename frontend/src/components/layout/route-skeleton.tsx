import { Skeleton, SkeletonRows } from "@/components/ui/skeleton"

/**
 * One skeleton per screen, in one place, shared by everything that waits.
 *
 * The problem this solves: a screen had as many placeholders as it had things
 * to wait for, and they did not agree. Reloading the browser on a chat put up
 * three in a row - a spinner, then the console drawn with a TASK LIST in the
 * middle of it, then the chat's own placeholder - so the page appeared to load
 * something else first and then change its mind. Each was individually
 * reasonable; together they read as a bug, because they were different shapes.
 *
 * So the shape is chosen by the ROUTE and nothing else. The shell renders it
 * while the session is being confirmed, the screens render it while their own
 * data lands, and because both ask this file the same question they get the
 * same answer - one placeholder that persists across the whole wait instead of
 * three that replace each other.
 *
 * The other rule, inherited from `Skeleton`: a placeholder has to be the shape
 * of what replaces it. Every block below was measured against the real screen
 * beside it, so nothing jumps at the moment the data lands.
 */
export type RouteSkeletonKind =
  | "chat"
  | "composer"
  | "tasks"
  | "newsroom"
  | "task"
  | "profile"

/**
 * Which screen a URL is about to become.
 *
 * `/new` is two screens wearing one path: with a `?run=` it is the chat
 * workspace, without one it is the centred composer. They do not look remotely
 * alike, so the query string is part of the question.
 */
export function routeSkeletonKind(pathname: string, search = ""): RouteSkeletonKind {
  if (pathname === "/new" || pathname === "/") {
    return new URLSearchParams(search).has("run") ? "chat" : "composer"
  }
  if (pathname === "/newsroom") return "newsroom"
  if (pathname === "/profile") return "profile"
  if (pathname.startsWith("/tasks/") || pathname.startsWith("/runs/")) return "task"
  return "tasks"
}

/** Whether this screen fills the viewport instead of sitting in the page. */
export function isFullHeightRoute(pathname: string): boolean {
  return pathname === "/new" || pathname === "/"
}

/**
 * The chat, drawn but not yet filled in.
 *
 * Laid out as the conversation actually is now, top to bottom: the prompt
 * bubble and its avatar, the activity line, the one-line trace header, the
 * agent's prose, the verified facts, and the sources pill. It used to stand in
 * for a layout that no longer exists - a tall bordered card and a row of tool
 * chips - and both of those were removed from the real page, so the skeleton
 * was holding space for furniture that never arrived.
 *
 * It deliberately contains NO text. The old loading state said "Connecting to
 * the task transcript…" above a heading that read "New carousel", and both
 * were guesses: the task is already connected as far as the user is concerned
 * (they clicked a chat that exists), and its name is whatever the server is
 * about to say. Showing a wrong title and then correcting it is worse than
 * showing no title, because the wrong one is indistinguishable from a real
 * answer for as long as it is up.
 */
/**
 * The one thing a screen reader hears while a screen loads.
 *
 * On the skeletons rather than on `RouteSkeleton`, because the screens render
 * these components directly too - the chat renders `ChatSkeleton` on its own
 * while its transcript arrives. With the announcement only on the picker, that
 * stretch of the wait was silent, so the page said "Loading" and then stopped
 * saying anything while it carried on loading.
 *
 * `announce={false}` is for the one nested case: a task page holds a chat
 * inside it, and two of these would say it twice.
 */
function LoadingAnnounce() {
  return (
    <p className="sr-only" role="status">
      Loading
    </p>
  )
}

export function ChatSkeleton({ announce = true }: { announce?: boolean } = {}) {
  return (
    <div className="space-y-6">
      {announce && <LoadingAnnounce />}
      <div className="flex justify-end gap-3" aria-hidden>
        <Skeleton className="h-12 w-[62%] rounded-[16px]" />
        <Skeleton className="mt-1 size-8 shrink-0 rounded-full" />
      </div>

      <div className="space-y-5" aria-hidden>
        {/* The activity line - a mark, a label, and the running time. */}
        <div className="flex items-center gap-3 py-2">
          <Skeleton className="size-4 shrink-0 rounded-[3px]" />
          <Skeleton className="h-4 w-52" />
        </div>

        {/* The trace header, which is one line now rather than a card. */}
        <Skeleton className="h-6 w-40 rounded-[8px]" />

        {/* Two paragraphs of agent prose at the real leading. */}
        <div className="space-y-2.5">
          <Skeleton className="h-4 w-[94%]" />
          <Skeleton className="h-4 w-[88%]" />
          <Skeleton className="h-4 w-[46%]" />
        </div>
        <div className="space-y-2.5">
          <Skeleton className="h-4 w-[91%]" />
          <Skeleton className="h-4 w-[62%]" />
        </div>

        {/* Verified facts: a caption over bulleted, cited lines. */}
        <div className="space-y-2">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-4 w-[78%]" />
          <Skeleton className="h-4 w-[66%]" />
        </div>

        {/* The grouped-sources pill. */}
        <Skeleton className="h-7 w-44 rounded-full" />
      </div>
    </div>
  )
}

/**
 * The chat screen, chrome and all.
 *
 * `ChatSkeleton` above is only the conversation, because that is all the
 * workspace itself needs - it already draws the header, the scroller and the
 * docked composer around it. The shell draws none of that, so a bare
 * `ChatSkeleton` there ran edge to edge across a 1440px window while the real
 * one sits in a 48rem column, and the text visibly narrowed the moment the
 * screen mounted. This is the same frame the workspace builds, so it does not.
 *
 * `data-rail="closed"` on purpose: a chat that is still loading has the full
 * width and the asset panel arrives INTO it afterwards. Reserving the track
 * here would put a gap on screen for something that has not been asked for
 * yet. See `useRailPanel`.
 */
export function ChatWorkspaceSkeleton() {
  return (
    <div className="agent-workspace-grid" data-rail="closed">
      <section className="agent-conversation-pane">
        <header className="agent-workspace-header" aria-hidden>
          <div className="min-w-0">
            <Skeleton className="h-5 w-64 max-w-full" />
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Skeleton className="h-9 w-28 rounded-[10px]" />
            <Skeleton className="size-9 rounded-[10px]" />
          </div>
        </header>

        <div className="agent-conversation-scroll">
          <div className="mx-auto w-full max-w-3xl space-y-6 px-5 pb-44 pt-6 sm:px-8 sm:pt-9">
            <ChatSkeleton />
          </div>
        </div>

        {/* The bar is already in its thin form. A chat opened from the
            sidebar always has a task behind it, and the tall composing shape
            only ever belongs to an empty screen - holding the tall one would
            mean the bar shrank the moment the run arrived. */}
        <div className="agent-running-composer-dock" aria-hidden>
          <div className="mx-auto w-full max-w-3xl px-4 pb-4 sm:px-8">
            <Skeleton className="h-[3.6rem] rounded-[20px]" />
          </div>
        </div>
      </section>
    </div>
  )
}

/**
 * The empty composer screen: a greeting, the bar, and its suggestions.
 *
 * Centred in the viewport exactly as the real one is, so the greeting does not
 * arrive somewhere else and slide into place.
 */
export function ComposerSkeleton() {
  return (
    <div className="agent-empty-workspace">
      <LoadingAnnounce />
      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col justify-center px-4 py-12 sm:px-8">
        <div className="w-full">
          {/* The mark and the greeting, at the two type steps the real
              heading uses. Fixed sizes rather than the heading's own `em`
              maths: the placeholder has no font to inherit from. */}
          <div className="mb-7 flex items-center justify-center gap-2 sm:gap-3">
            <Skeleton className="size-[1.35rem] shrink-0 rounded-full sm:size-[3.4rem]" />
            <Skeleton className="h-5 w-56 max-w-[62%] sm:h-9 sm:w-[24rem]" />
          </div>
          <Skeleton className="h-[7.5rem] rounded-[20px]" />
          <div className="mt-4 flex flex-wrap justify-center gap-2">
            <Skeleton className="h-[2.1rem] w-40 rounded-[10px]" />
            <Skeleton className="h-[2.1rem] w-48 rounded-[10px]" />
            <Skeleton className="h-[2.1rem] w-36 rounded-[10px]" />
          </div>
          <div className="mt-5 flex justify-center">
            <Skeleton className="h-3 w-80 max-w-full" />
          </div>
        </div>
      </main>
    </div>
  )
}

/** A screen that is a title, a row of controls, and a list of cards. */
function ListSkeleton({
  controls,
  rowClassName,
}: {
  controls: React.ReactNode
  rowClassName?: string
}) {
  return (
    <div className="space-y-5">
      <LoadingAnnounce />
      <div className="flex flex-wrap items-center justify-between gap-3" aria-hidden>
        <Skeleton className="h-7 w-32" />
        <Skeleton className="h-8 w-32 rounded-[var(--radius-md)]" />
      </div>
      {controls}
      <SkeletonRows rows={4} className={rowClassName} />
    </div>
  )
}

/**
 * Tasks: the filter chips are part of the layout, so they are held too.
 *
 * Widths are written out rather than generated, because Tailwind only ships
 * the classes it can see in the source - a width built from a variable would
 * compile to nothing and every chip would collapse.
 */
const CHIP_WIDTHS = ["w-14", "w-20", "w-16", "w-[4.5rem]", "w-15"]

export function TasksSkeleton() {
  return (
    <ListSkeleton
      controls={
        <div className="flex flex-wrap gap-1.5">
          {CHIP_WIDTHS.map((width) => (
            <Skeleton key={width} className={`h-[1.65rem] rounded-[var(--radius-pill)] ${width}`} />
          ))}
        </div>
      }
    />
  )
}

/** Newsroom: no chips, and its cards are taller than a task row. */
export function NewsroomSkeleton() {
  return (
    <ListSkeleton
      controls={<Skeleton className="h-4 w-[26rem] max-w-full" />}
      rowClassName="[&>*]:h-24"
    />
  )
}

/**
 * One task: the status chips, the title, the tab bar, and the body.
 *
 * The body is a chat, because the default tab is the transcript - holding a
 * blank rectangle there meant the tallest part of the screen was the one part
 * the skeleton got wrong.
 */
export function TaskSkeleton() {
  return (
    <div className="space-y-6">
      <LoadingAnnounce />
      <header className="space-y-3" aria-hidden>
        <div className="flex flex-wrap items-center gap-2">
          <Skeleton className="h-6 w-24 rounded-full" />
          <Skeleton className="h-6 w-20 rounded-full" />
        </div>
        <Skeleton className="h-7 w-2/3" />
        <Skeleton className="h-4 w-40" />
      </header>
      <Skeleton className="h-9 w-56 rounded-[var(--radius-md)]" />
      <ChatSkeleton announce={false} />
    </div>
  )
}

/** Profile: a title over the three cards, at the heights they really are. */
const PROFILE_CARDS = ["h-52", "h-36", "h-60"]

export function ProfileSkeleton() {
  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <LoadingAnnounce />
      <Skeleton className="h-7 w-24" />
      {PROFILE_CARDS.map((height) => (
        <div
          key={height}
          className="space-y-3 rounded-[var(--radius)] border border-[var(--border)] p-5"
        >
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-3 w-56 max-w-full" />
          <Skeleton className={`rounded-[var(--radius-md)] ${height}`} />
        </div>
      ))}
    </div>
  )
}

/**
 * The placeholder for whatever screen this URL is.
 *
 * Each skeleton announces itself in words and hides its own shapes, so a
 * screen reader hears "Loading" once rather than a list of empty boxes.
 */
export function RouteSkeleton({
  pathname,
  search = "",
}: {
  pathname: string
  search?: string
}) {
  const kind = routeSkeletonKind(pathname, search)
  return (
    <>
      {kind === "chat" ? (
        <ChatWorkspaceSkeleton />
      ) : kind === "composer" ? (
        <ComposerSkeleton />
      ) : kind === "newsroom" ? (
        <NewsroomSkeleton />
      ) : kind === "task" ? (
        <TaskSkeleton />
      ) : kind === "profile" ? (
        <ProfileSkeleton />
      ) : (
        <TasksSkeleton />
      )}
    </>
  )
}
