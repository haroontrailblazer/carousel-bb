import { Check, CircleAlert, LoaderCircle, WifiOff } from "lucide-react"

import { isStopped } from "@/lib/pipeline"
import type { RunStatus } from "@/lib/types"

export function AgentActivityStatus({ status, label, connected }: { status: RunStatus; label: string; connected: boolean }) {
  const live = status === "running"
  // `stopped`, not `failed`: two of the three statuses it covers are not
  // failures. Cancelling a task on purpose is not an error, and the icon here
  // is about "no more work is coming", not about blame.
  const stopped = isStopped(status)

  return (
    <p className="mt-1.5 flex min-w-0 items-center gap-2 text-xs text-[var(--muted-foreground)]">
      {live ? (
        <span className="size-1.5 shrink-0 rounded-full bg-[var(--brand)] animate-pip-pulse" />
      ) : stopped ? (
        <CircleAlert className="size-3.5 shrink-0 text-[var(--phase-failed)]" />
      ) : (
        <span className="grid size-3.5 shrink-0 place-items-center rounded-full bg-[var(--brand)] text-[var(--brand-foreground)]"><Check className="size-2.5 stroke-[3]" /></span>
      )}
      <span className="truncate">{label}</span>
      {live && !connected && (
        <span className="inline-flex shrink-0 items-center gap-1" title="History polling is keeping this view synchronized while SSE reconnects"><WifiOff className="size-3" /> polling</span>
      )}
      {live && connected && <LoaderCircle className="size-3 shrink-0 animate-spin-slow" />}
    </p>
  )
}
