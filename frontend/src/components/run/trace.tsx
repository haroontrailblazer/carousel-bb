import * as React from "react"
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Clock,
  Coins,
  Wrench,
} from "lucide-react"

import { Chip } from "@/components/ui/chip"
import { groupByAuthor } from "@/hooks/use-run-stream"
import { compactNumber } from "@/lib/format"
import { AGENT_BLURBS, AGENT_LABELS, PHASES, PHASE_LABELS } from "@/lib/pipeline"
import type { Phase, RunEvent, ToolCall, TraceSummary } from "@/lib/types"
import { cn } from "@/lib/utils"

/**
 * A duration in the units it deserves.
 *
 * Per-agent and per-tool timings are mostly seconds, and "00:00:04" spends
 * six characters saying almost nothing while burying the one digit that
 * matters. Compact units keep the significant figure at the front, where the
 * eye lands. Totals are the exception - see `clock` below.
 */
function duration(ms: number | null | undefined): string {
  if (ms == null) return "—"
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const m = Math.floor(ms / 60_000)
  const s = Math.round((ms % 60_000) / 1000)
  if (ms < 3_600_000) return `${m}m ${s.toString().padStart(2, "0")}s`
  // The orchestrator's block brackets the pause for review, so it really can
  // span hours. "1440m 00s" is not a number anyone can read.
  return `${Math.floor(m / 60)}h ${(m % 60).toString().padStart(2, "0")}m`
}

/**
 * hh:mm:ss - for the run TOTAL only.
 *
 * A total is the one number people read as a clock ("how long did this take?")
 * and compare between runs, so a fixed clock shape is right there. It is wrong
 * for the rows underneath, where the values span four orders of magnitude and
 * padding them all to the same width hides the differences.
 */
function clock(ms: number | null | undefined): string {
  if (ms == null) return "—"
  const total = Math.max(0, Math.round(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => n.toString().padStart(2, "0")
  return `${pad(h)}:${pad(m)}:${pad(s)}`
}

/** Slow things should look slow without the reader doing arithmetic. */
function heatTone(ms: number | null | undefined): string {
  if (ms == null) return "qa"
  if (ms >= 60_000) return "failed"
  if (ms >= 15_000) return "review"
  return "qa"
}

/**
 * The phase rail.
 *
 * `rework` is drawn as a return arc back to `qa` rather than as the fourth
 * step in a line, because that is what it actually is: rejected work goes back
 * and comes round again.
 */
export function PhaseRail({
  phase,
  live,
  stopped = false,
}: {
  phase: Phase
  live: boolean
  /** The task will not resume on its own: failed, cancelled or interrupted. */
  stopped?: boolean
}) {
  const linear = PHASES.filter((p) => p !== "rework")
  const activeIndex = linear.indexOf(phase === "rework" ? "qa" : phase)

  return (
    <div
      aria-label="Task progress"
      className="border-y border-[var(--border)] py-3"
    >
      <ol className="grid grid-cols-5">
        {linear.map((p, index) => {
          const done = index < activeIndex
          const active = index === activeIndex
          return (
            <li
              key={p}
              aria-current={active ? "step" : undefined}
              className="min-w-0"
            >
              <span className="flex items-center">
                <span
                  aria-hidden
                  className={cn(
                    "size-2 shrink-0 rounded-full transition-colors duration-300 motion-reduce:transition-none",
                    active && live && "animate-pip-pulse",
                  )}
                  style={{
                    backgroundColor:
                      done || active ? `var(--phase-${p})` : "var(--border)",
                  }}
                />
                {index < linear.length - 1 && (
                  <span
                    aria-hidden
                    className={cn(
                      "mx-1.5 h-px min-w-0 flex-1 transition-colors duration-300 sm:mx-2.5",
                      done && "animate-rail-fill",
                    )}
                    style={{
                      backgroundColor: done
                        ? `var(--phase-${p})`
                        : "var(--border)",
                    }}
                  />
                )}
              </span>

              <span
                className={cn(
                  "mt-2 block pr-1 text-[10px] font-medium leading-tight transition-colors duration-300 sm:text-xs motion-reduce:transition-none",
                  active && "font-semibold",
                  !done && !active && "text-[var(--muted-foreground)]",
                )}
                style={
                  done || active
                    ? { color: `var(--phase-${p}-fg)` }
                    : undefined
                }
              >
                {PHASE_LABELS[p]}
              </span>
            </li>
          )
        })}
      </ol>

      {phase === "rework" && (
        <p
          className="mt-3 flex items-center gap-2 text-xs font-medium"
          style={{ color: "var(--phase-rework-fg)" }}
        >
          <span
            aria-hidden
            className={cn(
              "size-1.5 rounded-full bg-[var(--phase-rework)]",
              live && "animate-pip-pulse",
            )}
          />
          {/* The rail draws five steps in a line; this sentence is the arc
              they cannot draw - rework goes BACK to checking. Which is worth
              saying in either tense, so a task that died mid-rework keeps the
              explanation and loses the "now" that claimed it was still
              working. `stopped` and not `!live`: a task awaiting review is
              not live either. */}
          {stopped
            ? "Stopped while reworking, before it returned to checking"
            : "Reworking now, then returning to checking"}
        </p>
      )}
    </div>
  )
}

/** One tool call, with its full payload available without relying on hover. */
const ToolCallRow = React.memo(function ToolCallRow({ tool }: { tool: ToolCall }) {
  const tone =
    tool.status === "error"
      ? "failed"
      : tool.status === "running"
        ? "generate"
        : heatTone(tool.ms)

  return (
    <details className="group border-b border-[var(--border)] last:border-b-0">
      <summary className="flex min-h-12 cursor-pointer list-none items-center gap-3 px-3 py-2.5 transition-colors hover:bg-[var(--muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--ring)] [&::-webkit-details-marker]:hidden">
        <span
          className="grid size-7 shrink-0 place-items-center rounded-[8px]"
          style={{
            backgroundColor: `var(--phase-${tone}-soft)`,
            color: `var(--phase-${tone}-fg)`,
          }}
        >
          <Wrench className="size-3.5" />
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs font-semibold">
          {tool.name}
        </span>
        <Chip tone={tool.status === "error" ? "failed" : tool.status === "running" ? "generate" : "done"}>
          {tool.status}
        </Chip>
        <span className="w-16 text-right font-mono text-[11px] tabular-nums text-[var(--muted-foreground)]">
          {duration(tool.ms)}
        </span>
        <ChevronDown className="size-3.5 shrink-0 text-[var(--muted-foreground)] transition-transform group-open:rotate-180" />
      </summary>

      {(tool.args || tool.result) && (
        <div className="grid gap-3 bg-[var(--muted)] px-3 py-3 lg:grid-cols-2">
          {tool.args && (
            <div className="min-w-0">
              <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--muted-foreground)]">
                Arguments
              </p>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-[8px] border border-[var(--border)] bg-[var(--background)] p-2.5 font-mono text-[11px] leading-relaxed">
                {tool.args}
              </pre>
            </div>
          )}
          {tool.result && (
            <div className="min-w-0">
              <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--muted-foreground)]">
                Result
              </p>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-[8px] border border-[var(--border)] bg-[var(--background)] p-2.5 font-mono text-[11px] leading-relaxed">
                {tool.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </details>
  )
})

/** The run's cost and duration at a glance, above the per-agent detail. */
export function TraceSummaryBar({
  summary,
  live,
}: {
  summary: TraceSummary | null | undefined
  live: boolean
}) {
  if (!summary) return null
  const stats = [
    {
      key: "ms",
      icon: Clock,
      label:
        (summary.invocations ?? 1) > 1
          ? `Agent time · ${summary.invocations} runs`
          : "Agent time",
      value: clock(summary.ms),
      title:
        summary.span_ms != null
          ? `Time the agents actually ran, excluding waits for review. ` +
            `Wall clock since it started: ${clock(summary.span_ms)}.`
          : "Time the agents actually ran, excluding waits for review.",
    },
    {
      key: "tok",
      icon: Coins,
      label: "Tokens",
      value: summary.tokens ? compactNumber(summary.tokens.total) : "—",
      title: summary.tokens
        ? `${summary.tokens.prompt.toLocaleString()} in · ${summary.tokens.output.toLocaleString()} out`
        : undefined,
    },
    {
      key: "tools",
      icon: Wrench,
      label: "Tool calls",
      value: String(summary.tool_calls ?? 0),
    },
    {
      key: "events",
      icon: Activity,
      label: "Events",
      value: String(summary.event_count),
    },
  ]

  return (
    <div className="grid grid-cols-2 border-y border-[var(--border)] sm:grid-cols-4">
      {stats.map(({ key, icon: Icon, label, value, title }, index) => (
        <div
          key={key}
          title={title}
          className={cn(
            "min-w-0 px-3 py-3 sm:px-4",
            index % 2 !== 0 && "border-l border-[var(--border)]",
            index >= 2 && "border-t border-[var(--border)] sm:border-t-0",
            index > 0 && "sm:border-l sm:border-[var(--border)]",
          )}
        >
          <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.06em] text-[var(--muted-foreground)]">
            <Icon className="size-3.5" />
            {label}
          </div>
          <div className="mt-1.5 flex items-baseline gap-1.5">
            <span className="font-mono text-base font-semibold leading-none tabular-nums">
              {value}
            </span>
            {live && key === "ms" && (
              <span
                aria-hidden
                className="size-1.5 animate-pip-pulse rounded-full bg-[var(--phase-generate)]"
              />
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

type GroupStat = {
  ms: number | null
  tokens: { prompt: number; output: number; total: number }
  toolCalls: number
}

/**
 * Timing and cost for ONE appearance of an agent, from its own events.
 *
 * Looking these up by agent name was wrong: an agent appears in the trace once
 * per contiguous block of its events, and the orchestrator interleaves with
 * every child - so it shows up four or five times in a single run, and a
 * rework round brings agents back again. Keying on the name gave every one of
 * those blocks the SAME aggregate, which for the orchestrator meant each of
 * its appearances claiming the entire run's duration.
 *
 * The per-agent totals still have their place - that is what the summary bar
 * reports - but a row in the timeline must describe the segment it actually
 * represents.
 */
function groupStat(events: RunEvent[]): GroupStat {
  const tokens = { prompt: 0, output: 0, total: 0 }
  let toolCalls = 0
  let first: number | null = null
  let last: number | null = null

  for (const event of events) {
    const stamp = event.ts ?? event.created_at
    if (stamp) {
      const t = new Date(stamp).getTime()
      if (!Number.isNaN(t)) {
        if (first === null || t < first) first = t
        if (last === null || t > last) last = t
      }
    }
    const tok = event.data?.tokens as
      | { prompt?: number; output?: number; total?: number }
      | undefined
    if (tok) {
      tokens.prompt += tok.prompt ?? 0
      tokens.output += tok.output ?? 0
      tokens.total += tok.total ?? 0
    }
    toolCalls += (event.tools ?? []).filter((t) => t.args !== "").length
  }

  return {
    ms: first !== null && last !== null ? last - first : null,
    tokens,
    toolCalls,
  }
}

/**
 * A string that changes whenever anything a block RENDERS changes.
 *
 * The master-detail view uses this to recompute only the selected block's
 * derived tool list when a streamed event actually changes.
 */
function blockSignature(
  author: string,
  events: RunEvent[],
  stat: GroupStat,
): string {
  let textChars = 0
  let toolChars = 0
  for (const event of events) {
    textChars += event.text?.length ?? 0
    for (const tool of event.tools ?? []) {
      toolChars +=
        tool.name.length +
        tool.status.length +
        (tool.ms ?? 0) +
        (tool.args?.length ?? 0) +
        (tool.result?.length ?? 0)
    }
  }
  return [
    author,
    events[0]?.seq ?? "",
    events[events.length - 1]?.seq ?? "",
    events.length,
    stat.ms ?? "",
    stat.tokens.total,
    stat.toolCalls,
    textChars,
    toolChars,
  ].join("|")
}

type AgentBlock = {
  author: string
  events: RunEvent[]
  stat: GroupStat
  occurrence: number
  totalOccurrences: number
  signature: string
}

function agentLabel(block: AgentBlock): string {
  const base = AGENT_LABELS[block.author] ?? block.author
  return block.totalOccurrences > 1
    ? `${base} · pass ${block.occurrence}`
    : base
}

function eventTime(event: RunEvent): string {
  const stamp = event.ts ?? event.created_at
  if (!stamp) return "—"
  const parsed = new Date(stamp)
  if (Number.isNaN(parsed.getTime())) return "—"
  return parsed.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })
}

const AgentRunRow = React.memo(function AgentRunRow({
  block,
  active,
  selected,
  onSelect,
}: {
  block: AgentBlock
  active: boolean
  selected: boolean
  onSelect: () => void
}) {
  const failed = block.events.some((event) => event.kind === "error")
  const label = agentLabel(block)

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "relative w-full border-b border-[var(--border)] px-3 py-3 text-left transition-colors last:border-b-0 hover:bg-[var(--muted)] focus-visible:z-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--ring)]",
        selected && "bg-[var(--card)]",
        selected && "before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full before:bg-[var(--brand)]",
        failed && selected && "before:bg-[var(--destructive)]",
      )}
    >
      <span className="flex items-center gap-2">
        <span
          aria-hidden
          className={cn("size-2 shrink-0 rounded-full", active && "animate-pip-pulse")}
          style={{
            backgroundColor: failed
              ? "var(--destructive)"
              : active
                ? "var(--phase-qa)"
                : "var(--brand)",
          }}
        />
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold">{label}</span>
        {failed && <AlertTriangle className="size-3.5 shrink-0 text-[var(--destructive)]" />}
        <ChevronRight className={cn("size-3.5 shrink-0 text-[var(--muted-foreground)]", selected && "text-[var(--foreground)]")} />
      </span>
      <span className="mt-2 grid grid-cols-3 gap-2 pl-4 font-mono text-[10px] tabular-nums text-[var(--muted-foreground)]">
        <span>{active ? "running" : duration(block.stat.ms)}</span>
        <span>{block.stat.tokens.total ? `${compactNumber(block.stat.tokens.total)} tok` : "— tok"}</span>
        <span>
          {block.stat.toolCalls || "—"} {block.stat.toolCalls === 1 ? "call" : "calls"}
        </span>
      </span>
    </button>
  )
})

function TraceBlockDetails({
  block,
  active,
}: {
  block: AgentBlock
  active: boolean
}) {
  const [view, setView] = React.useState<"activity" | "tools">("activity")
  const failed = block.events.some((event) => event.kind === "error")
  const tools = React.useMemo(
    () => block.events.flatMap((event) => event.tools ?? []),
    // The signature changes when a streamed tool response fills in.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [block.signature],
  )
  const label = agentLabel(block)

  return (
    <div className="flex min-h-0 flex-col">
      <div className="border-b border-[var(--border)] px-4 py-4 sm:px-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="truncate text-[15px] font-semibold">{label}</h3>
              {active && <Chip tone="generate" dot pulse>running</Chip>}
              {failed && <Chip tone="failed" dot>error</Chip>}
            </div>
            {AGENT_BLURBS[block.author] && (
              <p className="mt-1 max-w-2xl text-xs leading-relaxed text-[var(--muted-foreground)]">
                {AGENT_BLURBS[block.author]}
              </p>
            )}
          </div>
          <div className="flex shrink-0 gap-4 font-mono text-[11px] tabular-nums text-[var(--muted-foreground)]">
            <span>{active ? "running" : duration(block.stat.ms)}</span>
            <span title={`${block.stat.tokens.prompt.toLocaleString()} in · ${block.stat.tokens.output.toLocaleString()} out`}>
              {block.stat.tokens.total ? compactNumber(block.stat.tokens.total) : "—"} tokens
            </span>
            <span>{block.stat.toolCalls || 0} calls</span>
          </div>
        </div>

        <div className="mt-4 inline-flex rounded-[9px] bg-[var(--muted)] p-0.5" role="tablist" aria-label="Selected agent details">
          {(["activity", "tools"] as const).map((item) => (
            <button
              key={item}
              type="button"
              role="tab"
              aria-selected={view === item}
              onClick={() => setView(item)}
              className={cn(
                "rounded-[7px] px-3 py-1.5 text-xs font-medium capitalize transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
                view === item
                  ? "bg-[var(--background)] text-[var(--foreground)] shadow-sm"
                  : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]",
              )}
            >
              {item === "tools" ? `Tool calls (${tools.length})` : `Activity (${block.events.length})`}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {view === "activity" ? (
          <div role="tabpanel" aria-label="Activity">
            <div className="sticky top-0 z-[1] hidden grid-cols-[3.25rem_6.5rem_6rem_minmax(0,1fr)] gap-3 border-b border-[var(--border)] bg-[var(--card)] px-4 py-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--muted-foreground)] sm:grid">
              <span>#</span>
              <span>Time</span>
              <span>Event</span>
              <span>Details</span>
            </div>
            <div>
              {block.events.map((event, index) => {
                const text = event.text || (event.kind === "error" ? String(event.data?.error ?? "error") : "No text payload")
                return (
                  <div
                    key={event.id ?? `${event.seq}-${index}`}
                    className="grid gap-1 border-b border-[var(--border)] px-4 py-3 text-xs last:border-b-0 sm:grid-cols-[3.25rem_6.5rem_6rem_minmax(0,1fr)] sm:gap-3"
                  >
                    <span className="hidden font-mono tabular-nums text-[var(--muted-foreground)] sm:block">
                      {String(event.seq).padStart(2, "0")}
                    </span>
                    <span className="font-mono text-[10px] tabular-nums text-[var(--muted-foreground)] sm:text-[11px]">
                      {eventTime(event)}
                    </span>
                    <span className="w-fit rounded-full bg-[var(--muted)] px-2 py-0.5 text-[10px] font-medium capitalize text-[var(--muted-foreground)]">
                      {event.kind}
                    </span>
                    <span className={cn("min-w-0 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed", event.kind === "error" && "text-[var(--destructive)]")}>
                      {text}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        ) : (
          <div role="tabpanel" aria-label="Tool calls">
            {tools.length ? (
              tools.map((tool, index) => (
                <ToolCallRow key={tool.key ?? `${tool.id || tool.name}-${index}`} tool={tool} />
              ))
            ) : (
              <p className="p-8 text-center text-sm text-[var(--muted-foreground)]">
                This agent pass did not use any tools.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * The run trace.
 *
 * Frames come from ADK's own event transcript - the same log the /dev
 * inspector renders - so every task has a trace regardless of which surface
 * started it, and what is shown matches the inspector rather than
 * approximating it.
 *
 * Text is displayed verbatim from the server. The STRUCTURE (phase, tokens,
 * tool latency) comes from each event's data payload, never from parsing that
 * text - see app/runs/stream.py.
 */
export function AgentTrace({
  events,
  summary,
  live,
  synced,
}: {
  events: RunEvent[]
  summary?: TraceSummary | null
  live: boolean
  synced: boolean
}) {
  // Each contiguous block of one agent's events, with the stats for THAT
  // block - not for every time the agent ran.
  const blocks = React.useMemo(() => {
    const grouped = groupByAuthor(events)
    const seen: Record<string, number> = {}
    const counts: Record<string, number> = {}
    for (const g of grouped) counts[g.author] = (counts[g.author] ?? 0) + 1
    return grouped.map((g) => {
      seen[g.author] = (seen[g.author] ?? 0) + 1
      const stat = groupStat(g.events)
      return {
        ...g,
        stat,
        occurrence: seen[g.author],
        totalOccurrences: counts[g.author],
        // What this block IS, as a string that changes if any of it changes.
        //
        // Sequence numbers bound the frames and the count catches anything
        // inserted between them, which covers the ordinary case: a trace is
        // append-only and ADK never renumbers.
        //
        // The two lengths cover the case that is not append-only. A frame can
        // be REVISED in place - text streamed in against a seq that already
        // exists, or a tool call whose result lands after the call itself was
        // recorded. Neither moves a sequence number or a count, so without
        // these the memo would hold a half-written frame on screen and never
        // correct it. Summing lengths is O(1) per string and catches any edit
        // that changes what is rendered.
        signature: blockSignature(g.author, g.events, stat),
      }
    })
  }, [events])
  const [selectedKey, setSelectedKey] = React.useState<string | null>(null)
  const runListRef = React.useRef<HTMLDivElement>(null)
  const blockKey = React.useCallback(
    (block: AgentBlock) => `${block.author}:${block.occurrence}`,
    [],
  )
  const selected =
    blocks.find((block) => blockKey(block) === selectedKey) ?? blocks[blocks.length - 1]

  React.useLayoutEffect(() => {
    // The newest pass is selected by default. Keep that row visible in the
    // compact mobile master list instead of showing the first rows while the
    // inspector describes a row below the fold.
    if (!selectedKey && runListRef.current) {
      runListRef.current.scrollTop = runListRef.current.scrollHeight
    }
  }, [blocks.length, selectedKey])

  if (!synced && events.length === 0) {
    return (
      <div className="space-y-2">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="h-14 rounded-[var(--radius)] border border-[var(--border)] bg-[var(--muted)]"
          />
        ))}
      </div>
    )
  }

  if (events.length === 0) {
    return (
      <p className="rounded-[var(--radius)] border border-dashed border-[var(--border)] p-6 text-center text-sm text-[var(--muted-foreground)]">
        No trace for this task. Its agent transcript is gone — most often
        because the session was deleted from the agent inspector.
      </p>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-[var(--radius)] border border-[var(--border)] bg-[var(--background)]">
      <TraceSummaryBar summary={summary} live={live} />

      <div className="grid min-h-[30rem] md:min-h-0 md:flex-1 md:grid-cols-[17rem_minmax(0,1fr)]">
        <aside className="border-b border-[var(--border)] md:min-h-0 md:border-b-0 md:border-r">
          <div className="flex items-center justify-between border-b border-[var(--border)] px-3 py-2.5">
            <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--muted-foreground)]">
              Agent runs
            </h3>
            <span className="font-mono text-[10px] tabular-nums text-[var(--muted-foreground)]">
              {blocks.length}
            </span>
          </div>
          <div
            ref={runListRef}
            className="max-h-64 overflow-auto md:max-h-none md:h-[calc(100%-2.375rem)]"
          >
            {blocks.map((block, index) => {
              const key = blockKey(block)
              return (
                <AgentRunRow
                  key={key}
                  block={block}
                  active={live && index === blocks.length - 1}
                  selected={selected === block}
                  onSelect={() => setSelectedKey(key)}
                />
              )
            })}
          </div>
        </aside>

        {selected && (
          <TraceBlockDetails
            key={blockKey(selected)}
            block={selected}
            active={live && selected === blocks[blocks.length - 1]}
          />
        )}
      </div>
    </div>
  )
}
