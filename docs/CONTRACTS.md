# Carousel Factory - Build Contracts

The binding spec for every file in this repo. The C4 model
(`architecture/carousel.c4`) is the architecture authority; this file pins the
code-level contracts. If a task conflicts with this file, this file wins.

## Global constraints

- Python 3.11+ (dev venv: `.venv`, Python 3.13). Windows-friendly paths
  (always `pathlib`, never hard-coded `/tmp` - use `settings.workdir`).
- NO git operations anywhere. Never commit, never push.
- Secrets ONLY via `app.config.settings` (env-driven). Never hard-code keys.
- Every agent lives in its own file under `app/agents/`, exposing a builder
  function `build_<name>_agent() -> BaseAgent`-compatible object.
- Agent display/routing names come from `app/state.py` constants - never
  free-typed strings.
- All session-state access uses the `K_*` keys and `get_model`/`set_model`
  helpers from `app/state.py`. State values must stay JSON-serializable.
- Instruction text for each LlmAgent loads from `skills/agents/<name>.md` via
  `app.config.agent_instructions(name)`, with a sensible inline fallback
  string if the file is missing. (The Learner agent edits those files - this
  is how feedback permanently updates the harness.)
- Type hints + docstrings everywhere. No `TODO`-only stubs: every function is
  implemented. External calls get explicit timeouts and raise-for-status.

## ADK API cheatsheet (VERIFY against `.venv/Lib/site-packages/google/adk/`)

The installed `google-adk` package is the ground truth. Before using any ADK
symbol, confirm it exists by reading the installed source. Expected surface:

```python
from google.adk.agents import LlmAgent, BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.models.lite_llm import LiteLlm            # OpenAI models
from google.adk.tools import FunctionTool, LongRunningFunctionTool, ToolContext
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.artifacts import BaseArtifactService
from google.adk.memory import BaseMemoryService
from google.genai import types  # Content/Part for messages & function responses
```

- `LlmAgent(name=..., model="gemini-2.5-pro" | LiteLlm(model="openai/gpt-5"),
  instruction=..., tools=[...], output_schema=PydanticModel, output_key="...")`
  - `output_key` writes the final response into `session.state[output_key]`.
- Custom orchestrator: subclass `BaseAgent`, implement
  `async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]`
  and drive children with `async for event in child.run_async(ctx): yield event`.
  Declare children in `sub_agents=[...]` so `adk web` renders the agent graph.
- Tools are plain functions (type-hinted, docstring) wrapped in `FunctionTool`;
  they may accept `tool_context: ToolContext` to touch `tool_context.state`
  and `await tool_context.save_artifact(filename, types.Part(...))`.
- If a signature differs in the installed version, FOLLOW THE INSTALLED
  VERSION and note the difference in your report.

## The orchestrator state machine (app/orchestrator.py)

The review pause ends the invocation; the resume is a NEW invocation. The root
agent is therefore a re-entrant state machine over `state[K_PHASE]`:

| phase      | action                                                                            | next |
|------------|-----------------------------------------------------------------------------------|------|
| (missing)  | init run: set K_RUN_ID, K_REWORK_ROUND=0, K_REVIEW_ROUND=0                        | generate |
| generate   | research → planner → first_page_visual → phrasing → template_design → cta          | qa |
| qa         | stitch_verify (assembles Bundle + QAReport)                                        | review |
| review     | review_dispatcher: sends mail, calls `await_human_review` (LongRunningFunctionTool) - invocation PAUSES here. On resume the tool response carries the verdict; dispatcher writes K_VERDICT. approved → publish; rejected → rework | publish / rework |
| rework     | learner (store feedback) → feedback_router (writes K_REWORK_PLAN) → re-run ONLY the target agents (subset of REWORKABLE_AGENTS), passing K_REWORK_FEEDBACK; increment K_REWORK_ROUND (cap: settings.max_rework_rounds) | qa |
| publish    | learner (store optional feedback) → publisher (IG publish + confirmation mail)     | done |
| done       | emit final summary event, stop                                                     | - |

Rework targeting: `"the first visual is not good"` → only `first_page_visual`
re-runs; then qa → review again with the replaced piece. If the router targets
`planner`, downstream agents whose inputs changed re-run too (planner implies
full regenerate of dependents; `research` implies planner and therefore a full
regenerate on the corrected facts) - the router's `reasons` say why.

## Review resume protocol (web_api/routes_runs.py ↔ dispatcher)

1. Dispatcher tool `send_review_message(...)` posts the preview album to
   Telegram with one button: `{PUBLIC_BASE_URL}/tasks/{run_id}?tab=review`,
   the console's own review screen. There is no anonymous approval surface -
   the standalone `/review-api` Approve/Reject pages were deleted, because a
   URL that publishes to Instagram with no sign-in is a permanent, forwardable
   publish button. The verdict is submitted by `POST /api/runs/{id}/verdict`,
   which requires a session and records who decided.
2. Dispatcher then calls `await_human_review()` - a `LongRunningFunctionTool`
   returning no immediate result → ADK pauses; runner invocation ends with the
   pending function_call id (persist it in state via the tool callback or db).
3. Review API endpoints render a CONFIRM page (mail scanners prefetch GETs!).
   Approve: optional feedback textarea. Reject: feedback REQUIRED, form asks
   "what exactly is not good? (first visual / texts / design / CTA / other)".
4. On submit the API loads the session (same DatabaseSessionService database),
   builds `types.Content` with a `types.Part(function_response=...)` matching
   the pending call id/name, payload `{"status": "approved"|"rejected",
   "feedback": "..."}`, and calls `Runner.run_async` on the same
   app_name/user_id/session_id to resume the pipeline.

## File map & responsibilities

| file | must expose |
|---|---|
| app/tools/media_tools.py | `find_source_clip(news: dict, search_query="") -> dict` scans media_urls, including page-form media candidates, plus the source page and BODY-linked pages. It runs one live trend-aware visual search and ranks candidates by topicality, prominence, trusted-source affinity, and generic or unverified-blog penalties. URL-only news titles are converted into a readable subject query. Every result carries the best `image_url`, up to five `image_candidates`, and `trend_search` status. `download_image` rejects tiny/extreme-aspect assets so the agent tries the next ranked result. `download_and_trim(url, max_s=None, min_s=None) -> str(path)` defaults to the 4-15 s cover window. `placeholder_background(workdir) -> str(path)` is the non-AI last resort. `compose_cover(media_path, title, highlight, is_video: bool)` composites the overlay plus a fixed 128 px, up-to-three-line cover headline with solid `#8FB832` emphasis, then outputs 1080x1350 mp4 plus poster. ffmpeg runs via `settings.ffmpeg_bin`; yt-dlp uses its Python API. |
| app/tools/image_gen.py | `generate_slide_image(template_ref: str, copy_lines: list[str], headline: str, slide_no: int, out_path: str, layout_hint="editorial explainer", visual_context="", visual_reference="") -> str` uses `settings.image_model` to generate a text-free exact-aspect lower visual on the first `_GEN_SIZES` rung the API accepts (all exactly 2:1 and divisible by 16; the ladder exists because the API enforces a moving minimum pixel budget that retired the original 1088x544), then merges the complete artwork into the slide's 1080x540 slot (`y=620..1160`) without stretching or cropping. Unexpected source ratios and sourced subject images use aspect-preserving containment rather than cover-cropping. Pillow then adds preferred 76 px headline and 36 px body typography, stepping down only within the readable 60 px/30 px limits when approved copy needs more room. It also applies one coherent top background, a body-only counter beginning at `01`, and the official Baskaran Builds favicon/handle/arrow rail. The cover poster/video and `generate_cta_image(...)` are unnumbered. |
| app/tools/gmail_tools.py | `send_review_email(run_id, bundle: dict, round_no: int) -> dict`, `send_confirmation_email(run_id, ig_permalink) -> dict` - Gmail API (OAuth files from settings), HTML body, inline poster preview + slide thumbnails, Approve/Reject links. |
| app/tools/instagram_tools.py | `publish_carousel(bundle: dict, public_urls: list[str]) -> dict{media_id, permalink}` - Graph API `settings.ig_api_version`: children (VIDEO cover `is_carousel_item`, image children), CAROUSEL container, `media_publish`; enforce <= settings.max_carousel_slides children; poll container status until FINISHED. |
| app/services/artifact_service.py | `SupabaseArtifactService(BaseArtifactService)` - implements the full BaseArtifactService interface (match installed ABC exactly) over S3-compatible Supabase Storage (boto3, settings.s3_*). Keys: `{app_name}/{user_id}/{session_id}/{filename}` + versioning per ABC. Plus `public_url(filename)->str` helper (signed URL) used by publisher/mail. |
| app/services/memory_service.py | `PostgresMemoryService(BaseMemoryService)` (match installed ABC) storing/searching feedback + run summaries in Postgres (asyncpg); simple keyword search is fine. Plus `store_feedback(record: FeedbackRecord)` and `recent_feedback(limit=20) -> list[FeedbackRecord]`. |
| app/services/db.py | asyncpg pool helpers + `db/schema.sql` (news_queue, runs, feedback, pending_reviews tables); `enqueue_news`, `next_queued_news`, `mark_news_done`, `create_run`, `update_run_phase`, `save_pending_review(run_id, session_id, function_call_id)`, `load_pending_review(run_id)`, `record_verdict`. |
| app/tools/research_tools.py | `search_web(query) -> dict{status, answer, sources}` - OpenAI Responses API `web_search` tool on the utility model's bare id; `save_research_brief(summary, key_facts, suggested_angle, media_candidates, sources, tool_context) -> dict` - validates ResearchBrief, writes K_RESEARCH, merges media_candidates into news_item.media_urls (cap 8) for the cover agent. |
| app/agents/research.py | `build_research_agent()` - LlmAgent, model settings.planner_model, tools search_web + save_research_brief; runs FIRST in generate; 2-5 focused searches, verified facts only; planner/phrasing consume the brief via `{research_brief?}` templating. |
| app/agents/planner.py | `build_planner_agent()` - LlmAgent, model settings.planner_model, output_schema CarouselPlan, output_key K_PLAN; instruction covers: points vs prose, slide budget (cover+body+CTA <= 10), <=4 lines/slide, hook per skills/cover-style.md, uses recent feedback memory + the research brief (`{research_brief?}`). |
| app/agents/first_page_visual.py | `build_first_page_visual_agent()` - LlmAgent + media tools; picks best media_url (or searches source page), builds CoverSpec via compose_cover, saves artifacts, writes K_COVER. Honors K_REWORK_FEEDBACK. |
| app/agents/phrasing.py | `build_phrasing_agent()` - LlmAgent LiteLlm(settings.phrasing_model), output_schema CopySet, output_key K_COPY; enforces plan style, line budget, plain understandable wording, and the repository-wide prohibition on em dashes. Audience-facing Pydantic schemas reject forbidden characters, broken encoding, placeholders, and conservative nonsense patterns before rendering or publishing. |
| app/agents/template_design.py | `build_template_design_agent()` - LlmAgent + image_gen tool; passes the news item, research brief, and slide purpose into each visual prompt. Only subject-introduction slides receive an aspect-preserved sourced cover-media image for deterministic lower-zone compositing. Saves one PNG per body slide and writes K_BODY_SLIDES. |
| app/agents/cta.py | `build_cta_agent()` - picks CTA type (plan.cta_hint + content), renders CTA slide via image_gen, writes K_CTA_SLIDE with link_url from settings. |
| app/agents/stitch_verify.py | `build_stitch_verify_agent()` - assembles Bundle (ordered artifacts: cover video first), QA checks (count <= 10, durations 4-15 s, copy-vs-rendered text via LLM vision or size heuristics, line budgets, fixed body-slide-number geometry, exact body/CTA footer furniture and safe-area padding), writes K_BUNDLE + K_QA_REPORT; critical issues → auto-route back (set K_REWORK_PLAN) without mailing. |
| app/agents/review_dispatcher.py | `build_review_dispatcher_agent()` - sends review mail (gmail_tools), then `await_human_review` LongRunningFunctionTool; on resume writes K_VERDICT from the tool response; persists pending call id via db.save_pending_review. |
| app/agents/feedback_router.py | `build_feedback_router_agent()` - LlmAgent (utility model), output_schema ReworkPlan, output_key K_REWORK_PLAN; maps feedback text → targets from REWORKABLE_AGENTS only. |
| app/agents/publisher.py | `build_publisher_agent()` - public URLs from artifact service, publish_carousel, confirmation mail, writes result to state + runs table. |
| app/agents/learner.py | `build_learner_agent()` - stores FeedbackRecord (memory_service), and when a rule repeats (>=2 similar feedbacks) appends a distilled rule to the relevant skills/agents/<name>.md or skills/design-skill.md under "Learned rules". |
| app/orchestrator.py | `CarouselOrchestrator(BaseAgent)` per the state machine above; children passed via sub_agents so adk web draws the graph; emits one concise Event per phase transition for realtime visibility. |
| app/agent.py | builds everything, exposes `root_agent` (module-level) for `adk web`/`adk run`; also `build_runner()` returning a Runner wired to DatabaseSessionService(settings.database_url), SupabaseArtifactService, PostgresMemoryService (with in-memory fallbacks when env is missing so `adk web` works locally). |
| fetcher/fetch_news.py | pulls Gmail newsletters (query settings.newsletter_query), RSS (feedparser), YouTube channel feeds; dedupe by URL hash into news_queue; `python -m fetcher.fetch_news --run-one` pops one item and starts a pipeline run via build_runner(). |
| README.md | setup (venv, ffmpeg, .env), Supabase schema apply, `adk web` for the REALTIME AGENT GRAPH + event/state inspector (this is the "agent graph, loop visuals, realtime operation" surface), running fetcher/review API, the rework loop explained, NO git commits until review. |

## Package init files

`app/__init__.py`, `app/agents/__init__.py`, `app/tools/__init__.py`,
`app/services/__init__.py` exist and stay EMPTY (docstring only) - no
re-exports, to keep imports acyclic. Import submodules directly
(`from app.tools import media_tools`).
