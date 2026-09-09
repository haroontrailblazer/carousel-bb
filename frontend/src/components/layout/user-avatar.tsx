import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * A generated avatar, so nobody is ever faceless.
 *
 * A first-time user has no uploaded picture and usually no Gravatar either,
 * and the old fallback - a letter on a flat grey chip - read as a placeholder
 * that had failed to load rather than as their picture. This draws a real
 * image instead: a two-tone gradient with their monogram on it, derived from
 * the email, so it is stable for a person and different between people.
 *
 * An inline SVG data URI rather than a network request: it needs no round
 * trip, no service, and cannot 404 on first paint - which is exactly when it
 * is needed.
 */
export function defaultAvatar(seed: string, label: string): string {
  // FNV-1a: tiny, deterministic, and good enough to spread hues.
  let hash = 0x811c9dc5
  for (let i = 0; i < seed.length; i++) {
    hash ^= seed.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  const hue = hash % 360
  // The second hue is deliberately a long way round the wheel, so the
  // gradient reads as a designed pair rather than a smudge.
  const hue2 = (hue + 48) % 360
  const initial = (label.trim()[0] ?? "?").toUpperCase()

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="hsl(${hue} 62% 58%)"/>
<stop offset="1" stop-color="hsl(${hue2} 66% 44%)"/>
</linearGradient></defs>
<rect width="96" height="96" fill="url(#g)"/>
<text x="48" y="49" fill="#fff" fill-opacity="0.92" font-size="42"
 font-family="Georgia, 'Times New Roman', serif" text-anchor="middle"
 dominant-baseline="central">${initial}</text></svg>`

  // encodeURIComponent, not btoa: btoa throws on any non-Latin-1 character,
  // and an initial can be one.
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

/**
 * Round avatar: the stored picture, else Gravatar, else the generated one.
 *
 * Pass a changing `key` (e.g. `key={src}`) when the src can change at
 * runtime - otherwise React keeps the old <img> and its failed state, and a
 * newly-uploaded picture stays showing the previous one.
 */
export function UserAvatar({
  src,
  name,
  seed,
  className,
}: {
  src?: string | null
  name?: string | null
  /** What the generated fallback is derived from. Defaults to `name`. */
  seed?: string | null
  className?: string
}) {
  const [failed, setFailed] = React.useState(false)
  const label = name?.trim() || "?"
  const fallback = React.useMemo(
    () => defaultAvatar(seed?.trim() || label, label),
    [seed, label],
  )

  return (
    <span
      className={cn(
        "flex shrink-0 items-center justify-center overflow-hidden rounded-full",
        "ring-1 ring-[var(--border)]",
        className,
      )}
    >
      <img
        src={src && !failed ? src : fallback}
        alt=""
        className="size-full object-cover"
        onError={() => setFailed(true)}
      />
    </span>
  )
}

/**
 * Gravatar URL for an email, or null.
 *
 * SHA-256 via SubtleCrypto - Gravatar's modern hash. `d=404` so a missing
 * Gravatar fails the image load and the generated avatar takes over, rather
 * than Gravatar serving its own generic silhouette.
 */
export async function gravatarUrl(
  email: string,
  size = 96,
): Promise<string | null> {
  try {
    const normalized = email.trim().toLowerCase()
    const bytes = new TextEncoder().encode(normalized)
    const digest = await crypto.subtle.digest("SHA-256", bytes)
    const hash = Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("")
    return `https://www.gravatar.com/avatar/${hash}?s=${size}&d=404`
  } catch {
    return null
  }
}
