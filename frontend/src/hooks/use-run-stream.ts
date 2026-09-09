/**
 * A run's trace: full history over plain JSON, live updates over SSE.
 *
 * **History does not come from the stream.** SSE is lovely when the network
 * cooperates and invisible when it does not: Cloudflare quick tunnels buffer
 * the response body, and because an event stream never ends, the browser holds
 * an open connection that delivers nothing - measured as zero bytes in sixty
 * seconds while the identical request returned all 34 frames locally. Plenty
 * of corporate proxies behave the same way.
 *
 * So the trace is fetched like any other resource, and the stream is used only
 * to append what happens next. Where SSE works you get live updates; where it
 * does not you still get the whole trace. The alternative - streaming as the
 * only source - fails silently and looks exactly like "the app is broken".
 *
 * Frames come from ADK's own transcript, so every task has a trace no matter
 * which surface started it, and what is shown matches the /dev inspector.
 */

import * as React from "react"
import { useQuery } from "@tanstack/react-query"

import { get } from "@/lib/api"
import type { RunEvent, Trace, TraceSummary } from "@/lib/types"

export type TraceState = {
  events: RunEvent[]
  /** True once the history has loaded (whether or not the stream connected). */
  synced: boolean
  /** Latest phase seen, if any. */
  phase: string | null
  /** The client was told it missed events and should refetch. */
  gapped: boolean
  /** Whether the live stream is currently connected. */
  connected: boolean
  /**
   * Whether the trace has actually stopped keeping up.
   *
   * NOT the same as `connected`. SSE is an optimisation here - Cloudflare
   * buffers event streams, so `connected` is false on every tunnelled session
   * even while 3s polling keeps the trace perfectly in sync. Reporting that as
   * a connection problem cried wolf on a view that was working.
   *
   * This is true only when the polling itself is failing, and only while the
   * run is live: a finished trace is immutable and is not being fetched at
   * all, so it can never be out of date.
   */
  stale: boolean
  /** True while the history request is in flight. */
  loading: boolean
  /** Per-agent timing and token totals for the run. */
  summary: TraceSummary | null
}

/**
 * What makes two frames the same frame.
 *
 * Falls back to the position for a frame from a server that predates `id`,
 * which is no worse than the behaviour it replaces.
 */
function frameKey(event: RunEvent): string {
  return event.id ?? `seq:${event.seq}`
}

export function useRunStream(
  runId: string | null,
  {
    onPhase,
    onEnd,
    live = true,
  }: { onPhase?: (phase: string) => void; onEnd?: () => void; live?: boolean } = {},
): TraceState {
  const [tail, setTail] = React.useState<RunEvent[]>([])
  const [connected, setConnected] = React.useState(false)
  const [gapped, setGapped] = React.useState(false)

  // The whole trace, as an ordinary request.
  const history = useQuery({
    queryKey: ["trace", runId],
    queryFn: () => get<Trace>(`/api/runs/${runId}/trace`),
    enabled: !!runId,
    // No stale window while a run is going: every poll should ask.
    staleTime: live ? 0 : 30_000,
    // Poll HARD while the agents are working.
    //
    // The live stream is an optimisation, not the mechanism - Cloudflare and
    // many corporate proxies buffer SSE and deliver nothing until a stream
    // ends, which for an event stream is never. Polling is what actually keeps
    // the trace in sync, so it has to be fast enough that nobody reaches for
    // reload.
    //
    // 3s is chosen against the endpoint's measured cost (~0.6-1.2s locally,
    // ~2s over a tunnel): quick enough to feel live, slow enough that requests
    // never overlap. Once the run is finished the transcript is immutable, so
    // polling stops entirely.
    refetchInterval: live ? 3_000 : false,
    // A backgrounded tab does not need a 3s heartbeat.
    refetchIntervalInBackground: false,
  })

  const onPhaseRef = React.useRef(onPhase)
  const onEndRef = React.useRef(onEnd)
  React.useEffect(() => {
    onPhaseRef.current = onPhase
    onEndRef.current = onEnd
  })

  React.useEffect(() => {
    setTail([])
    setGapped(false)
    setConnected(false)
  }, [runId])

  React.useEffect(() => {
    if (!runId || !live) return

    const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`, {
      withCredentials: true,
    })

    source.onopen = () => setConnected(true)
    source.addEventListener("synced", () => setConnected(true))

    source.addEventListener("run", (message) => {
      let event: RunEvent
      try {
        event = JSON.parse((message as MessageEvent).data)
      } catch {
        // Ignore a malformed frame rather than tearing down the stream; the
        // next one is very likely fine.
        return
      }
      if (event.kind === "gap") {
        setGapped(true)
        return
      }
      setTail((current) =>
        current.some((e) => frameKey(e) === frameKey(event))
          ? current
          : [...current, event],
      )
      const phase = event.data?.phase
      if (typeof phase === "string") onPhaseRef.current?.(phase)
      if (event.kind === "terminal") onEndRef.current?.()
    })

    source.onerror = () => setConnected(false)

    return () => source.close()
  }, [runId, live])

  const events = React.useMemo(() => {
    const base = history.data?.items ?? []
    if (!tail.length) return base
    // The stream renumbers live frames onto the end of the replayed history,
    // and a poll may have already picked the same events up from ADK.
    //
    // Dedupe on IDENTITY, not position. The renumbered seq is a guess about
    // where the next poll will place the same event, and the two sources
    // count different things - so a wrong guess either filtered out a real
    // frame (the terminal "Waiting for your review" line was the reliable
    // casualty) or left the same line rendered twice.
    const seen = new Set(base.map(frameKey))
    return [...base, ...tail.filter((e) => !seen.has(frameKey(e)))]
  }, [history.data, tail])

  const phase = React.useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const p = events[i].data?.phase
      if (typeof p === "string") return p
    }
    return null
  }, [events])

  return {
    events,
    synced: !history.isLoading,
    phase,
    gapped,
    connected,
    // failureCount covers the window where a request has failed and React
    // Query is retrying; isError is the state after retries are exhausted.
    // Either one means this view is no longer tracking the run.
    stale: live && (history.isError || history.failureCount > 0),
    loading: history.isLoading,
    summary: history.data?.summary ?? null,
  }
}

/** Group a flat event list by the agent that produced it, preserving order. */
export function groupByAuthor(
  events: RunEvent[],
): { author: string; events: RunEvent[] }[] {
  const groups: { author: string; events: RunEvent[] }[] = []
  for (const event of events) {
    const author = event.author || "carousel_orchestrator"
    const last = groups[groups.length - 1]
    if (last && last.author === author) last.events.push(event)
    else groups.push({ author, events: [event] })
  }
  return groups
}
