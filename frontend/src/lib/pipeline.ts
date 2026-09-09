/**
 * Names and labels for the eleven agents and six phases.
 *
 * These MUST match app/state.py. A rename there would otherwise show up here
 * as blank rows in the trace with no error anywhere - the failure mode is
 * silence, which is the worst kind. `/api/meta` returns the authoritative
 * lists and `assertNoDrift` compares them in development, so a mismatch
 * complains in the console instead of quietly degrading the UI.
 */

import type { Phase, RunStatus } from "@/lib/types"

export const AGENTS = [
  "research",
  "planner",
  "first_page_visual",
  "phrasing",
  "template_design",
  "cta",
  "stitch_verify",
  "review_dispatcher",
  "feedback_router",
  "publisher",
  "learner",
] as const

export type AgentName = (typeof AGENTS)[number]

export const AGENT_LABELS: Record<string, string> = {
  research: "Research",
  planner: "Editorial plan",
  first_page_visual: "Cover visual",
  phrasing: "Copywriting",
  template_design: "Slide design",
  cta: "Call to action",
  stitch_verify: "Assemble & check",
  review_dispatcher: "Send for review",
  feedback_router: "Route feedback",
  publisher: "Publish",
  learner: "Learn from feedback",
  carousel_orchestrator: "Orchestrator",
}

/**
 * The agents a reviewer is allowed to point at, in pipeline order.
 *
 * Mirrors `REWORKABLE_AGENTS` in app/state.py, which is the list the server
 * enforces - anything not on it is dropped by the rework sanitizer, so
 * offering a seventh here would be offering a choice that silently does
 * nothing. The two lists are asserted equal by tests/test_agent_tree.py.
 *
 * `slash` is what someone types to reach it. Short, lowercase and distinct in
 * the first two characters, so the menu narrows fast.
 */
export const REWORKABLE_AGENTS = [
  { name: "research", slash: "research", hint: "Facts, sources, figures" },
  { name: "planner", slash: "plan", hint: "Slide count, hook, structure" },
  { name: "first_page_visual", slash: "cover", hint: "The cover image or clip" },
  { name: "phrasing", slash: "copy", hint: "Wording on every slide" },
  { name: "template_design", slash: "design", hint: "Layout and typography" },
  { name: "cta", slash: "cta", hint: "The final call-to-action slide" },
] as const

export type ReworkableAgent = (typeof REWORKABLE_AGENTS)[number]["name"]

/** One line on what each agent actually does, for the trace's expanded state. */
export const AGENT_BLURBS: Record<string, string> = {
  research: "Searches the web and saves a brief of verified facts.",
  planner: "Decides slide count, the hook headline, and each slide's points.",
  first_page_visual: "Sources a real clip from the news and builds the cover video.",
  phrasing: "Writes the verbatim slide lines and the Instagram caption.",
  template_design: "Renders each body slide, then composites the typography.",
  cta: "Picks and renders the closing call-to-action slide.",
  stitch_verify: "Assembles the bundle and runs the deterministic QA checks.",
  review_dispatcher: "Sends the previews out and pauses for a human decision.",
  feedback_router: "Turns rejection feedback into specific agents to re-run.",
  publisher: "Signs the artifact URLs and posts the carousel to Instagram.",
  learner: "Stores the feedback and distills repeated themes into rules.",
}

export const PHASES: Phase[] = [
  "generate",
  "qa",
  "review",
  "rework",
  "publish",
  "done",
]

export const PHASE_LABELS: Record<string, string> = {
  generate: "Generating",
  qa: "Checking",
  review: "Awaiting review",
  rework: "Reworking",
  publish: "Publishing",
  done: "Done",
}

/**
 * Which CSS custom-property family a phase uses.
 *
 * Chip text always uses the `-fg` step on the `-soft` background. Never put
 * white on the solid colour: #E56D24 with white is 3.21:1 and fails AA.
 */
export const PHASE_TOKEN: Record<string, string> = {
  generate: "generate",
  qa: "qa",
  review: "review",
  rework: "rework",
  publish: "publish",
  done: "done",
}

export const STATUS_LABELS: Record<RunStatus, string> = {
  running: "Running",
  awaiting_review: "Needs your review",
  done: "Published",
  interrupted: "Interrupted",
  failed: "Failed",
  cancelled: "Cancelled",
}

/**
 * Did this task stop, rather than pause or finish?
 *
 * failed / cancelled / interrupted differ in cause and in nothing else that
 * the UI does with them: no more work is coming, no decision is being waited
 * for, and everything on screen should be in the past tense. The header names
 * which of the three it was, in colour, so nothing below it repeats that -
 * they only ask this question.
 *
 * Not `!isLive`. A task awaiting review is not live either, and it is very
 * much not stopped - reading one for the other is how "Nothing to approve
 * yet" ended up on a carousel that was ready to approve.
 */
export function isStopped(status: RunStatus): boolean {
  return status === "failed" || status === "cancelled" || status === "interrupted"
}

/** Status -> phase token family, for colouring status chips consistently. */
export const STATUS_TOKEN: Record<RunStatus, string> = {
  running: "generate",
  awaiting_review: "review",
  done: "done",
  interrupted: "rework",
  failed: "failed",
  cancelled: "failed",
}

/**
 * The reject categories, matching review_api's REJECT_QUESTION.
 *
 * Not decoration: feedback_router maps prose onto rework targets using this
 * vocabulary, so nudging the reviewer into these words measurably improves
 * which agents get re-run.
 */
export const REJECT_CATEGORIES = [
  { key: "facts", label: "Facts", targets: ["research", "planner"] },
  { key: "first visual", label: "Cover visual", targets: ["first_page_visual"] },
  { key: "texts", label: "Texts", targets: ["phrasing", "template_design"] },
  { key: "slide design", label: "Slide design", targets: ["template_design"] },
  { key: "CTA", label: "CTA", targets: ["cta"] },
  { key: "structure", label: "Structure", targets: ["planner"] },
  { key: "other", label: "Something else", targets: [] },
] as const

/**
 * Re-running one agent forces its dependents to re-run too.
 *
 * Mirrors _REWORK_DEPENDENTS in app/orchestrator.py. Shown to the reviewer as
 * a PREDICTION - the router has the final say, and the UI says so.
 */
const REWORK_DEPENDENTS: Record<string, string[]> = {
  research: ["planner"],
  planner: ["first_page_visual", "phrasing", "template_design", "cta"],
  phrasing: ["template_design"],
}

export function predictRework(targets: string[]): string[] {
  const out = new Set<string>()
  const visit = (name: string) => {
    if (out.has(name)) return
    out.add(name)
    ;(REWORK_DEPENDENTS[name] ?? []).forEach(visit)
  }
  targets.forEach(visit)
  return AGENTS.filter((a) => out.has(a))
}

/**
 * Warn loudly (in development) if the server's vocabulary has moved.
 *
 * Twenty lines that permanently close a whole class of silent UI breakage.
 */
export function assertNoDrift(meta: { agents: string[]; phases: string[] }): void {
  if (!import.meta.env.DEV) return
  const missing = meta.agents.filter((a) => !AGENTS.includes(a as AgentName))
  const extra = AGENTS.filter((a) => !meta.agents.includes(a))
  if (missing.length || extra.length) {
    console.error(
      "[pipeline] agent names have drifted from app/state.py.",
      { onlyOnServer: missing, onlyInUi: extra },
    )
  }
  const phaseMissing = meta.phases.filter((p) => !PHASES.includes(p as Phase))
  if (phaseMissing.length) {
    console.error("[pipeline] unknown phases from the server:", phaseMissing)
  }
}
