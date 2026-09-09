import * as React from "react"
import {
  Brain,
  Check,
  ChevronDown,
  Circle,
  Image as ImageIcon,
  ListChecks,
  LoaderCircle,
  Search,
  Sparkles,
} from "lucide-react"

import {
  AgentFacts,
  AgentSources,
  SourceFavicon,
  collectFacts,
  groupSources,
} from "@/components/agent/agent-sources"
import { AGENT_BLURBS, AGENT_LABELS } from "@/lib/pipeline"
import type { RunEvent, ToolCall, TraceSummary } from "@/lib/types"
import { cn } from "@/lib/utils"
import { LoadingState, type LoadingVariant } from "@/components/agent/loading-state"

type ThinkingView = "steps" | "reasoning" | "search" | "rendering"

const VIEWS: { value: ThinkingView; label: string; icon: typeof ListChecks }[] = [
  { value: "steps", label: "Steps", icon: ListChecks },
  { value: "reasoning", label: "Reasoning", icon: Brain },
  { value: "search", label: "Search", icon: Search },
  { value: "rendering", label: "Rendering", icon: ImageIcon },
]

/**
 * The header line, which names what the trace is showing rather than always
 * saying "Thinking".
 *
 * Switching to Search and still reading "Thought for 25s" made the tab look
 * decorative - the label has to move with the view or it is telling you about
 * something else.
 */
const THINKING_LABEL: Record<ThinkingView, string> = {
  steps: "Working",
  reasoning: "Thinking",
  search: "Searching the web",
  rendering: "Rendering",
}

const THINKING_DONE: Record<ThinkingView, string> = {
  steps: "Worked",
  reasoning: "Thought",
  search: "Searched the web",
  rendering: "Rendered",
}

/**
 * How the duration joins the label.
 *
 * "Thought for 25s" is the phrasing everyone recognises, but "Searched the
 * web for 25s" reads as having searched FOR twenty-five seconds - the wrong
 * sense of the word, on the one label where it matters. Those get a
 * separator instead.
 */
const THINKING_JOIN: Record<ThinkingView, string> = {
  steps: "for",
  reasoning: "for",
  search: "·",
  rendering: "·",
}

const RENDER_AGENTS = new Set(["first_page_visual", "template_design", "cta"])
const SEARCH_TOOL = /search|fetch|source|download|scrape|url|media/i
const RENDER_TOOL = /render|image|cover|slide|stitch|artifact|video|caption/i

function compactDuration(ms: number | null | undefined): string {
  if (ms == null) return ""
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}

function cleanEventText(text: string): string {
  return text.replace(/^\[[^\]]+\]\s*/, "").trim()
}

function eventThoughts(event: RunEvent): string[] {
  const thoughts = event.data?.thoughts
  return Array.isArray(thoughts)
    ? thoughts.filter((value): value is string => typeof value === "string" && !!value.trim())
    : []
}

function allTools(events: RunEvent[]): ToolCall[] {
  const tools: ToolCall[] = []
  const seen = new Set<string>()
  for (const event of events) {
    for (const [index, tool] of (event.tools ?? []).entries()) {
      // A call with no id (ADK coerces a missing id to "") cannot be deduped
      // against anything, so it gets a position-derived identity instead.
      // Sharing `tool.name` as a React key across two such calls made React
      // reuse one row for both and drop the other.
      const key = tool.id || `${event.seq}:${tool.name}:${index}`
      if (seen.has(key)) continue
      seen.add(key)
      tools.push({ ...tool, key })
    }
  }
  return tools
}

export function PixelLoader({
  label,
  live,
  outcome = "complete",
  variant = "Drive",
  startedAt,
}: {
  label: string
  live: boolean
  outcome?: string
  variant?: LoadingVariant
  /** The task's start instant, so the timer is not reset by a remount. */
  startedAt?: string | null
}) {
  return (
    <div className="py-2">
      <LoadingState
        label={label}
        variant={variant}
        running={live}
        outcome={outcome}
        startedAt={startedAt}
      />
    </div>
  )
}

function EmptyTrace({ children }: { children: React.ReactNode }) {
  return (
    <p className="animate-fade-up px-2 py-1.5 text-[13px] text-[var(--muted-foreground)]">
      {children}
    </p>
  )
}

/**
 * How long a row waits before it arrives, and how far the stagger counts.
 *
 * Capped so a twenty-step trace does not spend two seconds unfolding: past
 * the cap every remaining row shares the last delay and lands together.
 */
const ROW_MS = 55
const ROW_CAP = 10

function rowDelay(index: number): React.CSSProperties {
  return { animationDelay: `${Math.min(index, ROW_CAP) * ROW_MS}ms` }
}

/**
 * One line of the trace: a leading mark, what happened, and what it cost.
 *
 * Every view is built from this, which is the point - a trace reads as one
 * thing whether the row is an agent, a thought, a search or a render, and the
 * only thing that varies is the mark on the left and whether the text wraps.
 */
function TraceRow({
  mark,
  primary,
  secondary,
  meta,
  wrap = false,
  mono = false,
  style,
  href,
}: {
  mark?: React.ReactNode
  primary: React.ReactNode
  secondary?: React.ReactNode
  meta?: React.ReactNode
  /** Prose, which wraps, rather than a label, which truncates. */
  wrap?: boolean
  mono?: boolean
  style?: React.CSSProperties
  href?: string
}) {
  const body = (
    <>
      {mark != null && <span className="mt-[3px] shrink-0">{mark}</span>}
      <span className={cn("min-w-0 flex-1", wrap ? "leading-6" : "truncate")}>
        <span className={cn("text-[13px]", wrap ? "text-[var(--muted-foreground)]" : "font-medium")}>
          {primary}
        </span>
        {secondary != null && (
          <span
            className={cn(
              "ml-2 text-[12px] text-[var(--muted-foreground)]",
              mono && "font-mono text-[11.5px]",
              wrap ? "mt-0.5 block ml-0" : "",
            )}
          >
            {secondary}
          </span>
        )}
      </span>
      {meta != null && (
        <span className="shrink-0 pt-px text-[11.5px] tabular-nums text-[var(--muted-foreground)]">
          {meta}
        </span>
      )}
    </>
  )

  const className = cn(
    "animate-fade-up flex w-full items-start gap-2.5 rounded-[8px] px-2 py-1.5 text-left",
    href && "transition-colors hover:bg-[var(--muted)]",
  )

  if (href) {
    return (
      <a href={href} target="_blank" rel="noreferrer" title={href} className={className} style={style}>
        {body}
      </a>
    )
  }
  return (
    <div className={className} style={style}>
      {body}
    </div>
  )
}

/** The mark on a step: running, failed, or done. */
function StepMark({ active, failed }: { active: boolean; failed: boolean }) {
  if (failed) {
    return <Circle className="size-3.5 fill-[var(--destructive)] text-[var(--destructive)]" />
  }
  if (active) {
    return <LoaderCircle className="size-3.5 animate-spin-slow text-[var(--foreground)]" />
  }
  return <Check className="size-3.5 text-[var(--muted-foreground)]" />
}

/**
 * What a tool call was actually asked to do.
 *
 * The server hands `args` over as a JSON string capped at 600 characters, so
 * this is a best-effort read: the recognised keys first, then any short
 * string value, and the raw text if it will not parse at all (a truncated
 * payload never will). Nothing here is load-bearing - a row with no subject
 * still shows the tool's name, which is what it showed before.
 */
const ARG_KEYS = ["query", "q", "url", "prompt", "filename", "artifact", "path", "name"]

function toolSubject(args: string): string {
  const text = (args ?? "").trim()
  if (!text) return ""
  try {
    const parsed = JSON.parse(text)
    if (typeof parsed === "string") return parsed
    if (parsed && typeof parsed === "object") {
      for (const key of ARG_KEYS) {
        const value = (parsed as Record<string, unknown>)[key]
        if (typeof value === "string" && value.trim()) return value.trim()
      }
      for (const value of Object.values(parsed as Record<string, unknown>)) {
        if (typeof value === "string" && value.trim() && value.length <= 140) return value.trim()
      }
    }
  } catch {
    /* truncated or not JSON at all; fall through to the raw text */
  }
  return text.length <= 140 ? text : ""
}

/** How many source rows the Search view shows before it counts the rest. */
const SEARCH_SOURCES = 5

export function ThinkingPanel({
  events,
  summary,
  live,
}: {
  events: RunEvent[]
  summary: TraceSummary | null
  live: boolean
}) {
  // Open while the agents work, shut once they stop - which is what a trace
  // is for. `null` means "nobody has decided", so the automatic behaviour
  // holds until someone clicks, and their choice then sticks for the rest of
  // the session rather than being overridden the moment the run finishes.
  const [choice, setChoice] = React.useState<boolean | null>(null)
  const open = choice ?? live
  const [view, setView] = React.useState<ThinkingView>("steps")

  const tools = React.useMemo(() => allTools(events), [events])
  const agentSteps = React.useMemo(() => {
    // One step per CONTIGUOUS block of an agent's events, not one per agent.
    //
    // A rework brings agents back: planner runs, the reviewer rejects, planner
    // runs again. Keying on the author alone recorded only the first visit, so
    // the list froze the moment a round-2 agent was one that had already
    // appeared - the screen said "Editorial plan" from twenty minutes ago
    // while the pipeline redid the whole carousel.
    const blocks: { author: string; event: RunEvent }[] = []
    for (const event of events) {
      if (!event.author || event.author === "user" || event.author === "carousel_orchestrator") {
        continue
      }
      const last = blocks[blocks.length - 1]
      if (!last || last.author !== event.author) {
        blocks.push({ author: event.author, event })
      }
    }
    // The agent currently emitting, which is the last BLOCK - not the last
    // newly-seen author. Those differ exactly when an agent re-appears, so
    // the spinner used to sit on whichever agent happened to be new last.
    const lastAuthor = blocks[blocks.length - 1]?.author
    return blocks.map(({ author, event }) => {
      const stat = summary?.agents.find((item) => item.name === author)
      return {
        // seq is unique per frame, so a second visit gets its own row.
        id: `${event.seq}:${author}`,
        author,
        label: AGENT_LABELS[author] ?? author.replaceAll("_", " "),
        detail: AGENT_BLURBS[author] ?? cleanEventText(event.text),
        duration: compactDuration(stat?.ms),
        active: live && author === lastAuthor,
        failed: stat?.errors ? stat.errors > 0 : false,
      }
    })
  }, [events, live, summary])

  const thoughts = React.useMemo(
    () => events.flatMap((event) => eventThoughts(event).map((text) => ({ event, text }))),
    [events],
  )
  const sources = React.useMemo(() => groupSources(events), [events])
  const searchTools = tools.filter((tool) => SEARCH_TOOL.test(tool.name))
  const renderTools = tools.filter((tool) => RENDER_TOOL.test(tool.name))
  const renderEvents = events.filter((event) => RENDER_AGENTS.has(event.author))
  const visibleSteps = agentSteps.length > 6 ? agentSteps.slice(-5) : agentSteps
  const hiddenStepCount = agentSteps.length - visibleSteps.length

  // What the header says once the work is over. The summary's `ms` is time
  // the agents actually ran, so a task that waited overnight on a review
  // still reports the minute of work it took.
  const spent = compactDuration(summary?.ms)
  const label = live
    ? THINKING_LABEL[view]
    : spent
      ? `${THINKING_DONE[view]} ${THINKING_JOIN[view]} ${spent}`
      : THINKING_DONE[view]

  return (
    <section className="flex flex-col">
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={() => setChoice(!open)}
          aria-expanded={open}
          className="-ml-1.5 flex min-w-0 items-center gap-2 rounded-[8px] px-1.5 py-1 transition-colors hover:bg-[var(--muted)]"
        >
          <Sparkles
            className={cn("size-4 shrink-0", live ? "text-[var(--foreground)]" : "text-[var(--muted-foreground)]")}
          />
          <span role="status" className="truncate text-[13px] font-medium">
            {live ? (
              <span className="text-shimmer">{label}</span>
            ) : (
              <span className="text-[var(--muted-foreground)]">{label}</span>
            )}
          </span>
          <ChevronDown
            className={cn(
              "size-3.5 shrink-0 text-[var(--muted-foreground)] transition-transform duration-300",
              open && "rotate-180",
            )}
          />
        </button>

        {/* The four kinds of trace this pipeline produces. Only offered while
            the trace is open - a filter for something you cannot see is a
            control that appears to do nothing. */}
        {open && (
          <div className="ml-auto flex min-w-0 gap-0.5 overflow-x-auto" role="tablist" aria-label="Thinking detail">
            {VIEWS.map(({ value, label: name, icon: Icon }) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={view === value}
                onClick={() => setView(value)}
                title={name}
                className={cn(
                  "flex shrink-0 items-center gap-1.5 rounded-[7px] px-2 py-1 text-[12px] transition-colors",
                  view === value
                    ? "bg-[var(--muted)] text-[var(--foreground)]"
                    : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
                )}
              >
                <Icon className="size-3.5" />
                <span className={cn(view === value ? "" : "hidden sm:inline")}>{name}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      <div
        className="grid transition-[grid-template-rows,opacity] duration-400 ease-[cubic-bezier(0.23,1,0.32,1)]"
        style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}
        aria-hidden={!open || undefined}
        inert={!open || undefined}
      >
        <div className="overflow-hidden">
          {/* The hairline is a border on the rows themselves rather than a
              measured element, so it grows with the list instead of needing a
              layout pass every time a row arrives. */}
          <div className="ml-[7px] mt-1 flex flex-col border-l border-[var(--border)] py-1 pl-3.5">
            {view === "steps" &&
              (agentSteps.length ? (
                <>
                  {hiddenStepCount > 0 && (
                    <TraceRow
                      mark={<Check className="size-3.5 text-[var(--muted-foreground)]" />}
                      primary={
                        <span className="font-normal text-[var(--muted-foreground)]">
                          {hiddenStepCount} earlier steps
                        </span>
                      }
                    />
                  )}
                  {visibleSteps.map((step, index) => (
                    <TraceRow
                      key={step.id}
                      style={rowDelay(index)}
                      mark={<StepMark active={step.active} failed={step.failed} />}
                      primary={step.label}
                      secondary={step.detail}
                      meta={step.active ? "running" : step.duration}
                    />
                  ))}
                </>
              ) : (
                <EmptyTrace>
                  {live ? "Preparing the first agent." : "No agent ran on this task."}
                </EmptyTrace>
              ))}

            {view === "reasoning" &&
              (thoughts.length ? (
                <div className="max-h-72 overflow-y-auto">
                  {thoughts.slice(-8).map(({ event, text }, index) => (
                    <TraceRow
                      key={`${event.seq}:${index}`}
                      style={rowDelay(index)}
                      wrap
                      primary={text}
                      secondary={AGENT_LABELS[event.author] ?? event.author}
                    />
                  ))}
                </div>
              ) : (
                <EmptyTrace>
                  {live
                    ? "Reasoning appears when a model emits its thoughts."
                    : "This task recorded no model reasoning."}
                </EmptyTrace>
              ))}

            {view === "search" &&
              (searchTools.length || sources.total ? (
                <>
                  {/* What was asked, then what was read - the order it
                      happened in, and the order it is useful in. */}
                  {searchTools.slice(-6).map((tool, index) => (
                    <TraceRow
                      key={tool.key ?? tool.id ?? tool.name}
                      style={rowDelay(index)}
                      mark={<Search className="size-3.5 text-[var(--muted-foreground)]" />}
                      primary={toolSubject(tool.args) || tool.name}
                      meta={
                        live && tool.status === "running"
                          ? "searching"
                          : tool.status === "error"
                            ? "failed"
                            : compactDuration(tool.ms)
                      }
                    />
                  ))}
                  {sources.groups.slice(0, SEARCH_SOURCES).map((group, index) => (
                    <TraceRow
                      key={group.host}
                      style={rowDelay(searchTools.length + index)}
                      href={group.links[0].url}
                      mark={<SourceFavicon host={group.host} size={14} className="rounded-[4px]" />}
                      primary={group.host}
                      secondary={group.links.length > 1 ? `${group.links.length} pages` : group.links[0].path}
                    />
                  ))}
                  {sources.groups.length > SEARCH_SOURCES && (
                    <span className="animate-fade-up px-2 py-1 text-[12px] text-[var(--muted-foreground)]">
                      +{sources.groups.length - SEARCH_SOURCES} more sites
                    </span>
                  )}
                </>
              ) : (
                <EmptyTrace>
                  {live ? "Searches and the pages read will appear here." : "This task made no searches."}
                </EmptyTrace>
              ))}

            {view === "rendering" &&
              (renderTools.length || renderEvents.length ? (
                <>
                  {renderTools.slice(-8).map((tool, index) => (
                    <TraceRow
                      key={tool.key ?? tool.id ?? tool.name}
                      style={rowDelay(index)}
                      mark={
                        live && tool.status === "running" ? (
                          <LoaderCircle className="size-3.5 animate-spin-slow text-[var(--foreground)]" />
                        ) : tool.status === "error" ? (
                          <Circle className="size-3.5 fill-[var(--destructive)] text-[var(--destructive)]" />
                        ) : (
                          <ImageIcon className="size-3.5 text-[var(--muted-foreground)]" />
                        )
                      }
                      primary={tool.name}
                      secondary={toolSubject(tool.args)}
                      mono
                      meta={
                        live && tool.status === "running" ? "rendering" : compactDuration(tool.ms)
                      }
                    />
                  ))}
                  {!renderTools.length &&
                    renderEvents.slice(-6).map((event, index) => (
                      <TraceRow
                        key={event.seq}
                        style={rowDelay(index)}
                        mark={<ImageIcon className="size-3.5 text-[var(--muted-foreground)]" />}
                        primary={cleanEventText(event.text) || AGENT_BLURBS[event.author]}
                      />
                    ))}
                </>
              ) : (
                <EmptyTrace>
                  {live ? "Cover and slide rendering will appear here." : "This task rendered nothing."}
                </EmptyTrace>
              ))}
          </div>
        </div>
      </div>
    </section>
  )
}


function usefulMessages(events: RunEvent[]): { seq: number; text: string }[] {
  const messages: { seq: number; text: string }[] = []
  for (const event of events) {
    if (!event.text || event.author === "user" || event.author === "carousel_orchestrator") continue
    const text = cleanEventText(event.text)
    if (!text || text.startsWith("->") || text.startsWith("<-") || text.startsWith("{")) continue
    if (text.length < 28) continue
    messages.push({ seq: event.seq, text })
  }
  return messages.slice(-3)
}

/**
 * How far into a paragraph the per-word stagger keeps counting.
 *
 * Beyond this every remaining word shares the last delay and they resolve
 * together. Without the cap a 200-word brief would take four seconds to
 * finish appearing, which stops being an arrival and starts being a wait.
 */
const STAGGER_WORDS = 44
const STAGGER_MS = 22

/**
 * One agent message, its words resolving out of blur as it lands.
 *
 * The stagger is CSS-only - a per-word `animation-delay`, no timer and no
 * state. That matters for more than tidiness: the entire paragraph is in the
 * DOM from the first frame, so it can be selected, copied and read aloud
 * immediately, and only its APPEARANCE is staggered. A JS typewriter would
 * withhold text the server has already sent, and would restart itself every
 * time a new event arrived.
 */
function StreamedLine({ text, caret }: { text: string; caret: boolean }) {
  const words = React.useMemo(() => text.split(/\s+/), [text])
  return (
    <p className="text-[15px] leading-7 text-[var(--foreground)]">
      {words.map((word, index) => (
        <React.Fragment key={index}>
          {/* The space sits OUTSIDE the span so the line still breaks
              normally between words. */}
          <span
            className="stream-word"
            style={{ animationDelay: `${Math.min(index, STAGGER_WORDS) * STAGGER_MS}ms` }}
          >
            {word}
          </span>{" "}
        </React.Fragment>
      ))}
      {caret && (
        <span
          className="ml-0.5 inline-block h-4 w-0.5 translate-y-0.5 animate-pulse bg-[var(--foreground)]"
          aria-label="Streaming"
        />
      )}
    </p>
  )
}

export function StreamedAgentText({ events, live }: { events: RunEvent[]; live: boolean }) {
  const messages = usefulMessages(events)
  const sources = React.useMemo(() => groupSources(events), [events])
  const facts = React.useMemo(() => collectFacts(events), [events])
  if (!messages.length && !sources.total && !facts.length) return null

  return (
    <section className="space-y-4" aria-live="polite">
      {messages.map((message, index) => (
        <StreamedLine
          key={message.seq}
          text={message.text}
          caret={index === messages.length - 1 && live}
        />
      ))}

      {/* The verified claims, each cited to the page it was checked against.
          Above the source group on purpose: a specific citation is worth more
          than the run's full reading list, and burying it under one would
          make the list look like the answer. */}
      <AgentFacts facts={facts} />

      <AgentSources groups={sources.groups} total={sources.total} />
    </section>
  )
}
