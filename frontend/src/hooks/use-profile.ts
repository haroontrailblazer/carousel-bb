import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { gravatarUrl } from "@/components/layout/user-avatar"
import { useAuth } from "@/hooks/use-auth"
import { supabase } from "@/lib/supabase"

export type Profile = {
  email: string
  /** Display name, or the email's local part when none is set. */
  displayName: string
  /** What the user actually typed, empty when unset. */
  name: string
  avatarUrl: string | null
}

/** Cached shape. `gravatar` is resolved once and kept, so it costs one hash. */
type ProfileData = {
  name: string
  avatarUrl: string | null
  gravatar: string | null
}

export const PROFILE_KEY = (email: string) => ["profile", email] as const

/** Bumped when the shape changes, so an old snapshot is ignored, not rendered. */
const SNAPSHOT_VERSION = "v1"

/**
 * Scoped by email on purpose: with two accounts on one browser, neither must
 * ever paint the other one's face for the frame before the network answers.
 */
function snapshotKey(email: string): string {
  return `carousel-profile-${SNAPSHOT_VERSION}-${email}`
}

/**
 * A remembered profile is UNTRUSTED input - another tab, an older build or a
 * curious person could have written anything into that key - so every field
 * is checked before a component is handed it.
 */
function readSnapshot(email: string): ProfileData | undefined {
  if (!email) return undefined
  try {
    const raw = localStorage.getItem(snapshotKey(email))
    if (!raw) return undefined
    const parsed = JSON.parse(raw) as Partial<ProfileData>
    const str = (v: unknown) => (typeof v === "string" ? v : "")
    return {
      name: str(parsed.name),
      avatarUrl: str(parsed.avatarUrl) || null,
      gravatar: str(parsed.gravatar) || null,
    }
  } catch {
    return undefined
  }
}

function writeSnapshot(email: string, value: ProfileData): void {
  if (!email) return
  try {
    localStorage.setItem(snapshotKey(email), JSON.stringify(value))
  } catch {
    /* private mode or a full quota: the avatar arrives with the network */
  }
}

/**
 * Ask Gravatar once per person, not once per page load.
 *
 * Gravatar is requested with `d=404` so that a person who has never heard of
 * it fails the image load and falls through to the generated monogram - which
 * is the right picture, but means the console was firing a cross-origin
 * request on every single load, to a third party, to be told 404 again. It
 * also meant handing that third party a hash of the user's email every time
 * the app opened, for nothing.
 *
 * So the answer is remembered. `null` is a real answer here - "this person has
 * no Gravatar" - and is cached exactly like a URL would be.
 *
 * The check itself is an <img> load rather than a fetch: Gravatar sends no
 * CORS headers, so a fetch can only be made in `no-cors` mode, which returns
 * an opaque response whose status cannot be read. An image element reports
 * success and failure without needing to see the response at all.
 */
const GRAVATAR_TTL_MS = 7 * 24 * 60 * 60 * 1000

function gravatarKey(email: string): string {
  return `carousel-gravatar-${SNAPSHOT_VERSION}-${email}`
}

/** In-memory, so several components mounting at once ask once between them. */
const gravatarInFlight = new Map<string, Promise<string | null>>()

function imageLoads(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = new Image()
    probe.onload = () => resolve(true)
    probe.onerror = () => resolve(false)
    probe.src = url
  })
}

async function resolveGravatar(email: string): Promise<string | null> {
  if (!email) return null

  const cached = gravatarInFlight.get(email)
  if (cached) return cached

  const task = (async () => {
    try {
      const raw = localStorage.getItem(gravatarKey(email))
      if (raw) {
        const saved = JSON.parse(raw) as { url: string | null; at: number }
        // Re-check occasionally: somebody who signs up for Gravatar later
        // should eventually see their picture rather than being told no
        // forever by a decision this browser made once.
        if (Date.now() - saved.at < GRAVATAR_TTL_MS) {
          return typeof saved.url === "string" ? saved.url : null
        }
      }
    } catch {
      /* unreadable or blocked: fall through and just ask */
    }

    const url = await gravatarUrl(email, 96)
    const verdict = url && (await imageLoads(url)) ? url : null
    try {
      localStorage.setItem(
        gravatarKey(email),
        JSON.stringify({ url: verdict, at: Date.now() }),
      )
    } catch {
      /* it will simply be asked again next time */
    }
    return verdict
  })()

  gravatarInFlight.set(email, task)
  return task
}

function metadataOf(user: { user_metadata?: unknown } | null | undefined) {
  const meta = (user?.user_metadata ?? {}) as Record<string, unknown>
  return {
    name: String(meta.username ?? meta.full_name ?? ""),
    avatarUrl: String(meta.avatar_url ?? "") || null,
  }
}

/**
 * The signed-in person's name and picture, kept true across devices.
 *
 * The profile lives in Supabase Auth `user_metadata` rather than a table of
 * our own: it is already per-user and already authenticated.
 *
 * The subtlety is WHERE it is read from. `user_metadata` is baked into the
 * access token at issue time, so `getSession()` - which reads the token this
 * browser holds in localStorage - reports whatever was true when that token
 * was minted. Set a picture on a phone and the laptop kept showing the old
 * one until its token happened to refresh, which is why it only ever appeared
 * after a hard reload. So the read here is `getUser()`, a real request to
 * Supabase, and it is a React Query entry rather than component state:
 *
 *  - one cache for every component that asks, instead of a private copy per
 *    caller each firing its own request,
 *  - refetched when the tab is looked at again (see the focus wiring in
 *    App.tsx), so returning to the laptop is enough,
 *  - seeded from a per-email snapshot so the first frame still paints a face.
 *
 * Gravatar is the fallback, requested with `d=404` so a missing one fails the
 * image load and the avatar falls back to a generated monogram. A generic
 * silhouette would look like a picture the user had set.
 */
export function useProfile() {
  const { identity } = useAuth()
  const queryClient = useQueryClient()
  const email = identity?.email ?? ""

  const query = useQuery({
    queryKey: PROFILE_KEY(email),
    enabled: !!email,
    queryFn: async (): Promise<ProfileData> => {
      // getUser() asks Supabase. getSession() is the offline answer: better a
      // slightly old name than an empty sidebar on a flaky connection.
      //
      // Both are allowed to fail. Neither is reachable when the server has no
      // Supabase configuration at all, and the console still has a person
      // signed in at that point - our own cookie says so. The right outcome is
      // the generated monogram, not a query stuck in an error state retrying
      // something that will never work.
      let user: { user_metadata?: unknown } | null = null
      try {
        const { data, error } = await supabase.auth.getUser()
        if (error) throw error
        user = data.user
      } catch {
        try {
          const { data } = await supabase.auth.getSession()
          user = data.session?.user ?? null
        } catch {
          user = null
        }
      }

      const { name, avatarUrl } = metadataOf(user)
      // Only worth resolving when it will actually be used - a Gravatar miss
      // is still a request to a third party.
      const gravatar = avatarUrl ? null : await resolveGravatar(email)
      const next: ProfileData = { name, avatarUrl, gravatar }
      writeSnapshot(email, next)
      return next
    },
    initialData: () => readSnapshot(email),
    // Dated to the epoch, so the snapshot paints instantly AND is treated as
    // stale - the real answer is already on its way behind it.
    initialDataUpdatedAt: 0,
    staleTime: 30_000,
  })

  // A rename or a new picture in ANOTHER TAB of this browser fires
  // USER_UPDATED here. Cross-device is handled by the focus refetch; this is
  // what makes one browser agree with itself immediately.
  React.useEffect(() => {
    if (!email) return
    return supabase.auth.onAuthStateChange(() => {
      void queryClient.invalidateQueries({ queryKey: PROFILE_KEY(email) })
    })
  }, [email, queryClient])

  const mutation = useMutation({
    mutationFn: async (next: { name?: string; avatarUrl?: string | null }) => {
      const payload: Record<string, unknown> = {}
      if (next.name !== undefined) payload.username = next.name.trim()
      if (next.avatarUrl !== undefined) payload.avatar_url = next.avatarUrl ?? ""
      const { data, error } = await supabase.auth.updateUser({ data: payload })
      if (error) throw new Error(error.message)
      return data
    },
    // Paint the change on the click. updateUser is a round trip to Supabase,
    // and waiting for it before redrawing the sidebar is the difference
    // between an app that responds and one that thinks about it.
    onMutate: async (next) => {
      const key = PROFILE_KEY(email)
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<ProfileData>(key)
      queryClient.setQueryData<ProfileData>(key, (old) => {
        const base: ProfileData = old ?? {
          name: "",
          avatarUrl: null,
          gravatar: null,
        }
        return {
          ...base,
          ...(next.name !== undefined ? { name: next.name.trim() } : {}),
          ...(next.avatarUrl !== undefined ? { avatarUrl: next.avatarUrl } : {}),
        }
      })
      return { previous }
    },
    onError: (_error, _next, context) => {
      // Put back what was there. Leaving the optimistic value on screen would
      // tell the user a save worked when it did not.
      if (context?.previous) {
        queryClient.setQueryData(PROFILE_KEY(email), context.previous)
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: PROFILE_KEY(email) })
    },
  })

  // mutateAsync, not `mutation`: React Query keeps the bound methods stable
  // across renders, so `save` is stable too and does not re-fire the effects
  // of anything that lists it as a dependency.
  const { mutateAsync } = mutation
  const save = React.useCallback(
    async (next: { name?: string; avatarUrl?: string | null }) => {
      await mutateAsync(next)
    },
    [mutateAsync],
  )

  const reload = React.useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: PROFILE_KEY(email) })
  }, [email, queryClient])

  const data = query.data
  const name = data?.name ?? ""

  const profile = React.useMemo(
    () =>
      ({
        email,
        name,
        displayName: name || email.split("@")[0] || "Account",
        avatarUrl: data?.avatarUrl || data?.gravatar || null,
      }) satisfies Profile,
    [email, name, data?.avatarUrl, data?.gravatar],
  )

  return { profile, save, reload, saving: mutation.isPending }
}
