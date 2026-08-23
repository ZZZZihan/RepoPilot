"""Deep planning module: inspect, derive evidence, validate, persist, and approve."""

from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from repopilot.errors import InspectionLimitExceededError, IssueRepositoryMismatchError
from repopilot.inspection import (
    InspectedDocument,
    RepositoryInspector,
    RepositorySnapshot,
    classify_path,
)
from repopilot.models import (
    ApprovePlanRequest,
    CreatePlanRequest,
    EvidenceCategory,
    EvidenceItem,
    FileAction,
    FileReference,
    ImplementationPlan,
    InspectionSummary,
    IssueInput,
    PlanStatus,
    PlanStep,
    StepKind,
    VerificationIntent,
    parse_github_issue_url,
)
from repopilot.storage import SQLitePlanStore, utc_now

_NON_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
_TEXT_TOKEN = re.compile(r"[a-z][a-z0-9]*")
_SYMBOL_REFERENCE = re.compile(r"\b([a-z_][a-z0-9_]*)\s*\(", re.IGNORECASE)
_PYTHON_DEFINITION = re.compile(
    r"^[ \t]*(?:async[ \t]+def|def|class)[ \t]+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.MULTILINE,
)
_FILE_REFERENCE = re.compile(
    r"(?<![a-z0-9_.-])(?:[a-z0-9_.-]+/)*[a-z0-9_.-]+\.(?:cfg|ini|md|py|toml|yaml|yml)"
    r"(?![a-z0-9_.-])",
    re.IGNORECASE,
)
_ISSUE_STOP_WORDS = {
    "add",
    "behavior",
    "change",
    "clear",
    "error",
    "exact",
    "from",
    "give",
    "into",
    "keep",
    "message",
    "preserve",
    "raise",
    "regression",
    "return",
    "should",
    "test",
    "tests",
    "that",
    "this",
    "update",
    "when",
    "with",
}


class PlanBuilder:
    """Turn a bounded snapshot into one deterministic, schema-validated plan."""

    def build(self, snapshot: RepositorySnapshot, issue: IssueInput) -> ImplementationPlan:
        self._require_evidenced_planning_categories(snapshot)
        issue_text = f"{issue.title}\n{issue.body}".lower()
        evidence = self._build_evidence(snapshot.documents, issue_text)
        evidence_by_path = {item.path: item for item in evidence}
        issue_delta = self._issue_delta(issue)

        ranked_source_documents = self._rank_documents(
            snapshot.documents, issue_text, EvidenceCategory.SOURCE
        )
        ranked_test_documents = self._rank_documents(
            snapshot.documents, issue_text, EvidenceCategory.TEST
        )
        observed_source_documents = [
            document
            for document in snapshot.documents
            if document.category is EvidenceCategory.SOURCE
        ]
        observed_test_documents = [
            document
            for document in snapshot.documents
            if document.category is EvidenceCategory.TEST
        ]
        source_low_confidence = not ranked_source_documents and bool(observed_source_documents)
        test_low_confidence = not ranked_test_documents and bool(observed_test_documents)
        source_documents = ranked_source_documents or self._fallback_documents(
            observed_source_documents, EvidenceCategory.SOURCE
        )
        test_documents = ranked_test_documents or self._fallback_documents(
            observed_test_documents, EvidenceCategory.TEST
        )
        config_documents = [
            document
            for document in snapshot.documents
            if document.category in {EvidenceCategory.PROJECT_CONFIG, EvidenceCategory.TEST_CONFIG}
        ]
        readme_documents = self._rank_documents(
            snapshot.documents, issue_text, EvidenceCategory.README
        )

        fallback_document = (
            source_documents
            + test_documents
            + readme_documents
            + config_documents
            + list(snapshot.documents)
        )[0]
        analysis_documents: list[InspectedDocument] = []
        for candidates in (source_documents, test_documents, readme_documents):
            if candidates and candidates[0].path not in {
                document.path for document in analysis_documents
            }:
                analysis_documents.append(candidates[0])
        if not analysis_documents:
            analysis_documents = (config_documents or list(snapshot.documents))[:2]
        implementation_references = self._implementation_references(
            snapshot,
            source_documents,
            evidence_by_path,
            fallback_document,
            low_confidence=source_low_confidence,
        )
        test_references = self._test_references(
            snapshot,
            test_documents,
            config_documents,
            evidence_by_path,
            fallback_document,
            low_confidence=test_low_confidence,
        )
        verification_documents = (config_documents or test_documents or readme_documents)[:3]
        if not verification_documents:
            verification_documents = [fallback_document]

        steps = [
            PlanStep(
                sequence=1,
                kind=StepKind.ANALYSIS,
                title="Confirm the affected behavior against repository evidence",
                description=(
                    f"Read the observed implementation and project contract before changing "
                    f"behavior for: {issue.title}. Keep the change pinned to the inspected tree."
                ),
                file_references=[
                    self._reference(
                        document,
                        evidence_by_path,
                        action=FileAction.INSPECT,
                        reason=(
                            "Establishes the current behavior or project contract for this issue."
                        ),
                    )
                    for document in analysis_documents
                ],
            ),
            PlanStep(
                sequence=2,
                kind=StepKind.IMPLEMENTATION,
                title="Implement the smallest repository-local change",
                description=(
                    f"Apply the requested delta: {issue_delta} Change only the selected Python "
                    "implementation path, preserving existing public behavior not named by the "
                    "Issue."
                ),
                file_references=implementation_references,
            ),
            PlanStep(
                sequence=3,
                kind=StepKind.TEST,
                title="Add regression coverage for the Issue behavior",
                description=(
                    f"Encode the requested delta: {issue_delta} Assert the observable behavior "
                    "and at least one relevant negative or edge case in the observed test layout."
                ),
                file_references=test_references,
            ),
            PlanStep(
                sequence=4,
                kind=StepKind.VERIFICATION,
                title="Verify against the repository's declared tooling",
                description=(
                    "Run the structured verification intents in a future isolated execution "
                    "stage. This planning slice records them but never executes commands."
                ),
                file_references=[
                    self._reference(
                        document,
                        evidence_by_path,
                        action=FileAction.VERIFY,
                        reason=(
                            "Declares or demonstrates the repository's current verification "
                            "workflow."
                        ),
                    )
                    for document in verification_documents
                ],
            ),
        ]

        verification_intents = self._verification_intents(snapshot.documents, evidence_by_path)
        risks = self._risks(
            snapshot,
            low_confidence_source_path=(
                source_documents[0].path if source_low_confidence else None
            ),
            low_confidence_test_path=(test_documents[0].path if test_low_confidence else None),
        )
        issue_label = f"Issue #{issue.number}" if issue.number is not None else "the supplied Issue"
        created_at = utc_now()
        plan = ImplementationPlan(
            plan_id=uuid4(),
            status=PlanStatus.PROPOSED,
            version=1,
            repository=snapshot.repository,
            issue=issue,
            summary=(
                f"Implement {issue_label} ({issue.title}) in {snapshot.repository.owner}/"
                f"{snapshot.repository.name} using evidence from tree "
                f"{snapshot.repository.tree_sha[:12]}."
            ),
            inspection=InspectionSummary(
                files_seen=len(snapshot.all_paths),
                documents_read=len(snapshot.documents),
                selection_truncated=snapshot.selection_truncated,
                max_tree_entries=snapshot.limits.max_tree_entries,
                max_selected_files=snapshot.limits.max_selected_files,
                max_file_bytes=snapshot.limits.max_file_bytes,
                max_total_bytes=snapshot.limits.max_total_bytes,
            ),
            evidence=evidence,
            steps=steps,
            verification_intents=verification_intents,
            assumptions=[
                (
                    "The supplied Issue title and body are input; RepoPilot did not fetch or "
                    "authenticate the Issue."
                ),
                (
                    "The plan is valid for the recorded repository tree and must be regenerated "
                    "after material changes."
                ),
                (
                    "Approval records reviewer intent only; it does not execute code or modify "
                    "the repository."
                ),
            ],
            risks=risks,
            out_of_scope=[
                "Applying patches or executing an implementation plan",
                "Arbitrary host shell or subprocess access",
                "Docker or other sandbox provisioning",
                "Creating branches, commits, pull requests, or releases",
            ],
            created_at=created_at,
            approval=None,
        )
        # Force a complete schema round trip at construction, not only at HTTP serialization.
        return ImplementationPlan.model_validate_json(plan.model_dump_json())

    @staticmethod
    def _require_evidenced_planning_categories(snapshot: RepositorySnapshot) -> None:
        """Fail closed when the tree has code or tests that inspection did not read."""

        tree_categories = {
            category for path in snapshot.all_paths if (category := classify_path(path)) is not None
        }
        evidenced_categories = {document.category for document in snapshot.documents}
        missing_evidence = [
            category
            for category in (EvidenceCategory.SOURCE, EvidenceCategory.TEST)
            if category in tree_categories and category not in evidenced_categories
        ]
        if not missing_evidence:
            return

        category_names = " and ".join(category.value for category in missing_evidence)
        raise InspectionLimitExceededError(
            f"repository tree contains {category_names} paths, but bounded inspection did not "
            "capture corresponding evidence; increase inspection limits or narrow the "
            "repository before planning"
        )

    @classmethod
    def _build_evidence(
        cls, documents: tuple[InspectedDocument, ...], issue_text: str
    ) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        observations = {
            EvidenceCategory.README: "README records the repository's stated purpose or usage.",
            EvidenceCategory.PROJECT_CONFIG: (
                "Project configuration records Python packaging or dependency conventions."
            ),
            EvidenceCategory.TEST_CONFIG: (
                "Test or CI configuration records an existing verification convention."
            ),
            EvidenceCategory.TEST: "Existing test code demonstrates the repository's test layout.",
            EvidenceCategory.SOURCE: (
                "Python source demonstrates the repository's implementation layout."
            ),
        }
        for index, document in enumerate(documents, start=1):
            line_start, line_end = cls._evidence_line_window(document, issue_text)
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    path=document.path,
                    category=document.category,
                    line_start=line_start,
                    line_end=line_end,
                    sha256=document.sha256,
                    observation=observations[document.category],
                )
            )
        return evidence

    @classmethod
    def _evidence_line_window(cls, document: InspectedDocument, issue_text: str) -> tuple[int, int]:
        lines = document.content.splitlines()
        if not lines:
            return 1, 1

        issue_signals = cls._meaningful_tokens(issue_text) | cls._symbol_references(issue_text)

        def anchor_score(line: str) -> int:
            stripped = line.strip().casefold()
            if document.category in {EvidenceCategory.SOURCE, EvidenceCategory.TEST}:
                return int(stripped.startswith(("def ", "class ")))
            return 0

        scored_lines = [
            (len(issue_signals & cls._text_tokens(line)), anchor_score(line), index)
            for index, line in enumerate(lines)
        ]
        best_score, _, best_index = max(scored_lines, key=lambda item: (item[0], item[1], -item[2]))
        if best_score:
            target_line = best_index + 1
            return max(1, target_line - 2), min(len(lines), target_line + 2)

        needles = {
            EvidenceCategory.README: ("#",),
            EvidenceCategory.PROJECT_CONFIG: ("[project]", "[tool.", "requirements"),
            EvidenceCategory.TEST_CONFIG: ("pytest", "test", "jobs:", "def "),
            EvidenceCategory.TEST: ("def test_", "class test"),
            EvidenceCategory.SOURCE: ("def ", "class "),
        }[document.category]
        start_index = next(
            (
                index
                for index, line in enumerate(lines)
                if line.strip() and any(needle in line.lower() for needle in needles)
            ),
            next((index for index, line in enumerate(lines) if line.strip()), 0),
        )
        line_start = start_index + 1
        return line_start, min(len(lines), line_start + 2)

    @classmethod
    def _rank_documents(
        cls,
        documents: tuple[InspectedDocument, ...],
        issue_text: str,
        category: EvidenceCategory,
    ) -> list[InspectedDocument]:
        matching = [document for document in documents if document.category is category]
        issue_terms = cls._meaningful_tokens(issue_text)
        issue_symbols = cls._symbol_references(issue_text)
        explicit_references = {
            reference.casefold() for reference in _FILE_REFERENCE.findall(issue_text)
        }
        explicit_paths = {reference for reference in explicit_references if "/" in reference}
        explicit_names = {PurePosixPath(reference).name for reference in explicit_references}

        def relevance(document: InspectedDocument) -> tuple[int, int, int, int, int]:
            path = document.path.casefold()
            name = PurePosixPath(path).name
            document_tokens = cls._text_tokens(f"{path}\n{document.content}")
            definition_tokens = cls._python_definition_tokens(document.content)
            return (
                int(path in explicit_paths),
                int(name in explicit_names),
                len(issue_symbols & definition_tokens),
                len(issue_symbols & document_tokens),
                len(issue_terms & document_tokens),
            )

        scored = [(document, relevance(document)) for document in matching]
        relevant = [(document, score) for document, score in scored if any(score)]

        return [
            document
            for document, _ in sorted(
                relevant,
                key=lambda item: (
                    -item[1][0],
                    -item[1][1],
                    -item[1][2],
                    -item[1][3],
                    -item[1][4],
                    item[0].path.casefold(),
                ),
            )
        ]

    @staticmethod
    def _text_tokens(value: str) -> set[str]:
        return set(_TEXT_TOKEN.findall(value.casefold().replace("_", " ")))

    @classmethod
    def _meaningful_tokens(cls, value: str) -> set[str]:
        return {
            token
            for token in cls._text_tokens(value)
            if len(token) >= 4 and token not in _ISSUE_STOP_WORDS
        }

    @staticmethod
    def _symbol_references(value: str) -> set[str]:
        return {symbol.casefold() for symbol in _SYMBOL_REFERENCE.findall(value)}

    @classmethod
    def _python_definition_tokens(cls, value: str) -> set[str]:
        tokens: set[str] = set()
        for name in _PYTHON_DEFINITION.findall(value):
            tokens.add(name.casefold())
            tokens.update(cls._text_tokens(name))
        return tokens

    @staticmethod
    def _issue_delta(issue: IssueInput) -> str:
        normalized = " ".join((issue.body or issue.title).split())
        if len(normalized) <= 900:
            return normalized
        return f"{normalized[:897].rstrip()}..."

    @staticmethod
    def _fallback_documents(
        documents: list[InspectedDocument], category: EvidenceCategory
    ) -> list[InspectedDocument]:
        """Order observed documents for a deterministic low-confidence fallback."""

        if category not in {EvidenceCategory.SOURCE, EvidenceCategory.TEST}:
            raise ValueError("fallback document ranking only supports source and test categories")

        def fallback_key(document: InspectedDocument) -> tuple[int, int, int, str]:
            path = PurePosixPath(document.path)
            parts = tuple(part.casefold() for part in path.parts)
            name = path.name.casefold()
            if category is EvidenceCategory.SOURCE:
                conventional_penalty = int(not parts or parts[0] != "src")
                shallow_file_penalty = int(name == "__init__.py")
            else:
                conventional_test = name.startswith("test_") or any(
                    part in {"test", "tests"} for part in parts[:-1]
                )
                conventional_penalty = int(not conventional_test)
                shallow_file_penalty = 0
            return conventional_penalty, shallow_file_penalty, len(parts), document.path.casefold()

        return sorted(documents, key=fallback_key)

    @staticmethod
    def _reference(
        document: InspectedDocument,
        evidence_by_path: dict[str, EvidenceItem],
        *,
        action: FileAction,
        reason: str,
    ) -> FileReference:
        return FileReference(
            path=document.path,
            action=action,
            exists=True,
            reason=reason,
            evidence_ids=[evidence_by_path[document.path].id],
        )

    def _implementation_references(
        self,
        snapshot: RepositorySnapshot,
        source_documents: list[InspectedDocument],
        evidence_by_path: dict[str, EvidenceItem],
        fallback_document: InspectedDocument,
        *,
        low_confidence: bool,
    ) -> list[FileReference]:
        if source_documents:
            document = source_documents[0]
            reason = (
                "Low-confidence deterministic fallback to an observed Python implementation "
                "path because the Issue produced no repository-specific source signal."
                if low_confidence
                else "Strongest issue-supported Python implementation path."
            )
            return [
                self._reference(
                    document,
                    evidence_by_path,
                    action=FileAction.MODIFY,
                    reason=reason,
                )
            ]
        package_name = _NON_IDENTIFIER.sub("_", snapshot.repository.name.lower()).strip("_")
        package_name = package_name or "repopilot_change"
        return [
            FileReference(
                path=f"src/{package_name}/feature.py",
                action=FileAction.CREATE,
                exists=False,
                reason=(
                    "No Python source file was observed; this inferred path follows the "
                    "configured src layout."
                ),
                evidence_ids=[evidence_by_path[fallback_document.path].id],
            )
        ]

    def _test_references(
        self,
        snapshot: RepositorySnapshot,
        test_documents: list[InspectedDocument],
        config_documents: list[InspectedDocument],
        evidence_by_path: dict[str, EvidenceItem],
        fallback_document: InspectedDocument,
        *,
        low_confidence: bool,
    ) -> list[FileReference]:
        if test_documents:
            document = test_documents[0]
            reason = (
                "Low-confidence deterministic fallback to an observed Python test path because "
                "the Issue produced no repository-specific test signal."
                if low_confidence
                else "Strongest issue-supported regression-test home."
            )
            return [
                self._reference(
                    document,
                    evidence_by_path,
                    action=FileAction.MODIFY,
                    reason=reason,
                )
            ]
        anchor = config_documents[0] if config_documents else fallback_document
        issue_slug = _NON_IDENTIFIER.sub("_", snapshot.repository.name.lower()).strip("_")
        issue_slug = issue_slug or "feature"
        return [
            FileReference(
                path=f"tests/test_{issue_slug}.py",
                action=FileAction.CREATE,
                exists=False,
                reason=(
                    "No test file was observed; create regression coverage in the conventional "
                    "tests path."
                ),
                evidence_ids=[evidence_by_path[anchor.path].id],
            )
        ]

    @staticmethod
    def _verification_intents(
        documents: tuple[InspectedDocument, ...],
        evidence_by_path: dict[str, EvidenceItem],
    ) -> list[VerificationIntent]:
        intents: list[VerificationIntent] = []

        def evidence_containing(term: str) -> EvidenceItem | None:
            document = next(
                (candidate for candidate in documents if term in candidate.content.lower()), None
            )
            if document is None:
                return None
            return evidence_by_path[document.path]

        pytest_evidence = evidence_containing("pytest")
        if pytest_evidence is None:
            test_document = next(
                (document for document in documents if document.category is EvidenceCategory.TEST),
                None,
            )
            if test_document is not None:
                pytest_evidence = evidence_by_path[test_document.path]
        if pytest_evidence is not None:
            intents.append(
                VerificationIntent(
                    tool="pytest", arguments=[], evidence_ids=[pytest_evidence.id], executed=False
                )
            )

        for tool in ("ruff", "mypy"):
            tool_evidence = evidence_containing(tool)
            if tool_evidence is not None:
                intents.append(
                    VerificationIntent(
                        tool=tool,
                        arguments=["check", "."] if tool == "ruff" else [],
                        evidence_ids=[tool_evidence.id],
                        executed=False,
                    )
                )
        return intents

    @staticmethod
    def _risks(
        snapshot: RepositorySnapshot,
        *,
        low_confidence_source_path: str | None,
        low_confidence_test_path: str | None,
    ) -> list[str]:
        categories = {document.category for document in snapshot.documents}
        risks: list[str] = []
        if EvidenceCategory.README not in categories:
            risks.append(
                "No bounded README was observed, so intended user behavior may be underspecified."
            )
        if EvidenceCategory.TEST_CONFIG not in categories:
            risks.append("No dedicated test or CI configuration was observed.")
        if EvidenceCategory.TEST not in categories:
            risks.append(
                "No existing Python test file was observed; the proposed test path is inferred."
            )
        if snapshot.selection_truncated:
            risks.append(
                "The evidence selection hit a configured bound; unselected files may affect scope."
            )
        if low_confidence_source_path is not None:
            risks.append(
                "Low-confidence source selection: the Issue produced no repository-specific "
                f"source signal, so the plan deterministically falls back to observed path "
                f"{low_confidence_source_path!r}."
            )
        if low_confidence_test_path is not None:
            risks.append(
                "Low-confidence test selection: the Issue produced no repository-specific test "
                f"signal, so the plan deterministically falls back to observed path "
                f"{low_confidence_test_path!r}."
            )
        if not risks:
            risks.append(
                "Static repository evidence may not reveal runtime integration constraints."
            )
        return risks


class PlanningService:
    """The application-facing interface for the complete planning and approval slice."""

    def __init__(
        self,
        *,
        inspector: RepositoryInspector,
        store: SQLitePlanStore,
        builder: PlanBuilder | None = None,
    ) -> None:
        self._inspector = inspector
        self._store = store
        self._builder = builder or PlanBuilder()

    async def create_plan(self, request: CreatePlanRequest) -> ImplementationPlan:
        self._validate_issue_repository(request)
        snapshot = await self._inspector.inspect(request.repository)
        plan = self._builder.build(snapshot, request.issue)
        await asyncio.to_thread(self._store.create, plan)
        return plan

    async def get_plan(self, plan_id: UUID) -> ImplementationPlan:
        return await asyncio.to_thread(self._store.get, plan_id)

    async def approve_plan(self, plan_id: UUID, request: ApprovePlanRequest) -> ImplementationPlan:
        return await asyncio.to_thread(
            self._store.approve,
            plan_id,
            approved_by=request.approved_by,
            expected_version=request.expected_version,
        )

    @staticmethod
    def _validate_issue_repository(request: CreatePlanRequest) -> None:
        if request.issue.url is None:
            return
        issue_owner, issue_name, _, _ = parse_github_issue_url(request.issue.url)
        if (
            issue_owner.casefold() != request.repository.owner.casefold()
            or issue_name.casefold() != request.repository.name.casefold()
        ):
            raise IssueRepositoryMismatchError(
                "issue URL does not belong to the requested GitHub repository"
            )
