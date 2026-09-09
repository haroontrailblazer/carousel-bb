import * as React from "react"
import { useQuery } from "@tanstack/react-query"
import { Check, Pencil, Search, X } from "lucide-react"
import { Link, useLocation, useSearchParams } from "react-router"

import { Dot } from "@/components/ui/dot"
import { InlineEdit } from "@/components/ui/inline-edit"
import { GlideList } from "@/components/ui/glide-list"
import { Skeleton } from "@/components/ui/skeleton"
import { useRenameRun } from "@/hooks/use-rename-run"
import { STATUS_LABELS, STATUS_TOKEN } from "@/lib/pipeline"
import { isRemembered, runsQuery } from "@/lib/queries"
import { newRunChunk } from "@/lib/route-chunks"
import type { RunSummary } from "@/lib/types"
import { cn } from "@/lib/utils"

/**
 * Where a task's conversation lives.
 *
 * One function so that the sidebar, the task page and anything added later
 * cannot disagree about it. `/new` is the chat screen; `?run=` is which chat.
 */
export function chatPath(runId: string): string {
  return `/new?run=${encodeURIComponent(runId)}`
}

/** The name to show when nobody has given the task one. */
function chatTitle(run: Pick<RunSummary, "title" | "run_id">): string {
  return run.title?.trim() || run.run_id
}

/**
 * The two states that are asking for attention right now, and nothing else.
 *
 * A halo on "published" or "failed" would be an animation next to a fact that
 * is not going to change, and once some of the pulsing dots mean "look at me"
 * and others mean "this happened", none of them mean anything.
 */
function isLive(status: RunSummary["status"]): boolean {
  return status === "running" || status === "awaiting_review"
}

/**
 * One chat: a status dot, its name, and a way to rename it.
 *
 * The row is a link and the rename is a button beside it, rather than a
 * double-click on the link itself. Double-click to rename is invisible until
 * somebody tells you about it, and on a link it fights the single click that
 * is already navigating - the first click of the pair fires the navigation
 * and the second lands on a screen that has changed underneath it.
 *
 * While renaming, the link is replaced by the input rather than wrapping it.
 * A focusable field inside an anchor is a keyboard trap: Enter submits and
 * follows the link at the same time.
 */
function ChatRow({
  run,
  active,
  editing,
  onEdit,
  onDone,
  onNavigate,
}: {
  run: RunSummary
  active: boolean
  editing: boolean
  onEdit: () => void
  onDone: () => void
  onNavigate?: () => void
}) {
  const rename = useRenameRun()

  const tone = STATUS_TOKEN[run.status]
  const label = STATUS_LABELS[run.status]

  const commit = React.useCallback(
    (next: string) => {
      const trimmed = next.trim()
      if (trimmed !== (run.title ?? "").trim()) {
        rename.mutate({ runId: run.run_id, title: trimmed })
      }
      onDone()
    },
    [run.title, run.run_id, rename, onDone],
  )

  if (editing) {
    return (
      <div className="relative z-10 flex h-9 items-center gap-2 rounded-[var(--radius-md)] bg-[var(--muted)] px-2.5">
        <Dot tone={tone} label={label} live={isLive(run.status)} />
        <InlineEdit
          value={run.title ?? ""}
          placeholder={run.run_id}
          label={`Rename ${chatTitle(run)}`}
          onCommit={commit}
          onCancel={onDone}
          className="text-sm font-medium text-[var(--foreground)]"
        />
        <button
          type="button"
          aria-label="Save name"
          onMouseDown={(event) => event.preventDefault()}
          className="grid size-6 shrink-0 place-items-center rounded-[var(--radius-sm)] text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
        >
          <Check className="size-3.5" />
        </button>
      </div>
    )
  }

  return (
    <div className="group/row relative z-10 flex h-9 items-center rounded-[var(--radius-md)]">
      <Link
        data-glide-row
        to={chatPath(run.run_id)}
        viewTransition
        onClick={onNavigate}
        onPointerEnter={() => void newRunChunk().catch(() => undefined)}
        title={chatTitle(run)}
        className={cn(
          "flex h-9 min-w-0 flex-1 items-center gap-2 rounded-[var(--radius-md)]",
          // The rename control sits ON this row, so the label has to stop
          // before it. Reserved at every width rather than only where the
          // pencil is always visible: on a desktop it appears on hover, and
          // padding that arrives with it would shove the text sideways under
          // the pointer - and truncate a name at the exact moment someone is
          // reading it to decide whether to rename it.
          "pl-2.5 pr-9",
          "text-sm font-medium transition-colors",
          active
            ? "bg-[var(--muted)] text-[var(--foreground)]"
            : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
        )}
      >
        <Dot tone={tone} label={label} live={isLive(run.status)} />
        <span className="min-w-0 flex-1 truncate">{chatTitle(run)}</span>
      </Link>

      <button
        type="button"
        aria-label={`Rename ${chatTitle(run)}`}
        onClick={onEdit}
        className={cn(
          "absolute right-1 grid size-7 place-items-center rounded-[var(--radius-sm)]",
          "text-[var(--muted-foreground)] transition-opacity hover:text-[var(--foreground)]",
          // Always visible below `md`, revealed on hover at `md` and up.
          //
          // The breakpoint is doing real work here rather than guessing at
          // screen size: below `md` this list is inside the mobile drawer,
          // and at `md` it is the desktop sidebar - two different rendered
          // surfaces, gated by exactly this breakpoint in AppShell. So the
          // rule reads "in the drawer, always; in the sidebar, on hover".
          //
          // `@media (hover: hover)` was the first attempt and is the more
          // precise question, but nothing can verify it: Chrome's remote
          // debugging protocol cannot emulate the `hover` media feature, so
          // the branch where the pencil is a phone's ONLY way to rename a
          // chat would have shipped untested.
          "opacity-100 md:opacity-0",
          "md:group-hover/row:opacity-100 md:focus-visible:opacity-100",
        )}
      >
        <Pencil className="size-3.5" />
      </button>
    </div>
  )
}

/**
 * Every task, as a list of chats.
 *
 * Reads the SAME cache entry the Tasks screen reads, through the same
 * `runsQuery()`. That is deliberate and it is what makes this free: the
 * sidebar already prefetched that list on hover, so showing it here adds no
 * request, and a rename or a new task updates both screens at once because
 * there is only one copy of the answer.
 *
 * All tasks appear, not only the ones typed into the composer. A queue or
 * scheduled run opens the same workspace - it simply has no "you typed this"
 * bubble at the top, because nobody did. Filtering them out would mean tasks
 * that exist, are working, and are nowhere in the sidebar.
 */
export function ChatList({ onNavigate }: { onNavigate?: () => void }) {
  const runs = useQuery(runsQuery())
  const location = useLocation()
  const [params] = useSearchParams()
  const openRunId = location.pathname === "/new" ? params.get("run") : null

  const [searchOpen, setSearchOpen] = React.useState(false)
  const [query, setQuery] = React.useState("")
  const [editingId, setEditingId] = React.useState<string | null>(null)
  const searchInput = React.useRef<HTMLInputElement>(null)

  React.useEffect(() => {
    if (searchOpen) searchInput.current?.focus()
  }, [searchOpen])

  const items = runs.data?.items ?? []
  const needle = query.trim().toLowerCase()
  const visible = needle
    ? items.filter(
        (run) =>
          chatTitle(run).toLowerCase().includes(needle) ||
          run.run_id.toLowerCase().includes(needle),
      )
    : items

  const closeSearch = () => {
    setSearchOpen(false)
    setQuery("")
  }

  return (
    <div className="mt-3 flex min-h-0 flex-1 flex-col">
      {/* The header and the search field occupy the same row. The field grows
          out of the search control rather than pushing the label aside, so
          nothing below it moves when it opens. */}
      <div className="relative mx-1 mb-1 h-8 shrink-0">
        <div
          aria-hidden={searchOpen}
          className={cn(
            "absolute inset-0 flex items-center px-1.5",
            "text-[11px] font-semibold uppercase tracking-wide text-[var(--muted-foreground)]",
            "transition-[opacity,transform] duration-[180ms] ease-[cubic-bezier(0.16,1,0.3,1)]",
            searchOpen
              ? "pointer-events-none -translate-x-1 opacity-0"
              : "translate-x-0 opacity-100",
          )}
        >
          Chats
        </div>

        <button
          type="button"
          aria-label="Search chats"
          aria-expanded={searchOpen}
          onClick={() => setSearchOpen(true)}
          className={cn(
            "absolute right-0 top-0 z-10 grid size-8 place-items-center rounded-[var(--radius-md)]",
            "text-[var(--muted-foreground)] transition-opacity duration-[180ms]",
            "hover:bg-[var(--muted)] hover:text-[var(--foreground)]",
            searchOpen ? "pointer-events-none opacity-0" : "opacity-100",
          )}
        >
          <Search className="size-3.5" />
        </button>

        <div
          className={cn(
            "absolute right-0 top-0 z-20 flex h-8 items-center overflow-hidden",
            "rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--background)]",
            "transition-[width,opacity] duration-[180ms] ease-[cubic-bezier(0.16,1,0.3,1)]",
            searchOpen
              ? "pointer-events-auto opacity-100"
              : "pointer-events-none opacity-0",
          )}
          style={{ width: searchOpen ? "100%" : 32 }}
        >
          <Search className="ml-2 size-3.5 shrink-0 text-[var(--muted-foreground)]" />
          <input
            ref={searchInput}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") closeSearch()
            }}
            placeholder="Search chats"
            aria-label="Search chats"
            className="ml-1.5 min-w-0 flex-1 bg-transparent text-[13px] outline-none placeholder:text-[var(--muted-foreground)]"
          />
          <button
            type="button"
            aria-label="Close chat search"
            onClick={closeSearch}
            className="grid size-8 shrink-0 place-items-center text-[var(--muted-foreground)] hover:text-[var(--foreground)]"
          >
            <X className="size-3.5" />
          </button>
        </div>
      </div>

      {/* The one scrolling region in the sidebar. Everything above and below
          it stays put, so the brand row and the account never travel with a
          long list. */}
      <div className="-mx-1 min-h-0 flex-1 overflow-y-auto px-1 [overscroll-behavior:contain]">
        {/* isRemembered: the list on screen is the snapshot from last visit
            and has not been confirmed yet. Only then - not on every
            background poll, which would be a permanent flicker. */}
        {runs.isLoading && !runs.data ? (
          <div className="space-y-1">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} className="h-9" />
            ))}
          </div>
        ) : visible.length === 0 ? (
          <p className="px-2.5 py-2 text-[13px] text-[var(--muted-foreground)]">
            {query ? "No chats found" : "No chats yet"}
          </p>
        ) : (
          <GlideList className="flex flex-col gap-px pb-2">
            {visible.map((run) => (
              <ChatRow
                key={run.run_id}
                run={run}
                active={run.run_id === openRunId}
                editing={editingId === run.run_id}
                onEdit={() => setEditingId(run.run_id)}
                onDone={() => setEditingId(null)}
                onNavigate={onNavigate}
              />
            ))}
          </GlideList>
        )}

        {isRemembered(runs) && (
          <p className="px-2.5 py-1 text-[11px] text-[var(--muted-foreground)]">
            refreshing…
          </p>
        )}
      </div>
    </div>
  )
}
