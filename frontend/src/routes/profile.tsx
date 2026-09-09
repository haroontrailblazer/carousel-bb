import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Check,
  ExternalLink,
  Instagram,
  Moon,
  Send,
  Sun,
  Trash2,
  Unplug,
  Upload,
} from "lucide-react"
import { toast } from "sonner"

import { UserAvatar } from "@/components/layout/user-avatar"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Chip, MutedChip } from "@/components/ui/chip"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { useProfile } from "@/hooks/use-profile"
import { useTheme } from "@/hooks/use-theme"
import { ApiError, del, get, post, postBytes } from "@/lib/api"
import { compressAvatar } from "@/lib/image"

type InstagramAccount = {
  id: string
  username: string
  handle: string
  name: string
  avatar_key: string
  is_default: boolean
  disabled: boolean
  /** True when the token is expired, unreadable, or the account is disabled. */
  needs_reconnect: boolean
  expires_in_days: number | null
  connected_by: string
  connected_at: string
}

type InstagramStatus = {
  /** False when IG_APP_ID / IG_APP_SECRET are missing - nothing can connect. */
  app_configured: boolean
  public_base_url_set: boolean
  /** False when SECRETS_KEY is absent - no token can be stored safely. */
  secrets_ready: boolean
  redirect_uri: string
  accounts: InstagramAccount[]
}

type TelegramStatus = {
  connected: boolean
  /** False when SECRETS_KEY is absent - nothing can be stored safely. */
  secrets_ready: boolean
  source: "console" | "environment" | "unset"
  bot_username: string
  chat_id: string
  token_masked: string
  connected_by: string
  connected_at: string
}

function Section({
  title,
  description,
  children,
}: {
  title: string
  description: string
  children: React.ReactNode
}) {
  return (
    <Card className="p-5">
      <div className="mb-4">
        <h2 className="text-sm font-semibold">{title}</h2>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">{description}</p>
      </div>
      {children}
    </Card>
  )
}

/** Name and picture. */
function IdentitySection() {
  const { profile, save } = useProfile()
  const [name, setName] = React.useState(profile.name)
  const [preview, setPreview] = React.useState<string | null>(null)
  const [busy, setBusy] = React.useState(false)
  const [uploading, setUploading] = React.useState(false)
  const fileInput = React.useRef<HTMLInputElement>(null)

  // Track whether this field has been TYPED IN rather than seeding it once and
  // freezing it.
  //
  // The profile is now a shared query that revalidates on focus, so its value
  // legitimately changes under this component - a rename on a phone lands here
  // while the page is open. Seeding once meant the sidebar showed the new name
  // and this box still showed the old one. Mirroring it unconditionally would
  // be worse: it would overwrite whatever is half-typed the moment a refetch
  // returned. So the server wins until the user touches the field, and the
  // user wins from then until the save lands.
  const [dirty, setDirty] = React.useState(false)
  React.useEffect(() => {
    if (!dirty) setName(profile.name)
  }, [profile.name, dirty])

  // An object URL is a live handle into browser memory; letting it leak means
  // the decoded image is never freed.
  React.useEffect(() => {
    return () => {
      if (preview) URL.revokeObjectURL(preview)
    }
  }, [preview])

  async function onPick(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    // Clear immediately, so choosing the SAME file again still fires change.
    event.target.value = ""
    if (!file) return

    setUploading(true)
    try {
      const image = await compressAvatar(file)
      setPreview((old) => {
        if (old) URL.revokeObjectURL(old)
        return image.objectUrl
      })
      const { url } = await postBytes<{ url: string }>(
        "/api/profile/avatar",
        image.blob,
      )
      await save({ avatarUrl: url })
      toast.success("Picture updated", {
        description:
          `Compressed to ${Math.max(1, Math.round(image.blob.size / 1024))} KB ` +
          `from ${Math.max(1, Math.round(file.size / 1024))} KB.`,
      })
    } catch (error) {
      setPreview(null)
      toast.error(
        error instanceof Error ? error.message : "Could not use that image.",
      )
    } finally {
      setUploading(false)
    }
  }

  async function onRemove() {
    setUploading(true)
    try {
      await del("/api/profile/avatar")
      await save({ avatarUrl: null })
      setPreview((old) => {
        if (old) URL.revokeObjectURL(old)
        return null
      })
      toast.success("Picture removed")
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "Could not remove that.",
      )
    } finally {
      setUploading(false)
    }
  }

  async function onSaveName() {
    setBusy(true)
    try {
      await save({ name })
      // The field now matches the server again, so let the live value drive
      // it once more.
      setDirty(false)
      toast.success("Profile saved")
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Could not save that.")
    } finally {
      setBusy(false)
    }
  }

  const shown = preview ?? profile.avatarUrl

  return (
    <Section
      title="You"
      description="How you appear in the console, and against the verdicts you record."
    >
      <div className="flex flex-wrap items-start gap-4">
        <div className="space-y-2">
          <UserAvatar
            key={shown ?? "none"}
            src={shown}
            name={name || profile.displayName}
            seed={profile.email}
            className="size-16 text-xl"
          />
        </div>

        <div className="min-w-0 flex-1 space-y-3">
          <div className="space-y-1.5">
            <label htmlFor="display-name" className="block text-xs font-medium">
              Display name
            </label>
            <Input
              id="display-name"
              value={name}
              placeholder={profile.email.split("@")[0]}
              onChange={(event) => {
                setDirty(true)
                setName(event.target.value)
              }}
            />
          </div>

          <div className="space-y-1.5">
            <span className="block text-xs font-medium">Picture</span>
            <div className="flex flex-wrap gap-2">
              <input
                ref={fileInput}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(event) => void onPick(event)}
              />
              <Button
                size="sm"
                variant="ghost"
                disabled={uploading}
                onClick={() => fileInput.current?.click()}
              >
                <Upload /> {uploading ? "Working..." : "Upload"}
              </Button>
              {shown && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={uploading}
                  onClick={() => void onRemove()}
                >
                  <Trash2 /> Remove
                </Button>
              )}
            </div>
            <p className="text-[11px] leading-4 text-[var(--muted-foreground)]">
              Resized and compressed in your browser before it is sent, so a
              camera photo does not become a multi-megabyte upload. With no
              picture set, one is generated from your email.
            </p>
          </div>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button
          variant="brand"
          size="sm"
          onClick={() => void onSaveName()}
          disabled={busy}
        >
          {busy ? "Saving..." : "Save"}
        </Button>
        <span className="text-xs text-[var(--muted-foreground)]">{profile.email}</span>
      </div>
    </Section>
  )
}

/** Light / dark. */
function AppearanceSection() {
  const { dark, setDark } = useTheme()
  const options = [
    { value: false, label: "Light", icon: Sun },
    { value: true, label: "Dark", icon: Moon },
  ]
  return (
    <Section
      title="Appearance"
      description="Applies to this browser and is remembered on this device."
    >
      <div className="flex gap-2">
        {options.map(({ value, label, icon: Icon }) => {
          const active = dark === value
          return (
            <button
              key={label}
              type="button"
              onClick={() => setDark(value)}
              aria-pressed={active}
              className={
                "flex flex-1 items-center gap-2.5 rounded-[var(--radius-md)] border-2 px-3 py-2.5 text-sm transition-colors " +
                (active
                  ? "border-[var(--brand)] bg-[var(--brand-soft)]"
                  : "border-[var(--border)] hover:bg-[var(--muted)]")
              }
            >
              <Icon className="size-4 shrink-0" />
              <span className="font-medium">{label}</span>
              {active && <Check className="ml-auto size-4" />}
            </button>
          )
        })}
      </div>
    </Section>
  )
}

/**
 * Connect the review bot.
 *
 * The chat id is DISCOVERED rather than asked for - typing a numeric chat id
 * is the step everyone gets wrong. Telegram will only name a chat once a human
 * has messaged the bot, so "no chat yet" renders as a step to finish, not as
 * an error.
 */
function TelegramSection() {
  const queryClient = useQueryClient()
  const [token, setToken] = React.useState("")
  const [needsStart, setNeedsStart] = React.useState("")

  const status = useQuery({
    queryKey: ["telegram"],
    queryFn: () => get<TelegramStatus>("/api/settings/telegram"),
    // Opening this screen always re-asks. Connecting the bot is the kind of
    // thing done once, from whichever device is to hand - so the answer this
    // browser happens to be holding is exactly the one likely to be wrong,
    // and it is one small request to be certain instead.
    refetchOnMount: "always",
  })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["telegram"] })
    // The review screen warns when publishing is unconfigured; it reads /meta.
    void queryClient.invalidateQueries({ queryKey: ["meta"] })
  }

  const connect = useMutation({
    mutationFn: () => post<TelegramStatus>("/api/settings/telegram", { token }),
    onSuccess: () => {
      setToken("")
      setNeedsStart("")
      toast.success("Telegram connected", {
        description: "A hello message is waiting in your chat.",
      })
      refresh()
    },
    onError: (error) => {
      const code = error instanceof ApiError ? error.code : undefined
      if (code === "no_chat") {
        const detail = error instanceof ApiError ? error.detail : undefined
        const username =
          (detail as { bot_username?: string } | undefined)?.bot_username ?? ""
        setNeedsStart(username || "your_bot")
        return
      }
      toast.error(error instanceof Error ? error.message : "Could not connect.")
    },
  })

  const disconnect = useMutation({
    mutationFn: () => del<TelegramStatus>("/api/settings/telegram"),
    onSuccess: () => {
      toast.success("Telegram disconnected")
      refresh()
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "Could not disconnect."),
  })

  const data = status.data
  // First visit to this screen has nothing cached, and the section rendered
  // its "not connected" instructions while the answer was still in flight -
  // telling a connected user to go and make a bot. Hold the space instead.
  const unknown = status.isLoading && !data
  const connected = !!data?.connected
  const fromConsole = data?.source === "console"
  // Say so BEFORE someone types a bearer token into a form that will refuse
  // it - the server will not store a credential it cannot encrypt.
  const secretsMissing = data ? !data.secrets_ready : false

  return (
    <Section
      title="Telegram"
      description="Where carousel reviews are announced. The message carries the slides and a button that opens the review screen."
    >
      {unknown && (
        <div className="space-y-3">
          <Skeleton className="h-6 w-32 rounded-full" />
          <Skeleton className="h-4 w-full max-w-md" />
          <Skeleton className="h-4 w-full max-w-sm" />
          <Skeleton className="h-9 w-full" />
        </div>
      )}

      {!unknown && connected && (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <Chip tone="done" dot>
            Connected
          </Chip>
          {data?.bot_username && <MutedChip>@{data.bot_username}</MutedChip>}
          {data?.chat_id && <MutedChip>chat {data.chat_id}</MutedChip>}
        </div>
      )}

      {secretsMissing && (
        <p
          className="mb-4 rounded-[var(--radius-md)] px-3 py-2 text-sm"
          style={{
            background: "var(--phase-failed-soft)",
            color: "var(--phase-failed-fg)",
          }}
        >
          SECRETS_KEY is not set on the server, so a bot token cannot be stored
          encrypted - and it will not be stored any other way. Generate a key
          and put it in .env, then reload.
        </p>
      )}

      {unknown ? null : connected && fromConsole ? (
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-mono text-xs text-[var(--muted-foreground)]">
            {data?.token_masked}
          </span>
          <Button
            size="sm"
            variant="ghost"
            disabled={disconnect.isPending}
            onClick={() => disconnect.mutate()}
          >
            <Unplug /> {disconnect.isPending ? "Disconnecting..." : "Disconnect"}
          </Button>
        </div>
      ) : (
        <div className="space-y-3">
          <ol className="space-y-1.5 text-sm text-[var(--muted-foreground)]">
            <li>
              1. Message{" "}
              <a
                className="text-[var(--link)] hover:underline"
                href="https://t.me/BotFather"
                target="_blank"
                rel="noreferrer"
              >
                @BotFather <ExternalLink className="inline size-3" />
              </a>{" "}
              and send <code className="font-mono">/newbot</code>.
            </li>
            <li>2. Paste the token it gives you below.</li>
            <li>3. Send your new bot a message, so it knows where to reply.</li>
          </ol>

          <div className="flex flex-wrap gap-2">
            <Input
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="123456789:AA..."
              autoComplete="off"
              spellCheck={false}
              className="min-w-0 flex-1 font-mono"
            />
            <Button
              variant="brand"
              disabled={!token.trim() || connect.isPending || secretsMissing}
              onClick={() => connect.mutate()}
            >
              <Send /> {connect.isPending ? "Connecting..." : "Connect"}
            </Button>
          </div>

          {needsStart && (
            <p
              className="rounded-[var(--radius-md)] px-3 py-2 text-sm"
              style={{
                background: "var(--phase-review-soft)",
                color: "var(--phase-review-fg)",
              }}
            >
              The bot is real, but nobody has messaged it - Telegram only names
              a chat once a human has written to it. Open{" "}
              <a
                className="font-medium underline"
                href={"https://t.me/" + needsStart}
                target="_blank"
                rel="noreferrer"
              >
                t.me/{needsStart}
              </a>
              , send <code className="font-mono">/start</code>, then press
              Connect again.
            </p>
          )}
        </div>
      )}

    </Section>
  )
}

/** What each callback error code means, in words a person can act on. */
const INSTAGRAM_ERRORS: Record<string, string> = {
  bad_state:
    "That connection request could not be verified. Start it again from this page.",
  state_expired: "That connection request took too long. Press Connect again.",
  no_code: "Instagram did not send an authorisation code back.",
  no_identity:
    "Instagram would not identify that account. It must be a Professional (Business or Creator) account.",
  secrets_unconfigured:
    "SECRETS_KEY is not set on the server, so the token could not be stored encrypted. Nothing was saved.",
  not_configured:
    "This console has no Meta app credentials (IG_APP_ID / IG_APP_SECRET).",
  no_public_url: "PUBLIC_BASE_URL is not set on the server.",
}

/**
 * Connecting Instagram accounts.
 *
 * Nobody types an Instagram password here. "Connect" is a plain link to our
 * own authorize route, which redirects to Instagram's login page; the browser
 * comes back to /profile carrying ?instagram=connected or ?instagram_error=...
 * A full navigation rather than a fetch, because that is what OAuth is.
 *
 * The account is what a run is generated FOR, not merely published to - its
 * handle and picture are stamped onto every slide - which is why disconnecting
 * one is described as affecting future runs rather than as housekeeping.
 */
function InstagramSection() {
  const queryClient = useQueryClient()

  const status = useQuery({
    queryKey: ["instagram"],
    queryFn: () => get<InstagramStatus>("/api/settings/instagram"),
    // Same reasoning as Telegram: connecting is done once, from whichever
    // device is to hand, so a cached answer here is the one likely to be stale.
    refetchOnMount: "always",
  })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["instagram"] })
    // The newsroom and review screens read /meta for publish_configured and
    // for the account picker.
    void queryClient.invalidateQueries({ queryKey: ["meta"] })
  }

  // Read the outcome the callback redirected back with, then scrub it from the
  // URL so a reload does not re-announce a connection that happened minutes ago.
  React.useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const connected = params.get("instagram")
    const failed = params.get("instagram_error")
    if (!connected && !failed) return

    if (connected) {
      const account = params.get("account")
      toast.success(
        account ? `Connected @${account}` : "Instagram connected",
      )
      refresh()
    } else if (failed === "access_denied") {
      toast("Connection cancelled", {
        description: "Nothing was changed.",
      })
    } else {
      toast.error("Could not connect that account", {
        description: params.get("detail") || INSTAGRAM_ERRORS[failed!] || failed!,
      })
    }

    params.delete("instagram")
    params.delete("instagram_error")
    params.delete("account")
    params.delete("detail")
    const query = params.toString()
    window.history.replaceState(
      {},
      "",
      window.location.pathname + (query ? `?${query}` : ""),
    )
    // Once, on mount. The URL is scrubbed immediately, so there is nothing to
    // react to afterwards.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const setDefault = useMutation({
    mutationFn: (accountId: string) =>
      post<InstagramStatus>("/api/settings/instagram/default", {
        account_id: accountId,
      }),
    onSuccess: () => {
      toast.success("Default account updated")
      refresh()
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "Could not update."),
  })

  const disconnect = useMutation({
    mutationFn: (accountId: string) =>
      del<InstagramStatus>(`/api/settings/instagram/${accountId}`),
    onSuccess: () => {
      toast.success("Account disconnected")
      refresh()
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "Could not disconnect."),
  })

  const data = status.data
  const unknown = status.isLoading && !data
  const accounts = data?.accounts ?? []
  const canConnect = !!data?.app_configured && !!data?.public_base_url_set
  const secretsMissing = data ? !data.secrets_ready : false

  return (
    <Section
      title="Instagram"
      description="Where carousels are published. The account's handle and profile picture are part of the artwork, so each run is generated for one of them."
    >
      {unknown && (
        <div className="space-y-3">
          <Skeleton className="h-6 w-32 rounded-full" />
          <Skeleton className="h-4 w-full max-w-md" />
          <Skeleton className="h-9 w-full" />
        </div>
      )}

      {secretsMissing && (
        <p
          className="mb-4 rounded-[var(--radius-md)] px-3 py-2 text-sm"
          style={{
            background: "var(--phase-failed-soft)",
            color: "var(--phase-failed-fg)",
          }}
        >
          SECRETS_KEY is not set on the server, so an access token cannot be
          stored encrypted - and it will not be stored any other way. Generate
          a key and put it in .env, then reload.
        </p>
      )}

      {!unknown && !canConnect && (
        <p
          className="mb-4 rounded-[var(--radius-md)] px-3 py-2 text-sm"
          style={{
            background: "var(--phase-review-soft)",
            color: "var(--phase-review-fg)",
          }}
        >
          {!data?.app_configured
            ? "This console has no Meta app credentials. Set IG_APP_ID and IG_APP_SECRET on the server, then restart."
            : "PUBLIC_BASE_URL is not set, so there is no redirect URI to hand Instagram. Set it to this service's public URL and allowlist the callback in the Meta app."}
        </p>
      )}

      {!unknown && accounts.length > 0 && (
        <ul className="mb-4 space-y-2">
          {accounts.map((account) => (
            <li
              key={account.id}
              className="flex flex-wrap items-center gap-3 rounded-[var(--radius-md)] border border-[var(--border)] px-3 py-2"
            >
              <img
                src={`/api/settings/instagram/${account.id}/avatar`}
                alt=""
                width={32}
                height={32}
                className="size-8 shrink-0 rounded-full object-cover"
                // No stored picture is an ordinary state, not a broken image.
                onError={(event) => {
                  event.currentTarget.style.visibility = "hidden"
                }}
              />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-medium">
                    {account.handle}
                  </span>
                  {account.is_default && <Chip tone="done">Default</Chip>}
                  {account.needs_reconnect ? (
                    <MutedChip>Needs reconnecting</MutedChip>
                  ) : (
                    account.expires_in_days !== null &&
                    account.expires_in_days <= 14 && (
                      <MutedChip>
                        Expires in {account.expires_in_days}d
                      </MutedChip>
                    )
                  )}
                </div>
                {account.name && (
                  <p className="truncate text-xs text-[var(--muted-foreground)]">
                    {account.name}
                  </p>
                )}
              </div>
              {!account.is_default && !account.needs_reconnect && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={setDefault.isPending}
                  onClick={() => setDefault.mutate(account.id)}
                >
                  <Check /> Make default
                </Button>
              )}
              <Button
                size="sm"
                variant="ghost"
                disabled={disconnect.isPending}
                onClick={() => disconnect.mutate(account.id)}
              >
                <Unplug /> Disconnect
              </Button>
            </li>
          ))}
        </ul>
      )}

      {!unknown && (
        <div className="space-y-3">
          {accounts.length === 0 && (
            <ol className="space-y-1.5 text-sm text-[var(--muted-foreground)]">
              <li>
                1. Make sure the Instagram account is a Professional one
                (Business or Creator) - a free switch in the Instagram app.
              </li>
              <li>
                2. Press Connect. You sign in on Instagram's own page; this
                console never sees the password.
              </li>
              <li>3. Approve the permissions Instagram asks about.</li>
            </ol>
          )}
          <Button
            variant="brand"
            disabled={!canConnect || secretsMissing}
            onClick={() => {
              // A full navigation, not fetch: this is an OAuth redirect.
              window.location.href = "/api/settings/instagram/authorize"
            }}
          >
            <Instagram />{" "}
            {accounts.length === 0 ? "Connect Instagram" : "Connect another"}
          </Button>
        </div>
      )}
    </Section>
  )
}

export function ProfileRoute() {
  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <h1 className="text-xl font-semibold tracking-tight">Profile</h1>
      <IdentitySection />
      <AppearanceSection />
      <InstagramSection />
      <TelegramSection />
    </div>
  )
}
