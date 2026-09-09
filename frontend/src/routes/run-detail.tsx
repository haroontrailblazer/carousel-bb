import * as React from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router"
import {
  ArrowUpRight,
  Images,
  ListTree,
  MessagesSquare,
} from "lucide-react"

import { chatPath } from "@/components/layout/chat-list"
import { ReviewPanel } from "@/components/review/review-panel"
import { TaskHeader } from "@/components/run/task-header"
import { AgentTrace, PhaseRail } from "@/components/run/trace"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { TaskSkeleton } from "@/components/layout/route-skeleton"
import { MutedChip } from "@/components/ui/chip"
import { TabPanel, Tabs } from "@/components/ui/tabs"
import { isStopped } from "@/lib/pipeline"
import { useRunWorkspace } from "@/hooks/use-run-workspace"
import type { RunStatus } from "@/lib/types"

type TaskTab = "trace" | "review"

/**
 * Which view a task opens on.
 *
 * A task that is still working is a process to watch, so it opens on the
 * trace. A task that is waiting for a decision, or already published, is a
 * carousel to look at, so it opens on the review. Everything else - failed,
 * cancelled, interrupted - opens on the trace, because that is where the
 * explanation is.
 */
function defaultTab(status: RunStatus): TaskTab {
  return status === "awaiting_review" || status === "done" ? "review" : "trace"
}

export function RunDetailRoute() {
  const { runId = "" } = useParams()
  const navigate = useNavigate()

  // The SAME hook the New carousel screen uses. Both screens read the same
  // three cache entries, and React Query keys the cache by key alone - so two
  // sets of options for one key means whichever screen mounted last decides
  // how both behave. One hook, one answer.
  const workspace = useRunWorkspace(runId)
  const { run, stream, isLive } = workspace

  // --- which tab ----------------------------------------------------------
  // Resolved once, on arrival, then pinned. The status moves under the user -
  // a task reaches review while they are reading the trace - and yanking the
  // screen out from under someone mid-read is not helpful. The Review tab
  // pulses instead, and they switch when they are ready.
  const [params, setParams] = useSearchParams()
  const urlTab = params.get("tab")
  const [tab, setTab] = React.useState<TaskTab | null>(
    urlTab === "review" || urlTab === "trace" ? urlTab : null,
  )
  const status = run.data?.status

  // `?tab=chat` was a real view on this page until the conversation moved to
  // its own screen. Those links are in browser history and in anything already
  // shared, so they land on the chat rather than silently falling back to the
  // trace - which would look like the link had simply stopped working.
  React.useEffect(() => {
    if (urlTab === "chat" && runId) {
      navigate(chatPath(runId), { replace: true, viewTransition: true })
    }
  }, [urlTab, runId, navigate])

  const selectTab = React.useCallback(
    (next: TaskTab) => {
      setTab(next)
      // Kept in the query string rather than the path: changing the path
      // remounts this route, which would tear down and rebuild the event
      // stream on every tab click.
      setParams(
        (prev) => {
          const copy = new URLSearchParams(prev)
          copy.set("tab", next)
          return copy
        },
        { replace: true },
      )
    },
    [setParams],
  )

  // The resolved default is written into the URL, not just into state. The
  // shell reads `?tab=` to decide whether this screen scrolls or fits the
  // viewport, and a tab that only existed in a component's head left the
  // layout guessing - and a shared link pointing at a different tab than the
  // one it was copied from.
  // Layout effect, not effect: this decides whether the shell scrolls or
  // fits, and running it after the browser has painted means painting one
  // frame of the wrong layout first.
  React.useLayoutEffect(() => {
    if (tab || !status) return
    selectTab(defaultTab(status))
  }, [tab, status, selectTab])

  // Derived, not awaited. Reading the default straight out of the status
  // means the first paint is already the right tab; the effect above only
  // has to catch the URL up.
  const active: TaskTab = tab ?? (status ? defaultTab(status) : "trace")

  // Both views own the available task viewport. Their dense content scrolls
  // inside its own pane instead of turning the entire page into a document.
  const isReview = active === "review"


  if (run.isLoading) {
    // The same placeholder the shell puts up for this URL, so a reload holds
    // one shape from the first paint to the last instead of handing over
    // between two that do not match.
    return <TaskSkeleton />
  }
  if (run.isError || !run.data) {
    return (
      <Card className="p-6">
        <p className="font-medium">Task not found</p>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          It may have been removed, or the id is wrong.
        </p>
        <Button className="mt-4" variant="ghost" asChild>
          <Link to="/tasks" viewTransition>Back to tasks</Link>
        </Button>
      </Card>
    )
  }

  const data = run.data
  const live = isLive
  return (
    <div
      className={
        "flex min-h-0 flex-col gap-4 md:min-h-0 md:flex-1 md:gap-5"
      }
    >
      <TaskHeader data={data} live={live} stale={stream.stale} compact />

      {!isReview && (
        <PhaseRail phase={data.phase} live={live} stopped={isStopped(data.status)} />
      )}

      {/* NO card for a stopped task.

          The header already carries the whole fact: a coloured dot, the
          status word (Interrupted / Failed / Cancelled) and the phase it
          got to, with Resume sitting right beside them. A card underneath
          saying the same thing in a paragraph is the second half of a
          sentence nobody asked for - and on a task that was interrupted
          while awaiting review it stacked with the approval card's own
          version, so one task explained itself twice before the reader
          got to the trace. */}
      {/* Only on the trace tab: on the review tab the decision card below is
          already saying this, louder. */}
      {data.pending_review && active === "trace" && (
        <Card className="flex flex-wrap items-center gap-4 p-4">
          <div className="min-w-0 flex-1">
            <p className="font-medium">Ready for review</p>
            <p className="text-sm text-[var(--muted-foreground)]">
              {data.slide_count} slides and a cover are waiting for a decision.
            </p>
          </div>
          <Button variant="brand" onClick={() => selectTab("review")}>
            Review it
          </Button>
        </Card>
      )}

      {data.qa.issues.length > 0 && (
        // Capped when fitted: a long QA list is worth reading, but not at the
        // price of the slide it is about.
        <Card className="p-4 md:max-h-28 md:shrink-0 md:overflow-y-auto">
          <p className="mb-2 text-sm font-semibold">QA found {data.qa.issues.length} issue(s)</p>
          <ul className="space-y-1 text-sm">
            {data.qa.issues.map((issue, i) => (
              <li key={i} className="flex gap-2">
                <MutedChip>{issue.severity}</MutedChip>
                <span>
                  {issue.slide_index != null && (
                    <span className="text-[var(--muted-foreground)]">
                      slide {issue.slide_index}:{" "}
                    </span>
                  )}
                  {issue.message}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <section
        className="flex min-h-0 flex-col gap-3 md:flex-1"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <Tabs
            label="Task views"
            value={active}
            onChange={selectTab}
            items={[
              { value: "trace", label: "Trace", icon: <ListTree /> },
              {
                value: "review",
                label: "Review",
                icon: <Images />,
                // The dot is the only reason a person would switch tabs
                // unprompted, so it appears when - and only when - a decision
                // is actually wanted.
                badge: data.pending_review ? (
                  <span
                    aria-label="waiting for your decision"
                    className="size-1.5 rounded-full animate-pip-pulse"
                    style={{ backgroundColor: "var(--phase-review)" }}
                  />
                ) : null,
              },
            ]}
          />
          <div className="flex items-center gap-3 [&_a]:rounded-[10px]">
            {/* A link, not a tab.
                
                Trace and Review are two views OF this page; the conversation
                is a different screen, at its own URL, with the composer docked
                to the viewport. It used to be embedded here as a third tab,
                which meant the chat you were working in existed in two places
                at slightly different sizes. Now there is one chat screen and
                this is the way to it - and the arrow says so before the click
                rather than after it. */}
            <Button variant="ghost" size="sm" asChild>
              <Link to={chatPath(runId!)} viewTransition>
                <MessagesSquare />
                Open chat
                <ArrowUpRight className="size-3.5 opacity-70" />
              </Link>
            </Button>
          </div>
        </div>

        <TabPanel
          value="trace"
          selected={active === "trace"}
          className="min-h-0 md:flex-1"
        >
          <AgentTrace
            events={stream.events}
            summary={stream.summary}
            live={live}
            synced={stream.synced}
          />
        </TabPanel>

        <TabPanel
          value="review"
          selected={active === "review"}
          className="min-h-0 md:flex-1"
        >
          <ReviewPanel run={data} fit />
        </TabPanel>
      </section>
    </div>
  )
}
