import * as React from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router"
import { RotateCcw, Square, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { DeleteTaskDialog } from "@/components/run/delete-task-dialog"
import { Button } from "@/components/ui/button"
import { ApiError, del, post } from "@/lib/api"
import type { RunStatus } from "@/lib/types"

/**
 * Resume / re-run / delete for a task that is not currently running.
 *
 * Which actions appear is driven by status rather than shown-and-disabled:
 *
 *   running                     -> Stop
 *   awaiting_review             -> Send again and Delete
 *   interrupted / cancelled     -> Resume and Delete
 *   failed / cancelled          -> Re-run and Delete
 *   done                        -> Delete
 *
 * No status is a dead end: every one of them offers a way forward or a way to
 * clear it away.
 *
 * Resume and Re-run both continue the SAME task - neither starts a new one -
 * and the difference is the rework budget. Resume picks the phase back up with
 * the budget as it stands. Re-run resets the round cap to zero first, which is
 * what you want when hitting that cap is why the task stopped.
 *
 * A stopped task offers both because they answer different questions: "carry
 * on from here" and "give it another go at the part that kept failing".
 *
 * One component, used in the task list, the trace page and the review page -
 * three places where "this task is stuck, do something about it" is the
 * obvious next thought, and where three separate implementations would
 * eventually disagree about what delete removes or when it is allowed.
 */
export function TaskActions({
  runId,
  status,
  title,
  size = "sm",
  onDeleted,
}: {
  runId: string
  status: RunStatus
  title?: string | null
  size?: "sm" | "default"
  /** Where to go once the task no longer exists. Defaults to the task list. */
  onDeleted?: () => void
}) {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [confirming, setConfirming] = React.useState(false)

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["runs"] })
    void queryClient.invalidateQueries({ queryKey: ["queue"] })
  }

  // awaiting_review is the one status a task can get permanently stuck in. If
  // a verdict's background resume dies before it can restore the pending
  // review, the row is already gone: no surface can decide the task, and
  // startup reconcile skips it because ACTIVE_PHASES excludes "review".
  // Re-entering the review phase re-sends the notice and writes a fresh
  // pending row, which is exactly the way out - the server has always allowed
  // it, the console simply never offered the button. Same endpoint as Resume,
  // different words, because here it means "ask me again".
  const awaitingReview = status === "awaiting_review"

  const fail = (error: unknown, fallback: string) => {
    const code = error instanceof ApiError ? error.code : undefined
    if (code === "too_many_active_runs") {
      toast.error("One at a time", {
        description: "A carousel is already being made. Wait for it to reach review.",
      })
      return
    }
    if (code === "daily_limit_reached") {
      toast.error("Daily limit reached", {
        description:
          "Too many carousels have been started in the last 24 hours. This " +
          "only blocks new ones — finishing this task is still allowed.",
      })
      return
    }
    if (code === "not_resumable") {
      toast.info("Nothing to pick up", {
        description: "That task is already running, or no longer exists.",
      })
      return
    }
    if (code === "not_running") {
      // With the backend correcting stale statuses this should be rare: it
      // now means the task genuinely already ended.
      toast.info("Already stopped", {
        description: "That task is no longer running.",
      })
      return
    }
    if (code === "run_is_active") {
      toast.error("Still running", {
        description: "Only finished tasks can be deleted.",
      })
      return
    }
    toast.error(error instanceof Error ? error.message : fallback)
  }

  const resume = useMutation({
    mutationFn: () => post(`/api/runs/${runId}/resume`),
    onSuccess: () => {
      toast.success(awaitingReview ? "Sending again" : "Resuming", {
        description: awaitingReview
          ? "Asking for the review again."
          : "Picking up where it stopped.",
      })
      refresh()
      void queryClient.invalidateQueries({ queryKey: ["run", runId] })
      void queryClient.invalidateQueries({ queryKey: ["trace", runId] })
      navigate(`/tasks/${runId}`)
    },
    onError: (e) => fail(e, "Could not resume that task."),
  })

  const rerun = useMutation({
    mutationFn: () => post<{ run_id: string }>(`/api/runs/${runId}/rerun`),
    onSuccess: (data) => {
      toast.success("Trying again", {
        description: "Same task, rework budget reset.",
      })
      refresh()
      void queryClient.invalidateQueries({ queryKey: ["run", runId] })
      void queryClient.invalidateQueries({ queryKey: ["trace", runId] })
      // Same id now - a re-run continues this task rather than opening one.
      navigate(`/tasks/${data.run_id}`)
    },
    onError: (e) => fail(e, "Could not re-run that task."),
  })

  const stop = useMutation({
    mutationFn: () => post(`/api/runs/${runId}/cancel`),
    onSuccess: () => {
      toast.success("Stopping", { description: "The agents are being cancelled." })
      refresh()
      void queryClient.invalidateQueries({ queryKey: ["run", runId] })
    },
    onError: (e) => fail(e, "Could not stop that task."),
  })

  const remove = useMutation({
    mutationFn: () => del(`/api/runs/${runId}`),
    onSuccess: () => {
      setConfirming(false)
      toast.success("Task deleted")
      refresh()
      // The task is gone, so anything showing it must stop.
      if (onDeleted) onDeleted()
      else navigate("/tasks")
    },
    onError: (e) => fail(e, "Could not delete that task."),
  })

  const canStop = status === "running"
  const canResume = status === "interrupted" || status === "cancelled" || awaitingReview
  const canRerun = status === "failed" || status === "cancelled"
  const canDelete = canResume || canRerun || status === "done"
  if (!canStop && !canResume && !canRerun && !canDelete) return null

  const busy =
    resume.isPending || rerun.isPending || remove.isPending || stop.isPending
  const swallow = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
  }

  return (
    <span className="flex items-center gap-1" onClick={swallow}>
      {canStop && (
        <Button
          variant="ghost"
          size={size}
          disabled={busy}
          title="Stop the agents now. The task can be resumed afterwards."
          onClick={(e) => {
            swallow(e)
            stop.mutate()
          }}
        >
          <Square /> {stop.isPending ? "Stopping…" : "Stop"}
        </Button>
      )}

      {canResume && (
        <Button
          variant="default"
          size={size}
          disabled={busy}
          title={
            awaitingReview
              ? "Ask for the review again — re-sends the notification"
              : "Resume from the phase it stopped in"
          }
          onClick={(e) => {
            swallow(e)
            resume.mutate()
          }}
        >
          <RotateCcw />{" "}
          {resume.isPending
            ? awaitingReview
              ? "Sending…"
              : "Resuming…"
            : awaitingReview
              ? "Send again"
              : "Resume"}
        </Button>
      )}

      {canRerun && (
        <Button
          variant="default"
          size={size}
          disabled={busy}
          title="Start a new task from the same story"
          onClick={(e) => {
            swallow(e)
            rerun.mutate()
          }}
        >
          <RotateCcw /> {rerun.isPending ? "Starting…" : "Re-run"}
        </Button>
      )}

      {canDelete && (
        <>
          <Button
            variant="ghost"
            size="icon"
            disabled={busy}
            title="Delete this task, its trace and its media"
            onClick={(e) => {
              swallow(e)
              setConfirming(true)
            }}
          >
            <Trash2 className="text-[var(--destructive)]" />
            <span className="sr-only">Delete task</span>
          </Button>

          <DeleteTaskDialog
            open={confirming}
            onOpenChange={setConfirming}
            runId={runId}
            title={title}
            busy={remove.isPending}
            onConfirm={() => remove.mutate()}
          />
        </>
      )}
    </span>
  )
}
