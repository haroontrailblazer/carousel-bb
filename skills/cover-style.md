# Cover Style - First Page of Every Carousel

The cover is the strongest frame of the winning-carousel system. It uses the
existing sourced-media pipeline and overlay mechanics while adopting the
current Baskaran Builds site palette.

## Format

- 1080 x 1350 px (4:5). This aspect ratio governs every following slide.
- The cover remains a 4-15 second sourced video under the current runtime
  contract; a sourced still with a restrained push-in is the fallback.
- The media is never AI-generated. Use the announcement/event clip, product UI,
  paper figure, launch image, or another source-grounded visual.

## Brand tokens

- Ink: `#161811`.
- Primary text: `#E8E4D6`.
- Accent: `#8FB832` only. Never use another green shade or a gradient.
- Muted: `#B9C5AA`.

Legacy orange is not used for new cover text or accent furniture.

## Composition

1. **Media zone (top ~62%).** Keep the subject/product recognizable and away
   from the title. Crop for one clear focal point; slight darkening is allowed.
2. **Grain dissolve (bottom ~38%).** Ink rises from the bottom through a
   stippled/noise edge, never a generic smooth gradient.
3. **Title block (lower third).** Use a large 128 px condensed bold grotesk,
   tight editorial line height, warm-white, with exactly one verbatim phrase
   in `#8FB832`. This is intentionally larger than the fixed 76 px inside-slide
   headline so the cover has correct feed-thumbnail proportion. Wrap to no
   more than three balanced lines and never shrink it.
4. **Continuity furniture.** Preserve the faint perspective floor/grid and
   compact side-arrow cues from the current overlay, recolored to `#8FB832`.
5. **Brand rail.** Keep the lower edge quiet. Do not add a second headline,
   badges, stats, or source labels.

## Title rules

- Maximum 9 words total; 5-7 words is preferred.
- Lead with tension, consequence, or a surprising mechanism-not a generic
  announcement such as "X IS HERE".
- `hook_highlight` must be a verbatim substring of `hook_title` and contain the
  consequence or turn in the idea.
- Use punctuation only when it improves spoken rhythm; no emoji or hashtags.
- Never use an em dash. Use a period, comma, or colon instead.
- The title must remain readable at feed-thumbnail size.
- Slide `01` is drawn deterministically at x=88, y=76 in the same 32 px
  semibold style used by every body and CTA slide.

Example shape: `YOUR AGENT LOOKS SMART UNTIL REALITY HITS`, highlighting
`REALITY HITS`.

## Image treatment

- Prefer real source imagery with useful negative space over generic cinematic
  AI imagery.
- The cover must show the current prominent image or clip of the exact news
  subject. Prefer attached official media pages and trusted news or media
  catalogs. Reject unverified blog OG graphics, text-heavy generic graphics,
  and images that do not visibly show the subject.
- Do not tint the whole media frame green. `#8FB832` belongs only to the highlight
  phrase and small directional furniture.
- Avoid glows, lens flares, floating logos, and decorative circuit patterns.
- If the source is a paper or UI screenshot, keep one identifiable proof region
  visible rather than blurring the entire image behind the title.

## Legacy template note

`STRANGE-COVER (1).png` remains the geometry source for the grain dissolve,
grid, and arrows. Runtime compositing scrubs its example title and recolors its
legacy orange accents to the single `#8FB832` token before rendering new copy.
