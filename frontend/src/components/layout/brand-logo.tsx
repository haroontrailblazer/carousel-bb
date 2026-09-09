import { cn } from "@/lib/utils"

/**
 * Shared decorative brand mark. Nearby text supplies the accessible name.
 *
 * `/logo.webp` is 192px square - four times the largest place it is drawn
 * (`size-12`, 48px), so it stays sharp on any display anyone is likely to have
 * and costs 13 KB. It used to be the 1254px master at 165 KB, which every
 * visitor downloaded to render a 45px mark in the sidebar. The master is kept
 * at `frontend/brand/logo-source.webp`, outside `public/` so it is not
 * deployed; regenerate from it if the mark ever needs to be bigger.
 *
 * width/height match the file so the browser reserves the box before the
 * image arrives - without them the sidebar and the sign-in card reflow around
 * it as it lands.
 */
export function BrandLogo({ className }: { className?: string }) {
  return (
    <img
      src="/logo.webp"
      alt=""
      aria-hidden="true"
      className={cn(
        "shrink-0 object-contain drop-shadow-[0_3px_5px_rgb(22_24_17_/_0.18)]",
        className,
      )}
      width={192}
      height={192}
      // It is on screen on the first frame of every route, including sign-in,
      // so it should not queue behind anything the browser guesses is more
      // urgent. Decoding stays off the main thread.
      fetchPriority="high"
      decoding="async"
    />
  )
}
