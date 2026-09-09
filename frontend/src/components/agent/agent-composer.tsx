import * as React from "react"
import { ArrowUp, Plus, Square } from "lucide-react"

import {
  TargetChip,
  TargetMenu,
  useTargetPicker,
} from "@/components/agent/agent-target-picker"
import { cn } from "@/lib/utils"

/**
 * What the bar can do, which is not the same as what the task is doing.
 *
 * `review` is split out from `complete` because it is the one state where a
 * typed message reaches the agents: the pipeline is paused on
 * `await_human_review`, so words sent now become rework feedback and the
 * targeted agents run again. Everything after that - published, failed,
 * cancelled - has no paused step left to answer, so a message there starts a
 * new carousel instead.
 */
export type ComposerState =
  /** The chat itself is still arriving. Nothing is offered, because nothing
   *  is known yet - see the `loading` note in the component. */
  | "loading"
  | "idle"
  | "starting"
  | "running"
  | "review"
  | "complete"
  | "failed"

const PLACEHOLDER: Record<ComposerState, string> = {
  loading: "",
  idle: "Describe a story or paste a news URL…",
  starting: "",
  running: "",
  review: "Ask for a change to this carousel…",
  complete: "Describe another story or paste a news URL…",
  failed: "Describe another story or paste a news URL…",
}

export function AgentComposer({
  value,
  onChange,
  onSubmit,
  onStop,
  state,
  stopping = false,
  target = null,
  onTargetChange,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  onStop?: () => void
  state: ComposerState
  /** A stop is in flight; the button says so instead of looking ignored. */
  stopping?: boolean
  /** The agent this message is addressed to, when one was picked. */
  target?: string | null
  onTargetChange?: (target: string | null) => void
}) {
  const inputRef = React.useRef<HTMLTextAreaElement>(null)

  // Three shapes, from two questions: is the agent busy, and is this the very
  // first message of a chat?
  const working = state === "running" || state === "starting"
  const composing = state === "idle"

  /**
   * The chat is still a skeleton, so the bar offers nothing.
   *
   * It used to offer Stop. `composerStateFor` mapped "the run request is in
   * flight" onto `starting`, which is the state a task the user just launched
   * is in - so merely OPENING a finished chat put a live Stop button on
   * screen for as long as the page took to load. Pressing it asked the server
   * to cancel a task that had ended days ago.
   *
   * Stop belongs to `starting` for the case it was written for: a run this
   * tab has just kicked off, where the agents really are already spending
   * money and a button that refuses for the first few seconds refuses at
   * exactly the moment someone realises they mistyped. That case is unchanged.
   */
  const loading = state === "loading"
  const canType = !working && !loading

  // Pointing only exists while the pipeline is paused on a review. See the
  // hook for why offering it anywhere else would be a menu that does nothing.
  const picker = useTargetPicker({
    value,
    onChange,
    onPick: (agent) => onTargetChange?.(agent),
    enabled: state === "review" && !!onTargetChange,
  })

  function submit(event: React.FormEvent) {
    event.preventDefault()
    if (canType && value.trim().length >= 3) onSubmit()
  }

  return (
    <form
      onSubmit={submit}
      className={cn(
        "agent-composer relative rounded-[20px] border border-[var(--border)] bg-[var(--card)] p-2 shadow-[var(--shadow-lift)]",
        // Only the empty composer is tall. Once a task exists the bar is a
        // single line for the rest of its life - working, waiting on you,
        // finished or stopped.
        !composing && "agent-composer--thin",
      )}
    >
      {picker.open && (
        <TargetMenu
          matches={picker.matches}
          active={picker.active}
          onChoose={picker.choose}
        />
      )}

      {/* The collapsing half. It keeps rendering while closed rather than
          unmounting, because a grid row cannot animate to the height of
          something that is not there - and the textarea is `disabled` when
          closed, which also takes it out of the tab order. */}
      <div className="agent-composer-field">
        <div>
          <textarea
            ref={inputRef}
            rows={2}
            value={composing ? value : ""}
            disabled={!composing}
            aria-hidden={!composing}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault()
                submit(event)
              }
            }}
            placeholder={PLACEHOLDER.idle}
            className="max-h-40 min-h-14 w-full resize-none bg-transparent px-3 py-2 text-[15px] leading-6 outline-none placeholder:text-[var(--muted-foreground)] disabled:cursor-default"
            aria-label="Carousel prompt"
          />
        </div>
      </div>

      <div className="agent-composer-controls flex items-center gap-2 px-1 pb-1">
        {composing ? (
          <button
            type="button"
            onClick={() => {
              if (!value) onChange("https://")
              requestAnimationFrame(() => inputRef.current?.focus())
            }}
            className="grid size-10 shrink-0 place-items-center rounded-full border border-[var(--border)] text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
            title="Paste a news URL"
          >
            <Plus className="size-4" />
            <span className="sr-only">Paste a news URL</span>
          </button>
        ) : canType ? (
          // One line, in the thin bar. The placeholder is the only thing that
          // says where this message will go, and it differs by state on
          // purpose: asking for a change while the carousel waits on you is a
          // completely different act from starting another one.
          <>
            {target && (
              <TargetChip name={target} onClear={() => onTargetChange?.(null)} />
            )}
            <input
              value={value}
              onChange={(event) => onChange(event.target.value)}
              // The menu gets first refusal on arrows and Enter while it is
              // open, so choosing an agent cannot also submit the message.
              onKeyDown={(event) => picker.onKeyDown(event)}
              placeholder={
                target
                  ? "What should it change?"
                  : state === "review"
                    ? "Ask for a change, or type / to pick an agent…"
                    : PLACEHOLDER[state]
              }
              aria-label={PLACEHOLDER[state]}
              className="min-w-0 flex-1 bg-transparent px-3 text-[15px] outline-none placeholder:text-[var(--muted-foreground)]"
            />
          </>
        ) : (
          // Working: bare. The button says what it does, and the conversation
          // above says what the agents are doing.
          <span className="flex-1" />
        )}

        <div className="ml-auto shrink-0">
          {working ? (
            // Stop is live from the first frame, including while the run is
            // still "starting". The agents are already spending money by then,
            // and a button that refuses for the first few seconds is refusing
            // at exactly the moment someone realises they typed the wrong
            // thing.
            <button
              type="button"
              onClick={onStop}
              disabled={!onStop || stopping}
              aria-busy={stopping}
              className="grid size-10 shrink-0 place-items-center rounded-full bg-[var(--foreground)] text-[var(--background)] transition-opacity hover:opacity-85 disabled:opacity-45"
              title={stopping ? "Stopping…" : "Stop the agents now"}
            >
              <Square className="size-3.5 fill-current" />
              <span className="sr-only">{stopping ? "Stopping" : "Stop task"}</span>
            </button>
          ) : (
            <button
              type="submit"
              disabled={loading || value.trim().length < 3}
              className="grid size-10 shrink-0 place-items-center rounded-full bg-[var(--foreground)] text-[var(--background)] transition-all hover:-translate-y-px hover:opacity-85 disabled:translate-y-0 disabled:opacity-25"
              title={
                loading
                  ? "Loading this task…"
                  : state === "review"
                    ? "Send this change to the agents"
                    : "Start a carousel"
              }
            >
              <ArrowUp className="size-4 stroke-[2.5]" />
              <span className="sr-only">
                {state === "review" ? "Send this change" : "Start a carousel"}
              </span>
            </button>
          )}
        </div>
      </div>
    </form>
  )
}
