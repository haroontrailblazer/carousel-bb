import { ExternalLink, WifiOff } from "lucide-react"

import { NewChatButton } from "@/components/agent/agent-workspace"
import { TaskActions } from "@/components/run/task-actions"
import { elapsed, relativeTime } from "@/lib/format"
import { PHASE_LABELS, STATUS_LABELS, STATUS_TOKEN } from "@/lib/pipeline"
import type { RunDetail } from "@/lib/types"
import { cn } from "@/lib/utils"

export function TaskHeader({
  data,
  live,
  stale,
  compact = false,
}: {
  data: RunDetail
  live: boolean
  stale: boolean
  /** Use the shorter production-workbench header on viewport-fitted screens. */
  compact?: boolean
}) {
  const statusTone = STATUS_TOKEN[data.status]

  return (
    <header className={cn(compact ? "space-y-2" : "space-y-5")}>
      <div
        className={cn(
          "flex flex-wrap items-start justify-between",
          compact ? "gap-3" : "gap-5",
        )}
      >
        <h1
          className={cn(
            "max-w-2xl font-semibold tracking-[-0.035em]",
            compact
              ? "text-xl leading-tight sm:text-2xl"
              : "text-[1.9rem] leading-[1.08] sm:text-4xl",
          )}
        >
          {data.title || data.news.title || data.run_id}
        </h1>
        <div
          className={cn(
            "flex shrink-0 items-center gap-2 [&_button]:rounded-[10px]",
            compact ? "[&_button]:h-8" : "[&_button]:h-10",
          )}
        >
          {/* Several carousels can run at once, so starting another does not
              cost you this one - it keeps working and stays in Tasks. */}
          <NewChatButton showLabel />
          <TaskActions
            runId={data.run_id}
            status={data.status}
            title={data.title}
          />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[13px] text-[var(--muted-foreground)] sm:gap-x-0 sm:[&>*+*]:ml-3 sm:[&>*+*]:border-l sm:[&>*+*]:border-[var(--border)] sm:[&>*+*]:pl-3">
        <span
          className="inline-flex items-center gap-1.5 font-medium"
          style={{ color: `var(--phase-${statusTone}-fg)` }}
        >
          <span
            aria-hidden
            className={cn("size-2 rounded-full", live && "animate-pip-pulse")}
            style={{ backgroundColor: `var(--phase-${statusTone})` }}
          />
          {STATUS_LABELS[data.status]}
        </span>
        <span
          className="font-medium"
          style={{ color: `var(--phase-${data.phase}-fg)` }}
        >
          {PHASE_LABELS[data.phase] ?? data.phase}
        </span>
        {data.rework_round > 0 && <span>Rework round {data.rework_round}</span>}
        <span className="font-mono">{data.run_id}</span>
        <span>Started {relativeTime(data.created_at)}</span>
        <span>{elapsed(data.created_at, live ? null : data.updated_at)}</span>
        {data.requested_by && <span>By {data.requested_by}</span>}
        {data.news.source_url && (
          <a
            className="inline-flex items-center gap-1 text-[var(--link)] hover:underline"
            href={data.news.source_url}
            target="_blank"
            rel="noreferrer"
          >
            source <ExternalLink className="size-3" />
          </a>
        )}
        {/* Only when the view has genuinely lost track of the run. A finished
            task has no stream to connect to, so this follows the stale flag
            rather than treating a closed connection as an error. */}
        {stale && (
          <span
            title="Could not reach the server for the latest trace; retrying."
            className="inline-flex items-center gap-1 font-medium"
            style={{ color: "var(--phase-failed-fg)" }}
          >
            <WifiOff className="size-3" /> Reconnecting
          </span>
        )}
      </div>
    </header>
  )
}
