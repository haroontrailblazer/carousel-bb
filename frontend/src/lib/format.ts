/**
 * Small formatting helpers.
 *
 * Intl handles both cases the app actually needs, so there is no date library
 * here - date-fns would be ~20KB to format two things.
 */

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
  ["second", 1],
]

/** "3 minutes ago", "yesterday". Empty string for a missing timestamp. */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return ""
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ""
  const deltaSeconds = (then - Date.now()) / 1000
  for (const [unit, seconds] of UNITS) {
    if (Math.abs(deltaSeconds) >= seconds || unit === "second") {
      return rtf.format(Math.round(deltaSeconds / seconds), unit)
    }
  }
  return ""
}

/** "4:07" or "1:02:33" - elapsed wall clock, not a duration in prose. */
export function elapsed(fromIso: string | null, toIso?: string | null): string {
  if (!fromIso) return "—"
  const start = new Date(fromIso).getTime()
  const end = toIso ? new Date(toIso).getTime() : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end)) return "—"
  const total = Math.max(0, Math.floor((end - start) / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, "0")
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
}

export function compactNumber(value: number | undefined | null): string {
  if (value == null) return "—"
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value)
}

/** Instagram's hard caption limit. Shown as a live count while reviewing. */
export const IG_CAPTION_LIMIT = 2200
