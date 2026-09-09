# Design Skill - Winning Carousel System

This is the production design authority for body and CTA slides. It is
distilled from the seven reference carousel sets supplied in `C:\1` through
`C:\7`, the current Baskaran Builds website tokens, and the coordinated concept
at `skills/references/carousel-system-concept.png`.

The references are a pattern library, not seven templates to copy. Preserve
their teaching clarity, editorial pacing, and visual proof while keeping one
coherent Baskaran Builds identity across the final carousel.

## Brand tokens

Use these exact current `baskaranbuilds.com` variables:

- `--bg-dark: #161811` - ink background.
- `--bg-card: #1F2218` - elevated dark surface.
- `--surface-light: #F7F7F5` - paper background.
- `--text-primary-light: #E8E4D6` - primary text on ink.
- `--text-muted-light: #B9C5AA` - muted text.
- `--text-dark: #1A1A18` - primary text on paper.
- `--accent-green: #8FB832` - the only accent green. Every emphasized phrase,
  number, node, and data mark uses this exact solid color with no shade change,
  tint, gradient, glow, or alternate green.

Do not use the legacy orange palette on new slides. Do not introduce blue,
purple, neon glow, or unrelated gradients. Body slides use one consistent
paper frame for the title field, number, divider, and footer. Ink may appear
inside the lower explanatory visual. The CTA uses an ink frame.

## Format and type

- Canvas: 1080 x 1350 px, portrait 4:5.
- Safe area: at least 88 px left/right and 76 px top/bottom.
- Inside-slide headings: exactly 76 px at 1080 px canvas width, Bricolage
  Grotesque or a close bold condensed grotesk, with tight editorial
  line-height. Every body and CTA headline uses this same size and face.
- The cover keeps the same condensed family but uses its own larger 128 px
  display scale. Never use the cover image or cover palette as a body-slide
  layout template.
- Editorial emphasis: Instrument Serif or a close high-contrast serif, used
  sparingly for one phrase rather than whole paragraphs.
- Labels/data: Instrument Sans or a compact clean grotesk.
- Preferred body size: visually equivalent to 36 px at 1080 px width. When
  approved copy needs more room, the compositor may step down to the shared
  readable minimum of 30 px, never smaller.
- Headline: normally <= 7 words. Prefer 76 px and step down only as far as the
  shared 60 px minimum when necessary. Body: at most 3 short lines or one
  compact paragraph. One thought per line.

## Carousel rhythm

Every slide must do one job and have one dominant visual. Vary composition,
not identity. A strong sequence normally moves through:

1. statement or tension;
2. explanation;
3. evidence;
4. mechanism/process;
5. implication or comparison;
6. takeaway;
7. CTA.

Do not repeat the same headline-plus-bullets layout on consecutive slides.
Do not build a grid of cards inside a slide. Prefer open editorial composition,
a single diagram, one chart, one comparison, or one proof object.

## Body slide template

There is no single fixed body template. Choose the archetype that best proves
the approved copy. Keep the approved headline and body text verbatim.

### Editorial explainer

Use on definitions, context, and a single important idea. Paper background,
large headline in the upper third, one compact paragraph, and one simple
hand-drawn line illustration or annotation in the lower half. The visual must
explain the idea rather than decorate it.

### Data evidence

Use when the copy includes a meaningful number, percentage, price, benchmark,
date, or count. Make that value or a simple chart the focal point. Supporting
copy stays secondary. Show honest scale and labels; never invent data points.

### Process line

Use for steps, sequences, loops, workflows, or cause-and-effect. Draw one
continuous line or timeline through 2-4 labeled stops. Avoid three separate
cards. The reader should understand direction in one glance.

### Comparison

Use for before/after, versus, old/new, promise/reality, or two-owner examples.
Create a clean split composition around one meaningful contrast. Match visual
weight on both sides and make the conclusion obvious without extra copy.

### Dark proof

Use for a decisive thesis, code example, command, interface fragment, or short
technical proof. Ink background, warm-white headline, lime emphasis, and one
bounded proof object. Syntax or interface text may use the paper and muted
tokens; no fake terminal chrome.

### Statement pause

Use after a dense slide to reset pace. One strong sentence, a small visual
metaphor, and generous negative space. Do not add bullets or filler labels.

## Persistent furniture

- Body slides use one deterministic two-digit sequence at x=88, y=76,
  32 px semibold, with fixed weight and line height. The first body slide is
  `01` and the last body slide is the body-slide count. The cover image/video
  and the final CTA are unnumbered. The image model must leave this zone blank
  on body slides and must never draw its own number.
- Body-slide bottom rail uses the official Baskaran Builds favicon from
  `skills/references/baskaranbuilds-favicon.png`, followed by the exact
  configured Instagram handle and an icon-only right arrow. Never redraw,
  simplify, or replace the favicon with a generic lime spark.
- The footer is composited deterministically after generation. Leave the full
  area below the divider (y=1161 through the bottom edge) empty; never draw
  replacement footer furniture inside the generated artwork.
- One thin divider may anchor the rail. Do not add a thick footer block.
- Exactly one `#8FB832` emphasis moment per slide: a phrase, value, path, or node.
- The rail is consistent across light and dark slides; invert text colors for
  contrast while preserving geometry.

## Illustration, charts, and proof

- The image model generates only the lower visual layer. Headline, body copy,
  body-slide number, and footer are composited deterministically after generation.
  Generated artwork must contain no text, letters, digits, labels, watermarks,
  pseudo-writing, or green accent.
- When a slide introduces the exact news subject, use the sourced cover media
  as a factual image inside the lower visual zone. Composite it after visual
  generation. Never send the cover image through the image-edit endpoint as a
  body template because that contaminates the slide palette and composition.
- All non-photo visual styling stays inside the Baskaran Builds ink, paper,
  warm-white, muted, and single-green system. Do not inherit red, orange, or
  unrelated colors from a cover image into the text panel or footer.
- Prefer line drawings with human irregularity, simple geometric data marks,
  sparse dot matrices, flow lines, comparison dividers, and real code snippets.
- Never fabricate a chart to make a slide look analytical. Every plotted value
  must come from the approved text/research.
- Use diagrams for relationships, not as generic AI imagery.
- Use at most one visual technique per slide. Do not combine a chart, diagram,
  code block, portrait, and decorative texture.
- Keep illustrations away from body copy and preserve breathing room.
- Generate every explanatory visual directly as the exact 2:1 lower panel
  (generated at a 2:1 divisible-by-16 canvas, merged into slide coordinates x=0..1080 and
  y=620..1160). Compose for that wide frame from the start. Keep important
  subjects fully visible and use the complete panel so no post-generation
  stretching or cropping is needed.
- The compositor must preserve the artwork's aspect ratio. If an unexpected
  source ratio is returned, contain the whole image and use the surrounding
  paper or ink field as padding. Never use cover-cropping or non-uniform resize.
- Sourced subject images are contained, centered horizontally, and aligned to
  the divider. The full source remains visible; the generated panel behind it
  supplies any side space needed for a different aspect ratio.

## CTA slide

The CTA is the final beat, not an advertisement pasted onto the sequence.
Use an ink background with a dark, cinematic creator/subject image when one is
available; otherwise use a restrained textured ink field.

Choose one action only:

- **follow** - a concrete value promise and the configured handle;
- **comment** - one specific question and a direct comment instruction;
- **redirect** - what the deeper breakdown contains and the configured link.

The headline is <= 7 words, large and left-aligned or centered according to the
image balance. Highlight one phrase in lime. Keep supporting copy to 1-2 lines.
The CTA bottom rail starts with one official Baskaran Builds favicon followed
by `@baskaranbuilds`. Do not add a small profile portrait or a second identity
icon in the footer. The CTA has no swipe arrow. A small save cue is allowed
only when saving is the action. Do not show multiple buttons, multiple actions,
or invented links.

## Padding contract

- Editorial content: x=88..992 and y=140..1110. On body slides only, the number
  occupies the reserved x=88..200, y=76..130 zone.
- The entire upper field y=0..620 is repainted as one coherent ink or paper
  surface before text and any body-slide number are added. Body slides use
  paper and the CTA uses ink. Never leave a mismatched top strip or a
  different-color tile behind the body-slide number.
- Footer reservation: y=1161..1350; generated content never enters it. A
  dominant lower visual may extend through y=1160 so it meets the divider.
- Divider: x=88..992 at y=1160.
- Footer centerline: y=1232.
- Body favicon and CTA favicon begin at x=88; both handles begin at x=160.
- All footer furniture ends above y=1274, preserving the 76 px bottom safe area.
- The runtime validator rejects missing or mispositioned favicons, dividers,
  incorrect dimensions, or footer geometry outside this safe area.

## Hard quality gates

- Approved copy is rendered verbatim. Never paraphrase, correct, or add words.
- Use Latin-script English transliterations in visible slide copy. Do not add
  Chinese characters or alternate-script names in parentheses.
- Every visible word must be valid, correctly formed, and understandable. Do
  not draw pseudo-text, garbled letters, decorative writing, invented labels,
  or random symbols that resemble language. When an illustration would
  normally need an unapproved label, leave that element unlabeled.
- Never render an em dash in any cover, body, CTA, or caption text.
- No text may be clipped, warped, illegible, or smaller than the 30 px minimum
  body size. If copy still does not fit at the readable minimums, shorten it
  upstream without changing facts.
- Do not add fake metrics, sources, quotes, handles, labels, or watermarks.
- No default card grids, nested cards, glossy 3D icons, stock AI brains, robots
  as decoration, or generic circuit-board backgrounds.
- No more than one accent treatment and one dominant visual per slide.
- Maintain enough contrast for both paper and ink surfaces.
- The full carousel must look authored as one story, not seven unrelated social
  templates.

## Learned rules (appended by the Learner agent - do not delete)
