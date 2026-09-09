# Carousel Factory

Carousel Factory turns AI/product news (a new model release, a Lovable or
Supabase feature drop, a paper worth explaining) into finished Instagram
carousels - planned, written, designed, QA-checked, human-reviewed by email,
and auto-published - using a Google ADK multi-agent pipeline. A fetcher pulls
updates from Gmail newsletters, RSS feeds and YouTube channels into a queue;
each queued item drives one pipeline run: an Editorial Planner decides
structure (points vs prose, slide count, hook title), a First-Page Visual
agent builds the cover as a **sourced** 4–8 s video (never AI-generated -
trimmed from the announcement itself, composited with the brand overlay per
`skills/cover-style.md`), a Phrasing agent writes the copy, a Template Design
agent renders body slides with gpt-image-2 against the designer's templates, a
CTA agent renders the closing slide, and a Stitch & Verify agent assembles and
QA-checks the bundle.

The human stays in the loop by email: a review mail with the preview and
Approve/Reject links pauses the run (an ADK `LongRunningFunctionTool` - the
invocation genuinely ends and is resumed later by the review API). **Approve**
(optional feedback) publishes to Instagram via the Graph API and sends a
confirmation mail. **Reject** (feedback compulsory) re-runs *only the blamed
agent* - "the first visual is not good" regenerates just the cover - then
re-assembles and mails a fresh review. Every piece of feedback is stored in
long-term memory, and recurring feedback is distilled by the Learner agent
into permanent edits to the instruction files under `skills/` - the pipeline's
editable "harness" - so the system permanently improves from review to review.
State and the run ledger live in Supabase Postgres; media artifacts live in
Supabase Storage. The architecture is modeled in `architecture/carousel.c4`
(LikeC4 - views: `index`, `containers`, `agentPipeline`, `happyPath`,
`rejectRework`, `approvePublish`, `production`).

> **IMPORTANT - no git commits.** Nothing in this working tree gets committed
> or pushed by tooling or agents. The tree stays as-is, awaiting the owner's
> review. Commit only when the owner has reviewed and says so.

---

## Repo map

| path | what it is |
|---|---|
| `app/agent.py` | `root_agent` (discovered by `adk web` / `adk run`) + `build_runner()` used by the fetcher and review API |
| `app/orchestrator.py` | `CarouselOrchestrator` - the re-entrant phase state machine (`generate → qa → review → publish/rework → done`) |
| `app/agents/` | one file per agent (planner, first_page_visual, phrasing, template_design, cta, stitch_verify, review_dispatcher, feedback_router, publisher, learner) |
| `app/tools/` | media (yt-dlp + FFmpeg), gpt-image-2, Gmail, Instagram Graph API tools |
| `app/services/` | Supabase Storage artifact service, Postgres memory service, asyncpg helpers |
| `fetcher/fetch_news.py` | pulls newsletters/RSS/YouTube into the news queue; starts runs |
| `db/schema.sql` | `news_queue`, `runs`, `feedback`, `pending_reviews` |
| `skills/` | the editable harness: cover style, design skill, per-agent instructions |
| `docs/CONTRACTS.md` | the binding code-level spec |
| `architecture/carousel.c4` | C4 model (preview: `npx likec4 start architecture`) |

---

## Setup

Prerequisites: Python 3.11+ (developed on 3.13), FFmpeg, a Supabase project,
a Google Cloud project with the Gmail API enabled, an OpenAI API key (all
models run on it by default; a Gemini key is only needed if you point a
`*_MODEL` at a bare `gemini-*` id),
and an Instagram **professional** account connected to a Facebook app with the
Graph API content-publishing permissions.

### 1. Python environment

```powershell
cd C:\Projects\carousel
python -m venv .venv
.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 2. FFmpeg

FFmpeg is a system dependency, not a pip package. Install it and make sure
`ffmpeg` is on `PATH` (Windows: `winget install Gyan.FFmpeg` or grab a build
from https://ffmpeg.org; macOS: `brew install ffmpeg`). If it lives elsewhere,
point `FFMPEG_BIN` in `.env` at the executable.

### 3. Configuration

```powershell
copy .env.example .env            # macOS/Linux: cp .env.example .env
```

Fill in every section of `.env` - LLM keys, Supabase, Gmail, Instagram, CTA
links, fetch sources. All secrets flow through `app/config.py`; nothing is
hard-coded. Never commit `.env`.

`IG_HANDLE` defaults to `@baskaranbuilds`. The final CTA rail uses the same
single official favicon and handle arrangement as the other slides.

### 4. Database (Supabase Postgres)

Apply the schema once (idempotent - safe to re-run). Use the direct Postgres
URL **without** the `+asyncpg` marker for psql:

```powershell
psql "postgresql://postgres:PASSWORD@HOST:5432/postgres" -f db/schema.sql
```

or paste `db/schema.sql` into the Supabase Dashboard → SQL Editor. The ADK
`DatabaseSessionService` creates its own session tables automatically on
first use.

### 5. Artifact bucket (Supabase Storage)

Create a **private** bucket named after `MEDIA_BUCKET` (default
`carousel-media`) in Supabase Dashboard → Storage. Then enable the
S3-compatible protocol (Project Settings → Storage → S3 access keys) and copy
the endpoint, region, access key and secret into the `SUPABASE_S3_*` vars.
The artifact service stores every cover video and slide PNG there and signs
time-limited public URLs for review mails and Instagram publishing.

### 6. Gmail OAuth (first run is interactive)

1. In Google Cloud Console create **OAuth client ID → Desktop app**
   credentials and download the JSON to `secrets/gmail-credentials.json`
   (path configurable via `GMAIL_CREDENTIALS_PATH`).
2. The first time anything touches Gmail (fetching newsletters or sending a
   review mail) a browser consent window opens; approve it once. The token is
   cached at `secrets/gmail-token.json` and refreshes itself afterwards.
   Do this first run on a machine with a browser - not on a headless server.

---

## Running

### `adk web` - the realtime surface

This is the window into the system: the live agent graph, the event stream,
and the session-state inspector. From the **repo root**, with the venv active:

```powershell
adk web
# equivalent: python -m google.adk.cli web
# custom port: adk web --port 8000
```

Verified against the installed google-adk 2.7.0 CLI: `adk web [AGENTS_DIR]`
defaults `AGENTS_DIR` to the current directory and scans its subfolders for
agent packages, so run it from `C:\Projects\carousel` and it discovers the
`app/` package (via `app/agent.py`'s module-level `root_agent`). Open
http://127.0.0.1:8000 and pick **app** in the app dropdown.

What you get, live, while a run executes:

- **Agent graph** - `carousel_orchestrator` at the root with all eleven
  sub-agents attached (they are declared as `sub_agents`, which is what makes
  ADK render the tree): research, planner, first_page_visual, phrasing,
  template_design, cta, stitch_verify, review_dispatcher, feedback_router,
  publisher, learner. The research agent runs FIRST: it web-searches the
  update (OpenAI Responses `web_search`), saves a source-cited fact brief to
  state for the planner/phrasing agents, and feeds any official announcement
  media it finds to the cover agent.
- **Event stream** - every LLM turn, tool call and tool response as it
  happens. The orchestrator additionally emits one concise event per phase
  transition, authored by `carousel_orchestrator`, of the form
  `[phase] generate -> qa`, `[phase] qa -> review`, `[phase] rework -> qa` …
  - these are the state machine's heartbeat and make the **rework loop
  directly visible**: after a rejection you'll see
  `[phase] review -> rework`, the feedback_router's `ReworkPlan`, only the
  blamed agent(s) re-running, then `[phase] rework -> qa` and
  `[phase] qa -> review` as the corrected carousel goes back out for review.
- **State inspector** - the session state is the pipeline's entire memory
  (`phase`, `run_id`, `carousel_plan`, `cover`, `copy_set`, `body_slides`,
  `cta_slide`, `bundle`, `qa_report`, `review_verdict`, `rework_plan`,
  `rework_round`, …; keys defined in `app/state.py`). Because the review
  pause ends the invocation and the resume is a NEW invocation, everything
  lives here - inspecting it tells you exactly where a run stands.

To exercise the pipeline from the UI, send a message like *"run"* - the
orchestrator reads its phase from state and proceeds. Runs started by the
fetcher normally seed `news_item` in state first; without one the run halts
early with an explanatory event.

**Watching the full mail → click → resume loop inside `adk web`** takes two
extra alignments, because `adk web` builds its own runner (it does *not* use
`app.agent.build_runner()`): by default it stores sessions in local `.adk`
storage under its own app name (`app`, the folder name), while the review API
resumes sessions via `build_runner()` under `APP_NAME` in the shared Postgres
DB. So for an end-to-end run driven from the UI:

```powershell
# .env: APP_NAME=app   (must match the adk web app/folder name)
adk web --session_service_uri "postgresql+asyncpg://postgres:PASSWORD@HOST:5432/postgres"
```

Both processes then address the same sessions and the review API's resume
lands in the very session you're inspecting. For plain observation of the
generate/QA phases none of this is needed - `adk web` alone works, with
graceful in-memory fallbacks when Supabase env vars are missing.

`adk web` is a development server with **no authentication** - keep it on
localhost. (Terminal alternative without the UI: `adk run app`.)

### Fetching news

```powershell
python -m fetcher.fetch_news --fetch     # pull newsletters + RSS + YouTube, dedupe into news_queue
python -m fetcher.fetch_news --run-one   # pop the next queued item and start one pipeline run
```

`--fetch` reads Gmail (query `NEWSLETTER_QUERY`), the `RSS_FEEDS` list and
`YOUTUBE_CHANNELS` feeds, dedupes by URL hash, and enqueues. `--run-one`
starts a run via `build_runner()` - the run executes generate → qa → review
and then **pauses**, waiting for the email verdict. In production these are a
Cloud Scheduler → Cloud Run job; locally you run them by hand.

### Reviewing a carousel

Reviews happen in the console, at `/tasks/{run_id}?tab=review`. Telegram sends
one **REVIEW CAROUSEL** button pointing there, so the reviewer sees the cover
video and still side by side, every slide at full size, and the caption
counted against Instagram's limit before deciding.

Signing in is required. There used to be standalone Approve/Reject pages that
needed no credentials, on the reasoning that a Telegram link opens where
nobody can log in - but approving auto-publishes to Instagram, so any leaked
URL was a permanent, anonymous publish button. Those pages are gone; the
verdict now goes through `POST /api/runs/{id}/verdict`, which records who
decided.

Set `PUBLIC_BASE_URL` in `.env` to the address the button should carry.
Telegram refuses a non-public URL in a button, so with `localhost` (or nothing)
the review message falls back to a plain-text link - visible and copyable, just
not tappable. Tunnel the console or deploy it to get a real button.

---

## Token & cost traceability

Two layers, both on by default:

- **Run totals (always on, no setup).** The orchestrator sums every model
  call's `usage_metadata` (Gemini natively; OpenAI via the LiteLLM wrapper,
  which maps usage the same way) plus gpt-image-2 token usage from the
  Images API into session state under `token_usage` -
  `prompt_tokens / output_tokens / total_tokens / llm_calls` and
  `image_*` counters. The `[done]` event prints the full line, e.g.
  `tokens in 41,203 / out 9,882 / total 51,085 over 14 LLM call(s) +
  31,440 image tokens over 8 image call(s)`, and every image call is also
  logged individually as `[tokens] gpt-image-2 images.edit: ...`.
- **Langfuse (per-call traces + cost).** Create a free project at
  https://cloud.langfuse.com, put `LANGFUSE_PUBLIC_KEY` /
  `LANGFUSE_SECRET_KEY` in `.env`, restart - nothing else. Every run
  becomes a trace: one span per agent, one generation per LLM call with
  input/output/total tokens and cost (computed by Langfuse from the model
  id), plus a generation per gpt-image-2 call. Instrumentation lives in
  `app/observability.py` (OpenInference Google-ADK instrumentor) and is
  initialized by `app/agent.py`, the fetcher CLI, and the review API; with
  the keys unset it is a logged no-op.

## The review flow

1. After QA passes, the Review Dispatcher mails the preview (cover poster +
   slide thumbnails) with **Approve** / **Reject** links, then calls the
   `await_human_review` long-running tool - the ADK invocation ends, the
   pending function-call id is persisted in `pending_reviews`, and the run
   sleeps. It survives restarts: everything needed lives in the session DB.
2. The links open a **confirm page** first (so a mail scanner prefetching the
   GET cannot auto-approve anything).
   - **Approve** - feedback optional. Add a note if you want the Learner to
     remember something ("great, but hooks could be shorter").
   - **Reject** - feedback **compulsory**. The form asks what exactly is not
     good (first visual / texts / design / CTA / other). Be concrete: the
     text is what routing and learning run on.
3. On submit the review API builds a function response for the pending call
   and resumes the same session through `Runner.run_async` - a new invocation
   that picks the state machine up at the `review` phase.
4. On **reject**: the Learner stores the feedback, then the Feedback Router
   maps it to targets - only from {planner, first_page_visual, phrasing,
   template_design, cta} - and **only those agents re-run**, with your
   feedback injected as their highest-priority instruction. "The first visual
   is not good" → only `first_page_visual` re-runs; everything else is kept.
   If the router blames the `planner`, dependents whose inputs changed re-run
   too. Then stitch/QA re-assembles and a fresh review mail goes out.
   Rounds are capped by `MAX_REWORK_ROUNDS` (default 5).
5. On **approve**: the Learner stores any optional feedback, then publishing
   proceeds.

## Publishing

The Publisher agent turns the approved bundle into an Instagram carousel via
the Graph API (`IG_API_VERSION`): it gets signed public URLs for every
artifact from the Supabase artifact service, creates the children (the cover
as a plain VIDEO carousel item - not a Reel - then the image slides; max 10
children; the cover's 4:5 aspect ratio governs the whole carousel), creates
the CAROUSEL container, polls it until `FINISHED`, publishes, writes the
result to the run ledger, and sends you a confirmation mail with the
permalink.

## `skills/` - the editable harness

`skills/` is the system's personality, and it is meant to be edited:

- `skills/cover-style.md` - the cover composition contract (media zone, black
  grain dissolve, solid `#8FB832` highlight rules).
- `skills/design-skill.md` - the Baskaran Builds body/CTA slide system,
  including layout archetypes, the single `#8FB832` accent, exact safe areas,
  and the official website footer favicon.
- `skills/references/` - visual proofs and canonical brand assets. Footer
  furniture is composited deterministically after image generation so the
  logo, handle, arrow, and padding remain exact on every slide.
- `skills/agents/<name>.md` - one instruction file per agent, loaded fresh
  from disk at agent-build time.

The Learner agent appends distilled rules under "Learned rules" in these
files when the same feedback recurs (≥ 2 similar complaints) - this is the
mechanism by which review feedback becomes a **permanent** upgrade of the
harness rather than a one-off fix. Edit the files yourself any time; the next
run picks the changes up. Treat diffs in `skills/` as reviewable output of
the system.

---

## Honest caveats

- **Instagram prerequisites are real.** Content publishing needs an Instagram
  professional (business/creator) account linked through a Facebook app with
  approved publishing permissions and a long-lived access token, and the
  Graph API only ingests media from **publicly reachable URLs** - so
  publishing requires the Supabase artifact bucket (signed URLs); the
  in-memory artifact fallback can never publish. Carousels are capped at 10
  items and the video cover must satisfy Meta's format rules.
- **yt-dlp is fragile by nature.** Sites change, formats break, and downloads
  from cloud/datacenter IPs get throttled or blocked (YouTube especially).
  The cover build is best-effort: when no usable 4–8 s clip can be fetched it
  falls back to the update's own image as a static cover. Also remember
  sourced clips are third-party media - the rights check is on you.
- **gpt-image-2 renders text imperfectly.** Body slides are *generated
  images*; even with strict verbatim-text prompts the model can mangle words.
  Stitch & Verify QA-checks rendered text against the approved copy and
  routes failures back, but that costs regeneration rounds (real API money)
  and is not infallible - the review mail is the final gate for typos.
- **The review pause depends on shared state.** Resume only works when the
  fetcher, review API (and `adk web`, if you drive runs from it) share the
  same session database and app name - see the `--session_service_uri` /
  `APP_NAME` note above. With in-memory fallbacks a paused run dies with its
  process.
- **`adk web` is unauthenticated** and for local development only; on Windows
  the CLI may disable auto-reload (a known ADK limitation - restart it after
  code changes, or use `--reload_agents` for agent files).
- **Model ids are config switches**, not gospel: `PLANNER_MODEL`,
  `PHRASING_MODEL` (keep the `openai/` prefix for LiteLLM), `IMAGE_MODEL`
  can all be swapped in `.env` when providers move. GPT-5 text agents are
  constructed with `reasoning_effort="high"`; the default phrasing model is
  `openai/gpt-5.5` for final slide and caption writing.
