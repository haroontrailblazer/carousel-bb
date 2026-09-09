import * as React from "react"
import { useNavigate } from "react-router"

import { BrandLogo } from "@/components/layout/brand-logo"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input, Label } from "@/components/ui/input"
import { supabase } from "@/lib/supabase"

/**
 * The landing page for a Supabase password-recovery email.
 *
 * This route MUST stay public. Oreag's proxy.ts documents what happens when an
 * email-link target is missing from the public allowlist: every link in every
 * auth mail dead-ends on the sign-in screen, and it looks like the mail is
 * broken rather than the routing.
 *
 * Supabase puts the recovery token in the URL fragment; detectSessionInUrl on
 * the client picks it up, which is why updateUser works here without the user
 * having signed in normally.
 */
export function ResetPasswordRoute() {
  const navigate = useNavigate()
  const [password, setPassword] = React.useState("")
  const [error, setError] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [done, setDone] = React.useState(false)

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError("")
    try {
      const { error: updateError } = await supabase.auth.updateUser({ password })
      if (updateError) throw updateError
      setDone(true)
      setTimeout(() => navigate("/login", { replace: true }), 1500)
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Could not set that password. The link may have expired.",
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-dvh place-items-center px-4">
      <Card className="w-full max-w-sm p-7">
        <div className="mb-6 flex items-center gap-2">
          <BrandLogo className="size-10" />
          <div>
            <p className="text-base font-semibold leading-tight">Carousel Factory</p>
            <h1 className="text-xs text-[var(--muted-foreground)]">
              Choose a new password
            </h1>
          </div>
        </div>

        {done ? (
          <p className="text-sm text-[var(--muted-foreground)]">
            Password updated. Taking you to sign in…
          </p>
        ) : (
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="password">New password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="new-password"
                required
                minLength={8}
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
            <Button type="submit" variant="brand" className="w-full" disabled={busy}>
              {busy ? "Saving…" : "Set password"}
            </Button>
          </form>
        )}
      </Card>
    </div>
  )
}
