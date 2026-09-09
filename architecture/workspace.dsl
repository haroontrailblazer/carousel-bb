/*
 * Carousel Factory - C4 architecture (Structurizr DSL)
 *
 * A Google ADK multi-agent pipeline that turns AI/product news (new model
 * releases, Lovable/Supabase updates, ...) into human-approved, auto-published
 * Instagram carousels.
 *
 * How to render these diagrams:
 *   Option A (local):  docker run -it --rm -p 8080:8080 -v "C:\Projects\carousel\architecture:/usr/local/structurizr" structurizr/lite
 *                      then open http://localhost:8080
 *   Option B (online): paste this file into https://structurizr.com/dsl
 *
 * Views defined below:
 *   L1-Context        - system context
 *   L2-Containers     - deployable pieces
 *   L3-AgentPipeline  - the agents inside the ADK runtime
 *   HappyPath         - news item → review mail (dynamic)
 *   RejectRework      - rejection → targeted re-generation → re-review (dynamic)
 *   ApprovePublish    - approval → Instagram auto-publish (dynamic)
 *   Production        - deployment (Cloud Run + Supabase)
 */
workspace "Carousel Factory" "Google ADK multi-agent pipeline that turns AI/product news into human-approved, auto-published Instagram carousels" {

    model {

        owner = person "Content Owner / Reviewer" "Receives the review mail, approves or rejects carousels, gives feedback. Owns the Instagram, Substack and YouTube accounts."
        designer = person "Designer" "Authors the carousel design skill (design tokens, layout rules) and the slide templates."

        // ---- External systems ----
        sources = softwareSystem "Followed Channels & News Sources" "RSS feeds, YouTube channels, X posts and vendor blogs announcing model and product updates (Opus 5, Fable 5, Lovable, Supabase, ...). Also where the cover clip/image of an update is sourced from." "External"
        gmail = softwareSystem "Gmail" "Mailbox receiving update newsletters; also the sender of review and confirmation mails." "External"
        gemini = softwareSystem "Gemini API" "Google LLM endpoints (optional: used only when a model id is configured to a bare gemini-* id; the default configuration is all-OpenAI)." "External"
        openai = softwareSystem "OpenAI API" "OpenAI endpoints: LLM models used through ADK's LiteLLM wrapper (model per agent is a config switch) and gpt-image-2 for slide image generation." "External"
        instagram = softwareSystem "Instagram Graph API" "Meta endpoint used to auto-publish the approved carousel (video cover + image slides as carousel children). Constraints: max 10 children; cover video as a plain VIDEO child (not a Reel); the first item's aspect ratio governs all slides; requires an Instagram professional account." "External"
        ctaTargets = softwareSystem "Substack / YouTube" "The owner's channels that redirect-type CTA slides deep-link to." "External"

        carousel = softwareSystem "Carousel Factory" "Turns news/updates into approved, auto-published Instagram carousels via a Google ADK multi-agent pipeline with a human review loop." {

            fetcher = container "News Fetcher" "Scheduled job that pulls newsletters (Gmail API), RSS, YouTube and X updates, dedupes them, ranks 'top updates' and queues one news item per pipeline run." "Python, Cloud Run job + Cloud Scheduler"

            pipeline = container "ADK Agent Pipeline" "google-adk runtime hosting the agent pipeline, the human-review pause and the targeted rework loop." "Python, google-adk, FastAPI, Cloud Run" {
                root = component "Root Orchestrator" "Custom agent that runs the happy path in order, pauses the run while waiting for the human verdict, and triggers targeted rework after a rejection." "ADK custom BaseAgent" "Agent"
                research = component "Research Agent" "Runs FIRST: web-searches the update - official announcement, exact specs/numbers/prices with source URLs, the sharpest angle, official announcement media - and hands a verified fact brief to the planner, phrasing and cover agents. On 'facts are wrong' rejections only this agent re-runs, forcing a re-plan." "ADK LlmAgent + OpenAI Responses web_search tool" "Agent"
                planner = component "Editorial Planner Agent" "The 'main agent'. Reads the news item and the research brief and decides the carousel plan: points vs prose style, slide count, max lines per slide, hook title and a CTA-type hint." "ADK LlmAgent (OpenAI via LiteLLM), structured output" "Agent"
                visual = component "First-Page Visual Agent" "Builds ONLY the cover: finds a 4-8 s clip of the update from the source itself (announcement/event video, trimmed via yt-dlp + FFmpeg) or falls back to the update's own image, then composites the bottom black gradient and the absolute-positioned hook title. Source clips are third-party media (rights check); downloads from cloud IPs are best-effort - the image fallback covers failures." "ADK LlmAgent + yt-dlp + FFmpeg tools" "Agent"
                phrasing = component "Content Phrasing Agent" "Writes the final wording for every slide following the plan - bullet points or max-4-line prose per slide - and enforces line/character budgets." "ADK LlmAgent (OpenAI via LiteLLM)" "Agent"
                template = component "Template Design Agent" "Generates all body slides (2..n-1) with gpt-image-2, giving it the designer's template as reference image plus the phrased copy, following the design skill." "ADK LlmAgent + gpt-image-2 (OpenAI Images API)" "Agent"
                cta = component "CTA Agent" "Chooses one of three CTA types (follow-for-more / comment / redirect to Substack or YouTube) based on the content, and renders the CTA slide in the same template family." "ADK LlmAgent + gpt-image-2 (OpenAI Images API)" "Agent"
                stitch = component "Stitch & Verify Agent" "Assembles cover video + body slides + CTA slide into the carousel bundle, runs QA checks (slide order, text overflow, generated-slide text vs approved copy, brand rules, video length 4-8 s), then hands over for review." "ADK agent + FFmpeg/Pillow QA tools" "Agent"
                dispatcher = component "Review Dispatcher" "Sends the review mail (carousel preview + Approve / Reject links) to the configured reviewer addresses and parks the run as pending-review." "Gmail API tool + ADK LongRunningFunctionTool" "Agent"
                router = component "Feedback Router" "Reads rejection feedback, identifies the responsible agent(s) - e.g. 'the first visual is not good' maps to the First-Page Visual Agent - and re-runs only those, passing the feedback as prompt." "ADK LlmAgent + custom routing" "Agent"
                publisher = component "Publisher Agent" "After approval: uploads media, builds the caption, publishes the carousel through the Instagram Graph API, then sends a confirmation mail." "ADK agent + Instagram Graph API tool" "Agent"
                learner = component "Feedback Memory & Skill Updater" "Stores every piece of feedback in long-term memory and distills recurring feedback into updates of agent instructions and the design skill ('harness updates')." "Custom ADK MemoryService (Postgres/pgvector) + skill-writer tool" "Agent"
                services = component "ADK Platform Services" "Shared plumbing used by all agents: DatabaseSessionService (sessions/state), ArtifactService (media), MemoryService (custom BaseMemoryService on Postgres/pgvector - feedback memory recalled at generation time)." "google-adk services"
            }

            review = container "Review & Approval API" "Public endpoints behind the Approve/Reject links in the review mail. Approve accepts optional feedback; Reject opens a small form where feedback is compulsory and asks what exactly is not good (first visual? texts? design?). Links land on a confirm page so mail-scanner prefetch cannot auto-approve. Resumes the paused run with the verdict." "Supabase Edge Functions (or FastAPI routes on Cloud Run)"

            db = container "State & Feedback Store" "News queue, run ledger, ADK sessions/state, feedback history, skill/instruction versions." "Supabase Postgres" "Database"

            media = container "Media Store" "Source clips, cover videos, slide PNGs and final carousel bundles. Backend of the ADK ArtifactService via a custom BaseArtifactService adapter over the S3-compatible API (ADK ships only InMemory and GCS backends)." "Supabase Storage" "Database"

            skillLib = container "Skill & Template Library" "Designer's design skill, slide/CTA templates (HTML/CSS, exported from the designer's tool), per-agent instruction files. Versioned; updated from distilled feedback." "Git repo + Supabase Storage"
        }

        // ---- People ----
        designer -> skillLib "Authors design skill and slide templates in"
        owner -> review "Clicks Approve (optional feedback) or Reject (compulsory feedback) in"
        gmail -> owner "Delivers review and confirmation mails to"

        // ---- Fetch side ----
        fetcher -> gmail "Reads newsletter mails via Gmail API from"
        fetcher -> sources "Polls RSS feeds and followed channels for updates from"
        fetcher -> db "Dedupes, ranks and enqueues news items into"
        fetcher -> root "Starts a pipeline run for the next queued news item"

        // ---- Pipeline orchestration ----
        root -> research "1. Asks to research the update: verified facts + official media"
        research -> planner "Hands the verified fact brief over to"
        root -> planner "2. Asks to classify the news and plan the carousel"
        root -> visual "3. Asks to build the cover (only page 1)"
        root -> phrasing "4. Asks to write per-slide copy"
        root -> template "5. Asks to render the body slides"
        root -> cta "6. Asks to pick the CTA type and render the CTA slide"
        root -> stitch "7. Asks to assemble and QA the carousel"
        stitch -> dispatcher "Hands the verified bundle for human review to"
        root -> router "On rejection: hands the feedback for targeted rework to"
        root -> learner "Sends every feedback (optional or compulsory) for storage and learning to"
        root -> publisher "On approval: hands the approved bundle for publishing to"

        router -> visual "Re-runs with feedback as prompt when the cover is at fault"
        router -> phrasing "Re-runs when the wording is at fault"
        router -> template "Re-runs when the slide design is at fault"
        router -> cta "Re-runs when the CTA is at fault"
        router -> planner "Re-runs when the carousel structure/classification is at fault"
        router -> research "Re-runs when the facts are at fault (forces a re-plan)"

        // ---- LLM and external calls (all-OpenAI in the default config) ----
        research -> openai "LLM inference + live web search (Responses API web_search)"
        planner -> openai "LLM inference via LiteLLM"
        visual -> openai "LLM inference via LiteLLM (clip selection, title layout)"
        phrasing -> openai "LLM inference via LiteLLM"
        template -> openai "Generates body slide images (gpt-image-2)"
        cta -> openai "Generates the CTA slide image (gpt-image-2)"
        router -> openai "LLM inference (feedback classification)"
        learner -> openai "LLM inference (feedback distillation)"

        research -> sources "Searches announcements, docs and coverage for facts + official media from"
        visual -> sources "Downloads the update's own announcement clip/image from"
        dispatcher -> gmail "Sends the review mail (preview + Approve/Reject links) via"
        publisher -> instagram "Uploads media and publishes the approved carousel via"
        publisher -> gmail "Sends the publish-confirmation mail via"
        cta -> ctaTargets "Generates redirect deep-links to"

        // ---- State, media and skills ----
        services -> db "Persists sessions, state and the run ledger in"
        services -> media "Stores and serves media artifacts in"
        research -> services "Writes the research brief to session state via"
        planner -> services "Reads/writes session state and recalls past feedback memory via"
        visual -> services "Stores the cover video via"
        phrasing -> services "Reads the plan / writes copy via"
        template -> services "Stores slide images via"
        cta -> services "Stores the CTA slide via"
        stitch -> services "Reads all assets / stores the final bundle via"
        publisher -> services "Reads the approved bundle via"
        learner -> db "Writes feedback history and memory records into"
        learner -> skillLib "Commits instruction / design-skill updates into"
        template -> skillLib "Loads templates and the design skill from"
        cta -> skillLib "Loads CTA templates from"
        visual -> skillLib "Loads brand fonts / title style from"
        phrasing -> skillLib "Loads tone-of-voice rules from"

        review -> db "Records the verdict and feedback in"
        review -> root "Resumes the paused run with verdict + feedback"

        // ---- Deployment ----
        production = deploymentEnvironment "Production" {
            deploymentNode "Google Cloud" "" "GCP project" {
                scheduler = infrastructureNode "Cloud Scheduler" "Cron: triggers fetch runs"
                deploymentNode "Cloud Run" "" "Serverless containers" {
                    fetcherInstance = containerInstance fetcher
                    containerInstance pipeline
                }
            }
            deploymentNode "Supabase" "" "Managed backend platform" {
                deploymentNode "Edge Functions" "" "Deno" {
                    containerInstance review
                }
                deploymentNode "Postgres" "" "Managed Postgres" {
                    containerInstance db
                }
                deploymentNode "Storage" "" "S3-compatible object storage" {
                    containerInstance media
                }
            }
            deploymentNode "GitHub" "" "Version control" {
                containerInstance skillLib
            }
            scheduler -> fetcherInstance "Fires scheduled fetch"
        }
    }

    views {

        systemContext carousel "L1-Context" "Who and what the Carousel Factory talks to." {
            include *
            autoLayout lr
        }

        container carousel "L2-Containers" "Deployable/runtime pieces of the Carousel Factory." {
            include *
            autoLayout lr
        }

        component pipeline "L3-AgentPipeline" "The agents plus orchestration, routing and learning components inside the ADK runtime." {
            include *
            autoLayout lr
        }

        dynamic pipeline "HappyPath" "News item comes in, carousel is built, review mail goes out." {
            fetcher -> root "Starts run with the queued news item"
            root -> research "Research first: verified facts, exact numbers, official media"
            root -> planner "Classify: points vs content, lines per slide, hook title"
            root -> visual "Build cover: 4-8 s source clip + gradient + title"
            visual -> sources "Fetch the update's own clip/image"
            root -> phrasing "Write per-slide copy inside the line budget"
            root -> template "Render body slides from the design skill"
            root -> cta "Pick CTA type; render the CTA slide"
            root -> stitch "Assemble + QA"
            stitch -> dispatcher "Ready for review"
            dispatcher -> gmail "Send review mail"
            gmail -> owner "Deliver Approve/Reject mail"
            autoLayout lr
        }

        dynamic pipeline "RejectRework" "Rejection with compulsory feedback: only the faulty agent re-runs, then re-review." {
            owner -> review "Reject + compulsory feedback ('the first visual is not good')"
            review -> db "Store the feedback"
            review -> root "Resume run: rejected + feedback"
            root -> learner "Record feedback in memory; update skills if recurring"
            root -> router "Route feedback to the responsible agent"
            router -> visual "Re-generate only the cover, feedback as prompt"
            root -> stitch "Re-stitch with the replaced cover"
            stitch -> dispatcher "Ready for re-review"
            dispatcher -> gmail "Send updated review mail"
            autoLayout lr
        }

        dynamic pipeline "ApprovePublish" "Approval (+ optional feedback) leads to Instagram auto-publish." {
            owner -> review "Approve (+ optional feedback)"
            review -> db "Store the verdict (and feedback if given)"
            review -> root "Resume run: approved"
            root -> learner "Store the optional feedback"
            root -> publisher "Publish the approved carousel"
            publisher -> instagram "Create carousel container + publish"
            publisher -> gmail "Send confirmation mail"
            autoLayout lr
        }

        deployment carousel production "Production" "Where each container runs." {
            include *
            autoLayout lr
        }

        styles {
            element "Person" {
                shape person
                background #08427b
                color #ffffff
            }
            element "Software System" {
                background #1168bd
                color #ffffff
            }
            element "External" {
                background #8c8c8c
                color #ffffff
            }
            element "Container" {
                background #438dd5
                color #ffffff
            }
            element "Component" {
                background #85bbf0
                color #000000
            }
            element "Agent" {
                shape hexagon
            }
            element "Database" {
                shape cylinder
            }
        }
    }
}
