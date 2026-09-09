import * as React from "react"
import { AlertTriangle } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"

/**
 * Type-to-confirm before deleting a task.
 *
 * The friction is the point. Deleting removes the task, its trace, its ADK
 * session and every rendered slide from storage, and none of it comes back -
 * so the gesture that triggers it should be impossible to make by accident.
 * An inline "are you sure?" next to the button is one stray click away from
 * the button itself; a modal that will not act until you have typed the task's
 * id cannot be reached by a mis-click at all.
 *
 * The id is used rather than the title because it is unambiguous: two tasks
 * from the same story share a title, and confirming the wrong one is exactly
 * the mistake this is here to prevent.
 */
export function DeleteTaskDialog({
  open,
  onOpenChange,
  runId,
  title,
  busy,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  runId: string
  title?: string | null
  busy: boolean
  onConfirm: () => void
}) {
  const [typed, setTyped] = React.useState("")
  const matches = typed.trim() === runId

  // Clear on close, so reopening never starts pre-confirmed.
  React.useEffect(() => {
    if (!open) setTyped("")
  }, [open])

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent showClose={!busy}>
        <DialogHeader>
          <DialogTitle>Delete this task?</DialogTitle>
          <DialogDescription>
            {title ? (
              <>
                <span className="font-medium text-[var(--foreground)]">{title}</span>{" "}
                and everything it produced.
              </>
            ) : (
              "This task and everything it produced."
            )}
          </DialogDescription>
        </DialogHeader>

        <DialogBody>
          <div
            className="flex gap-2.5 rounded-[var(--radius-md)] p-3 text-sm"
            style={{
              background: "var(--phase-failed-soft)",
              color: "var(--phase-failed-fg)",
            }}
          >
            <AlertTriangle className="mt-0.5 size-4 shrink-0" />
            <div className="space-y-1">
              <p className="font-medium">This cannot be undone.</p>
              <ul className="list-inside list-disc space-y-0.5 opacity-90">
                <li>The cover video and every rendered slide</li>
                <li>The agent trace and its full transcript</li>
                <li>The review verdict and feedback</li>
              </ul>
            </div>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="confirm-id" className="block text-sm">
              Type{" "}
              <code className="rounded bg-[var(--muted)] px-1.5 py-0.5 font-mono text-[13px] font-semibold text-[var(--foreground)]">
                {runId}
              </code>{" "}
              to confirm.
            </label>
            <Input
              id="confirm-id"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={runId}
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
              disabled={busy}
              onKeyDown={(e) => {
                if (e.key === "Enter" && matches && !busy) onConfirm()
              }}
            />
          </div>
        </DialogBody>

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={onConfirm}
            disabled={!matches || busy}
            title={matches ? undefined : "Type the task id to enable this"}
          >
            {busy ? "Deleting…" : "Delete permanently"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
