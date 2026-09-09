/**
 * The live agent workspace: one implementation, two places.
 *
 * This is what the New carousel screen becomes once a run is under way - the
 * conversation, the asset rail, and a composer that turns into a Stop button
 * while the agents work. The task page's Chat tab renders the SAME component,
 * because "open the chat for this task" should give you the screen you were
 * just looking at, not a read-only transcript of it.
 *
 * Two variants, differing only in chrome:
 *
 * * `standalone` - /new. Owns the page: its own header, a full-height scroll
 *   pane, and a composer docked to the bottom of the viewport.
 * * `embedded` - the Chat tab. The task page already supplies a header and its
 *   own scrolling, so the composer sits inline at the end of the conversation
 *   and the assets run underneath rather than in a side rail.
 *
 * What is NOT a variant: which data it reads, how hard it polls, or what the
 * Stop button does. Those come from `useRunWorkspace` and are identical, which
 * is the whole point - the two screens cannot drift into disagreeing about the
 * state of one task.
 */

import * as React from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Link, useNavigate } from "react-router"
import { ListTree, PanelRightOpen, Pencil, Plus } from "lucide-react"
import { toast } from "sonner"

import { AgentAssetRail, AgentAssetStrip } from "@/components/agent/agent-assets"
import { AgentComposer, type ComposerState } from "@/components/agent/agent-composer"
import { AgentConversation } from "@/components/agent/agent-conversation"
import { ChatSkeleton } from "@/components/layout/route-skeleton"
import { Skeleton } from "@/components/ui/skeleton"
import { InlineEdit } from "@/components/ui/inline-edit"
import { useRenameRun } from "@/hooks/use-rename-run"
import { ChatScrollContext, useChatTail } from "@/hooks/use-chat-tail"
import { useRailPanel } from "@/hooks/use-rail-panel"
import { invalidateRun, type RunWorkspace } from "@/hooks/use-run-workspace"
import { ApiError, post } from "@/lib/api"
import { AGENT_LABELS, PHASE_LABELS } from "@/lib/pipeline"
import type { RunDetail, RunEvent } from "@/lib/types"

function activeAgentLabel(events: RunEvent[]): string {
  for (let index = events.length - 1; index >= 0; index--) {
    const author = events[index].author
    if (author && author !== "user" && author !== "carousel_orchestrator") {
      return AGENT_LABELS[author] ?? author.replaceAll("_", " ")
    }
  }
  return "Preparing your carousel"
}

export function activityLabel(run: RunDetail, events: RunEvent[]): string {
  if (run.status === "awaiting_review") return "Your carousel is ready for review"
  if (run.status === "done") return "Carousel published"
  if (run.status === "cancelled") return "Task stopped"
  if (run.status === "failed") return "The carousel agent stopped"
  if (run.status === "interrupted") return "The background task was interrupted"
  const agent = activeAgentLabel(events)
  return `${agent} · ${(PHASE_LABELS[run.phase] ?? run.phase).toLowerCase()}`
}

/**
 * Which affordance the composer offers.
 *
 * Derived from the run rather than from local state, so a task decided on
 * another device lands on the right control here without a reload. `cancelled`
 * is grouped with the finished states rather than with `failed`: stopping a
 * task on purpose is not a failure, and the difference is what the button
 * beneath it says.
 */
export function composerStateFor(
  workspace: RunWorkspace,
  /** The transcript is still a skeleton - see `loading` in the composer. */
  booting: boolean,
  /** This tab created this run seconds ago, so it is running by construction. */
  justStarted = false,
): ComposerState {
  const { run, isLive } = workspace
  if (run.isError) return "failed"

  // A run this tab just created does not need the server to confirm that it
  // is running - we made it. The seconds before the first response are
  // exactly when someone realises they typed the wrong thing, so Stop is live
  // from the first frame, which is what it was always meant to be. What it
  // was NOT meant to cover is the case below.
  if (justStarted && (run.isLoading || !run.data)) return "starting"

  // Nothing is known yet, so nothing is offered. `loading`, NOT `starting`:
  // both mean "a request is in flight", but they are opposite situations.
  // `starting` is a run this tab just launched, and its Stop button is the
  // whole point of it. This is someone opening a chat that may well have
  // finished last week, and offering to stop that is offering an action which
  // cannot apply - the old code mapped one onto the other, so merely opening
  // a finished chat put a live Stop on screen for as long as the page loaded.
  if (run.isLoading || !run.data) return "loading"

  // Checked BEFORE `booting`, deliberately. The status arrives well before the
  // transcript does, and a task we now know is running is one the agents are
  // already spending money on - which is exactly the moment someone realises
  // they typed the wrong thing. Stop is live from here even though the chat
  // behind it is still a skeleton.
  if (isLive) return "running"

  // Known, and not running. There is nothing to stop and nothing useful to
  // send until the transcript says which of the finished states this is.
  if (booting) return "loading"
  // `pending_review`, not the status: it is the authoritative "is a decision
  // still wanted?" flag, and it is NOT monotonic - a failed resume restores
  // the pending row. This is the only state where a typed message has a
  // paused tool call to answer, so it has to be read from the flag that
  // actually tracks one.
  if (run.data.pending_review) return "review"
  return ["awaiting_review", "done", "cancelled"].includes(run.data.status)
    ? "complete"
    : "failed"
}

/**
 * The chat's name, renameable in place.
 *
 * A task is named by the pipeline the moment it starts, from whatever was
 * typed or fetched, and those names are serviceable rather than yours - "the
 * new viral news in AI" is what you asked for, not what you would call it a
 * week later when you are looking for it in a list of forty.
 *
 * Editing happens where the name is, at the size it is displayed, rather than
 * in a dialog: what you type is what will be there. The heading keeps its
 * exact type while being edited for the same reason.
 *
 * Only for a task that exists. There is nothing to rename before one has been
 * started, and the empty composer's heading is a greeting, not a name.
 */
function ChatTitle({
  runId,
  run,
  isError,
}: {
  runId: string | null
  run: RunDetail | undefined
  isError: boolean
}) {
  const rename = useRenameRun()
  const [editing, setEditing] = React.useState(false)

  const shown = run?.title || run?.news.title || "Task unavailable"
  // Small, and in the interface font rather than the display serif.
  //
  // This used to be a 31px Georgia headline, which made sense when it sat in
  // a tall bar of its own. Without the bar it is a label on a row of controls,
  // and the same name is already the highlighted row in the sidebar two
  // inches to the left - so restating it in the largest type on the screen was
  // spending the page's most valuable line on something the user just clicked.
  const typography = "truncate text-sm font-semibold tracking-tight"

  // Nothing is known yet: hold the line's space and say nothing. Falling back
  // to "New carousel" here was the bug - it named a task that already has a
  // name, for as long as the request took.
  if (!run) {
    if (isError) return <h1 className={typography}>Task unavailable</h1>
    return <Skeleton className="h-5 w-64 max-w-full" />
  }
  if (!runId) return <h1 className={typography}>{shown}</h1>

  if (editing) {
    return (
      <h1 className={typography}>
        <InlineEdit
          value={run.title ?? ""}
          placeholder={run.news.title || runId}
          label="Rename this chat"
          onCommit={(next) => {
            if (next.trim() !== (run.title ?? "").trim()) {
              rename.mutate({ runId, title: next })
            }
            setEditing(false)
          }}
          onCancel={() => setEditing(false)}
          className="w-full"
        />
      </h1>
    )
  }

  return (
    <h1 className={typography}>
      <button
        type="button"
        onClick={() => setEditing(true)}
        title="Rename this chat"
        className="group/title inline-flex max-w-full items-center gap-2 text-left"
      >
        <span className="truncate">{shown}</span>
        <Pencil
          aria-hidden
          className="size-4 shrink-0 text-[var(--muted-foreground)] opacity-0 transition-opacity group-hover/title:opacity-100 group-focus-visible/title:opacity-100 [@media(hover:none)]:opacity-60"
        />
        <span className="sr-only">Rename this chat</span>
      </button>
    </h1>
  )
}

export function AgentWorkspace({
  runId,
  workspace,
  prompt,
  justStarted = false,
  variant = "standalone",
  onReset,
}: {
  runId: string
  workspace: RunWorkspace
  /** What the person typed, when this browser is the one that typed it. */
  prompt?: string
  /** This tab started this run, so Stop is live before the first response. */
  justStarted?: boolean
  variant?: "standalone" | "embedded"
  /** Clear the workspace and go back to an empty composer. */
  onReset?: () => void
}) {
  const { run, artifacts, stream, isLive } = workspace
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const embedded = variant === "embedded"

  const cancel = useMutation({
    mutationFn: () => post(`/api/runs/${runId}/cancel`),
    onSuccess: () => {
      // Everything a stop changes, re-read together. Invalidating only the run
      // snapshot left the trace pulsing on a task that had already stopped.
      invalidateRun(queryClient, runId)
      toast.success("Stopping", { description: "The agents are being cancelled." })
    },
    onError: (error) => {
      const code = error instanceof ApiError ? error.code : undefined
      if (code === "not_running") {
        // The task ended between the click and the request arriving.
        toast.info("Already stopped", { description: "That task is no longer running." })
        invalidateRun(queryClient, runId)
        return
      }
      toast.error(error instanceof Error ? error.message : "Could not stop the task.")
    },
  })

  /**
   * Nothing real goes on screen until everything real is here.
   *
   * "Everything" is the run AND its trace. The run alone gives the title and
   * the status, but the body of the page is the transcript - so rendering as
   * soon as the run lands produced a titled, empty chat that then filled in,
   * which is two loading states in a row rather than one.
   *
   * Artifacts are deliberately NOT waited on. That endpoint 404s for most of a
   * run's life by design - the carousel is only assembled at the end - so
   * waiting for it would hold the skeleton up for fifteen minutes on a task
   * that is working perfectly.
   */
  const booting = !run.isError && (!run.data || !stream.synced)

  // Declared before the composer's state reads it, so the bar and the
  // transcript cannot disagree about whether the chat has arrived. It used to
  // be a second copy of the same expression, which is how two things that are
  // meant to agree start not agreeing.
  const state = composerStateFor(workspace, booting, justStarted)

  /**
   * What happens to a message typed after the agents have stopped.
   *
   * Two destinations, and which one it is depends entirely on whether the
   * pipeline still has a paused step to answer.
   *
   * **Waiting on review.** The orchestrator is suspended inside
   * `await_human_review`. Rejecting with feedback answers that call, and the
   * rework phase runs `learner` and `feedback_router` over the text before
   * re-running only the agents it names. So a sentence typed here genuinely
   * reaches the agents and changes the carousel - it is the same channel the
   * Reject box on the review tab uses, which is why it posts the same verdict
   * rather than inventing a second path to keep in step with it.
   *
   * **Anything after that.** Published, failed or cancelled runs have no
   * suspended call left, and the root agent is a phase state machine rather
   * than a chat partner - handed text at `done` it emits its closing summary
   * and stops. So a message here starts a NEW carousel, which is what someone
   * typing a fresh topic into a finished chat means anyway.
   */
  const [followUp, setFollowUp] = React.useState("")
  /** The agent this message is addressed to, when the reviewer picked one. */
  const [target, setTarget] = React.useState<string | null>(null)

  const rework = useMutation({
    mutationFn: ({ feedback, to }: { feedback: string; to: string | null }) =>
      // `targets` is honoured exactly by the server when it is non-empty (see
      // the sanitizer in app/agents/feedback_router.py); omitted, the router
      // reads the text and decides, as it always has.
      post(`/api/runs/${runId}/verdict`, {
        status: "rejected",
        feedback,
        targets: to ? [to] : [],
      }),
    onSuccess: (_data, variables) => {
      setFollowUp("")
      setTarget(null)
      invalidateRun(queryClient, runId)
      toast.success(
        variables.to
          ? `Sent to ${AGENT_LABELS[variables.to] ?? variables.to}`
          : "Sent to the agents",
        { description: "Reworking the carousel with your notes." },
      )
    },
    onError: (error) =>
      toast.error(
        error instanceof Error ? error.message : "Could not send that.",
      ),
  })

  const startAnother = useMutation({
    mutationFn: (text: string) =>
      post<{ run_id: string; title: string }>(
        "/api/runs",
        /^https?:\/\/\S+$/i.test(text)
          ? { source: "url", url: text }
          : { source: "topic", topic: text },
      ),
    onSuccess: (data) => {
      setFollowUp("")
      void queryClient.invalidateQueries({ queryKey: ["runs"] })
      // Pushed, not replaced: the chat that was on screen is a real place the
      // user may want to go back to.
      navigate(`/new?run=${encodeURIComponent(data.run_id)}`, {
        viewTransition: true,
      })
      toast.success("Your carousel is cooking", { description: data.title })
    },
    onError: (error) =>
      toast.error(
        error instanceof Error ? error.message : "Could not start that.",
      ),
  })

  const sendFollowUp = React.useCallback(() => {
    const text = followUp.trim()
    if (text.length < 3 || rework.isPending || startAnother.isPending) return
    if (state === "review") rework.mutate({ feedback: text, to: target })
    else startAnother.mutate(text)
  }, [followUp, target, state, rework, startAnother])


  // Gated on `booting`, so the skeleton gets the whole width and the rail
  // slides in afterwards - the chat narrowing is the arrival.
  const rail = useRailPanel(!booting)

  // Open at the end of the transcript, and stay there while it is still being
  // written. Only in the workspace's own scroller - the task page puts this
  // conversation inside a tabbed screen whose scroller is the window, and
  // yanking a whole page to its bottom on arrival would take the tabs and the
  // task's header off screen with it.
  const tail = useChatTail({ ready: !booting, live: isLive })
  // Memoised, or every render hands the context a new object and re-renders
  // the whole conversation under it.
  const scrollApi = React.useMemo(() => ({ reveal: tail.reveal }), [tail.reveal])

  const conversation = (
    <>
      {booting ? (
        <ChatSkeleton />
      ) : run.isError || !run.data ? (
        <div className="rounded-[14px] border border-[var(--phase-failed)]/35 bg-[var(--phase-failed-soft)] p-4 text-sm text-[var(--phase-failed-fg)]">
          <p className="font-medium">This task could not be loaded.</p>
          <p className="mt-1 opacity-90">
            It may have been deleted.{" "}
            <button
              type="button"
              className="underline underline-offset-2"
              onClick={() => (onReset ? onReset() : navigate("/new"))}
            >
              Start a new one
            </button>
            .
          </p>
        </div>
      ) : (
        <AgentConversation
          run={run.data}
          events={stream.events}
          summary={stream.summary}
          live={isLive}
          artifacts={artifacts.data}
          runId={runId}
          prompt={prompt}
          showReviewCta={!embedded}
        />
      )}
    </>
  )

  const composer = (
    <AgentComposer
      value={followUp}
      onChange={setFollowUp}
      onSubmit={sendFollowUp}
      target={target}
      onTargetChange={setTarget}
      // Stop stays available for the whole time the agents are up, including
      // the seconds before the first snapshot arrives - a run you cannot
      // cancel because the page has not finished loading is the worst moment
      // to be told to wait.
      onStop={() => cancel.mutate()}
      state={state}
      stopping={cancel.isPending}
    />
  )

  if (embedded) {
    return (
      <div className="space-y-6">
        <div className="space-y-6">{conversation}</div>
        {!booting && (
          <AgentAssetStrip
            artifacts={artifacts.data}
            live={isLive}
            runId={runId}
            className="animate-strip-in"
          />
        )}
        {composer}
      </div>
    )
  }

  return (
    <div className="agent-workspace-grid" data-rail={rail.open ? "open" : "closed"}>
      <section className="agent-conversation-pane">
        <header className="agent-workspace-header">
          {/* The title alone. The activity line that used to sit under it -
              "Your carousel is ready for review", "Connecting to the carousel
              agent" - is not lost: the conversation below says all three of
              those things already, in its own status row, its loader and its
              error card. In the header it was a second copy of whatever the
              top of the transcript was saying. */}
          <div className="min-w-0">
            <ChatTitle runId={runId} run={run.data} isError={run.isError} />
          </div>

          <div className="flex shrink-0 items-center gap-1">
            <NewChatButton />
            {/* The trace, named as the trace.
                
                This was a three-dot overflow control, which promises "more
                options" and delivered one destination. The same icon the task
                page uses for its Trace tab says where the click goes before
                it is made, and `?tab=trace` opens on that tab rather than on
                whichever one the task's status would have chosen. */}
            <Link
              to={`/tasks/${runId}?tab=trace`}
              viewTransition
              className="grid size-9 shrink-0 place-items-center rounded-[10px] text-[var(--muted-foreground)] hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
              title="Open the agent trace"
            >
              <ListTree className="size-4.5" />
              <span className="sr-only">Open the agent trace</span>
            </Link>
            {/* Only while the panel is shut, and last in the row - the
                rightmost control is the one nearest the edge the panel comes
                back from. Open, this lives inside the rail beside its own
                heading, so the control is always attached to the thing it
                acts on. Keyed off the PREFERENCE rather than whether the rail
                is currently showing, so it does not blink into the header for
                the second a chat spends loading. */}
            {!rail.preferred && (
              <button
                type="button"
                onClick={rail.toggle}
                className="rail-toggle size-9 shrink-0 place-items-center rounded-[10px] text-[var(--muted-foreground)] hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
                title="Show the assets panel"
              >
                <PanelRightOpen className="size-4.5" />
                <span className="sr-only">Show the assets panel</span>
              </button>
            )}
          </div>
        </header>

        <div ref={tail.scrollRef} className="agent-conversation-scroll">
          <div
            ref={tail.contentRef}
            className="mx-auto w-full max-w-3xl space-y-6 px-5 pb-44 pt-6 sm:px-8 sm:pt-9"
          >
            {/* Only here. The task page renders the same conversation inside
                a tabbed screen with no floating bar and the window as its
                scroller, so there is nothing for this to be about there. */}
            <ChatScrollContext.Provider value={scrollApi}>
              {conversation}
            </ChatScrollContext.Provider>
          </div>
        </div>

        <div className="agent-running-composer-dock">
          <div className="mx-auto w-full max-w-3xl px-4 pb-4 sm:px-8">{composer}</div>
        </div>
      </section>

      {/* Always mounted, because the grid track animates to its width and
          cannot animate to the width of something absent. What changes is the
          track: 0 while the chat is still a skeleton or the panel is shut,
          15rem when it is open - so the conversation widens back over the
          space rather than leaving a hole. */}
      <AgentAssetRail
        artifacts={artifacts.data}
        loading={artifacts.isLoading}
        live={isLive}
        runId={runId}
        hidden={!rail.open}
        onCollapse={rail.toggle}
      />
    </div>
  )
}

/**
 * Start another carousel without giving up this one.
 *
 * Several runs can be in flight at once, so this is not "abandon and restart" -
 * the task you are looking at keeps working, keeps streaming, and stays in
 * Tasks. It just stops being the thing on screen.
 */
export function NewChatButton({
  className,
  showLabel = false,
}: {
  className?: string
  showLabel?: boolean
}) {
  const navigate = useNavigate()
  return (
    <button
      type="button"
      onClick={() => navigate("/new", { viewTransition: true })}
      className={
        className ??
        "inline-flex h-9 shrink-0 items-center gap-1.5 rounded-[10px] border border-[var(--border)] px-2.5 text-[13px] font-medium text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
      }
      title="Start another carousel - this one keeps running"
    >
      <Plus className="size-4" />
      <span className={showLabel ? "inline" : "hidden sm:inline"}>New chat</span>
      {!showLabel && <span className="sr-only sm:hidden">New chat</span>}
    </button>
  )
}
