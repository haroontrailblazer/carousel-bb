import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Hourglass } from "lucide-react"
import { toast } from "sonner"

import { ApprovalCard } from "@/components/review/approval-card"
import { CarouselViewer } from "@/components/review/carousel-viewer"
import { Card } from "@/components/ui/card"
import { ApiError, get, post } from "@/lib/api"
import { isStopped, PHASE_LABELS } from "@/lib/pipeline"
import { cn } from "@/lib/utils"
import type { CoverChoice, Meta, RunArtifacts, RunDetail } from "@/lib/types"

/**
 * The review surface: the decision card plus the carousel it is about.
 *
 * Lives beside the trace as a tab rather than at its own URL. The run snapshot
 * is passed in rather than fetched again - the task screen already polls it,
 * and `pending_review` is not monotonic, so two independent pollers would
 * disagree with each other in front of the reviewer.
 *
 * It renders in EVERY phase, including the ones with nothing to show yet. A
 * tab that appears and disappears as the pipeline moves is worse than one that
 * is honest about being empty.
 *
 * `fit` lays it out to fill one screen instead of flowing down the page: the
 * decision card keeps its height, the carousel takes whatever is left, and
 * nothing scrolls. Approving something you have to scroll away from to see is
 * how a slide with a typo gets published.
 */
export function ReviewPanel({ run, fit = false }: { run: RunDetail; fit?: boolean }) {
  const runId = run.run_id
  const queryClient = useQueryClient()
  const [coverChoice, setCoverChoice] = React.useState<CoverChoice>(null)

  const artifacts = useQuery({
    queryKey: ["artifacts", runId],
    queryFn: () => get<RunArtifacts>(`/api/runs/${runId}/artifacts`),
    // Two different clocks. Once the carousel exists, the only reason to
    // refetch is that signed URLs expire, and refetching well inside their
    // lifetime keeps a long review session from turning into a wall of broken
    // images. Before it exists this 404s, and the tab has to notice the
    // moment the slides land - the phase event that fires then invalidates
    // this key, but a running task is polled anyway in case the stream is
    // down.
    refetchInterval: (query) => {
      // Having the carousel is not a reason to stop asking for it. A rework
      // rewrites the bundle and every slide artifact, so a task that is
      // working can invalidate what is on screen at any moment - and the push
      // channel that was supposed to cover it (onPhase invalidating this key)
      // rides on the SSE stream, which carries nothing for a leg resumed by a
      // verdict. Rejecting a task and then watching this tab showed the slides
      // you had just rejected, with Approve enabled, for up to a quarter of an
      // hour.
      if (run.status === "running") return 20_000
      // Idle: the only reason left to refetch is signed-URL expiry, and the
      // URLs last an hour.
      return query.state.data ? 15 * 60_000 : false
    },
  })

  const meta = useQuery({ queryKey: ["meta"], queryFn: () => get<Meta>("/api/meta") })

  // Retrying the notification is just re-entering the review phase: the
  // dispatcher runs again in SEND_MAIL mode and sends. Resume already does
  // exactly that, so there is no second endpoint to keep in step with it.
  const resend = useMutation({
    mutationFn: () => post(`/api/runs/${runId}/resume`),
    onSuccess: () => {
      toast.success("Sending again", { description: "Retrying the Telegram notice." })
      void queryClient.invalidateQueries({ queryKey: ["run", runId] })
    },
    onError: (error) =>
      toast.error(
        error instanceof Error ? error.message : "Could not send that again.",
      ),
  })

  const decide = useMutation({
    mutationFn: (payload: { status: string; feedback: string }) =>
      post(`/api/runs/${runId}/verdict`, { ...payload, cover: coverChoice }),
    onSuccess: (_data, variables) => {
      toast.success(
        variables.status === "approved" ? "Approved — publishing" : "Rejected — reworking",
      )
      void queryClient.invalidateQueries({ queryKey: ["run", runId] })
      void queryClient.invalidateQueries({ queryKey: ["runs"] })
    },
    onError: (error) => {
      // Branch on the code, never the message. 409 here means someone decided
      // it first - almost always the same person, from Telegram. That is a
      // normal outcome, so it is reported as information, not a failure.
      const code = error instanceof ApiError ? error.code : undefined
      if (code === "not_pending") {
        toast.info("Already decided", {
          description: "This task was decided elsewhere — probably from Telegram.",
        })
        void queryClient.invalidateQueries({ queryKey: ["run", runId] })
        return
      }
      toast.error(error instanceof Error ? error.message : "Could not record that.")
    },
  })

  // Both covers present and nothing picked: approving now would publish
  // whichever the pipeline happened to order first, which is not a decision
  // anyone made.
  const cover = artifacts.data?.cover
  const coverChoiceNeeded =
    !!cover && !!cover.video?.url && !!cover.poster?.url && !coverChoice

  const stopped = isStopped(run.status)

  // A 404 here is the normal early state, not a failure: the carousel is only
  // assembled at the end of the generate phase.
  const notAssembled =
    artifacts.isError &&
    (artifacts.error instanceof ApiError ? artifacts.error.status === 404 : false)

  const approval = (
    <ApprovalCard
      run={run}
      publishConfigured={meta.data?.publish_configured ?? true}
      coverChoiceNeeded={coverChoiceNeeded}
      busy={decide.isPending}
      onApprove={() => decide.mutate({ status: "approved", feedback: "" })}
      onReject={(feedback) => decide.mutate({ status: "rejected", feedback })}
      onResend={() => resend.mutate()}
      resending={resend.isPending}
      embedded={!!artifacts.data}
    />
  )

  return (
    <div
      className={
        fit
          ? "flex min-h-0 flex-col gap-4 md:h-full"
          : "space-y-6"
      }
    >
      {!artifacts.data && (
        <div className={fit ? "shrink-0 md:max-h-[55%] md:overflow-y-auto" : undefined}>
          {approval}
        </div>
      )}

      {artifacts.isLoading && (
        <div
          className={cn(
            "animate-pulse rounded-[var(--radius)] bg-[var(--muted)]",
            fit ? "min-h-0 flex-1" : "h-96",
          )}
        />
      )}
      {notAssembled && (
        <Card className="flex items-center gap-3 p-6">
          <Hourglass className="size-5 shrink-0 text-[var(--muted-foreground)]" />
          <div>
            <p className="font-medium">Nothing to look at yet</p>
            {/* A task that has stopped gets the past tense. This is the only
                thing left on the tab for one now that the approval card
                returns nothing - and telling someone slides "appear here as
                soon as the task assembles them" about a task that is never
                going to assemble any is worse than saying nothing. */}
            <p className="text-sm text-[var(--muted-foreground)]">
              {stopped ? (
                <>No slides were assembled. The Trace tab shows how far it got.</>
              ) : (
                <>
                  The slides appear here as soon as the task assembles them
                  {run.status === "running"
                    ? ` — it is ${(PHASE_LABELS[run.phase] ?? run.phase).toLowerCase()} right now.`
                    : "."}{" "}
                  The Trace tab shows what it is doing.
                </>
              )}
            </p>
          </div>
        </Card>
      )}
      {artifacts.isError && !notAssembled && (
        <Card className="p-6 text-sm text-[var(--muted-foreground)]">
          Could not load this carousel:{" "}
          {artifacts.error instanceof Error ? artifacts.error.message : "unknown error"}
        </Card>
      )}
      {artifacts.data && (
        <CarouselViewer
          artifacts={artifacts.data}
          coverChoice={coverChoice}
          onCoverChoice={setCoverChoice}
          onExpired={() => void artifacts.refetch()}
          decision={approval}
          fit={fit}
        />
      )}
    </div>
  )
}
