/**
 * Shapes returned by the console API.
 *
 * These mirror web_api/routes_runs.py. They are hand-written rather than
 * generated because the surface is small and stable; the drift risk that
 * actually matters - agent and phase NAMES - is closed differently, by
 * fetching them from /api/meta and asserting against the local list (see
 * pipeline.ts). A renamed constant would otherwise produce silently blank rows
 * in the trace with no error anywhere.
 */

export type Phase = "generate" | "qa" | "review" | "rework" | "publish" | "done"

export type RunStatus =
  | "running"
  | "awaiting_review"
  | "done"
  | "interrupted"
  | "failed"
  | "cancelled"

export type EventKind =
  | "phase"
  | "progress"
  | "tool"
  | "error"
  | "terminal"
  | "gap"

export type RunSummary = {
  run_id: string
  news_id: string | null
  phase: Phase
  status: RunStatus
  review_round: number
  title: string | null
  source: string | null
  requested_by: string | null
  created_at: string | null
  updated_at: string | null
  is_live: boolean
}

export type QAIssue = {
  severity: string
  slide_index: number | null
  message: string
}

export type RunDetail = RunSummary & {
  news: {
    title: string
    summary: string
    source_name: string
    source_url: string
  }
  phase_state: Phase
  rework_round: number
  caption: string
  slide_count: number
  qa: { passed: boolean | null; issues: QAIssue[] }
  verdict: { status: string; feedback: string; reviewer?: string } | null
  publish: { media_id: string | null; permalink: string | null; error: string | null }
  token_usage: Record<string, number>
  last_seq: number
  /**
   * Whether a human decision is still wanted.
   *
   * NOT monotonic. A failed background resume restores the pending row, so a
   * run can go pending -> not pending -> pending again. Anything that reads
   * this once and caches the answer will eventually show the wrong thing.
   */
  pending_review: boolean
  /** Carousel is ready, but the review notification could not be sent. */
  notice_failed?: boolean
}

export type SignedArtifact = {
  filename: string | null
  url: string | null
  error?: string
}

export type RunArtifacts = {
  run_id: string
  expires_in: number
  caption: string
  /** False while the endpoint is exposing progressive pre-bundle renders. */
  complete?: boolean
  /** Planned total including cover and CTA, when the planner has run. */
  expected_count?: number
  cover: {
    poster: SignedArtifact | null
    video: SignedArtifact | null
    /** No source clip was found, so the "cover" is a still - do not render a
     *  <video> element for it. */
    is_still: boolean
    duration_s: number
  }
  slides: (SignedArtifact & { index: number })[]
  cta: SignedArtifact & { cta_type: string | null; link_url: string }
  ordered: string[]
}

/** Which cover goes out as the first slide. null = not yet chosen. */
export type CoverChoice = "video" | "image" | null

export type ToolCall = {
  id: string
  /**
   * Unique within one run, for React keys.
   *
   * ADK coerces a missing call id to "", so two same-named calls can both
   * arrive with no id at all. `id || name` then collides and React reuses one
   * row for both, dropping the other from the list. Assigned client-side by
   * `allTools`, which knows each call's position.
   */
  key?: string
  name: string
  /** Pre-rendered and length-capped by the server. */
  args: string
  status: "running" | "ok" | "error"
  /** Wall clock from the call to its response, paired by call id. */
  ms: number | null
  result: string | null
}

export type RunEvent = {
  /**
   * Stable identity for this frame, and what dedupe must key on.
   *
   * `seq` is a POSITION, and the two sources that produce frames number
   * positions differently: history comes from ADK's transcript, the live tail
   * from the run_events counter renumbered onto the end of whatever history
   * was replayed. Comparing those two numbers is comparing coordinate
   * systems - it hides a real frame when they happen to collide and shows one
   * twice when they do not. `id` comes from the event itself, so it means the
   * same thing whichever way the frame arrived.
   *
   * Optional only for frames from an older server; the fallback is `seq`.
   */
  id?: string
  seq: number
  kind: EventKind
  author: string
  text: string
  data: Record<string, unknown>
  created_at: string | null
  ts?: string | null
  tools?: ToolCall[]
}

export type TokenCount = { prompt: number; output: number; total: number }

export type AgentStat = {
  name: string
  /** First to last event for this agent. */
  ms: number | null
  tokens: TokenCount
  tool_calls: number
  events: number
  errors: number
}

export type TraceSummary = {
  tokens: TokenCount | null
  /** Time the agents actually ran, summed per invocation - excludes waits. */
  ms: number | null
  /** Wall clock from the first event to the last, including idle waits. */
  span_ms?: number | null
  /** One per stretch of work: the first run, plus one per resume. */
  invocations?: number
  agents: AgentStat[]
  event_count: number
  tool_calls?: number
}

export type Trace = {
  run_id: string
  after: number
  items: RunEvent[]
  summary: TraceSummary
}

export type QueueItem = {
  id: string
  title: string
  summary: string
  source_name: string
  source_url: string
  created_at: string | null
}

/**
 * The whole console in five numbers, for the sidebar dots.
 *
 * `stopped` is failed + interrupted over a window of recent runs, not all
 * time - see PULSE_WINDOW in app/services/db.py for why.
 */
export type Pulse = {
  running: number
  awaiting_review: number
  stopped: number
  queued: number
  /** A feed check is polling the sources right now. */
  fetching: boolean
}

export type QueueResponse = {
  items: QueueItem[]
  /**
   * A feed check is running right now - from the cron tick or from someone
   * pressing "check now".
   *
   * NOT the same as the schedule's `running`, which reports whether the timer
   * itself is alive. This one says whether it is doing something.
   */
  fetching?: boolean
}

export type Meta = {
  agents: string[]
  reworkable_agents: string[]
  phases: Phase[]
  statuses: RunStatus[]
  reject_question: string
  max_slides: number
  /** False when IG_USER_ID / IG_ACCESS_TOKEN are unset: approving will still
   *  record the verdict, but publishing will fail loudly. */
  publish_configured: boolean
}

export type Identity = {
  email: string
  role: string
  is_admin: boolean
  source: string
}
