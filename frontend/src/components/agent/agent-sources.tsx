import * as React from "react"
import { ChevronDown } from "lucide-react"

import { defaultAvatar } from "@/components/layout/user-avatar"
import { useChatScroll } from "@/hooks/use-chat-tail"
import type { RunEvent } from "@/lib/types"
import { cn } from "@/lib/utils"

/**
 * What the research agent read, shown the way a search result is shown.
 *
 * The old version was a wrap of six chips, each a grey globe and a hostname,
 * with the rest silently dropped. Two things were wrong with that: a run
 * routinely consults a dozen URLs and only six were reachable, and every chip
 * looked identical, so a list of sources carried no information until you had
 * read all of it.
 *
 * So: one favicon PER SITE, overlapped into a group, with the total number of
 * URLs beside it. Three tiles and "10 sources" says "ten pages from three
 * publications" at a glance, which is the thing a reader actually wants to
 * know about a brief. Opening it lists every URL, grouped under the site it
 * came from - nothing is dropped any more.
 */

export type SourceGroup = {
  host: string
  links: { url: string; path: string }[]
}

/** `https://www.reuters.com/a/b?c` -> `reuters.com`, or "" if unparseable. */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "")
  } catch {
    return ""
  }
}

/** How many favicons the stack shows before it stops. */
const STACK = 4

/**
 * Every URL the agents cited, deduped, grouped by site, in the order the run
 * found them.
 *
 * `Map` rather than an object because insertion order is the whole ordering:
 * the site the research agent hit first is the one at the front of the stack.
 */
export function groupSources(events: RunEvent[]): {
  groups: SourceGroup[]
  total: number
} {
  const byHost = new Map<string, SourceGroup>()
  const seen = new Set<string>()
  let total = 0

  for (const event of events) {
    const sources = event.data?.sources
    if (!Array.isArray(sources)) continue
    for (const source of sources) {
      if (typeof source !== "string" || seen.has(source)) continue
      seen.add(source)

      let host: string
      let path: string
      try {
        const parsed = new URL(source)
        host = parsed.hostname.replace(/^www\./, "")  // == hostOf, inlined: the URL is already parsed
        // The bit that distinguishes two URLs on the same site. A bare "/" is
        // kept as "/" rather than shown as nothing, so a homepage citation
        // still reads as a row you can click.
        path = (parsed.pathname + parsed.search).replace(/\/+$/, "") || "/"
      } catch {
        // The backend only forwards http(s), so this is a malformed record
        // rather than a shape to support. Skipping it beats rendering a link
        // that cannot be opened.
        continue
      }

      total++
      const group = byHost.get(host)
      if (group) group.links.push({ url: source, path })
      else byHost.set(host, { host, links: [{ url: source, path }] })
    }
  }

  return { groups: [...byHost.values()], total }
}

/**
 * favicon.so, the documented fetch endpoint.
 *
 * The query form rather than the short `favicon.so/{domain}` one, which is a
 * catch-all sharing a routing table with the site's own pages - `favicon.so/en`
 * and `favicon.so/api` both answer with HTML, not an icon. The query form
 * cannot collide with anything.
 *
 * There is no size parameter. The API takes `url` and `raw`, and nothing
 * else: `size`, `s` and `sz` were each tried against the live endpoint and
 * all three returned byte-identical output. Sizing is therefore ours to do,
 * which is what `size` below is for.
 */
function faviconUrl(host: string): string {
  return `https://favicon.so/api/favicon?url=${encodeURIComponent(host)}`
}

/**
 * The site's icon, over a generated tile.
 *
 * Resolved through favicon.so rather than by hitting `https://{host}/favicon.ico`
 * directly. Two things change for the better: plenty of sites declare their
 * icon only in their HTML and have no `/favicon.ico` at all, and the ones
 * that do usually serve a 16px image, which is a blurry smear on a 2x
 * display. The service resolves the icon the site actually declares - often
 * a large PNG or an SVG - so a small tile is a clean downscale instead. It is
 * free, needs no key, sends `Access-Control-Allow-Origin: *` and caches for
 * seven days.
 *
 * The trade is that the service learns which publications this account
 * researches, which a direct fetch would not have told anyone.
 *
 * `size` is a real pixel count, not a class, and it lands on the `width` and
 * `height` attributes as well as the box. That is what "the correct size"
 * amounts to here: the box is exact before the image arrives so nothing
 * reflows, and a multi-frame `.ico` has the intrinsic size it needs to pick
 * a matching frame rather than the first one.
 *
 * The generated tile is the BACKGROUND rather than an `onError` swap. Waiting
 * for the error meant a row sat with a hole in it for as long as the request
 * took, and a request that simply hangs never errors at all, so the hole was
 * permanent. Painting the tile first means the row is complete from the first
 * frame and the icon fades in over it, or does not, and either way nothing
 * moves.
 */
export function SourceFavicon({
  host,
  size,
  className,
}: {
  host: string
  /** Rendered edge in CSS pixels. Drives the box AND the image attributes. */
  size: number
  className?: string
}) {
  const [loaded, setLoaded] = React.useState(false)
  const tile = React.useMemo(() => defaultAvatar(host, host), [host])
  return (
    <span
      aria-hidden
      className={cn("relative block shrink-0 overflow-hidden bg-cover bg-center", className)}
      style={{ width: size, height: size, backgroundImage: `url("${tile}")` }}
    >
      <img
        src={faviconUrl(host)}
        alt=""
        width={size}
        height={size}
        loading="lazy"
        decoding="async"
        // Which page of ours was being read is not the service's business.
        referrerPolicy="no-referrer"
        onLoad={() => setLoaded(true)}
        // Its own background, so an icon with transparent margins does not
        // show the monogram through them once it has loaded.
        className={cn(
          "absolute inset-0 size-full bg-[var(--card)] object-contain transition-opacity duration-200",
          loaded ? "opacity-100" : "opacity-0",
        )}
      />
    </span>
  )
}

export function AgentSources({
  groups,
  total,
  className,
}: {
  groups: SourceGroup[]
  total: number
  className?: string
}) {
  const [open, setOpen] = React.useState(false)
  const panelRef = React.useRef<HTMLDivElement>(null)
  const listRef = React.useRef<HTMLDivElement>(null)
  const chat = useChatScroll()

  /**
   * Opening it moves the page down with it.
   *
   * The list unfolds INTO the conversation - it pushes everything below it
   * down rather than floating over it - so on a full screen it can open
   * entirely below the fold, and clicking the control appears to do nothing.
   *
   * The height is measured BEFORE the animation, off the clipped content,
   * which is why this can run on the click rather than on `transitionend`:
   * the scroll knows where the box will end up and travels there alongside
   * the expansion, as one movement. It also means the chat's tail stops
   * following before the box starts growing, so the page cannot pin itself to
   * the bottom on the way and then jump back.
   *
   * Outside the chat workspace there is no floating bar and no scroller of our
   * own, so the plain browser behaviour is the right one.
   */
  const toggle = React.useCallback(() => {
    const next = !open
    setOpen(next)
    if (!next) return
    const height = listRef.current?.scrollHeight ?? 0
    if (chat) chat.reveal(panelRef.current, height)
    else {
      requestAnimationFrame(() =>
        panelRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }),
      )
    }
  }, [open, chat])

  if (!total) return null

  return (
    <div className={className}>
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        // The ring around each stacked favicon has to be the colour behind it
        // or the overlap reads as a smudge - so the button publishes its own
        // background as a variable and the images ring themselves with it.
        // Otherwise the hover state would leave four grey haloes behind.
        className="flex max-w-full items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--card)] py-1 pl-1.5 pr-2.5 text-left transition-colors [--stack-ring:var(--card)] hover:bg-[var(--muted)] hover:[--stack-ring:var(--muted)]"
      >
        <span className="flex shrink-0 -space-x-1.5">
          {groups.slice(0, STACK).map((group) => (
            <SourceFavicon
              key={group.host}
              host={group.host}
              size={18}
              // Rounded squares, not circles. A circular mask crops a wide
              // wordmark - AP's, most obviously - to a slice of itself, and a
              // favicon is already a square image.
              className="rounded-[5px] ring-2 ring-[var(--stack-ring)]"
            />
          ))}
        </span>
        <span className="truncate text-xs text-[var(--muted-foreground)]">
          {total} source{total === 1 ? "" : "s"}
          {groups.length > 1 ? ` · ${groups.length} sites` : ""}
        </span>
        <ChevronDown
          className={cn(
            "size-3.5 shrink-0 text-[var(--muted-foreground)] transition-transform duration-300",
            open && "rotate-180",
          )}
        />
      </button>

      {/* 0fr -> 1fr, which is the only way to transition to "the height of the
          content" without measuring it. The inner div does the clipping; the
          grid does the animating. */}
      <div
        ref={panelRef}
        className="grid transition-[grid-template-rows,opacity] duration-300 ease-[cubic-bezier(0.23,1,0.32,1)]"
        style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}
        // Clipped to nothing when shut, so it must also be inert: without
        // this, Tab walks through a dozen links that are not on screen.
        aria-hidden={!open || undefined}
        inert={!open || undefined}
      >
        <div ref={listRef} className="overflow-hidden">
          <div className="mt-2 max-h-72 overflow-y-auto rounded-[12px] border border-[var(--border)] bg-[var(--card)] p-1.5">
            {groups.map((group, index) => (
              <div
                key={group.host}
                // Only while open, so the stagger runs on the way in and the
                // rows are not left parked at opacity 0 behind a shut panel.
                className={open ? "animate-fade-up" : undefined}
                style={{ animationDelay: `${Math.min(index, 8) * 45}ms` }}
              >
                <div className="flex items-center gap-2 px-1.5 py-1.5">
                  <SourceFavicon host={group.host} size={16} className="rounded-[4px]" />
                  <span className="min-w-0 truncate text-[12.5px] font-medium">
                    {group.host}
                  </span>
                  {group.links.length > 1 && (
                    <span className="ml-auto shrink-0 rounded-full bg-[var(--muted)] px-1.5 text-[10.5px] leading-4 tabular-nums text-[var(--muted-foreground)]">
                      {group.links.length}
                    </span>
                  )}
                </div>

                {/* Indented under the favicon with a hairline down the side,
                    so five URLs from one site read as five pages of that site
                    rather than as five separate sources. */}
                <div className="mb-1 ml-3.5 border-l border-[var(--border)] pl-3">
                  {group.links.map((link) => (
                    <a
                      key={link.url}
                      href={link.url}
                      target="_blank"
                      rel="noreferrer"
                      title={link.url}
                      className="block truncate rounded-[6px] px-1.5 py-1 font-mono text-[11px] leading-5 text-[var(--muted-foreground)] transition-colors hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
                    >
                      {link.path}
                    </a>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * One claim with the page it was verified on.
 *
 * This is a real citation, not a decoration: the Research agent records a
 * `source_url` PER FACT in its brief, and that mapping is what arrives here.
 * A fact with no URL renders without a chip rather than borrowing the run's
 * general source list - it came from the news item's own text, and saying so
 * by omission is honest where attaching the nearest link would not be.
 */
export function AgentFacts({
  facts,
  className,
}: {
  facts: { fact: string; source_url: string }[]
  className?: string
}) {
  if (!facts.length) return null
  return (
    <div className={className}>
      {/* Labelled, because these are not more of the agent's narration - they
          are the checked claims the carousel is allowed to state. Unlabelled
          and unmarked they read as three more paragraphs, and the citation
          chips look like decoration on prose rather than the point. */}
      <p className="mb-2 text-[12px] font-medium text-[var(--muted-foreground)]">
        Verified facts
      </p>
      <ul className="space-y-1.5">
      {facts.map((item, index) => {
        const host = hostOf(item.source_url)
        return (
          <li
            key={`${index}:${item.fact.slice(0, 40)}`}
            className="animate-fade-up flex gap-2.5 text-[14px] leading-6"
            style={{ animationDelay: `${Math.min(index, 8) * 60}ms` }}
          >
            <span className="mt-[10px] size-1 shrink-0 rounded-full bg-[var(--muted-foreground)]" />
            <span className="min-w-0">
            {item.fact}{" "}
            {host && (
              <a
                href={item.source_url}
                target="_blank"
                rel="noreferrer"
                title={item.source_url}
                // Baseline-aligned and inline, so it reads as a citation
                // inside the sentence rather than as a button after it.
                className="ml-0.5 inline-flex translate-y-[3px] items-center gap-1 rounded-[5px] bg-[var(--muted)] px-1 py-px align-baseline font-mono text-[10.5px] leading-4 text-[var(--muted-foreground)] transition-colors hover:bg-[var(--border)] hover:text-[var(--foreground)]"
              >
                <SourceFavicon host={host} size={12} className="rounded-[3px]" />
                {host}
              </a>
            )}
            </span>
          </li>
        )
      })}
      </ul>
    </div>
  )
}

/** Every fact any agent recorded, in order, deduped on the claim itself. */
export function collectFacts(events: RunEvent[]): { fact: string; source_url: string }[] {
  const out: { fact: string; source_url: string }[] = []
  const seen = new Set<string>()
  for (const event of events) {
    const facts = event.data?.facts
    if (!Array.isArray(facts)) continue
    for (const item of facts) {
      if (!item || typeof item !== "object") continue
      const fact = String((item as { fact?: unknown }).fact ?? "").trim()
      if (!fact || seen.has(fact)) continue
      seen.add(fact)
      out.push({
        fact,
        source_url: String((item as { source_url?: unknown }).source_url ?? ""),
      })
    }
  }
  return out
}
