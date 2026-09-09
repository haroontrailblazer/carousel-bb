/**
 * One run, read the same way everywhere.
 *
 * Two screens show the same task - the New carousel workspace and the task
 * page's tabs - and they read the same three React Query cache entries:
 * `["run", id]`, `["artifacts", id]` and `["trace", id]`. React Query keys the
 * cache by key alone, NOT by the options passed with it, so whichever screen
 * mounted last silently decides the refetch interval, the retry policy and the
 * stale time for both. Two copies of these options is therefore not a
 * duplication smell, it is a live bug: the behaviour of a screen changes
 * depending on which screen you visited before it.
 *
 * So the options live here, once, and both screens call this. Anything that
 * needs to differ between them is a parameter, not a second opinion.
 */

import { useQueryClient, useQuery } from "@tanstack/react-query"
import * as React from "react"

import { PULSE_KEY } from "@/hooks/use-pulse"
import { useRunStream } from "@/hooks/use-run-stream"
import { get } from "@/lib/api"
import type { RunArtifacts, RunDetail } from "@/lib/types"

/**
 * How hard to re-ask for the run snapshot.
 *
 * `awaiting_review` is polled rather than left alone because the same task can
 * be decided from Telegram, and this snapshot is what notices. Returning
 * `false` is permanent - React Query re-evaluates this callback only after a
 * fetch settles, so there is no later fetch to reconsider it - which is why a
 * finished task gets `false` and nothing else does.
 */
function runInterval(status: string | undefined): number | false {
  if (status === "running") return 4_000
  if (status === "awaiting_review") return 8_000
  return false
}

export type RunWorkspace = {
  run: ReturnType<typeof useQuery<RunDetail>>
  artifacts: ReturnType<typeof useQuery<RunArtifacts>>
  stream: ReturnType<typeof useRunStream>
  /** True while agents are actually working, which is what drives the stream. */
  isLive: boolean
}

export function useRunWorkspace(runId: string | null): RunWorkspace {
  const queryClient = useQueryClient()

  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => get<RunDetail>(`/api/runs/${runId}`),
    enabled: !!runId,
    refetchInterval: (query) => runInterval(query.state.data?.status),
    refetchIntervalInBackground: false,
  })

  const isLive = run.data?.status === "running"

  const artifacts = useQuery({
    queryKey: ["artifacts", runId],
    queryFn: () => get<RunArtifacts>(`/api/runs/${runId}/artifacts`),
    enabled: !!runId,
    // A 404 is the normal early state - the carousel is only assembled at the
    // end of the generate phase - so retrying it is a request storm that
    // answers the same way every time.
    retry: false,
    // While the task works, a rework can rewrite the bundle and every slide at
    // any moment. Once it is finished the carousel is immutable and the only
    // reason left to re-ask is signed-URL expiry, which is an hour away.
    refetchInterval: (query) =>
      isLive ? 15_000 : query.state.data ? 15 * 60_000 : false,
    refetchIntervalInBackground: false,
  })

  const stream = useRunStream(runId, {
    live: isLive,
    onPhase: () => {
      void queryClient.invalidateQueries({ queryKey: ["run", runId] })
      // The bundle is written at a phase boundary and rework rewrites it.
      void queryClient.invalidateQueries({ queryKey: ["artifacts", runId] })
      // Reaching review is the moment the sidebar dot has to change, and the
      // stream knows before any poll does.
      void queryClient.invalidateQueries({ queryKey: PULSE_KEY })
    },
    onEnd: () => {
      void queryClient.invalidateQueries({ queryKey: ["run", runId] })
      void queryClient.invalidateQueries({ queryKey: ["artifacts", runId] })
      void queryClient.invalidateQueries({ queryKey: ["runs"] })
      void queryClient.invalidateQueries({ queryKey: PULSE_KEY })
    },
  })

  // A gap means the browser fell behind and lost events. The database still
  // has them, so the thing to refetch is the TRACE - refetching the run
  // snapshot instead left the hole on screen.
  React.useEffect(() => {
    if (stream.gapped) {
      void queryClient.invalidateQueries({ queryKey: ["trace", runId] })
    }
  }, [stream.gapped, queryClient, runId])

  return { run, artifacts, stream, isLive }
}

/**
 * Everything a just-stopped or just-restarted task needs re-read.
 *
 * Pressing Stop changes the run row, the timeline and the counts behind the
 * sidebar dots. Invalidating only some of them is how a stopped task keeps
 * pulsing: the header catches up, the trace does not.
 */
export function invalidateRun(
  queryClient: ReturnType<typeof useQueryClient>,
  runId: string,
): void {
  void queryClient.invalidateQueries({ queryKey: ["run", runId] })
  void queryClient.invalidateQueries({ queryKey: ["trace", runId] })
  void queryClient.invalidateQueries({ queryKey: ["artifacts", runId] })
  void queryClient.invalidateQueries({ queryKey: ["runs"] })
  void queryClient.invalidateQueries({ queryKey: PULSE_KEY })
}
