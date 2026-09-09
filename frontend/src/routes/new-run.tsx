import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useSearchParams } from "react-router"
import { toast } from "sonner"

import { AgentComposer, type ComposerState } from "@/components/agent/agent-composer"
import { AgentWorkspace } from "@/components/agent/agent-workspace"
import { BrandLogo } from "@/components/layout/brand-logo"
import { useRunWorkspace } from "@/hooks/use-run-workspace"
import { ApiError, get, post } from "@/lib/api"
import type { InstagramAccountSummary, Meta } from "@/lib/types"

/**
 * Headlines for the empty state, one picked at random per visit.
 *
 * All ten ask the same question - "what are we making a carousel about?" -
 * without ever naming the mechanism. The page already shows a composer and
 * suggestion chips, so spending the largest type on the screen explaining
 * what the screen does is a waste of it.
 *
 * Kept as a flat list rather than generated: these are voice, and voice is
 * written, not computed.
 */
const GREETINGS = [
  "What’s the story?",
  "Got a story?",
  "What’s worth posting?",
  "What’s making noise today?",
  "What caught your eye?",
  "What should we cover?",
  "What’s breaking?",
  "Give me a headline.",
  "What’s worth a carousel?",
  "What are we posting today?",
] as const

const SUGGESTIONS = [
  "the new viral news in AI",
  "this week's biggest AI product launch",
  "a developer tool that just shipped something notable",
]

function looksLikeUrl(value: string): boolean {
  return /^https?:\/\/\S+$/i.test(value.trim())
}

/**
 * Which account this carousel is for.
 *
 * Shown BEFORE the run starts because the choice is not merely where the post
 * goes: the account's handle and profile picture are composited into every
 * slide as it is generated. Choosing afterwards would mean re-rendering the
 * carousel or shipping one brand's artwork under another's name.
 *
 * Hidden entirely when only one account is connected - a picker with one
 * option is a decision nobody is being asked to make.
 */
function AccountPicker({
  accounts,
  value,
  onChange,
  disabled,
}: {
  accounts: InstagramAccountSummary[]
  value: string
  onChange: (accountId: string) => void
  disabled: boolean
}) {
  const usable = accounts.filter((account) => !account.needs_reconnect)
  if (usable.length < 2) return null

  return (
    <div
      className="mt-4 flex flex-wrap items-center justify-center gap-2"
      aria-label="Publish to"
    >
      <span className="text-[11px] text-[var(--muted-foreground)]">
        Posting as
      </span>
      {usable.map((account) => {
        const selected = account.id === value
        return (
          <button
            key={account.id}
            type="button"
            disabled={disabled}
            aria-pressed={selected}
            onClick={() => onChange(account.id)}
            className={
              "rounded-[10px] border px-2.5 py-1.5 text-xs transition-colors disabled:cursor-default disabled:opacity-50 " +
              (selected
                ? "border-[var(--foreground)] bg-[var(--muted)] text-[var(--foreground)]"
                : "border-[var(--border)] bg-[var(--card)] text-[var(--muted-foreground)] hover:bg-[var(--muted)]")
            }
          >
            {account.handle}
          </button>
        )
      })}
    </div>
  )
}

/**
 * The New carousel screen.
 *
 * Two states in one route. With no `?run=`, it is a composer and a question.
 * With one, it hands over to `AgentWorkspace` - the same component the task
 * page's Chat tab renders, reading the same cache entries through the same
 * hook, so the two screens cannot disagree about a task.
 *
 * Several carousels can be in flight at once, so starting another is not
 * abandoning this one: `reset()` clears the URL and leaves the run working in
 * the background, exactly where Tasks will show it.
 */
export function NewRunRoute() {
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const runId = params.get("run")
  const [value, setValue] = React.useState("")
  // Chosen once per mount, via a lazy initialiser rather than inline in the
  // JSX. This component re-renders on every keystroke in the composer, so an
  // inline Math.random() would reshuffle the headline as you type.
  const [greeting] = React.useState(
    () => GREETINGS[Math.floor(Math.random() * GREETINGS.length)],
  )
  const [submittedPrompt, setSubmittedPrompt] = React.useState("")
  const isUrl = looksLikeUrl(value)

  const workspace = useRunWorkspace(runId)

  const meta = useQuery({ queryKey: ["meta"], queryFn: () => get<Meta>("/api/meta") })
  const accounts = React.useMemo(
    () => meta.data?.accounts ?? [],
    [meta.data?.accounts],
  )
  // Empty means "whichever is default", which is what the server does with an
  // empty account_id - so there is no wrong state while /meta is in flight.
  const [accountId, setAccountId] = React.useState("")

  const start = useMutation({
    mutationFn: (payload: {
      source: string
      topic?: string
      url?: string
      account_id?: string
    }) => post<{ run_id: string; title: string }>("/api/runs", payload),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ["runs"] })
      setSubmittedPrompt(value.trim())
      setParams({ run: data.run_id }, { replace: true })
      toast.success("Your carousel is cooking", { description: data.title })
    },
    onError: (error) => {
      const code = error instanceof ApiError ? error.code : undefined
      if (code === "too_many_active_runs") {
        toast.error("Too many at once", {
          description:
            "Every slot is busy. Open Tasks to watch one, or wait for one to reach review.",
        })
        return
      }
      if (code === "daily_limit_reached") {
        toast.error("Daily limit reached", {
          description: "Raise MAX_RUNS_PER_DAY to allow more today.",
        })
        return
      }
      if (code === "no_account" || code === "account_needs_reconnect") {
        toast.error("No Instagram account", {
          description:
            "Connect one from Profile -> Instagram. Its handle and picture are part of the artwork, so a carousel cannot be generated without one.",
        })
        return
      }
      toast.error(error instanceof Error ? error.message : "Could not start that carousel.")
    },
  })

  function submit() {
    const trimmed = value.trim()
    if (trimmed.length < 3) return
    start.mutate({
      ...(isUrl ? { source: "url", url: trimmed } : { source: "topic", topic: trimmed }),
      account_id: accountId,
    })
  }

  /** Back to an empty composer. The run keeps working; Tasks still has it. */
  function reset() {
    setValue("")
    setSubmittedPrompt("")
    setParams({}, { replace: true })
  }

  if (runId) {
    return (
      <AgentWorkspace
        runId={runId}
        workspace={workspace}
        prompt={submittedPrompt}
        // The mutation keeps its result, so this is true only for the run
        // this tab actually created - not for the next chat opened from the
        // sidebar, which would otherwise inherit a stale "just started".
        justStarted={start.data?.run_id === runId}
        onReset={reset}
      />
    )
  }

  // No run yet, so there are only two things this composer can be: waiting for
  // you to type, or waiting for the server to hand back a run id.
  const composerState: ComposerState = start.isPending ? "starting" : "idle"

  return (
    <div className="agent-empty-workspace">
      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col justify-center px-4 py-12 sm:px-8">
        <div className="w-full">
          {/* The TITLE is centred on the page, not the title-plus-logo
              group. Centring the pair as a flex row pushes the words right
              of centre by half the mark's width, so the heading no longer
              lines up with the composer beneath it.

              So the mark is taken out of flow (absolute, right-full) and
              hung off the text's left edge. It cannot affect where the
              words land, however wide it gets.

              Sized in em rather than pixels: the img inherits the h1's
              font-size, so 1.30438em is one line of type plus 30% and holds
              at both the 30px and the 42px step. */}
          {/* Two different centrings, on purpose.

              At every width the WORDS are what sits on the page's centre
              line: the spacer mirrors the mark, so the group's own width
              cannot push the text off centre.

              On a phone the title also never wraps, and that is why the
              type is fluid rather than fixed. The mark and its mirror cost
              two mark-widths of room, so clamp() shrinks the type just
              enough to keep the longest greeting on one line - down to a
              280px screen - and caps at 24px so it does not grow on a big
              phone. A fixed size cannot do both: whatever value fits a
              390px iPhone overflows a 360px Android. */}
          <h1 className="mb-7 flex items-center justify-center whitespace-nowrap text-center font-[Georgia,serif] text-[clamp(16px,calc(7.1vw_-_3.5px),24px)] font-normal tracking-[-0.035em] sm:whitespace-normal sm:text-[42px] sm:leading-tight">
            {/* 8px gap on a phone, 12px from sm up. The desktop spacer below still
                mirrors mr-3, so the words stay on the page's centre line there. */}
            <BrandLogo className="mr-2 size-[1.30438em] sm:mr-3" />
            <span className="min-w-0">{greeting}</span>
            {/* Optical centring on phones: the mirror is HALF the mark's width.

                Neither exact answer looks right, and both are genuinely off.
                Call L the mark plus its gap. Centre the words and the whole
                composition sits L/2 left of centre - it reads as leaning
                left. Centre the composition and the words sit L/2 right of
                centre - the title reads as leaning right. The eye is
                tracking both at once, so it disagrees with either.

                Reserving L/2 on the right splits the error: the words land
                L/4 right of centre, the composition L/4 left of it, and
                neither is far enough off to register. Desktop keeps the full
                mirror - with that much empty page around it, the words being
                exactly centred is what reads as correct. */}
            <span aria-hidden className="ml-1 w-[0.65219em] shrink-0 sm:ml-3 sm:w-[1.30438em]" />
          </h1>

          <AgentComposer
            value={value}
            onChange={setValue}
            onSubmit={submit}
            state={composerState}
          />

          <div className="mt-4 flex flex-wrap justify-center gap-2" aria-label="Suggested prompts">
            {SUGGESTIONS.map((suggestion) => (
              <button
                key={suggestion}
                type="button"
                disabled={start.isPending}
                onClick={() => setValue(suggestion)}
                className="rounded-[10px] border border-[var(--border)] bg-[var(--card)] px-3 py-2 text-left text-xs text-[var(--muted-foreground)] transition-[background-color,color,border-color,transform] hover:-translate-y-px hover:border-[color-mix(in_oklch,var(--foreground)_18%,var(--border))] hover:bg-[var(--muted)] hover:text-[var(--foreground)] disabled:translate-y-0 disabled:cursor-default disabled:opacity-50"
              >
                {suggestion}
              </button>
            ))}
          </div>

          <AccountPicker
            accounts={accounts}
            value={accountId || (accounts.find((a) => a.is_default)?.id ?? "")}
            onChange={setAccountId}
            disabled={start.isPending}
          />

          <p className="mt-5 text-center text-[11px] leading-5 text-[var(--muted-foreground)]">
            Carousel Factory can make mistakes. Every carousel pauses for human review before publishing.
          </p>
        </div>
      </main>
    </div>
  )
}
