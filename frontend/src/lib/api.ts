/**
 * The fetch wrapper everything goes through.
 *
 * Ported from C:\Projects\Oreag\frontend\src\lib\api.ts, keeping the parts that
 * earned their place there and dropping the Next.js-specific machinery.
 *
 * What carried over, and why:
 *
 *  - A module-level Set of session-expired listeners rather than React context.
 *    This is called from query functions, event handlers and cache internals
 *    that do not sit inside a provider, so context is simply not available at
 *    the call site.
 *  - Single-shot redirect guards. When a session dies, every in-flight request
 *    401s at once; without the guard you get a burst of navigations.
 *  - signOut({ scope: "local" }), so one expired tab does not sign the user out
 *    on their phone as well.
 *  - Clearing the dead token BEFORE navigating to /login, or the login screen
 *    reads a stale session from storage and bounces straight back.
 *
 * What is new here: `ApiError.code`. Oreag reads a machine-readable header
 * (X-MFA-Required) instead of matching on message text, and the comment there
 * spells out why - rewording a message must never break a code path. This app
 * has the same problem in a different shape: "someone already decided this run
 * from Telegram" is a normal outcome that the UI must detect exactly. So the
 * backend returns `detail.code` and callers branch on that, NEVER on the
 * message string.
 */

import { supabase } from "@/lib/supabase"

/** Same-origin in production (FastAPI serves this bundle) and in dev (Vite
 *  proxies /api), so there is no base URL to compute. */
const API_BASE = ""

export class ApiError extends Error {
  status: number
  /** Machine-readable discriminator from the response body. Branch on this. */
  code?: string
  /** The full structured detail, when the server sent one. */
  detail?: unknown

  constructor(message: string, status: number, code?: string, detail?: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.code = code
    this.detail = detail
  }

  /**
   * True when this failure means "sign in again".
   *
   * Callers must NOT render this error: a redirect is already in flight, so
   * the right UI is the loading state, not a red banner that flashes for
   * 200ms on the way out.
   */
  get sessionExpired(): boolean {
    return this.status === 401
  }
}

type SessionExpiredListener = () => void
const sessionExpiredListeners = new Set<SessionExpiredListener>()

export function onSessionExpired(listener: SessionExpiredListener): () => void {
  sessionExpiredListeners.add(listener)
  return () => sessionExpiredListeners.delete(listener)
}

let redirectingToLogin = false

/**
 * Pages that render fine with no session, and must never be redirected away
 * from. /login especially: sending /login to /login is an infinite reload, and
 * the usual in-flight guard cannot stop it because window.location.replace
 * reloads the page and resets every module-level variable - including that
 * guard. It only ever protected concurrent requests within ONE page load.
 */
const PUBLIC_ROUTES = ["/login", "/reset-password"]

function onPublicRoute(): boolean {
  const path = window.location.pathname
  return PUBLIC_ROUTES.some((p) => path === p || path.startsWith(`${p}/`))
}

async function redirectToLogin(): Promise<void> {
  if (redirectingToLogin) return
  if (onPublicRoute()) return
  redirectingToLogin = true

  sessionExpiredListeners.forEach((listener) => {
    try {
      listener()
    } catch {
      /* an overlay failing must not stop the redirect */
    }
  })

  // Clear the dead token first. /login reads the stored session on mount, and
  // a stale one sends the user straight back to where they came from.
  try {
    await supabase.auth.signOut({ scope: "local" })
  } catch {
    /* already gone */
  }
  // pathname + search: the tab a review link asked for lives in the query,
  // and dropping it lands the reviewer one screen short of where they were.
  window.location.replace(
    `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`,
  )
}

async function parseError(response: Response): Promise<ApiError> {
  let message = `Request failed (${response.status})`
  let code: string | undefined
  let detail: unknown

  try {
    const body = await response.json()
    detail = body?.detail ?? body
    if (detail && typeof detail === "object") {
      const d = detail as Record<string, unknown>
      if (typeof d.code === "string") code = d.code
      if (typeof d.message === "string") message = d.message
      else if (typeof d.error === "string") message = d.error
    } else if (typeof detail === "string") {
      message = detail
    }
    if (!code && typeof body?.code === "string") code = body.code
    if (typeof body?.error === "string" && message.startsWith("Request failed")) {
      message = body.error
    }
  } catch {
    /* not JSON - keep the generic message */
  }

  return new ApiError(message, response.status, code, detail)
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    // The session lives in an httpOnly cookie, so it must be sent explicitly.
    // The same cookie is what lets the ADK dev UI and EventSource work at all.
    credentials: "include",
    // Never let the HTTP cache answer for the API.
    //
    // React Query decides when this app re-asks a question; the browser cache
    // deciding a second time, on rules we do not set, is how a settings screen
    // ends up insisting Telegram is disconnected after it was connected on
    // another device. These responses carry no Cache-Control, which does not
    // mean "do not cache" - it means the browser is free to guess from
    // Last-Modified, and it does. Nothing is given up by opting out: an API
    // answer we deliberately re-requested is one we want from the server.
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(init.headers ?? {}),
    },
  })

  if (response.status === 401) {
    void redirectToLogin()
    throw new ApiError("Your session has expired.", 401, "unauthenticated")
  }

  if (!response.ok) throw await parseError(response)

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const get = <T>(path: string) => api<T>(path)

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) })

export const del = <T>(path: string) => api<T>(path, { method: "DELETE" })

/**
 * POST raw bytes (an already-compressed image, not a form).
 *
 * Deliberately not multipart: there is exactly one part, and multipart would
 * add a boundary, a parser dependency on the server and nothing else.
 */
export const postBytes = <T>(path: string, body: Blob) =>
  api<T>(path, {
    method: "POST",
    body,
    headers: { "Content-Type": body.type || "application/octet-stream" },
  })

/**
 * Ask a question about auth state WITHOUT the answer causing a navigation.
 *
 * `api()` treats 401 as "your session expired, go sign in". That is right for
 * a data request and wrong for "am I signed in?", where 401 is simply the
 * answer "no" - and acting on it navigates, which on the login page means
 * redirecting /login to /login forever.
 *
 * Returns null instead of throwing when unauthenticated.
 */
export async function probe<T>(path: string): Promise<T | null> {
  const response = await fetch(path, {
    credentials: "include",
    cache: "no-store",
    headers: { Accept: "application/json" },
  })
  if (response.status === 401 || response.status === 403) return null
  if (!response.ok) return null
  return (await response.json()) as T
}
