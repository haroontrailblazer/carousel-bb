import * as React from "react"
import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router"

import { TaskActions } from "@/components/run/task-actions"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Chip, MutedChip } from "@/components/ui/chip"
import { SkeletonRows } from "@/components/ui/skeleton"
import { relativeTime } from "@/lib/format"
import { isRemembered, runsQuery } from "@/lib/queries"
import { PHASE_LABELS, STATUS_LABELS, STATUS_TOKEN } from "@/lib/pipeline"
import { runDetailChunk } from "@/lib/route-chunks"
import type { RunStatus } from "@/lib/types"
import { cn } from "@/lib/utils"

const FILTERS: { label: string; value: RunStatus | "all" }[] = [
  { label: "All", value: "all" },
  { label: "Needs review", value: "awaiting_review" },
  { label: "Running", value: "running" },
  { label: "Published", value: "done" },
  { label: "Interrupted", value: "interrupted" },
  { label: "Failed", value: "failed" },
]

/**
 * Task history.
 *
 * The list is fetched ONCE and filtered in the browser.
 *
 * Filtering on the server looked tidier but was the wrong trade at this size:
 * every chip click became a new React Query key, which means no cached data,
 * which means a full loading skeleton and another round trip. Against a remote
 * database that is ~0.6s locally and up to 2s over a tunnel - so clicking
 * through six filters cost six waits to re-render at most fifty rows the
 * browser already had.
 *
 * One query, one cache entry, instant chips. It also means the sidebar's
 * "needs review" badge can read the SAME cache entry instead of issuing its
 * own request on every page.
 */
export function useRuns() {
  return useQuery({
    // Query and snapshot both live in lib/queries.ts, so the sidebar can
    // prefetch EXACTLY what this page is about to ask for. A second copy of
    // these options here would prefetch a different cache entry and warm
    // nothing.
    ...runsQuery(),
    refetchInterval: (query) =>
      query.state.data?.items.some((r) =>
        ["running", "awaiting_review"].includes(r.status),
      )
        ? 15_000
        : 60_000,
  })
}


export function HistoryRoute() {
  const [filter, setFilter] = React.useState<RunStatus | "all">("all")
  const runs = useRuns()

  const all = runs.data?.items ?? []
  const items = React.useMemo(
    () => (filter === "all" ? all : all.filter((r) => r.status === filter)),
    [all, filter],
  )

  // Counts come from the same data, so each chip can show its own tally
  // without a single extra request.
  const counts = React.useMemo(() => {
    const map: Record<string, number> = { all: all.length }
    for (const run of all) map[run.status] = (map[run.status] ?? 0) + 1
    return map
  }, [all])

  const firstLoad = runs.isLoading && !runs.data

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <h1 className="text-xl font-semibold tracking-tight">Tasks</h1>
          {/* Only while the remembered list is on screen unconfirmed - not on
              every background poll, which would be a permanent flicker. */}
          {isRemembered(runs) && (
            <span className="text-xs text-[var(--muted-foreground)]">
              refreshing…
            </span>
          )}
        </div>
        <Button variant="brand" size="sm" asChild>
          <Link to="/new" viewTransition>New carousel</Link>
        </Button>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => {
          const count = counts[f.value] ?? 0
          return (
            <button
              key={f.value}
              type="button"
              onClick={() => setFilter(f.value)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-[var(--radius-pill)] border px-3 py-1 text-xs font-medium transition-colors",
                filter === f.value
                  ? "border-transparent bg-[var(--foreground)] text-[var(--background)]"
                  : "border-[var(--border)] text-[var(--muted-foreground)] hover:bg-[var(--muted)]",
                !count && f.value !== "all" && "opacity-45",
              )}
            >
              {f.label}
              {count > 0 && <span className="opacity-70">{count}</span>}
            </button>
          )
        })}
      </div>

      {firstLoad && <SkeletonRows rows={4} />}

      {!firstLoad && items.length === 0 && (
        <Card className="p-10 text-center">
          <p className="font-medium">Nothing here yet</p>
          <p className="mt-1 text-sm text-[var(--muted-foreground)]">
            {filter === "all"
              ? "Start a task and it will show up here."
              : "No tasks match that filter."}
          </p>
          {filter === "all" && (
            <Button variant="brand" className="mt-4" asChild>
              <Link to="/new" viewTransition>Make one</Link>
            </Button>
          )}
        </Card>
      )}

      <div className="space-y-2">
        {items.map((run) => (
          <Card key={run.run_id} glide className="p-4">
            <Link
              to={`/tasks/${run.run_id}`}
              viewTransition
              // Every row leads to the same screen, so the first hover
              // anywhere in the list downloads it for all of them - the
              // module resolves once and the rest are free.
              onPointerEnter={() => void runDetailChunk().catch(() => undefined)}
              className="block"
            >
              <div className="flex flex-wrap items-start gap-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">
                    {run.title || <span className="font-mono text-sm">{run.run_id}</span>}
                  </p>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <Chip
                      tone={STATUS_TOKEN[run.status]}
                      dot
                      pulse={run.status === "running"}
                    >
                      {STATUS_LABELS[run.status]}
                    </Chip>
                    <MutedChip>{PHASE_LABELS[run.phase] ?? run.phase}</MutedChip>
                    {run.source && <MutedChip>{run.source}</MutedChip>}
                    <span className="text-xs text-[var(--muted-foreground)]">
                      {relativeTime(run.created_at)}
                    </span>
                  </div>
                </div>

                {run.status === "awaiting_review" ? (
                  <Button
                    variant="brand"
                    size="sm"
                    asChild
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Link to={`/tasks/${run.run_id}?tab=review`} viewTransition>Review</Link>
                  </Button>
                ) : (
                  <TaskActions
                    runId={run.run_id}
                    status={run.status}
                    title={run.title}
                  />
                )}
              </div>
            </Link>
          </Card>
        ))}
      </div>
    </div>
  )
}
