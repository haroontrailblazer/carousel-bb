import { ExternalLink } from "lucide-react"
import { Link } from "react-router"

import {
  PixelLoader,
  StreamedAgentText,
  ThinkingPanel,
} from "@/components/agent/agent-activity"
import { AgentAssetStrip } from "@/components/agent/agent-assets"
import { UserAvatar } from "@/components/layout/user-avatar"
import type { LoadingVariant } from "@/components/agent/loading-state"
import { useProfile } from "@/hooks/use-profile"
import { AGENT_LABELS, isStopped, PHASE_LABELS } from "@/lib/pipeline"
import type { RunArtifacts, RunDetail, RunEvent, TraceSummary } from "@/lib/types"

/**
 * The conversation as it happened: the prompt, then the agent's work on it.
 *
 * Extracted from the New carousel screen so the SAME transcript can be shown
 * inside a finished task. Two implementations would drift, and the whole point
 * is that opening a task later shows what you already watched live.
 *
 * `prompt` is what the person typed. The New carousel screen has it in local
 * state while the tab stays open; a task opened later does not, so it is
 * reconstructed from the run - the URL for a pasted link, the title otherwise.
 * That reconstruction is a paraphrase, not a recording, which is why the live
 * value is preferred whenever it exists.
 */
export function AgentConversation({
  run,
  events,
  summary,
  live,
  artifacts,
  runId,
  prompt,
  showReviewCta = true,
}: {
  run: RunDetail
  events: RunEvent[]
  summary?: TraceSummary | null
  live: boolean
  artifacts?: RunArtifacts
  runId: string
  prompt?: string
  /** Off inside the task page, where the Review tab is one click away. */
  showReviewCta?: boolean
}) {
  const variant: LoadingVariant =
    run.phase === "qa" ? "Dots" : run.phase === "generate" ? "Orbit" : "Drive"

  // Only show a "You" bubble for something a person actually said.
  //
  // `prompt` is the live value, held by the tab that typed it. Without it the
  // text is RECONSTRUCTED - the URL for a pasted link, a sentence built from
  // the title otherwise - and that reconstruction is only honest for a task
  // someone typed. A queue pick or a scheduled run was never asked for by
  // anyone, so attributing "Create a carousel about X" to the reader invents a
  // message they never sent. Those get a source line instead, which is true.
  const typed = prompt || (startedFromComposer(run) ? reconstructPrompt(run) : "")
  // Shared cache: the sidebar and the account menu are reading the same entry,
  // so this costs no request and cannot show a different face from them.
  const { profile } = useProfile()

  // Computed once and used twice, so the two controls cannot both appear.
  // Whichever screen this is rendered on, exactly one route into the review
  // is drawn: the card while the carousel is waiting to be judged, the
  // strip's own bar once it no longer is.
  const reviewCta = showReviewCta && run.pending_review

  return (
    <div className="space-y-6">
      {typed ? (
        <div className="flex justify-end gap-3">
          <div className="max-w-[82%] rounded-[16px] bg-[var(--muted)] px-4 py-3 text-sm leading-6">
            {typed}
          </div>
          {/* The person's actual picture, not the word "You".
              
              Every other place the signed-in user appears - the sidebar
              account row, the menu, the profile screen - shows their face,
              and this was the one that spelled out their pronoun in a black
              circle. `UserAvatar` falls through the same three steps as
              everywhere else: uploaded picture, then Gravatar, then a
              monogram generated from the email. So a user with no picture
              still gets the same coloured initial here as in the sidebar,
              rather than a different placeholder in each place. */}
          <UserAvatar
            key={profile.avatarUrl ?? "none"}
            src={profile.avatarUrl}
            name={profile.displayName}
            seed={profile.email}
            className="mt-1 size-8 text-[11px]"
          />
        </div>
      ) : (
        <p className="text-xs text-[var(--muted-foreground)]">
          {run.source === "schedule" ? "Picked automatically from" : "From the newsroom"}
          {run.news.source_name ? ` · ${run.news.source_name}` : ""}
        </p>
      )}

      <div className="min-w-0 space-y-5">
        <PixelLoader
          key={runId}
          label={agentActivityLabel(run, events)}
          live={live}
          outcome={isStopped(run.status) ? "stopped" : "complete"}
          variant={variant}
          // The run's own start instant, so the timer reads the same whether
          // you have been watching since it began or just opened the task.
          startedAt={run.created_at}
        />
        {/* The tool chips that used to sit here are gone: they were the same
            calls the trace already lists, minus the query that made them
            legible, so a research run showed six identical `web_search` pills
            above a panel explaining what each one searched for. */}
        <ThinkingPanel events={events} summary={summary ?? null} live={live} />
        <StreamedAgentText events={events} live={live} />
        <AgentAssetStrip
          artifacts={artifacts}
          live={live}
          runId={runId}
          showReview={!reviewCta}
        />

        {run.news.source_url && (
          <a
            href={run.news.source_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-xs text-[var(--link)] hover:underline"
          >
            Original story <ExternalLink className="size-3" />
          </a>
        )}

        {reviewCta && (
          <div className="flex flex-wrap items-center gap-3 rounded-[14px] border border-[var(--border)] bg-[var(--card)] p-4">
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">Your carousel is ready</p>
              <p className="mt-1 text-xs leading-5 text-[var(--muted-foreground)]">
                Review the cover and {Math.max(0, run.slide_count - 1)} remaining
                slides before anything is published.
              </p>
            </div>
            <Link
              to={`/tasks/${runId}?tab=review`}
              viewTransition
              className="rounded-[10px] bg-[var(--brand)] px-3 py-2 text-xs font-semibold text-[var(--brand-foreground)] hover:bg-[var(--brand-hover)]"
            >
              Review carousel
            </Link>
          </div>
        )}
      </div>
    </div>
  )
}

/** Whether this run has a conversation worth showing at all. */
/**
 * The best guess at what was typed, for a task this browser did not start.
 *
 * A paraphrase, not a recording - which is why the live value always wins and
 * why this is only used for runs that a person really did type.
 */
function reconstructPrompt(run: RunDetail): string {
  if (run.source === "url") return run.news.source_url || run.title || "this story"
  return `Create a carousel about ${run.title || run.news.title || "this story"}`
}

export function startedFromComposer(run: Pick<RunDetail, "source">): boolean {
  // The New carousel composer posts exactly these two. Queue and schedule runs
  // were never typed by anyone, so there is no conversation to replay.
  return run.source === "topic" || run.source === "url"
}

function agentActivityLabel(run: RunDetail, events: RunEvent[]): string {
  if (run.status === "awaiting_review") return "Your carousel is ready for review"
  if (run.status === "done") return "Carousel published"
  if (run.status === "cancelled") return "Task cancelled"
  if (run.status === "failed") return "The carousel agent stopped"
  if (run.status === "interrupted") return "The background task was interrupted"
  for (let index = events.length - 1; index >= 0; index--) {
    const author = events[index].author
    if (author && author !== "user" && author !== "carousel_orchestrator") {
      const agent = AGENT_LABELS[author] ?? author.replaceAll("_", " ")
      return `${agent} · ${(PHASE_LABELS[run.phase] ?? run.phase).toLowerCase()}`
    }
  }
  return `Preparing your carousel · ${(PHASE_LABELS[run.phase] ?? run.phase).toLowerCase()}`
}
