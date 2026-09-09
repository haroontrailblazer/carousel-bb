import { Check, LoaderCircle, PanelRightClose } from "lucide-react"
import { Link } from "react-router"

import type { RunArtifacts } from "@/lib/types"
import { cn } from "@/lib/utils"

type AssetItem = {
  key: string
  label: string
  url: string | null
  index: number
}

function readyAssets(artifacts: RunArtifacts | null | undefined): AssetItem[] {
  if (!artifacts) return []
  const items: AssetItem[] = []
  if (artifacts.cover.poster?.url) {
    items.push({ key: "cover", label: "Cover", url: artifacts.cover.poster.url, index: 1 })
  }
  for (const slide of artifacts.slides) {
    if (!slide.url) continue
    items.push({ key: slide.filename ?? `slide-${slide.index}`, label: `Slide ${slide.index}`, url: slide.url, index: slide.index })
  }
  if (artifacts.cta.url) {
    items.push({ key: "cta", label: "CTA", url: artifacts.cta.url, index: items.length + 1 })
  }
  return items
}

function AssetSkeleton({ index }: { index: number }) {
  return (
    <div className="relative aspect-[4/5] overflow-hidden rounded-[14px] border border-[var(--border)] bg-[var(--card)]">
      <LoaderCircle className="absolute left-3 top-3 size-4 animate-spin-slow text-[var(--muted-foreground)]" />
      <div className="absolute inset-x-5 top-[38%] space-y-2">
        <div className="h-2 rounded-full bg-[var(--muted)]" />
        <div className="h-2 w-4/5 rounded-full bg-[var(--muted)]" />
        <div className="mt-5 h-1.5 w-2/3 rounded-full bg-[var(--muted)]" />
        <div className="h-1.5 w-1/2 rounded-full bg-[var(--muted)]" />
      </div>
      <span className="absolute bottom-3 left-3 grid size-6 place-items-center rounded-lg bg-[var(--muted)] text-xs tabular-nums text-[var(--muted-foreground)]">
        {index}
      </span>
    </div>
  )
}

export function AgentAssetRail({
  artifacts,
  loading,
  live,
  runId,
  className,
  hidden,
  onCollapse,
}: {
  artifacts: RunArtifacts | null | undefined
  loading: boolean
  live: boolean
  runId: string
  /** Entrance animation; see `.animate-rail-in`. */
  className?: string
  /** Shut: the grid track is 0 wide, so nothing here is reachable. */
  hidden?: boolean
  /** Collapse the panel. The control lives in here while it is open. */
  onCollapse?: () => void
}) {
  const items = readyAssets(artifacts)
  const expected = Math.max(artifacts?.expected_count ?? 0, items.length, live ? 3 : 0)
  const skeletons = Math.min(Math.max(expected - items.length, loading && !items.length ? 2 : 0), 4)

  return (
    <aside
      // Clipped to nothing by the closed grid track, so it must also be
      // inert: without this, Tab still walks into a panel that is not there
      // and a screen reader still reads out every asset.
      aria-hidden={hidden || undefined}
      inert={hidden || undefined}
      className={cn(
        "agent-asset-rail border-[var(--border)] bg-[var(--background)]",
        className,
      )}
    >
      {/* pt-2.5, not pt-5: this row has to line up with the chat header beside
          it, so that the collapse control and the trace control sit on one
          line rather than ten pixels apart. Measured, not guessed - see
          measure-toggle in the harness. */}
      <div className="sticky top-0 z-10 flex items-center gap-2 bg-[var(--background)]/92 px-4 pb-3 pt-2.5 backdrop-blur">
        {/* Closing the panel belongs to the panel, beside its own name. The
            chat header only carries this control while the rail is shut,
            because that is the only state where the rail cannot carry it. */}
        {onCollapse && (
          <button
            type="button"
            onClick={onCollapse}
            title="Hide the assets panel"
            className="-ml-2 grid size-9 shrink-0 place-items-center rounded-[10px] text-[var(--muted-foreground)] hover:bg-[var(--muted)] hover:text-[var(--foreground)]"
          >
            <PanelRightClose className="size-4.5" />
            <span className="sr-only">Hide the assets panel</span>
          </button>
        )}
        <div className="min-w-0">
          <h2 className="text-sm font-semibold">Assets</h2>
          <p className="mt-1 flex items-center gap-1.5 text-xs text-[var(--muted-foreground)]">
            <span className="size-1.5 rounded-full bg-[var(--brand)]" />
            {items.length}{expected ? ` of ${expected}` : ""} ready
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 px-4 pb-5 sm:grid-cols-3 lg:grid-cols-1">
        {items.map((item) => (
          <figure key={item.key} className="group relative overflow-hidden rounded-[14px] border border-[var(--border)] bg-[var(--card)]">
            <img
              src={item.url ?? undefined}
              alt={`${item.label} preview`}
              className="aspect-[4/5] w-full object-contain"
              loading="lazy"
            />
            <figcaption className="absolute inset-x-2 bottom-2 flex items-center justify-between rounded-[9px] bg-black/70 px-2 py-1.5 text-[11px] text-white backdrop-blur-sm">
              <span>{item.label}</span>
              <span className="grid size-5 place-items-center rounded-full bg-[var(--brand)] text-[var(--brand-foreground)]">
                <Check className="size-3 stroke-[3]" />
              </span>
            </figcaption>
          </figure>
        ))}

        {Array.from({ length: skeletons }, (_, index) => (
          <AssetSkeleton key={`skeleton-${index}`} index={items.length + index + 1} />
        ))}

        {!loading && !live && !items.length && (
          <div className="col-span-full grid min-h-40 place-items-center rounded-[14px] border border-dashed border-[var(--border)] px-4 text-center text-xs leading-5 text-[var(--muted-foreground)]">
            No rendered assets are available for this task.
          </div>
        )}
      </div>

      {items.length > 0 && (
        <div className="border-t border-[var(--border)] p-4">
          <Link
            to={`/tasks/${runId}?tab=review`}
            viewTransition
            className="flex w-full items-center justify-center rounded-[10px] bg-[var(--foreground)] px-3 py-2 text-xs font-medium text-[var(--background)] transition-opacity hover:opacity-85"
          >
            Open carousel review
          </Link>
        </div>
      )}
    </aside>
  )
}

export function AgentAssetStrip({
  artifacts,
  live,
  runId,
  className,
  showReview = true,
}: {
  artifacts: RunArtifacts | null | undefined
  live: boolean
  runId: string
  /** Entrance animation; see `.animate-strip-in`. */
  className?: string
  /**
   * Off when the "Your carousel is ready" card is already on screen.
   *
   * That card carries its own Review button, so leaving this on gave a phone
   * two ways into the same screen, one under the other. The caller decides,
   * because it is the one that knows whether the card is being drawn.
   */
  showReview?: boolean
}) {
  const items = readyAssets(artifacts)
  if (!items.length && !live) return null

  return (
    <section className={cn("agent-asset-strip space-y-2", className)}>
      <p className="text-xs font-medium text-[var(--muted-foreground)]">
        Assets · {items.length}{artifacts?.expected_count ? ` of ${artifacts.expected_count}` : ""} ready
      </p>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {items.map((item) => (
          <img
            key={item.key}
            src={item.url ?? undefined}
            alt={`${item.label} preview`}
            className="h-32 w-[6.4rem] shrink-0 rounded-[10px] border border-[var(--border)] bg-[var(--card)] object-contain"
            loading="lazy"
          />
        ))}
        {/* One tile per slide still to come - and none once they have all
            arrived. Math.max(1, ...) forced a permanent phantom spinner onto
            the end of a complete strip, so a finished carousel looked like it
            was still rendering something. */}
        {live && Array.from({ length: Math.max(0, Math.min(3, (artifacts?.expected_count ?? 3) - items.length)) }, (_, index) => (
          <div key={index} className="grid h-32 w-[6.4rem] shrink-0 place-items-center rounded-[10px] border border-[var(--border)] bg-[var(--card)]">
            <LoaderCircle className="size-4 animate-spin-slow text-[var(--muted-foreground)]" />
          </div>
        ))}
      </div>

      {/* A bar, the same one the desktop rail ends with - not the 12px link
          that used to sit up beside the "Assets" count. On a phone that link
          was a 67x16 tap target next to a 111x32 button pointing at the same
          screen, which read as two different things and was the harder of the
          two to hit. */}
      {showReview && items.length > 0 && (
        <Link
          to={`/tasks/${runId}?tab=review`}
          viewTransition
          className="mt-1 flex w-full items-center justify-center rounded-[10px] bg-[var(--foreground)] px-3 py-2.5 text-xs font-medium text-[var(--background)] transition-opacity hover:opacity-85"
        >
          Open carousel review
        </Link>
      )}
    </section>
  )
}
