import * as React from "react"

/**
 * How close to the bottom still counts as "at the bottom".
 *
 * Not zero. Sub-pixel layout, a fractional device pixel ratio and a mid-flight
 * momentum scroll all leave a couple of pixels behind, and a strict test reads
 * every one of those as "the reader has scrolled up" - which switches
 * following off at the exact moment it is wanted.
 */
const BOTTOM_SLACK = 48

/** Air left between a revealed box and whatever is floating below it. */
const REVEAL_GAP = 12

/** How long a reveal keeps converging. The box's own expansion is 300ms. */
const REVEAL_WINDOW_MS = 480

/**
 * Open a chat at its end, and stay there while it is still being written.
 *
 * The transcript used to open at the TOP. On a finished task that meant the
 * first thing on screen was the prompt from twenty minutes ago and the answer
 * was somewhere below the fold; on a running one, every new line arrived off
 * screen. Both are the wrong end of a conversation - the last thing said is
 * the thing you came to read.
 *
 * Three behaviours, and the third is the one that makes the other two safe:
 *
 *  - **Open at the end.** The first time the transcript is real (not a
 *    skeleton) the scroller jumps to the bottom. A jump, not a smooth scroll:
 *    you should arrive already there, not watch the page travel past you.
 *  - **Follow while it grows.** New events, an image finishing, the asset rail
 *    sliding in - anything that changes the height re-pins the bottom.
 *  - **Let go the moment the reader scrolls up.** Following is a convenience,
 *    and a page that drags you back down while you are reading something
 *    further up is not one you can read at all. Scrolling away switches it
 *    off; scrolling back to the bottom switches it on again.
 *
 * A ResizeObserver rather than an effect on the event count, because most of
 * what changes the height is not an event: images decode late, the sources
 * list unfolds, the rail animates the column narrower and reflows every
 * paragraph in it. Watching the content itself catches all of those, and the
 * events are just one more thing that makes it taller.
 */
export function useChatTail({
  ready,
  live,
}: {
  /** The real transcript is on screen - not the skeleton. */
  ready: boolean
  /** The task is still running, so the transcript is still growing. */
  live: boolean
}) {
  const scrollRef = React.useRef<HTMLDivElement>(null)
  const contentRef = React.useRef<HTMLDivElement>(null)
  // Starts true so the first measurement after opening counts as "at the
  // bottom" and the very first growth is followed.
  const following = React.useRef(true)
  const landed = React.useRef(false)
  // A box that has asked to be shown, and how tall it will be when it stops
  // growing. Cleared on a timer rather than on a scroll event, because the
  // reveal's own scrolling would otherwise cancel it on the first tick.
  const pending = React.useRef<{ el: HTMLElement; height: number } | null>(null)

  const toBottom = React.useCallback((smooth = false) => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? "smooth" : "auto" })
  }, [])

  // Arrive at the end. Keyed on `ready` rather than on mount: while the chat
  // is a skeleton there is nothing to be at the end OF, and scrolling then
  // would be undone by the real content replacing it.
  React.useEffect(() => {
    if (!ready || landed.current) return
    landed.current = true
    following.current = true
    // Two frames: one for the transcript to lay out, one for the scroller to
    // have a scrollHeight worth scrolling to. One frame lands short on a long
    // transcript, which is worse than not scrolling at all - it looks like the
    // page moved and then gave up.
    const frame = requestAnimationFrame(() => requestAnimationFrame(() => toBottom()))
    return () => cancelAnimationFrame(frame)
  }, [ready, toBottom])

  // The reader's own scrolling decides whether following stays on.
  React.useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      following.current =
        el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK
    }
    el.addEventListener("scroll", onScroll, { passive: true })
    return () => el.removeEventListener("scroll", onScroll)
  }, [])

  /**
   * Bring the pending box up off the floor, as far as there is room to.
   *
   * Called on every resize tick while a reveal is in flight, and that
   * repetition is the whole design. A box that is opening does not have its
   * height yet, and a scroller already at its bottom has nowhere to scroll
   * to - the first attempt is clamped to zero. As the box grows it adds the
   * room the scroll needed, each tick moves a little further, and the last
   * one lands the box's bottom exactly on the floor. What the reader sees is
   * the page travelling down WITH the box rather than jumping after it.
   */
  const settle = React.useCallback(() => {
    const scroller = scrollRef.current
    const job = pending.current
    if (!scroller || !job) return

    const pane = scroller.getBoundingClientRect()
    const dock = scroller.parentElement?.querySelector<HTMLElement>(
      ".agent-running-composer-dock",
    )
    // The floating prompt bar is the real floor. The scrollport's own bottom
    // edge is about 74px lower and everything down there is behind the bar -
    // in the layout, invisible to the reader.
    const floor = (dock ? dock.getBoundingClientRect().top : pane.bottom) - REVEAL_GAP

    const box = job.el.getBoundingClientRect()
    const wanted = box.top + Math.max(job.height, box.height) - floor
    if (wanted <= 0) return
    // Never so far that the box's own top goes off the top: a list taller
    // than the space gets shown from its start, and scrolls internally.
    const room = box.top - pane.top - REVEAL_GAP
    const delta = Math.max(0, Math.min(wanted, room))
    if (delta < 1) return
    scroller.scrollBy({ top: delta })
  }, [])

  // Anything that makes the transcript taller either re-pins the bottom or
  // advances a reveal - never both, because a reveal turns following off.
  React.useEffect(() => {
    const content = contentRef.current
    if (!content || typeof ResizeObserver === "undefined") return
    const observer = new ResizeObserver(() => {
      if (pending.current) settle()
      else if (following.current) toBottom()
    })
    observer.observe(content)
    return () => observer.disconnect()
  }, [toBottom, settle])

  // While the agents work, the transcript is a live document. `live` is here
  // so a finished chat that the reader has scrolled up in is never re-pinned
  // by a late image decoding underneath them.
  React.useEffect(() => {
    if (!live || !ready) return
    const id = window.setInterval(() => {
      if (following.current) toBottom()
    }, 1000)
    return () => window.clearInterval(id)
  }, [live, ready, toBottom])

  /**
   * Bring a box fully into view, above the floating prompt bar.
   *
   * Two things this does that `scrollIntoView` cannot.
   *
   * **It knows where the floor actually is.** The prompt bar floats over the
   * bottom of the scroller, so the scrollport's bottom edge and the last
   * pixel a reader can see are two different lines about 74px apart.
   * `scrollIntoView({block:"nearest"})` scrolls to the former, which put the
   * bottom of a long source list behind the bar - visible to the layout,
   * invisible to the reader.
   *
   * **It happens once, at the moment of the click.** `height` is what the box
   * will be when its animation finishes, measured while it is still collapsed
   * (the content is rendered, only clipped). Knowing that up front means the
   * scroll can start immediately and run alongside the expansion, instead of
   * waiting for `transitionend` and arriving as a second, separate movement -
   * and it means following is switched off BEFORE the box starts growing,
   * so the tail never pins to the bottom on the way.
   *
   * The scroll is also bounded by the box's own top: a list taller than the
   * space available is shown from its start rather than scrolled past. It has
   * its own scrollbar for the rest.
   */
  const reveal = React.useCallback(
    (el: HTMLElement | null, height?: number) => {
      if (!scrollRef.current || !el) return
      // The reader asked to look at something specific. Following would be
      // arguing with them - and switching it off HERE, before the box has
      // started growing, is what stops the two from fighting at all.
      following.current = false
      pending.current = { el, height: height ?? el.getBoundingClientRect().height }
      settle()
      // Long enough to cover the 300ms expansion and a slow frame after it.
      // Without an end the next arriving event would be treated as more of
      // this reveal instead of as new content.
      window.setTimeout(() => {
        settle()
        pending.current = null
      }, REVEAL_WINDOW_MS)
    },
    [settle],
  )

  return { scrollRef, contentRef, toBottom, reveal }
}

export type ChatScroll = {
  /** Scroll a box clear of the floating prompt bar. See `reveal` above. */
  reveal: (el: HTMLElement | null, height?: number) => void
}

/**
 * How something deep in the conversation asks the scroller to show it.
 *
 * A context rather than props threaded through `AgentConversation`, because
 * the only thing that needs it is one collapsible panel four levels down and
 * every component in between would have to carry a prop it does not use.
 *
 * Null outside the chat workspace - on the task page the conversation is
 * inside a tabbed screen whose scroller is the window and whose prompt bar is
 * not floating over anything. Consumers fall back to `scrollIntoView` there.
 */
export const ChatScrollContext = React.createContext<ChatScroll | null>(null)

export function useChatScroll(): ChatScroll | null {
  return React.useContext(ChatScrollContext)
}
