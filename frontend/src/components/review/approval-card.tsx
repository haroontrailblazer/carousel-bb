import * as React from "react"
import { CheckCircle2, ExternalLink, Loader2, Send, XCircle } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Chip, MutedChip } from "@/components/ui/chip"
import { Textarea } from "@/components/ui/input"
import {
  REJECT_CATEGORIES,
  AGENT_LABELS,
  PHASE_LABELS,
  isStopped,
  predictRework,
} from "@/lib/pipeline"
import type { RunDetail } from "@/lib/types"
import { cn } from "@/lib/utils"

/**
 * The decision surface.
 *
 * Deliberately asymmetric: Approve is the lime pill, Reject is a quiet ghost
 * button. Approve is the action that publishes publicly, so it is behind a
 * confirmation step - the email flow already refuses to decide anything on a
 * GET, and the console should not be laxer than the email.
 *
 * Three states, keyed on `status` rather than on guessing from a snapshot:
 *
 *   0. too early        - the pipeline has not reached review; there is
 *                         nothing to decide and nothing has been decided.
 *   1. reviewable       - status is awaiting_review: the carousel is finished
 *                         and a human is needed. Show the buttons. This does
 *                         NOT wait for the Telegram notice - the console is
 *                         where verdicts are made, and the notification is
 *                         just a notification. If it failed, say so and offer
 *                         to send it again.
 *   2. decided          - a verdict is recorded; show what it was.
 *
 * And it is not monotonic: a failed resume restores the pending row, so a run
 * can go pending -> not pending -> pending again. Which is why a pending row
 * alone is NOT enough to show the buttons - see state 1b. The buttons appear
 * only when `status` is awaiting_review, because only the review phase sets
 * that, so they cannot come back mid-rework or after a rework has died.
 */
export function ApprovalCard({
  run,
  publishConfigured,
  coverChoiceNeeded,
  onApprove,
  onReject,
  busy,
  onResend,
  resending = false,
  embedded = false,
}: {
  run: RunDetail
  publishConfigured: boolean
  /** True when the task has both covers and none has been picked yet. */
  coverChoiceNeeded: boolean
  onApprove: () => void
  onReject: (feedback: string) => void
  busy: boolean
  /** Retry the review notification. Only offered when one has failed. */
  onResend?: () => void
  resending?: boolean
  /** Render inside the review inspector instead of drawing a second card. */
  embedded?: boolean
}) {
  const [rejecting, setRejecting] = React.useState(false)
  const [feedback, setFeedback] = React.useState("")
  const [picked, setPicked] = React.useState<string[]>([])
  const [confirming, setConfirming] = React.useState(false)

  const predicted = React.useMemo(() => {
    const targets = REJECT_CATEGORIES.filter((c) => picked.includes(c.key)).flatMap(
      (c) => [...c.targets],
    )
    return predictRework(targets)
  }, [picked])

  const panelClass = embedded
    ? "rounded-none border-0 bg-transparent p-0 shadow-none"
    : "p-5"

  // --- state 2: already decided ------------------------------------------
  if (!run.pending_review && run.verdict) {
    const approved = run.verdict.status === "approved"
    return (
      <Card className={panelClass}>
        <div className="flex items-start gap-3">
          {approved ? (
            <CheckCircle2 className="mt-0.5 size-5" style={{ color: "var(--phase-done)" }} />
          ) : (
            <XCircle className="mt-0.5 size-5 text-[var(--destructive)]" />
          )}
          <div className="min-w-0 flex-1">
            <p className="font-medium">
              {approved ? "Approved" : "Rejected"}
              {run.verdict.reviewer ? ` by ${run.verdict.reviewer}` : ""}
            </p>
            {run.verdict.feedback && (
              <p className="mt-1 text-sm text-[var(--muted-foreground)]">
                “{run.verdict.feedback}”
              </p>
            )}
            {run.publish.permalink && (
              <Button size="sm" variant="secondary" className="mt-3" asChild>
                <a href={run.publish.permalink} target="_blank" rel="noreferrer">
                  View on Instagram <ExternalLink />
                </a>
              </Button>
            )}
            {run.publish.error && (
              <p
                className="mt-3 rounded-[var(--radius-md)] px-3 py-2 text-sm"
                style={{
                  background: "var(--phase-failed-soft)",
                  color: "var(--phase-failed-fg)",
                }}
              >
                Publishing failed: {run.publish.error}
              </p>
            )}
          </div>
        </div>
      </Card>
    )
  }

  // --- state 0: the pipeline has not asked for a decision yet -------------
  // The review lives beside the trace now, so this card renders from the very
  // first phase. Falling through to state 3 in "generate" would claim a
  // decision is being processed, which is simply untrue.
  const reachedReview =
    run.phase === "review" || run.phase === "publish" || run.phase === "done"
  if (!run.pending_review && !reachedReview) {
    // A task that has stopped says nothing here. The status is already stated
    // once, at the top of the page, with a dot and the word for it - and the
    // panel below is about to say "Nothing to look at yet" for the same
    // reason. Three restatements of one fact is not more informative than one.
    if (isStopped(run.status)) return null
    return (
      <Card className={panelClass}>
        <div className="flex items-start gap-3">
          <Loader2 className="mt-0.5 size-4 animate-spin-slow text-[var(--muted-foreground)]" />
          <div className="min-w-0 flex-1">
            <p className="font-medium">Not ready for review yet</p>
            <p className="mt-1 text-sm text-[var(--muted-foreground)]">
              {`Still ${(PHASE_LABELS[run.phase] ?? run.phase).toLowerCase()}. `}
              Approve and reject appear here the moment the carousel is
              assembled and sent for review.
            </p>
          </div>
        </div>
      </Card>
    )
  }

  // NOTE: there is no longer a "a decision is being processed" state.
  //
  // It existed because a run could be decided on the standalone Telegram
  // approval pages, so a missing pending row genuinely might mean "someone
  // else just decided this". Those pages are deleted; every verdict is made
  // HERE now. What a missing pending row actually means today is that the
  // dispatcher has not paused yet - it is still sending the notification, or
  // the send failed - and in both cases the carousel is finished and
  // reviewable, so the honest thing to show is the buttons.
  //
  // Concurrency is still handled, just not by guessing from a snapshot: two
  // people deciding at once is caught by the atomic claim, and the loser gets
  // a 409 the panel reports as "already decided".

  // --- state 1b: the pending row is back, but nobody is waiting on you ----
  //
  // A failed resume RESTORES the pending_reviews row so a verdict can be
  // re-submitted. That is right for the database and wrong for this card: on
  // its own it put "Waiting for your review" and Approve & publish back on
  // screen for a run whose rework had just died. Approving there would have
  // published a carousel the pipeline never finished reworking.
  //
  // `status` is the authority on whether a human is actually being waited
  // for - it is derived from the phase machine, and only the review phase
  // sets awaiting_review. During rework it is running; after a failed rework,
  // failed or interrupted.
  //
  // `reachedReview` is the second half of that authority, and it is not
  // redundant. A real task sat for a day showing "The rework stopped before
  // finishing" while its finished carousel waited at phase `review`: the run
  // had paused for a human, but the pause was recorded as `interrupted`, so
  // this branch fired on a status that was simply wrong. Phase is the harder
  // fact - reaching `review` means QA passed and the bundle is assembled - so
  // a task that got there is reviewable whatever its status column says, and
  // telling someone otherwise sends them to Resume a task that only needed a
  // decision.
  if (
    run.pending_review &&
    run.status !== "awaiting_review" &&
    !run.notice_failed &&
    !reachedReview
  ) {
    // Same again: a rework that died is reported by the status at the top of
    // the page, and this branch's whole job is to keep Approve & publish OFF
    // screen - which it does by returning nothing at all. The card that used
    // to sit here ("The rework stopped before finishing / Nothing to approve
    // yet") only repeated the header, one paragraph lower and louder.
    if (isStopped(run.status)) return null
    return (
      <Card className={panelClass}>
        <div className="flex items-start gap-3">
          <Loader2 className="mt-0.5 size-4 animate-spin-slow text-[var(--muted-foreground)]" />
          <div className="min-w-0">
            <p className="font-medium">Reworking your feedback</p>
            <p className="mt-1 text-sm text-[var(--muted-foreground)]">
              The agents are applying your feedback. This comes back for review
              - here and on Telegram - once they finish.
            </p>
          </div>
        </div>
      </Card>
    )
  }

  // --- state 1: waiting for a human --------------------------------------
  return (
    <Card className={panelClass}>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Chip tone="review" dot pulse>
          Waiting for your review
        </Chip>
        {run.review_round > 1 && <MutedChip>Round {run.review_round}</MutedChip>}
        {run.qa.passed === true && <Chip tone="qa">QA passed</Chip>}
      </div>

      {run.notice_failed && (
        <div
          className="mb-4 flex flex-wrap items-center gap-3 rounded-[var(--radius-md)] px-3 py-2 text-sm"
          style={{
            background: "var(--phase-failed-soft)",
            color: "var(--phase-failed-fg)",
          }}
        >
          <span className="min-w-0 flex-1">
            Telegram was not notified — the message failed to send. Nothing is
            wrong with the carousel; you can decide it right here.
          </span>
          {onResend && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy || resending}
              onClick={onResend}
            >
              <Send /> {resending ? "Sending…" : "Send again"}
            </Button>
          )}
        </div>
      )}

      {!publishConfigured && (
        <p
          className="mb-4 rounded-[var(--radius-md)] px-3 py-2 text-sm"
          style={{
            background: "var(--phase-review-soft)",
            color: "var(--phase-review-fg)",
          }}
        >
          Instagram is not configured (IG_USER_ID / IG_ACCESS_TOKEN). Approving
          will record the verdict, but publishing will fail.
        </p>
      )}

      {coverChoiceNeeded && (
        <p
          className="mb-4 rounded-[var(--radius-md)] px-3 py-2 text-sm"
          style={{
            background: "var(--phase-review-soft)",
            color: "var(--phase-review-fg)",
          }}
        >
          Choose a cover first — this task has both a video and an image, and
          only one can be the opening slide.
        </p>
      )}

      {!rejecting && !confirming && (
        <div
          className={cn(
            "flex gap-2",
            embedded ? "grid grid-cols-1" : "flex-wrap items-center",
          )}
        >
          <Button
            variant="brand"
            className={cn(embedded && "w-full rounded-[var(--radius-md)]")}
            onClick={() => setConfirming(true)}
            disabled={busy || coverChoiceNeeded}
            title={
              coverChoiceNeeded ? "Pick a video or image cover first" : undefined
            }
          >
            <CheckCircle2 /> Approve &amp; publish
          </Button>
          <Button
            variant={embedded ? "default" : "ghost"}
            className={cn(embedded && "w-full rounded-[var(--radius-md)]")}
            onClick={() => setRejecting(true)}
            disabled={busy}
          >
            Reject
          </Button>
        </div>
      )}

      {confirming && (
        <div className="space-y-3">
          <p className="text-sm">
            This posts the carousel to Instagram publicly. Continue?
          </p>
          <div className={cn("flex gap-2", embedded && "grid grid-cols-1")}>
            <Button
              variant="brand"
              className={cn(embedded && "w-full rounded-[var(--radius-md)]")}
              onClick={onApprove}
              disabled={busy}
            >
              {busy ? "Approving…" : "Yes, approve and publish"}
            </Button>
            <Button
              variant={embedded ? "default" : "ghost"}
              className={cn(embedded && "w-full rounded-[var(--radius-md)]")}
              onClick={() => setConfirming(false)}
              disabled={busy}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}

      {rejecting && (
        <div className="space-y-3">
          <p className="text-sm font-medium">What exactly is not good?</p>
          <div className="flex flex-wrap gap-1.5">
            {REJECT_CATEGORIES.map((category) => {
              const on = picked.includes(category.key)
              return (
                <button
                  key={category.key}
                  type="button"
                  onClick={() =>
                    setPicked((p) =>
                      on ? p.filter((k) => k !== category.key) : [...p, category.key],
                    )
                  }
                  className="rounded-[var(--radius-pill)] border px-3 py-1 text-xs transition-colors"
                  style={
                    on
                      ? {
                          background: "var(--phase-rework-soft)",
                          color: "var(--phase-rework-fg)",
                          borderColor: "transparent",
                        }
                      : { borderColor: "var(--border)", color: "var(--muted-foreground)" }
                  }
                >
                  {category.label}
                </button>
              )
            })}
          </div>

          <Textarea
            rows={3}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder="Say what is wrong, in your own words. This is routed to the agents that need to redo their work."
          />

          {predicted.length > 0 && (
            <p className="text-xs text-[var(--muted-foreground)]">
              Likely to re-run:{" "}
              {predicted.map((a) => AGENT_LABELS[a] ?? a).join(", ")}. The
              router decides for certain once it reads your feedback.
            </p>
          )}

          <div className={cn("flex gap-2", embedded && "grid grid-cols-1")}>
            <Button
              variant="destructive"
              className={cn(embedded && "w-full rounded-[var(--radius-md)]")}
              onClick={() => {
                const categories = picked.length ? `[${picked.join(", ")}] ` : ""
                onReject(`${categories}${feedback.trim()}`.trim())
              }}
              disabled={busy || feedback.trim().length < 3}
            >
              {busy ? "Sending…" : "Reject and rework"}
            </Button>
            <Button
              variant={embedded ? "default" : "ghost"}
              className={cn(embedded && "w-full rounded-[var(--radius-md)]")}
              onClick={() => setRejecting(false)}
              disabled={busy}
            >
              Cancel
            </Button>
          </div>
          <p className="text-xs text-[var(--muted-foreground)]">
            Feedback is required to reject — it is what tells the pipeline which
            parts to redo.
          </p>
        </div>
      )}
    </Card>
  )
}
