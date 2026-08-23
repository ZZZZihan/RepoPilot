"""Deep planning module: inspect, derive evidence, validate, persist, and approve."""

from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from repopilot.errors import IssueRepositoryMismatchError
from repopilot.inspection import InspectedDocument, RepositoryInspector, RepositorySnapshot
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


class PlanBuilder:
    """Turn a bounded snapshot into one deterministic, schema-validated plan."""

    def build(self, snapshot: RepositorySnapshot, issue: IssueInput) -> ImplementationPlan:
        evidence = self._build_evidence(snapshot.documents)
        evidence_by_path = {item.path: item for item in evidence}
        issue_text = f"{issue.title}\n{issue.body}".lower()

        source_documents = self._rank_documents(
            snapshot.documents, issue_text, EvidenceCategory.SOURCE
        )
        test_documents = self._rank_documents(snapshot.documents, issue_text, EvidenceCategory.TEST)
        config_documents = [
            document
            for document in snapshot.documents
            if document.category in {EvidenceCategory.PROJECT_CONFIG, EvidenceCategory.TEST_CONFIG}
        ]
        readme_documents = [
            document
            for document in snapshot.documents
            if document.category is EvidenceCategory.README
        ]

        fallback_document = (
            source_documents + config_documents + test_documents + readme_documents
        )[0]
        analysis_documents = (
            source_documents or config_documents or test_documents or readme_documents
        )[:2]
        implementation_references = self._implementation_references(
            snapshot, source_documents, evidence_by_path, fallback_document
        )
        test_references = self._test_references(
            snapshot, test_documents, config_documents, evidence_by_path, fallback_document
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
                    "Change only the Python implementation paths supported by the inspected "
                    "layout, preserving existing public behavior not named by the issue."
                ),
                file_references=implementation_references,
            ),
            PlanStep(
                sequence=3,
                kind=StepKind.TEST,
                title="Add regression coverage for the Issue behavior",
                description=(
                    "Encode the requested behavior and at least one relevant negative or edge "
                    "case in the repository's observed test layout."
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
        risks = self._risks(snapshot)
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
    def _build_evidence(documents: tuple[InspectedDocument, ...]) -> list[EvidenceItem]:
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
            line_start, line_end = PlanBuilder._evidence_line_window(document)
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

    @staticmethod
    def _evidence_line_window(document: InspectedDocument) -> tuple[int, int]:
        lines = document.content.splitlines()
        if not lines:
            return 1, 1
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

    @staticmethod
    def _rank_documents(
        documents: tuple[InspectedDocument, ...],
        issue_text: str,
        category: EvidenceCategory,
    ) -> list[InspectedDocument]:
        matching = [document for document in documents if document.category is category]
        issue_terms = {
            term for term in re.findall(r"[a-z_][a-z0-9_]{2,}", issue_text) if len(term) >= 4
        }

        def relevance(document: InspectedDocument) -> int:
            searchable = f"{document.path}\n{document.content}".lower()
            return sum(term in searchable for term in issue_terms)

        return sorted(
            matching,
            key=lambda document: (
                -relevance(document),
                document.path.lower() not in issue_text,
                PurePosixPath(document.path).name.lower() not in issue_text,
                document.path.lower(),
            ),
        )

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
    ) -> list[FileReference]:
        if source_documents:
            return [
                self._reference(
                    document,
                    evidence_by_path,
                    action=FileAction.MODIFY,
                    reason="Observed Python implementation path most closely related to the Issue.",
                )
                for document in source_documents[:2]
            ]
        package_name = _NON_IDENTIFIER.sub("_", snapshot.repository.name.lower()).strip("_")
        package_name = package_name or "repopilot_change"
        return [
            FileReference(
                path=f"src/{package_name}/feature.py",
                action=FileAction.CREATE,
                exists=False,
                reason=(
                    "No Python source file was observed; this path follows the configured src "
                    "layout."
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
    ) -> list[FileReference]:
        if test_documents:
            return [
                self._reference(
                    document,
                    evidence_by_path,
                    action=FileAction.MODIFY,
                    reason="Observed test path provides the closest regression-test home.",
                )
                for document in test_documents[:2]
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
    def _risks(snapshot: RepositorySnapshot) -> list[str]:
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
