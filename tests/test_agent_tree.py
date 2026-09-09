"""The pipeline's vocabulary, pinned across all four places that hold it.

The agent and phase names live in four files that must agree, and every
disagreement fails silently rather than loudly:

* ``app/state.py`` - the constants the orchestrator routes on;
* ``app/agent.py`` - the tree actually built, whose children the orchestrator
  looks up BY NAME at runtime (``_child`` raises only when that phase runs, so
  a missing child is a crash twenty minutes into a paid run, not at startup);
* ``GET /api/meta`` - what the console is told;
* ``frontend/src/lib/pipeline.ts`` - the labels, colours and phase rail. A name
  the frontend does not know renders as a blank row or an ``undefined`` chip.

``assertNoDrift`` in pipeline.ts compares the last two, but only in a dev build
and only as a ``console.error`` nobody is watching. These tests are the part
that can actually fail a build.

Building the tree also exercises every agent builder, which is the cheapest
possible check that no agent is misconfigured - notably the ADK rule that an
agent with an ``output_schema`` may not also declare tools.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

# Keep the suite hermetic. Building the tree initialises tracing, and with
# real LANGFUSE_* keys in the environment that ships spans to a remote
# collector and blocks the run on its export timeout.
os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
os.environ.pop("LANGFUSE_SECRET_KEY", None)

from app.agent import build_root_agent  # noqa: E402
from app.orchestrator import GENERATE_ORDER, ORCHESTRATOR_NAME
from app.state import (
    AGENT_FEEDBACK_ROUTER,
    AGENT_LEARNER,
    AGENT_PUBLISHER,
    AGENT_REVIEW_DISPATCHER,
    AGENT_STITCH_VERIFY,
    PHASE_DONE,
    PHASE_GENERATE,
    PHASE_PUBLISH,
    PHASE_QA,
    PHASE_REVIEW,
    PHASE_REWORK,
    REWORKABLE_AGENTS,
)

REPO = Path(__file__).resolve().parent.parent
PIPELINE_TS = REPO / "frontend" / "src" / "lib" / "pipeline.ts"

#: Every agent the orchestrator asks for by name at some point in a run.
REQUIRED_CHILDREN = set(GENERATE_ORDER) | {
    AGENT_STITCH_VERIFY,
    AGENT_REVIEW_DISPATCHER,
    AGENT_FEEDBACK_ROUTER,
    AGENT_PUBLISHER,
    AGENT_LEARNER,
}

ALL_PHASES = {
    PHASE_GENERATE,
    PHASE_QA,
    PHASE_REVIEW,
    PHASE_REWORK,
    PHASE_PUBLISH,
    PHASE_DONE,
}


def _ts_string_array(source: str, name: str) -> list[str]:
    """Pull a ``const NAME[: T] = [ "a", "b" ]`` array out of the TypeScript."""
    match = re.search(rf"const {name}\s*(?::[^=]+)?=\s*\[(.*?)\]", source, re.DOTALL)
    if match is None:
        raise AssertionError(f"{name} not found in {PIPELINE_TS}")
    return re.findall(r'"([^"]+)"', match.group(1))


def _ts_record_keys(source: str, name: str) -> set[str]:
    """Pull the keys of a ``const NAME: Record<...> = { a: ..., }`` literal."""
    match = re.search(rf"const {name}[^=]*=\s*\{{(.*?)\n\}}", source, re.DOTALL)
    if match is None:
        raise AssertionError(f"{name} not found in {PIPELINE_TS}")
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1), re.M))


def _route_source(path: Path, depth: int = 1) -> str:
    """A screen's source, plus the source of the `@/` modules it imports.

    Components get extracted as a screen grows - the task page's header moved
    into its own file - and a test that greps a single route calls that a
    regression when nothing regressed. Following the imports asks the question
    that actually matters: is this still reachable from this screen?
    """
    text = path.read_text(encoding="utf-8")
    if depth <= 0:
        return text
    src = REPO / "frontend" / "src"
    for spec in re.findall(r'from "@/([^"]+)"', text):
        for suffix in (".tsx", ".ts"):
            child = src / f"{spec}{suffix}"
            if child.exists():
                text += "\n" + _route_source(child, depth - 1)
                break
    return text


class AgentTreeTests(unittest.TestCase):
    """The tree the orchestrator drives must contain everything it looks up."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = build_root_agent()
        cls.children = {child.name: child for child in cls.root.sub_agents}

    def test_the_root_is_the_orchestrator(self) -> None:
        self.assertEqual(self.root.name, ORCHESTRATOR_NAME)

    def test_every_agent_the_orchestrator_drives_is_in_the_tree(self) -> None:
        missing = sorted(REQUIRED_CHILDREN - set(self.children))
        self.assertEqual(
            missing,
            [],
            f"CarouselOrchestrator._child({missing!r}) raises ValueError only "
            "when that phase is reached, so a missing child surfaces as a "
            "crash part-way through a paid run rather than at startup.",
        )

    def test_every_reworkable_agent_can_actually_be_re_run(self) -> None:
        missing = sorted(set(REWORKABLE_AGENTS) - set(self.children))
        self.assertEqual(
            missing,
            [],
            "feedback_router may target these names, and the orchestrator "
            "then looks each one up in the tree.",
        )

    def test_no_agent_declares_both_an_output_schema_and_tools(self) -> None:
        conflicts = [
            name
            for name, child in self.children.items()
            if getattr(child, "output_schema", None) is not None
            and (getattr(child, "tools", None) or [])
        ]
        self.assertEqual(
            conflicts,
            [],
            "ADK refuses to call tools on an agent with an output_schema; "
            "such an agent silently produces no tool effects at all.",
        )

    def test_pipeline_nodes_never_transfer_control(self) -> None:
        """The orchestrator drives the order; a child must not re-route it.

        google-adk 2.7 ``LlmAgent._llm_flow`` returns ``SingleFlow()`` ONLY
        when both transfer flags are set and the agent has no sub_agents;
        otherwise it returns ``AutoFlow()``, which hands the model a
        ``transfer_to_agent`` tool naming every peer in the tree.

        Nine of the eleven pipeline agents set both flags. Any that does not
        can hand its turn to another agent mid-phase, and
        ``CarouselOrchestrator._drive`` only re-yields whatever events come
        back - it never checks the author - so the phase loop records the
        stage as done and moves on with the state key that stage was supposed
        to write left unwritten or stale.
        """
        loose = sorted(
            name
            for name, child in self.children.items()
            if not getattr(child, "disallow_transfer_to_parent", False)
            or not getattr(child, "disallow_transfer_to_peers", False)
        )
        self.assertEqual(
            loose,
            [],
            f"{loose} run on AutoFlow and can transfer control to a peer, "
            "silently skipping or repeating a pipeline stage. Set "
            "disallow_transfer_to_parent=True and "
            "disallow_transfer_to_peers=True, as every other node does.",
        )


class FrontendVocabularyTests(unittest.TestCase):
    """What the console can render must cover what the pipeline can emit."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = PIPELINE_TS.read_text(encoding="utf-8")
        cls.root = build_root_agent()

    def test_the_frontend_knows_every_agent_in_the_tree(self) -> None:
        ui_agents = set(_ts_string_array(self.source, "AGENTS"))
        backend = {child.name for child in self.root.sub_agents}
        self.assertEqual(
            sorted(backend - ui_agents),
            [],
            "an agent the UI does not know groups the trace under a raw "
            "snake_case name with no label and no blurb",
        )
        self.assertEqual(
            sorted(ui_agents - backend),
            [],
            "the UI lists an agent the pipeline no longer builds",
        )

    def test_every_agent_has_a_label_and_a_blurb(self) -> None:
        labels = _ts_record_keys(self.source, "AGENT_LABELS")
        blurbs = _ts_record_keys(self.source, "AGENT_BLURBS")
        backend = {child.name for child in self.root.sub_agents}
        self.assertEqual(sorted(backend - labels), [], "missing AGENT_LABELS entry")
        self.assertEqual(sorted(backend - blurbs), [], "missing AGENT_BLURBS entry")

    def test_the_frontend_knows_every_phase(self) -> None:
        ui_phases = set(_ts_string_array(self.source, "PHASES"))
        self.assertEqual(
            sorted(ALL_PHASES - ui_phases),
            [],
            "a phase the UI does not know renders an empty chip and puts the "
            "phase rail on index -1, so no step lights up at all",
        )

    def test_every_status_the_backend_can_record_has_a_label(self) -> None:
        from app.services import db

        statuses = {
            db.RUN_STATUS_RUNNING,
            db.RUN_STATUS_AWAITING_REVIEW,
            db.RUN_STATUS_DONE,
            db.RUN_STATUS_INTERRUPTED,
            db.RUN_STATUS_FAILED,
            db.RUN_STATUS_CANCELLED,
        }
        labels = _ts_record_keys(self.source, "STATUS_LABELS")
        tokens = _ts_record_keys(self.source, "STATUS_TOKEN")
        self.assertEqual(
            sorted(statuses - labels),
            [],
            "STATUS_LABELS[status] would be undefined, rendering an empty chip",
        )
        self.assertEqual(sorted(statuses - tokens), [], "missing STATUS_TOKEN entry")

    def test_the_reject_categories_only_name_reworkable_agents(self) -> None:
        """The reviewer's categories are a promise about what will re-run."""
        match = re.search(
            r"const REJECT_CATEGORIES\s*=\s*\[(.*?)\]\s*as const",
            self.source,
            re.DOTALL,
        )
        assert match is not None, "REJECT_CATEGORIES not found"
        named = set(re.findall(r"targets:\s*\[([^\]]*)\]", match.group(1)))
        targets = {
            t.strip().strip('"')
            for group in named
            for t in group.split(",")
            if t.strip()
        }
        unknown = sorted(targets - set(REWORKABLE_AGENTS))
        self.assertEqual(
            unknown,
            [],
            "the reject card predicts these agents will re-run, but "
            "_expand_rework_targets drops anything outside REWORKABLE_AGENTS, "
            f"so {unknown} would be shown to the reviewer and then ignored",
        )

    def test_both_run_screens_keep_polling_while_a_decision_is_pending(self) -> None:
        """A tab left open on /new must notice the publish.

        React Query re-evaluates ``refetchInterval`` only after a fetch
        settles, so returning ``false`` at ``awaiting_review`` stops the
        interval permanently - there is no further fetch to re-evaluate it.
        The same status sets ``isLive = false``, which closes the EventSource
        and disables the 3s trace poll. The page then has no polling, no
        stream and no push channel at exactly the moment the reviewer
        approves from Telegram and the pipeline does its remaining work.

        Both screens now read the run through ``useRunWorkspace``, so this
        pins two things: that the shared hook keeps polling at
        ``awaiting_review``, and that neither screen has grown its own copy of
        the options. The second half matters as much as the first - React
        Query keys its cache by key alone, so a second ``useQuery`` on
        ``["run", id]`` anywhere would silently decide the refetch behaviour
        for BOTH screens depending on which mounted last.
        """
        src = REPO / "frontend" / "src"
        hook = (src / "hooks" / "use-run-workspace.ts").read_text(encoding="utf-8")

        block = re.search(
            r"function runInterval\((.*?)\n\}", hook, re.DOTALL
        )
        assert block is not None, "runInterval was not found in the shared hook"
        self.assertIn(
            "awaiting_review",
            block.group(1),
            "The shared hook stops polling at awaiting_review, so a tab left "
            "open freezes on 'ready for review' through the approval, the "
            "rework rounds and the publish. React Query re-evaluates "
            "refetchInterval only after a fetch settles, so returning false "
            "there is permanent.",
        )

        for route in ("new-run.tsx", "run-detail.tsx"):
            text = (src / "routes" / route).read_text(encoding="utf-8")
            self.assertIn(
                "useRunWorkspace",
                text,
                f"{route} must read the run through the shared hook",
            )
            stray = re.findall(r'queryKey:\s*\["(run|artifacts|trace)",', text)
            self.assertEqual(
                stray,
                [],
                f"{route} declares its own query for {stray} - one cache key "
                "with two sets of options means the screen you visited "
                "previously decides how this one behaves.",
            )

    def test_no_status_strands_the_user_without_an_action(self) -> None:
        """Every state a task can rest in needs a way out of it.

        ``TaskActions`` gates on status: running->Stop; interrupted->Resume;
        failed->Re-run; cancelled->Resume+Re-run; done->Delete. Nothing at
        all for ``awaiting_review`` - it returns null.

        That is the status a run is stranded in when a verdict's background
        resume dies before ``restore_pending_review`` runs: the
        ``pending_reviews`` row is already deleted, so no surface can decide
        it; the phase is ``review``, which ``ACTIVE_PHASES`` excludes, so
        startup reconcile skips it. The console renders the ApprovalCard's
        "already decided" state with no buttons and no TaskActions in the
        header. The server would happily re-enter it - ``resume_run`` has no
        status guard and would mint a fresh pending row - the UI simply never
        offers the button.
        """
        actions = (
            REPO / "frontend" / "src" / "components" / "run" / "task-actions.tsx"
        ).read_text(encoding="utf-8")
        gates = re.search(
            r"const canStop.*?const canDelete[^\n]*", actions, re.DOTALL
        )
        assert gates is not None, "the status gates were not found"
        # A gate may name the status directly or through a flag declared
        # earlier (`const awaitingReview = status === "awaiting_review"`), so
        # resolve those aliases before looking.
        resolved = gates.group(0)
        for alias, literal in re.findall(
            r'const (\w+) = status === "([a-z_]+)"', actions
        ):
            resolved = resolved.replace(alias, f'"{literal}"')
        self.assertIn(
            "awaiting_review",
            resolved,
            "awaiting_review offers no Stop, Resume, Re-run or Delete, so a "
            "run whose verdict resume died is unrecoverable from the "
            "console. Add it to canResume: re-entering the review phase "
            "sends a fresh notice and writes a new pending row.",
        )

    def test_the_error_codes_the_ui_handles_are_the_ones_the_api_sends(self) -> None:
        """Branching on a code nobody emits is the same as not branching."""
        actions = (
            REPO / "frontend" / "src" / "components" / "run" / "task-actions.tsx"
        ).read_text(encoding="utf-8")
        handled = set(re.findall(r'code === "([a-z_]+)"', actions))

        backend = ""
        for name in ("routes_runs.py",):
            backend += (REPO / "web_api" / name).read_text(encoding="utf-8")
        backend += (REPO / "app" / "runs" / "service.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'"code":\s*"([a-z_]+)"', backend)) | set(
            re.findall(r'RunRefused\(\s*"([a-z_]+)"', backend)
        )

        dead = sorted(handled - emitted)
        self.assertEqual(
            dead,
            [],
            f"the UI branches on {dead}, which no endpoint emits - dead "
            "code that hides the fact that the real failure falls through "
            "to a raw message",
        )

        # The reverse: codes the API sends that reach the user as raw prose.
        actionable = {
            "too_many_active_runs",
            "daily_limit_reached",
            "not_running",
            "run_is_active",
            "not_resumable",
        }
        unhandled = sorted((emitted & actionable) - handled)
        self.assertEqual(
            unhandled,
            [],
            f"{unhandled} reach the user as the raw server message. "
            "daily_limit_reached in particular refuses Resume and Re-run "
            "for the rest of the day and deserves an explanation, not a "
            "sentence about MAX_RUNS_PER_DAY.",
        )

    def test_the_review_tab_refreshes_its_slides_after_a_rework(self) -> None:
        """A rework rewrites the bundle; the Review tab must not show round 1.

        ``review-panel.tsx`` drops to a 15-minute interval as soon as the
        artifacts query has data, on the reasoning that the only reason to
        refetch is signed-URL expiry. The other reason is a rework: the
        bundle and every slide artifact are rewritten. The push channel that
        was supposed to cover it - ``onPhase`` invalidating
        ``["artifacts", runId]`` - rides on the SSE stream, which carries
        nothing for a resumed leg because ``resume_pipeline`` never calls
        ``record_event``.
        """
        panel = (
            REPO / "frontend" / "src" / "components" / "review" / "review-panel.tsx"
        ).read_text(encoding="utf-8")
        block = re.search(
            r"refetchInterval:\s*\(query\)\s*=>\s*\{(.*?)\n\s*\},", panel, re.DOTALL
        )
        assert block is not None, "the artifacts refetchInterval was not found"
        self.assertIn(
            "rework",
            block.group(1),
            "Once loaded, the carousel is refetched every 15 minutes. Reject "
            "a task, let it rework, and the Review tab keeps showing the "
            "slides you already rejected - for up to a quarter of an hour, "
            "with Approve enabled the whole time. Key the interval on the "
            "run's phase, or invalidate on rework_round changing.",
        )

    def test_the_rework_dependency_map_matches_the_orchestrator(self) -> None:
        """The card's prediction must match what the orchestrator will do."""
        from app.orchestrator import _REWORK_DEPENDENTS

        match = re.search(
            r"const REWORK_DEPENDENTS: Record<string, string\[\]> = \{(.*?)\n\}",
            self.source,
            re.DOTALL,
        )
        assert match is not None, "REWORK_DEPENDENTS not found"
        ui_map = {
            key: re.findall(r'"([^"]+)"', value)
            for key, value in re.findall(
                r"^\s*([a-z_]+):\s*\[([^\]]*)\]", match.group(1), re.M
            )
        }
        backend_map = {k: list(v) for k, v in _REWORK_DEPENDENTS.items()}
        self.assertEqual(
            ui_map,
            backend_map,
            "predictRework() tells the reviewer which agents a rejection will "
            "re-run. When this map drifts from _REWORK_DEPENDENTS the console "
            "makes a promise the pipeline does not keep.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class WorkspaceIsOneImplementationTests(unittest.TestCase):
    """There is exactly ONE chat screen, and the task page points at it.

    The rule this protects has not changed - "open the chat for this task"
    must hand you the workspace you were working in, never a second, drifting
    copy of it. What changed is how: the task page used to EMBED the workspace
    as a Chat tab, so the same conversation existed in two places at two
    widths. It now links out to `/new?run=<id>` instead, which is the same
    guarantee enforced more cheaply - there is only one implementation because
    there is only one place it renders.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = REPO / "frontend" / "src"
        cls.workspace = (
            cls.src / "components" / "agent" / "agent-workspace.tsx"
        ).read_text(encoding="utf-8")
        cls.composer = (
            cls.src / "components" / "agent" / "agent-composer.tsx"
        ).read_text(encoding="utf-8")

    def test_the_chat_screen_renders_the_shared_workspace(self) -> None:
        text = (self.src / "routes" / "new-run.tsx").read_text(encoding="utf-8")
        self.assertIn(
            "AgentWorkspace",
            text,
            "new-run.tsx builds its own workspace instead of rendering the "
            "shared one, so the chat screen will drift from the component.",
        )

    def test_the_task_page_links_to_the_chat_rather_than_rebuilding_it(self) -> None:
        """One conversation, one place. The task page sends you there."""
        text = (self.src / "routes" / "run-detail.tsx").read_text(encoding="utf-8")
        self.assertIn(
            "chatPath",
            text,
            "run-detail.tsx no longer points at the chat screen; a second "
            "copy of the conversation is how the two drifted apart before.",
        )
        self.assertNotIn(
            "<AgentWorkspace",
            text,
            "run-detail.tsx embeds the workspace again, so the chat exists "
            "in two places at two widths.",
        )

    def test_stop_is_offered_from_the_first_frame(self) -> None:
        """A run you cannot cancel yet is a run spending money you cannot stop.

        The Stop button used to be disabled for the whole `starting` state -
        the seconds between the run being created server-side and the first
        snapshot arriving. The agents are already working by then, and that is
        precisely when someone realises they typed the wrong thing.
        """
        # The two states share one `working` flag now, which is the strongest
        # form of this guarantee: `starting` cannot be forgotten separately
        # because it is not written separately.
        self.assertIn(
            'const working = state === "running" || state === "starting"',
            self.composer,
            "starting and running no longer share one flag, so the Stop "
            "button can be enabled for one and not the other again",
        )
        block = re.search(r"\{working \? \((.*?)\) : \(", self.composer, re.DOTALL)
        assert block is not None, "the stop button branch was not found"
        self.assertNotIn(
            'state === "starting"',
            block.group(1),
            "the Stop button disables itself while the run is starting",
        )

    def test_stopping_refreshes_the_trace_too(self) -> None:
        """A stopped task must not keep pulsing in the view you stopped it from."""
        hook = (self.src / "hooks" / "use-run-workspace.ts").read_text(
            encoding="utf-8"
        )
        invalidate = re.search(r"export function invalidateRun\((.*?)\n\}", hook, re.DOTALL)
        assert invalidate is not None, "invalidateRun was not found"
        for key in ('"run"', '"trace"', '"runs"', "PULSE_KEY"):
            self.assertIn(
                key,
                invalidate.group(1),
                f"a stop leaves {key} stale, so something on screen keeps "
                "showing the task as live",
            )
        self.assertIn(
            "invalidateRun",
            self.workspace,
            "the workspace's Stop must go through invalidateRun",
        )

    def test_a_new_chat_can_be_started_without_losing_this_one(self) -> None:
        self.assertIn(
            "NewChatButton",
            self.workspace,
            "there is no way to start another carousel from the workspace",
        )
        # Follow the route's own imports rather than grepping one file. The
        # task page's header lives in its own component now, and a test that
        # reads only the route file calls that a regression when nothing
        # regressed - the button is still one click away on the same screen.
        detail = _route_source(self.src / "routes" / "run-detail.tsx")
        self.assertIn(
            "NewChatButton",
            detail,
            "the task page offers no way to start another carousel",
        )
