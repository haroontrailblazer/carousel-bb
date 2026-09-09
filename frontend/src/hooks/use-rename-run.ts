import { useMutation, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { ApiError, api } from "@/lib/api"
import { RUNS_KEY, type RunsResponse } from "@/lib/queries"
import type { RunDetail } from "@/lib/types"

/**
 * Why the rename did not stick, in words the person can act on.
 *
 * The distinction that matters is between "this task is gone" and "this
 * server does not have the endpoint". The second is what a deployment
 * half-updated looks like - a browser running the new console against a
 * backend process started before `PATCH /api/runs/{id}` existed - and it is
 * indistinguishable from a bug unless it is named. The catch-all that serves
 * the single-page app answers an unknown /api path with a 404 carrying no
 * body, so the absence of a machine-readable `code` is exactly the signal.
 */
function renameFailureMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "no_such_run") return "That task no longer exists."
    if ((error.status === 404 || error.status === 405) && !error.code) {
      return "This server does not support renaming yet - it may need restarting."
    }
    return error.message
  }
  return "Could not rename that task."
}

/**
 * Rename a task.
 *
 * The name is written to the server rather than kept in this browser, which
 * is the whole reason there is a `PATCH /api/runs/{id}` at all. A rename held
 * in localStorage would show on the laptop and not on the phone - the exact
 * shape of the staleness bug this console has already been through once, and
 * a worse version of it, because there would be no server copy to correct it
 * from.
 *
 * The optimistic update is what makes it feel like renaming rather than
 * submitting: the round trip to a remote database is 0.6-2s, and a name that
 * appears when you press Enter is a different interaction from one that
 * appears a second later.
 *
 * An empty title is a real value - it clears the custom name and lets the
 * generated one show through again - so it is passed through rather than
 * treated as nothing to do.
 */
export function useRenameRun() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({ runId, title }: { runId: string; title: string }) =>
      api<{ result: string; run_id: string; title: string | null }>(
        `/api/runs/${runId}`,
        { method: "PATCH", body: JSON.stringify({ title: title.trim() }) },
      ),

    onMutate: async ({ runId, title }) => {
      // Both places the name is on screen: the list every screen reads, and
      // the run's own record behind the task page and the chat header.
      await Promise.all([
        queryClient.cancelQueries({ queryKey: RUNS_KEY }),
        queryClient.cancelQueries({ queryKey: ["run", runId] }),
      ])

      const previousRuns = queryClient.getQueryData<RunsResponse>(RUNS_KEY)
      const previousRun = queryClient.getQueryData<RunDetail>(["run", runId])
      const next = title.trim() || null

      queryClient.setQueryData<RunsResponse>(RUNS_KEY, (old) =>
        old
          ? {
              ...old,
              items: old.items.map((run) =>
                run.run_id === runId ? { ...run, title: next } : run,
              ),
            }
          : old,
      )
      queryClient.setQueryData<RunDetail>(["run", runId], (old) =>
        old ? { ...old, title: next } : old,
      )

      return { previousRuns, previousRun }
    },

    onError: (error, { runId, title }, context) => {
      // Put back what was there. Leaving the optimistic name on screen would
      // tell the user a rename worked when it did not.
      if (context?.previousRuns) {
        queryClient.setQueryData(RUNS_KEY, context.previousRuns)
      }
      if (context?.previousRun) {
        queryClient.setQueryData(["run", runId], context.previousRun)
      }

      // And SAY SO. This is the whole reason renaming looked broken.
      //
      // An optimistic update paints the new name instantly, so a failed save
      // reverts a name the user watched appear - which is indistinguishable
      // from "typing a name and pressing Enter does nothing". Every other
      // mutation in this console reports its failures through a toast; this
      // one silently rolled back, and the result was a feature that looked
      // like it had simply not been built.
      toast.error(renameFailureMessage(error), {
        description: `"${title.trim()}" was not saved.`,
      })
    },

    onSettled: (_data, _error, { runId }) => {
      void queryClient.invalidateQueries({ queryKey: RUNS_KEY })
      void queryClient.invalidateQueries({ queryKey: ["run", runId] })
    },
  })
}
