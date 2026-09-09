import * as React from "react"
import { Navigate, useLocation, useNavigate } from "react-router"

import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { BrandLogo } from "@/components/layout/brand-logo"
import { Input, Label } from "@/components/ui/input"
import { useAuth } from "@/hooks/use-auth"
import { loadAuthConfig } from "@/lib/supabase"

/**
 * GoTrue's raw messages are not always the truth a user needs.
 *
 * "Invalid login credentials" is returned for a wrong password AND for an
 * unconfirmed email, and rate limiting arrives as an opaque 429. Mapping them
 * here is the difference between a user retrying usefully and giving up.
 */
function authMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error ?? "")
  const lower = raw.toLowerCase()
  if (lower.includes("invalid login credentials")) {
    return "That email and password do not match an account."
  }
  if (lower.includes("email not confirmed")) {
    return "That account still needs its email confirmed."
  }
  if (lower.includes("rate") || lower.includes("too many")) {
    return "Too many attempts. Wait a minute and try again."
  }
  if (lower.includes("not on the access list")) {
    return raw // the server's message is already specific and correct
  }
  return raw || "Could not sign in."
}

/**
 * Where to go once signed in.
 *
 * Reads `?next=` first: that is what the SERVER sets when it turns away an
 * unauthenticated HTML request, and it is the only channel that survives a
 * cold navigation from outside the app - a Telegram review link, a bookmark,
 * a pasted URL. React Router's `location.state` cannot carry those, because
 * there was no in-app navigation to attach state to.
 *
 * Only path-absolute, same-origin destinations are honoured. `//evil.com` and
 * `https://evil.com` are both rejected: an open redirect on a login page is a
 * phishing primitive - it lets an attacker send a real link to the real site
 * that deposits the user somewhere else immediately after they type a
 * password.
 */
function redirectTarget(search: string, stateFrom?: string): string {
  const candidate = new URLSearchParams(search).get("next") ?? stateFrom
  if (!candidate) return "/new"
  if (!candidate.startsWith("/") || candidate.startsWith("//")) return "/new"
  return candidate
}

export function LoginRoute() {
  const { status, signIn } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [email, setEmail] = React.useState("")
  const [password, setPassword] = React.useState("")
  const [error, setError] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [configured, setConfigured] = React.useState<boolean | null>(null)

  React.useEffect(() => {
    void loadAuthConfig().then((c) => setConfigured(c.configured))
  }, [])

  // A signed-in user landing here (via Back, or a stale bookmark) should not
  // see a login form.
  if (status === "in") {
    const from = (location.state as { from?: string } | null)?.from
    return <Navigate to={redirectTarget(location.search, from)} replace />
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError("")
    try {
      await signIn(email.trim(), password)
      const from = (location.state as { from?: string } | null)?.from
      navigate(redirectTarget(location.search, from), { replace: true })
    } catch (err) {
      setError(authMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-dvh place-items-center bg-[var(--background)] px-4">
      <Card className="w-full max-w-sm p-7">
        <div className="mb-6 flex items-center gap-2">
          <BrandLogo className="size-10" />
          <div>
            <h1 className="text-base font-semibold leading-tight">Carousel Factory</h1>
            <p className="text-xs text-[var(--muted-foreground)]">
              Sign in to run and review carousels.
            </p>
          </div>
        </div>

        {configured === false && (
          <p className="mb-4 rounded-[var(--radius-md)] bg-[var(--phase-failed-soft)] px-3 py-2 text-sm"
             style={{ color: "var(--phase-failed-fg)" }}>
            Sign-in is not configured on the server yet. Set SUPABASE_URL and
            SUPABASE_ANON_KEY, then reload.
          </p>
        )}

        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>

          {error && (
            <p
              className="rounded-[var(--radius-md)] px-3 py-2 text-sm"
              style={{
                background: "var(--phase-failed-soft)",
                color: "var(--phase-failed-fg)",
              }}
              role="alert"
            >
              {error}
            </p>
          )}

          <Button
            type="submit"
            variant="brand"
            className="w-full"
            disabled={busy || configured === false}
          >
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="mt-5 text-center text-xs text-[var(--muted-foreground)]">
          Accounts are invite-only. Ask an admin to add you.
        </p>
      </Card>
    </div>
  )
}
