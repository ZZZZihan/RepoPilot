"""Deep planning module: inspect, derive evidence, validate, persist, and approve."""

from __future__ import annotations

import asyncio
import keyword
import re
import tomllib
from bisect import bisect_left
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Literal
from unicodedata import normalize
from uuid import UUID, uuid4

from repopilot.errors import (
    AmbiguousIssuePathError,
    ConflictingIssuePathError,
    InspectionLimitExceededError,
    IssueRepositoryMismatchError,
    RepositoryUpstreamError,
)
from repopilot.inspection import (
    InspectedDocument,
    RepositoryInspector,
    RepositorySnapshot,
    classify_path,
)
from repopilot.models import (
    ISSUE_BODY_MAX_LENGTH,
    ISSUE_TITLE_MAX_LENGTH,
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
    VerificationDeclaration,
    VerificationDeclarationKind,
    VerificationIntent,
    parse_github_issue_url,
    validate_repository_path,
)
from repopilot.storage import SQLitePlanStore, utc_now

_NON_IDENTIFIER = re.compile(r"[^a-z0-9_]+")
_TEXT_TOKEN = re.compile(r"[a-z][a-z0-9]*")
_SYMBOL_REFERENCE = re.compile(
    r"\b([a-z_][a-z0-9_]*)\s*\(",
    re.ASCII | re.IGNORECASE,
)
_PYTHON_DEFINITION = re.compile(
    r"^[ \t]*(?:async[ \t]+def|def|class)[ \t]+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.MULTILINE,
)
_NONSPACE_TOKEN = re.compile(r"\S+")
_BARE_TOKEN_SEGMENT = re.compile(r"[^,，、;；。．｡！？…⋯]+")
_URL_TOKEN_SEGMENT = re.compile(r"[^\s，、；。．｡！？…⋯]+")
_MARKDOWN_REFERENCE_DEFINITION_START = re.compile(r"(?:(?<=\n)|(?<=\r)|\A)[ ]{0,3}\[")
_URI_SCHEME = re.compile(r"(?<![a-z0-9+./-])[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_HOST_WITH_URL_DELIMITER = re.compile(
    r"(?<![a-z0-9_./-])(?:(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+"
    r"[a-z]{2,63}|localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?[/?#]",
    re.IGNORECASE,
)
_EMAIL_WITH_URL_DELIMITER = re.compile(
    r"(?<![a-z0-9_./-])[^/@\s]+@(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)+"
    r"[a-z]{2,63}[/?#]",
    re.IGNORECASE,
)
_BARE_AUTHORITY_WITH_DELIMITER = re.compile(
    r"(?:^|[=(:：<])(?:\[[0-9a-f:.%]+\]|"
    r"[^/\s?#=()<>]+(?:\.[^/\s?#=()<>]+)+)(?::\d+)?[/?#]",
    re.IGNORECASE,
)
_SINGLE_LABEL_AUTHORITY_QUERY = re.compile(r"(?:^|[=(:：<])[^/\s?#=()<>]+/[?#]")
_URL_QUERY_OR_FRAGMENT = re.compile(r"[?#]")
_IDNA_DOT_CHARACTERS = frozenset("。．｡")
_IDNA_AUTHORITY_DOTS = frozenset(".。．｡")
_IDNA_AUTHORITY_FORBIDDEN = frozenset("/?#=():<>,，、;；！？…⋯。.．｡")
_IDNA_DOT_TRANSLATION = str.maketrans({"。": ".", "．": ".", "｡": "."})
_MAX_CJK_ACTION_LOOKAHEAD = 512
_MAX_URI_CONTEXT_LOOKBEHIND = 2_048
_CJK_PATH_CONNECTOR_PREFIXES = ("然后", "同时", "并", "且", "再")
_CJK_PATH_CONTEXT_LEADERS = (
    *(connector + "请" for connector in _CJK_PATH_CONNECTOR_PREFIXES),
    *_CJK_PATH_CONNECTOR_PREFIXES,
    "请",
    "",
)
_CJK_PATH_ACTION_WORDS = (
    "路径",
    "文件",
    "创建",
    "更新",
    "修改",
    "实现",
    "新增",
    "添加",
    "测试",
    "检查",
)
_CJK_PATH_LOCATION_WORDS = ("在", "于", "将")
_CJK_PATH_ACTION_PREFIXES = tuple(
    leader + action for leader in _CJK_PATH_CONTEXT_LEADERS for action in _CJK_PATH_ACTION_WORDS
)
_CJK_PATH_LOCATION_PREFIXES = tuple(
    leader + location
    for leader in _CJK_PATH_CONTEXT_LEADERS
    for location in _CJK_PATH_LOCATION_WORDS
)
_CJK_PATH_ACTION_PREFIXES_BY_INITIAL = {
    initial: tuple(prefix for prefix in _CJK_PATH_ACTION_PREFIXES if prefix[0] == initial)
    for initial in {prefix[0] for prefix in _CJK_PATH_ACTION_PREFIXES}
}
_CJK_PATH_LOCATION_PREFIXES_BY_INITIAL = {
    initial: tuple(prefix for prefix in _CJK_PATH_LOCATION_PREFIXES if prefix[0] == initial)
    for initial in {prefix[0] for prefix in _CJK_PATH_LOCATION_PREFIXES}
}
_CJK_PATH_AMBIGUITY_PREFIXES = (*_CJK_PATH_ACTION_PREFIXES, *_CJK_PATH_LOCATION_PREFIXES)
_CJK_PATH_AMBIGUITY_PREFIXES_BY_INITIAL = {
    initial: tuple(prefix for prefix in _CJK_PATH_AMBIGUITY_PREFIXES if prefix[0] == initial)
    for initial in {prefix[0] for prefix in _CJK_PATH_AMBIGUITY_PREFIXES}
}
_CJK_PATH_ACTION_PREFIX_PATTERN = "(?:" + "|".join(map(re.escape, _CJK_PATH_ACTION_PREFIXES)) + ")"
_CJK_PATH_LOCATION_PREFIX_PATTERN = (
    "(?:" + "|".join(map(re.escape, _CJK_PATH_LOCATION_PREFIXES)) + ")"
)
_URL_LABEL_NAMES = tuple(
    dict.fromkeys(
        (
            *(
                prefix + base
                for prefix in ("", "参见", "查看", "请参见", "请查看", "参考", "链接", "see", "use")
                for base in ("url", "uri", "link", "href", "src")
            ),
            *(
                prefix + base
                for prefix in ("", "参见", "查看", "请参见", "请查看", "参考")
                for base in ("链接", "网址", "URL地址", "链接地址")
            ),
        )
    )
)
_URL_LABEL_TOKEN = re.compile(
    "(?:" + "|".join(map(re.escape, sorted(_URL_LABEL_NAMES, key=len, reverse=True))) + ")"
    r"(?:[=:：])?",
    re.ASCII | re.IGNORECASE,
)
_MAX_URL_LABEL_LENGTH = max(map(len, _URL_LABEL_NAMES))
_LABELED_REFERENCE = re.compile(
    r"^(?:(?ai:path|file|create|update|modify|implement|add|test)|"
    rf"{_CJK_PATH_ACTION_PREFIX_PATTERN})[:：](?P<value>.+)$",
)
_PATH_ACTION_PREFIX = re.compile(
    r"^(?:@|(?ai:(?:(?:please|then|and|but|also|next)[ \t]{1,8})?"
    r"(?:add[ \t]{1,8}coverage[ \t]{1,8}in|coverage[ \t]{1,8}in|"
    r"path|file|create|update|"
    r"modify|implement|add|test))|"
    rf"{_CJK_PATH_ACTION_PREFIX_PATTERN})"
    r"[ \t:：]{0,32}$",
)
_PROSE_PATH_PREFIX = re.compile(
    r"^(?:the|a|an|this|that|existing|current|see|use|please|look(?:\s+at)?|"
    r"inspect|review|check)(?:\b|/)|"
    r"^(?:请|请修改|请检查|修改|更新|创建|参见|查看|检查)(?:\s|/)",
    re.IGNORECASE,
)
_SUPPORTED_REFERENCE_SUFFIXES = (".cfg", ".ini", ".md", ".py", ".toml", ".yaml", ".yml")
_REFERENCE_WRAPPERS = (
    ("`", "`"),
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ("(", ")"),
    ("（", "）"),
    ("[", "]"),
    ("【", "】"),
)
_ACTION_WRAPPER_OPENERS = frozenset(opener for opener, _ in _REFERENCE_WRAPPERS)
_REFERENCE_WRAPPER_CLOSERS = frozenset(closer for _, closer in _REFERENCE_WRAPPERS)
_MAX_SUPPORTED_REFERENCE_SUFFIX_LENGTH = max(map(len, _SUPPORTED_REFERENCE_SUFFIXES))
_SAME_TOKEN_WRAPPER_PAIRS = {
    "(": ")",
    "[": "]",
    "{": "}",
    "（": "）",
    "【": "】",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
}
_SAME_TOKEN_SYMMETRIC_QUOTES = frozenset({'"', "'", "`"})
_SAME_TOKEN_LIST_SEPARATOR_TEXT = ",，、;；"
_SAME_TOKEN_LIST_SEPARATORS = frozenset(_SAME_TOKEN_LIST_SEPARATOR_TEXT)
_SAME_TOKEN_WRAPPER_CLOSERS = frozenset(_SAME_TOKEN_WRAPPER_PAIRS.values())
_STRUCTURED_URL_VALUE_PAIRS = {
    **_SAME_TOKEN_WRAPPER_PAIRS,
    **{quote: quote for quote in _SAME_TOKEN_SYMMETRIC_QUOTES},
}
_STRUCTURED_URL_VALUE_OPENERS = frozenset(_STRUCTURED_URL_VALUE_PAIRS)
_REFERENCE_TRAILING_PUNCTUATION_TEXT = ".,;:!?，。；：！？、—–-…⋯．｡→|)]}】）》」』\"'"
_REFERENCE_TRAILING_PUNCTUATION = (
    frozenset(_REFERENCE_TRAILING_PUNCTUATION_TEXT) | _REFERENCE_WRAPPER_CLOSERS
)
_WRAPPER_CONTEXT_OPENERS = frozenset("([{<（【{")
_REFERENCE_CONTEXT_BOUNDARIES = frozenset("，、；。．｡！？…⋯")
_ASCII_REFERENCE_CONTEXT_BOUNDARIES = frozenset(",;")
_BARE_TRAILING_PUNCTUATION = ".,;:!?，。；：！？…⋯．"
_BARE_REFERENCE_FORBIDDEN = frozenset("`\"'()[]{}<>?#=&*,:;!，。；：！？…⋯．")
_REFERENCE_INVALID_SUFFIXES = (
    ".bak",
    ".backup",
    ".bk",
    ".copy",
    ".diff",
    ".disabled",
    ".dist",
    ".example",
    ".old",
    ".orig",
    ".patch",
    ".rej",
    ".sample",
    ".save",
    ".swp",
    ".tmp",
    "_bak",
    "_backup",
    "_copy",
    "_old",
    "backup",
    "bak",
    "copy",
    "disabled",
    "dist",
    "example",
    "junk",
    "old",
    "orig",
    "rej",
    "sample",
    "save",
    "swp",
    "tmp",
    "备份",
    "副本",
    "旧版",
)
_MAX_REFERENCE_INVALID_SUFFIX_LENGTH = max(map(len, _REFERENCE_INVALID_SUFFIXES))
_MAX_PATH_ACTION_CONTEXT = 256
_MAX_CACHED_OPAQUE_TOKEN_LENGTH = 512
_COMPACT_PATH_SIGIL = "@"
_CJK_BARE_PROSE_PREFIXES = (
    "中",
    "里",
    "内",
    "并",
    "且",
    "然",
    "同",
    "的",
    "来",
    "应",
    "需",
    "要",
    "将",
    "以",
)
_CJK_PATH_LOCATION_CONTEXT = re.compile(rf"^{_CJK_PATH_LOCATION_PREFIX_PATTERN}[ \t]{{0,32}}$")
_ASCII_SENTENCE_BOUNDARY = re.compile(r"[.!?][ \t]+", re.ASCII)
_SEMANTIC_BARE_SEPARATORS = frozenset("—–→|")
_SAME_TOKEN_CLAUSE_SEPARATOR_TEXT = _SAME_TOKEN_LIST_SEPARATOR_TEXT + "—–→|"
_COMPACT_CLAUSE_SEPARATORS = frozenset("?!:：")
_ENGLISH_CLAUSE_CONTINUATION = re.compile(
    r"(?ai:(?:then|and|but|please|also|next)(?:\b|_))",
)
_LEADING_APOSTROPHE_ELISION = re.compile(
    r"(?ai:(?:tis|twas|cause|em|90s|round|til|nother|n|bout|cept|gainst|neath|ere|scuse|sup|kay)"
    r"(?:\b|_))",
)
_VERIFICATION_COMMAND_BODY_PATTERN = (
    r"(?:(?:python(?:3(?:\.[0-9]+)?)?[ \t]+-m|uv[ \t]+run|poetry[ \t]+run)[ \t]+)?"
    r"(?:pytest|ruff|mypy)"
)
_VERIFICATION_COMMAND_BODY = re.compile(
    r"^(?:(?P<launcher>python(?:3(?:\.[0-9]+)?)?[ \t]+-m|uv[ \t]+run|"
    r"poetry[ \t]+run)[ \t]+)?(?P<tool>pytest|ruff|mypy)$"
)
_VERIFICATION_COMMAND_ATOM_PATTERN = (
    rf"(?:\$[ \t]+)?(?:``[ \t]*(?:\$[ \t]+)?{_VERIFICATION_COMMAND_BODY_PATTERN}"
    rf"[ \t]*``|`[ \t]*(?:\$[ \t]+)?{_VERIFICATION_COMMAND_BODY_PATTERN}[ \t]*`|"
    rf"{_VERIFICATION_COMMAND_BODY_PATTERN})"
)
_VERIFICATION_COMMAND_ATOM = re.compile(rf"^{_VERIFICATION_COMMAND_ATOM_PATTERN}$")
_VERIFICATION_POSITIVE_CUE_PATTERN = (
    r"(?:"
    r"(?:please[ \t]+)?(?:run|execute|use)"
    r"(?:[ \t]+(?:the[ \t]+)?(?:test[ \t]+suite|suite|tests?|checks?|verification|"
    r"following(?:[ \t]+command)?|command))?"
    r"(?:[ \t]+(?:with|using))?"
    r"|commands?|tests?|testing|verification|checks?"
    r"|(?:请)?(?:运行|执行|使用)(?:该|这个|以下)?(?:测试|测试套件|检查|验证|命令)?"
    r"|测试|测试命令|验证|验证命令|命令|检查"
    r")"
)
_VERIFICATION_POSITIVE_CONTEXT = re.compile(
    rf"^{_VERIFICATION_POSITIVE_CUE_PATTERN}[ \t]*(?::|：)?$",
    re.ASCII | re.IGNORECASE,
)
_VERIFICATION_INLINE_DECLARATION = re.compile(
    rf"^[ \t]*{_VERIFICATION_POSITIVE_CUE_PATTERN}"
    rf"(?:[ \t]*[:：][ \t]*|[ \t]+)"
    rf"(?P<atom>{_VERIFICATION_COMMAND_ATOM_PATTERN})"
    rf"(?:[ \t]*[.!。！][ \t]*|[ \t]*)$",
    re.ASCII | re.IGNORECASE,
)
_VERIFICATION_STANDALONE_PREFIX = re.compile(
    r"^(?:(?:[-+*>]|[0-9]+[.)])[ \t]+)+",
)
_VERIFICATION_SHELL_PROMPT = re.compile(r"^\$[ \t]+")
_VERIFICATION_HEADING_PREFIX = re.compile(r"^#{1,6}[ \t]+")
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


def _canonical_lines(content: str) -> list[str]:
    """Split repository text only on CRLF, LF, or CR physical line endings."""

    if not content:
        return []
    lines = re.split(r"\r\n|\r|\n", content)
    if lines[-1] == "":
        lines.pop()
    return lines


@dataclass(frozen=True, slots=True)
class _ParsedFileReference:
    path: str
    target_eligible: bool
    ambiguous_cjk_prefix: str | None = None
    ambiguous_cjk_suffix_span: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class _MarkdownReference:
    span: tuple[int, int]
    label_span: tuple[int, int]
    image: bool


@dataclass(frozen=True, slots=True)
class _SingleQuoteRoles:
    """Issue-wide single-quote roles computed once and reused by nested scans."""

    paired_openers: frozenset[int]
    deferred_closers: frozenset[int]


class _ProtectedSpanInventory:
    """Keep parser-phase interval runs sorted without cross-phase list insertion."""

    def __init__(self) -> None:
        self._layers: list[list[tuple[int, int]]] = [[]]

    def copy(self) -> _ProtectedSpanInventory:
        copied = _ProtectedSpanInventory()
        copied._layers = [list(layer) for layer in self._layers]
        return copied

    def begin_phase(self) -> None:
        if self._layers[-1]:
            self._layers.append([])

    def add(self, span: tuple[int, int]) -> None:
        spans = self._layers[-1]
        if not spans or span[0] >= spans[-1][1]:
            spans.append(span)
            return
        start, end = span
        index = bisect_left(spans, (start, -1))
        if index > 0 and spans[index - 1][1] > start:
            index -= 1
            start = min(start, spans[index][0])
            end = max(end, spans[index][1])
            spans.pop(index)
        while index < len(spans) and spans[index][0] < end:
            start = min(start, spans[index][0])
            end = max(end, spans[index][1])
            spans.pop(index)
        spans.insert(index, (start, end))

    def overlaps(self, candidate: tuple[int, int]) -> bool:
        return any(self._layer_overlaps(candidate, layer) for layer in self._layers)

    def starts_in_protected(self, candidate: tuple[int, int]) -> bool:
        for spans in self._layers:
            index = bisect_left(spans, (candidate[0], -1))
            if index > 0:
                previous = spans[index - 1]
                if previous[0] <= candidate[0] < previous[1]:
                    return True
            if index < len(spans) and spans[index][0] == candidate[0]:
                return True
        return False

    def overlapping(self, candidate: tuple[int, int]) -> tuple[tuple[int, int], ...]:
        overlaps: list[tuple[int, int]] = []
        for spans in self._layers:
            index = bisect_left(spans, (candidate[0], -1))
            if index > 0 and spans[index - 1][1] > candidate[0]:
                index -= 1
            while index < len(spans) and spans[index][0] < candidate[1]:
                if spans[index][1] > candidate[0]:
                    overlaps.append(spans[index])
                index += 1
        if len(overlaps) < 2:
            return tuple(overlaps)
        merged: list[tuple[int, int]] = []
        for start, end in sorted(overlaps):
            if not merged or start >= merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        return tuple(merged)

    def previous(self, position: int) -> tuple[int, int] | None:
        previous: tuple[int, int] | None = None
        for spans in self._layers:
            index = bisect_left(spans, (position, -1))
            if index == 0:
                continue
            candidate = spans[index - 1]
            if previous is None or candidate[1] > previous[1]:
                previous = candidate
        return previous

    @staticmethod
    def _layer_overlaps(
        candidate: tuple[int, int],
        spans: list[tuple[int, int]],
    ) -> bool:
        index = bisect_left(spans, (candidate[0], -1))
        if index > 0 and candidate[0] < spans[index - 1][1]:
            return True
        return index < len(spans) and candidate[1] > spans[index][0]


type _ProtectedSpans = list[tuple[int, int]] | _ProtectedSpanInventory


class PlanBuilder:
    """Turn a bounded snapshot into one deterministic, schema-validated plan."""

    def build(self, snapshot: RepositorySnapshot, issue: IssueInput) -> ImplementationPlan:
        self._require_evidenced_planning_categories(snapshot)
        raw_issue_text = f"{issue.title}\n{issue.body}"
        parsed_references = self._parse_file_references(raw_issue_text)
        ambiguous_reference = next(
            (
                reference
                for reference in parsed_references
                if reference.ambiguous_cjk_prefix is not None
            ),
            None,
        )
        if ambiguous_reference is not None:
            ambiguous_prefix = ambiguous_reference.ambiguous_cjk_prefix
            assert ambiguous_prefix is not None
            operand = ambiguous_reference.path[len(ambiguous_prefix) :]
            ambiguous_suffix_span = ambiguous_reference.ambiguous_cjk_suffix_span
            ambiguous_suffix = (
                raw_issue_text[slice(*ambiguous_suffix_span)]
                if ambiguous_suffix_span is not None
                else None
            )
            compact_example = _COMPACT_PATH_SIGIL + operand + (ambiguous_suffix or "")
            if ambiguous_prefix in _CJK_PATH_LOCATION_PREFIXES:
                if ambiguous_suffix is None:
                    resolution_guidance = (
                        f"Explicitly label the intended operand (for example, {compact_example!r})"
                    )
                else:
                    assert ambiguous_suffix_span is not None
                    suffix_separator = "" if ambiguous_suffix[0] in " \t" else " "
                    separated_example = (
                        ambiguous_prefix + " " + operand + suffix_separator + ambiguous_suffix
                    )
                    path_start = ambiguous_suffix_span[0] - len(ambiguous_reference.path)
                    source_length, source_limit = (
                        (len(issue.title), ISSUE_TITLE_MAX_LENGTH)
                        if path_start < len(issue.title)
                        else (len(issue.body), ISSUE_BODY_MAX_LENGTH)
                    )
                    replacement_growth = len(separated_example) - len(
                        ambiguous_reference.path + ambiguous_suffix
                    )
                    if source_length + replacement_growth <= source_limit:
                        resolution_guidance = (
                            "Add a separator before the path operand "
                            f"(for example, {separated_example!r})"
                        )
                    else:
                        resolution_guidance = (
                            "Use the length-preserving compact path label "
                            f"(for example, {compact_example!r})"
                        )
            else:
                separated_example = ambiguous_prefix + " " + operand
                resolution_guidance = (
                    "Add a separator to name the operand "
                    f"(for example, {separated_example!r}); the compact path label "
                    f"{compact_example!r} is a length-preserving alternative"
                )
            raise AmbiguousIssuePathError(
                f"The repository path {ambiguous_reference.path!r} is ambiguous: its attached "
                f"CJK prefix may also be part of a real path. {resolution_guidance}, or delimit "
                "the "
                f"literal path (for example, {'路径:' + ambiguous_reference.path!r})."
            )
        explicit_references = tuple(reference.path for reference in parsed_references)
        eligible_target_categories = [
            category
            for reference in parsed_references
            if reference.target_eligible
            and (category := self._planning_category(reference.path))
            in {EvidenceCategory.SOURCE, EvidenceCategory.TEST}
        ]
        multi_target_categories = tuple(
            category
            for category in (EvidenceCategory.SOURCE, EvidenceCategory.TEST)
            if eligible_target_categories.count(category) > 1
        )
        explicit_targets = self._resolve_explicit_targets(
            snapshot,
            tuple(reference.path for reference in parsed_references if reference.target_eligible),
        )
        issue_text = raw_issue_text
        evidence = self._build_evidence(snapshot.documents, issue_text)
        evidence_by_path = {item.path: item for item in evidence}
        issue_delta = self._issue_delta(issue)

        source_target = explicit_targets.get(EvidenceCategory.SOURCE)
        test_target = explicit_targets.get(EvidenceCategory.TEST)

        ranked_source_documents = self._rank_documents(
            snapshot.documents,
            issue_text,
            EvidenceCategory.SOURCE,
            explicit_path=source_target[0] if source_target is not None else None,
            explicit_references=explicit_references,
        )
        ranked_test_documents = self._rank_documents(
            snapshot.documents,
            issue_text,
            EvidenceCategory.TEST,
            explicit_path=test_target[0] if test_target is not None else None,
            explicit_references=explicit_references,
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
        source_low_confidence = (
            source_target is None
            and not ranked_source_documents
            and bool(observed_source_documents)
        )
        test_low_confidence = (
            test_target is None and not ranked_test_documents and bool(observed_test_documents)
        )
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
            explicit_target=source_target,
            low_confidence=source_low_confidence,
        )
        test_references = self._test_references(
            snapshot,
            test_documents,
            config_documents,
            evidence_by_path,
            fallback_document,
            explicit_target=test_target,
            low_confidence=test_low_confidence,
        )
        declaration_paths = {item.path for item in evidence if item.declared_tools}
        declared_documents = [
            document for document in snapshot.documents if document.path in declaration_paths
        ]
        verification_documents = (
            declared_documents or config_documents or test_documents or readme_documents
        )[:3]
        if not verification_documents:
            verification_documents = [fallback_document]
        verification_intents = self._verification_intents(evidence)
        verification_readiness: Literal["ready", "needs_human_input"] = (
            "ready"
            if any(intent.tool == "pytest" for intent in verification_intents)
            else "needs_human_input"
        )

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
                    (
                        "Run the structured verification intents in a future isolated execution "
                        "stage. This planning slice records them but never executes commands."
                    )
                    if verification_readiness == "ready"
                    else (
                        "Resolve the missing evidence-backed test-runner declaration with a "
                        "reviewer before any future execution stage. This planning slice does "
                        "not invent or execute a command."
                    )
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

        risks = self._risks(
            snapshot,
            low_confidence_source_path=(
                source_documents[0].path if source_low_confidence else None
            ),
            low_confidence_test_path=(test_documents[0].path if test_low_confidence else None),
            multi_target_categories=multi_target_categories,
            verification_needs_human_input=verification_readiness == "needs_human_input",
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
            verification_readiness=verification_readiness,
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
    def _resolve_explicit_targets(
        cls,
        snapshot: RepositorySnapshot,
        references: tuple[str, ...],
    ) -> dict[EvidenceCategory, tuple[str, bool]]:
        """Resolve the first path-qualified source and test references against one tree."""

        tree_paths = set(snapshot.all_paths)
        tree_paths_by_casefold: dict[str, list[str]] = {}
        for path in tree_paths:
            tree_paths_by_casefold.setdefault(path.casefold(), []).append(path)
        evidence_by_path = {document.path: document for document in snapshot.documents}
        targets: dict[EvidenceCategory, tuple[str, bool]] = {}
        missing_evidence: list[str] = []
        ambiguous_references: list[str] = []

        for reference in references:
            requested_category = cls._planning_category(reference)

            existing_path: str | None = None
            if reference in tree_paths:
                existing_path = reference
            else:
                casefold_matches = sorted(tree_paths_by_casefold.get(reference.casefold(), []))
                if len(casefold_matches) == 1:
                    existing_path = casefold_matches[0]
                elif len(casefold_matches) > 1:
                    ambiguous_references.append(reference)
                    continue

            if existing_path is None:
                if requested_category in {
                    EvidenceCategory.SOURCE,
                    EvidenceCategory.TEST,
                }:
                    cls._assert_creatable_path(snapshot, reference)
                    if requested_category not in targets:
                        targets[requested_category] = (reference, False)
            elif existing_path not in evidence_by_path:
                missing_evidence.append(existing_path)
            else:
                # Existing paths inherit the inspector's canonical evidence
                # category. In particular, a root file named ``test new.py`` is
                # SOURCE when observed, even though the same absent name is a
                # supported explicit TEST creation target.
                existing_category = evidence_by_path[existing_path].category
                if (
                    existing_category
                    in {
                        EvidenceCategory.SOURCE,
                        EvidenceCategory.TEST,
                    }
                    and existing_category not in targets
                ):
                    targets[existing_category] = (existing_path, True)

        if ambiguous_references:
            references_text = ", ".join(repr(reference) for reference in ambiguous_references)
            raise InspectionLimitExceededError(
                f"Issue path {references_text} matches multiple repository paths when compared "
                "case-insensitively; use the exact tree path before planning"
            )
        if missing_evidence:
            references_text = ", ".join(repr(reference) for reference in missing_evidence)
            raise InspectionLimitExceededError(
                f"Issue explicitly references existing source or test path {references_text}, "
                "but bounded inspection did not capture corresponding evidence; increase "
                "inspection limits or narrow the repository before planning"
            )
        return targets

    @staticmethod
    def _assert_creatable_path(snapshot: RepositorySnapshot, path: str) -> None:
        """Reject CREATE targets whose exact path or ancestor cannot become a file."""

        exact_opaque_claims = (*snapshot.directory_paths, *snapshot.opaque_paths)
        exact_matches = sorted(
            claim for claim in exact_opaque_claims if claim.casefold() == path.casefold()
        )
        blocking_claims = (*snapshot.all_paths, *snapshot.opaque_paths)
        parts = PurePosixPath(path).parts
        ancestor_casefolds = {"/".join(parts[:index]).casefold() for index in range(1, len(parts))}
        ancestor_matches = sorted(
            claim for claim in blocking_claims if claim.casefold() in ancestor_casefolds
        )
        conflicts = (*exact_matches, *ancestor_matches)
        if not conflicts:
            return

        conflict = conflicts[0]
        raise ConflictingIssuePathError(
            f"Cannot create Issue path {path!r}: repository path {conflict!r} already "
            "occupies that path namespace"
        )

    @staticmethod
    def _planning_category(path: str) -> EvidenceCategory | None:
        """Classify explicit planning targets, including a spaced root test filename."""

        category = classify_path(path)
        if (
            category is EvidenceCategory.SOURCE
            and "/" not in path
            and PurePosixPath(path).name.casefold().startswith("test ")
        ):
            return EvidenceCategory.TEST
        return category

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
            declarations = cls._verification_declarations(document)
            line_start, line_end = cls._evidence_line_window(document, issue_text)
            if declarations:
                line_start = min(
                    line_start,
                    min(declaration.line_start for declaration in declarations),
                )
                line_end = max(
                    line_end,
                    max(declaration.line_end for declaration in declarations),
                )
            evidence.append(
                EvidenceItem(
                    id=f"E{index}",
                    path=document.path,
                    category=document.category,
                    line_start=line_start,
                    line_end=line_end,
                    sha256=document.sha256,
                    observation=observations[document.category],
                    declared_tools=list(declarations),
                )
            )
        return evidence

    @classmethod
    def _verification_declarations(
        cls,
        document: InspectedDocument,
    ) -> tuple[VerificationDeclaration, ...]:
        """Extract bounded, line-addressable tool declarations exactly once per document."""

        if document.category not in {
            EvidenceCategory.README,
            EvidenceCategory.PROJECT_CONFIG,
            EvidenceCategory.TEST_CONFIG,
        }:
            return ()

        lines = _canonical_lines(document.content)
        declarations: list[VerificationDeclaration] = []
        seen: set[tuple[str, VerificationDeclarationKind, int, int]] = set()

        previous_nonempty_line: dict[int, int | None] = {}
        previous_line_number: int | None = None
        for candidate_line_number, candidate_line in enumerate(lines, start=1):
            previous_nonempty_line[candidate_line_number] = previous_line_number
            if candidate_line.strip(" \t"):
                previous_line_number = candidate_line_number

        def fenced_delimiter(
            line: str,
        ) -> tuple[str, str, bool] | None:
            direct_match = re.fullmatch(
                r" {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)",
                line,
            )
            if direct_match is not None:
                fence = direct_match.group("fence")
                info = direct_match.group("info")
                if fence.startswith("`") and "`" in info:
                    return None
                return fence, info, True

            # Container and over-indented fence-like blocks are deliberately
            # opaque. Recognizing their full CommonMark nesting would require a
            # Markdown parser; treating their remainder as prose could instead
            # turn examples into verification authorization.
            position = 0
            line_length = len(line)
            while position < line_length and line[position] in " \t":
                position += 1
            removed_container = position > 3 or "\t" in line[:position]
            while position < line_length:
                marker_end: int | None = None
                if line[position] == ">":
                    marker_end = position + 1
                elif (
                    line[position] in "-+*"
                    and position + 1 < line_length
                    and line[position + 1] in " \t"
                ):
                    marker_end = position + 2
                elif line[position].isdigit():
                    digit_end = position
                    while (
                        digit_end < line_length
                        and digit_end - position < 9
                        and line[digit_end].isdigit()
                    ):
                        digit_end += 1
                    if (
                        digit_end < line_length - 1
                        and line[digit_end] in ".)"
                        and line[digit_end + 1] in " \t"
                    ):
                        marker_end = digit_end + 2
                if marker_end is None:
                    break
                removed_container = True
                position = marker_end
                while position < line_length and line[position] in " \t":
                    position += 1

            if not removed_container:
                return None
            opaque_match = re.fullmatch(
                r"(?P<fence>`{3,}|~{3,})(?P<info>.*)",
                line[position:],
            )
            if opaque_match is None:
                return None
            fence = opaque_match.group("fence")
            info = opaque_match.group("info")
            if fence.startswith("`") and "`" in info:
                return None
            return fence, info, False

        fence_opener_by_line: dict[int, int] = {}
        complete_fenced_line_numbers: set[int] = set()
        complete_fence_delimiters: set[int] = set()
        open_fence: tuple[int, str, int, bool, bool] | None = None
        for candidate_line_number, candidate_line in enumerate(lines, start=1):
            if open_fence is None:
                delimiter = fenced_delimiter(candidate_line)
                if delimiter is not None:
                    fence, raw_info, direct = delimiter
                    info = raw_info.strip(" \t")
                    open_fence = (
                        candidate_line_number,
                        fence[0],
                        len(fence),
                        direct and info in {"", "bash", "console", "sh", "shell", "zsh"},
                        direct,
                    )
                continue

            start_line, fence_character, fence_length, command_eligible, direct = open_fence
            if not direct:
                continue
            delimiter = fenced_delimiter(candidate_line)
            if (
                delimiter is None
                or not delimiter[2]
                or delimiter[0][0] != fence_character
                or len(delimiter[0]) < fence_length
                or delimiter[1].strip(" \t")
            ):
                continue
            fenced_line_range = range(start_line + 1, candidate_line_number)
            complete_fenced_line_numbers.update(fenced_line_range)
            if command_eligible:
                for fenced_line_number in fenced_line_range:
                    fence_opener_by_line[fenced_line_number] = start_line
            complete_fence_delimiters.update((start_line, candidate_line_number))
            open_fence = None

        # CommonMark treats an unclosed fenced block as extending through EOF.
        # It is not a complete command declaration, so keep both its opener and
        # every following line opaque instead of parsing apparent inline commands.
        if open_fence is not None:
            start_line, _, _, _, _ = open_fence
            complete_fence_delimiters.add(start_line)
            complete_fenced_line_numbers.update(range(start_line + 1, len(lines) + 1))

        def add(
            tool: Literal["pytest", "ruff", "mypy"],
            kind: VerificationDeclarationKind,
            line_start: int,
            line_end: int | None = None,
        ) -> None:
            resolved_line_end = line_start if line_end is None else line_end
            key = (tool, kind, line_start, resolved_line_end)
            if key in seen:
                return
            seen.add(key)
            declarations.append(
                VerificationDeclaration(
                    tool=tool,
                    kind=kind,
                    arguments=[],
                    line_start=line_start,
                    line_end=resolved_line_end,
                )
            )

        configuration_headers_by_path: dict[
            str,
            dict[Literal["pytest", "ruff", "mypy"], tuple[str, ...]],
        ] = {
            "pyproject.toml": {
                "pytest": ("[tool.pytest.ini_options]",),
                "ruff": ("[tool.ruff]",),
                "mypy": ("[tool.mypy]",),
            },
            "pytest.ini": {"pytest": ("[pytest]",)},
            "tox.ini": {"pytest": ("[pytest]",)},
            "setup.cfg": {
                "pytest": ("[tool:pytest]",),
                "mypy": ("[mypy]",),
            },
        }
        configuration_headers = configuration_headers_by_path.get(document.path, {})
        configuration_declaration_lines: dict[
            int,
            Literal["pytest", "ruff", "mypy"],
        ] = {}

        if document.path == "pyproject.toml":
            toml_tables: dict[
                Literal["pytest", "ruff", "mypy"],
                tuple[str, tuple[str, ...]],
            ] = {
                "pytest": ("[tool.pytest.ini_options]", ("tool", "pytest", "ini_options")),
                "ruff": ("[tool.ruff]", ("tool", "ruff")),
                "mypy": ("[tool.mypy]", ("tool", "mypy")),
            }
            try:
                tomllib.loads(document.content)
            except (RecursionError, ValueError):
                pass
            else:
                probe_key = f"__repopilot_probe_{document.sha256}__"
                for tool, (header, table_path) in toml_tables.items():
                    candidate_lines = [
                        line_number
                        for line_number, line in enumerate(lines, start=1)
                        if line == header
                    ]
                    if len(candidate_lines) != 1:
                        continue
                    line_number = candidate_lines[0]
                    prefix = "\n".join((*lines[:line_number], f"{probe_key} = true"))
                    try:
                        prefix_data: object = tomllib.loads(prefix)
                    except (RecursionError, ValueError):
                        continue
                    table: object = prefix_data
                    for component in table_path:
                        if not isinstance(table, dict):
                            break
                        table = table.get(component)
                    if isinstance(table, dict) and table.get(probe_key) is True:
                        configuration_declaration_lines[line_number] = tool

        elif configuration_headers:
            parser = ConfigParser(interpolation=None, strict=True)
            try:
                parser.read_string(document.content)
            except ConfigParserError:
                pass
            else:
                for tool, headers in configuration_headers.items():
                    for header in headers:
                        section = header[1:-1]
                        if not parser.has_section(section):
                            continue
                        for line_number, line in enumerate(lines, start=1):
                            if line == header:
                                configuration_declaration_lines[line_number] = tool

        def normalize_positive_context(line: str) -> str:
            candidate = line.strip(" \t")
            candidate = _VERIFICATION_STANDALONE_PREFIX.sub("", candidate, count=1).strip(" \t")
            candidate = _VERIFICATION_HEADING_PREFIX.sub("", candidate, count=1).strip(" \t")
            for emphasis in ("**", "__", "*", "_"):
                if (
                    candidate.startswith(emphasis)
                    and candidate.endswith(emphasis)
                    and len(candidate) > 2 * len(emphasis)
                ):
                    candidate = candidate[len(emphasis) : -len(emphasis)].strip(" \t")
                    break
            return candidate

        def parse_command_atom(
            line: str,
            *,
            allow_markdown_prefix: bool = True,
        ) -> tuple[Literal["pytest", "ruff", "mypy"] | None, bool]:
            candidate = line.strip(" \t")
            markdown_marker_count = 0
            if allow_markdown_prefix:
                candidate, markdown_marker_count = _VERIFICATION_STANDALONE_PREFIX.subn(
                    "", candidate, count=1
                )
                candidate = candidate.strip(" \t")
            if _VERIFICATION_COMMAND_ATOM.fullmatch(candidate) is None:
                return None, False

            candidate, outer_shell_prompt_count = _VERIFICATION_SHELL_PROMPT.subn(
                "", candidate, count=1
            )
            candidate = candidate.strip(" \t")
            backtick_marker = False
            if candidate.startswith("``") and candidate.endswith("``"):
                candidate = candidate[2:-2].strip(" \t")
                backtick_marker = True
            elif candidate.startswith("`") and candidate.endswith("`"):
                candidate = candidate[1:-1].strip(" \t")
                backtick_marker = True
            candidate, inner_shell_prompt_count = _VERIFICATION_SHELL_PROMPT.subn(
                "", candidate, count=1
            )
            candidate = candidate.strip(" \t")
            body_match = _VERIFICATION_COMMAND_BODY.fullmatch(candidate)
            if body_match is None:
                return None, False
            explicitly_marked = bool(
                markdown_marker_count
                or outer_shell_prompt_count
                or inner_shell_prompt_count
                or backtick_marker
            )
            tool = body_match.group("tool")
            if tool == "pytest":
                return "pytest", explicitly_marked
            if tool == "ruff":
                return "ruff", explicitly_marked
            if tool == "mypy":
                return "mypy", explicitly_marked
            return None, explicitly_marked

        positive_context_by_line: dict[int, bool] = {}

        def is_positive_context(line_number: int) -> bool:
            cached = positive_context_by_line.get(line_number)
            if cached is not None:
                return cached
            normalized = normalize_positive_context(lines[line_number - 1])
            result = _VERIFICATION_POSITIVE_CONTEXT.fullmatch(normalized) is not None
            positive_context_by_line[line_number] = result
            return result

        authorized_command_lines: set[int] = set()

        for line_number, line in enumerate(lines, start=1):
            configured_tool = configuration_declaration_lines.get(line_number)
            if configured_tool is not None:
                add(configured_tool, VerificationDeclarationKind.CONFIGURATION, line_number)

            if (
                document.category is not EvidenceCategory.README
                or line_number in complete_fence_delimiters
            ):
                continue

            inside_complete_fence = line_number in complete_fenced_line_numbers
            if inside_complete_fence and line_number not in fence_opener_by_line:
                continue
            if not inside_complete_fence:
                inline_match = _VERIFICATION_INLINE_DECLARATION.fullmatch(line)
                if inline_match is not None:
                    inline_tool, _ = parse_command_atom(
                        inline_match.group("atom"),
                        allow_markdown_prefix=False,
                    )
                    if inline_tool is None:
                        continue
                    add(
                        inline_tool,
                        VerificationDeclarationKind.COMMAND,
                        line_number,
                    )
                    authorized_command_lines.add(line_number)
                    continue

            fence_opener = fence_opener_by_line.get(line_number)
            standalone_tool, _ = parse_command_atom(
                line,
                allow_markdown_prefix=fence_opener is None,
            )
            if standalone_tool is None:
                continue

            anchor_line_number = previous_nonempty_line[
                fence_opener if fence_opener is not None else line_number
            ]
            anchor_is_positive = False
            if anchor_line_number is not None:
                anchor_is_positive = is_positive_context(anchor_line_number)
                if not anchor_is_positive and anchor_line_number not in authorized_command_lines:
                    continue

            declaration_start = (
                anchor_line_number
                if anchor_line_number is not None and anchor_is_positive
                else line_number
            )
            add(
                standalone_tool,
                VerificationDeclarationKind.COMMAND,
                declaration_start,
                line_number,
            )
            authorized_command_lines.add(line_number)

        # EvidenceItem permits at most one declaration for each supported M0 tool.
        # Prefer an executable command over configuration and otherwise keep the
        # earliest line, so repeated prose cannot overflow the evidence contract.
        selected_by_tool: dict[str, VerificationDeclaration] = {}
        for declaration in declarations:
            existing = selected_by_tool.get(declaration.tool)
            declaration_rank = (
                int(declaration.kind is VerificationDeclarationKind.CONFIGURATION),
                declaration.line_start,
                declaration.line_end,
            )
            if existing is None or declaration_rank < (
                int(existing.kind is VerificationDeclarationKind.CONFIGURATION),
                existing.line_start,
                existing.line_end,
            ):
                selected_by_tool[declaration.tool] = declaration

        return tuple(
            sorted(
                selected_by_tool.values(),
                key=lambda declaration: (declaration.line_start, declaration.tool),
            )
        )

    @classmethod
    def _evidence_line_window(cls, document: InspectedDocument, issue_text: str) -> tuple[int, int]:
        lines = _canonical_lines(document.content)
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
        *,
        explicit_path: str | None = None,
        explicit_references: tuple[str, ...] | None = None,
    ) -> list[InspectedDocument]:
        matching = [document for document in documents if document.category is category]
        issue_terms = cls._meaningful_tokens(issue_text)
        issue_symbols = cls._symbol_references(issue_text)
        parsed_references = explicit_references or cls._explicit_file_references(issue_text)
        explicit_paths = {reference for reference in parsed_references if "/" in reference}
        if explicit_path is not None:
            explicit_paths.add(explicit_path)
        explicit_names = {
            PurePosixPath(reference).name.casefold() for reference in parsed_references
        }

        def relevance(document: InspectedDocument) -> tuple[int, int, int, int, int]:
            path = document.path
            name = PurePosixPath(path).name.casefold()
            document_tokens = cls._text_tokens(f"{path.casefold()}\n{document.content}")
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

    @classmethod
    def _explicit_file_references(cls, value: str) -> tuple[str, ...]:
        return tuple(reference.path for reference in cls._parse_file_references(value))

    @classmethod
    def _parse_file_references(cls, value: str) -> tuple[_ParsedFileReference, ...]:
        """Parse complete path tokens without salvaging suffixes or URL substrings."""

        # Every supported repository reference ends in an ASCII-dot suffix. This
        # linear precondition avoids running the multi-phase grammar over large
        # URL/prose inputs that cannot possibly produce a path candidate.
        if "." not in value:
            return ()

        # Keep the exact syntactic envelope end with each candidate. Root-path
        # authorization is revalidated after every parser branch has converged,
        # and CJK location grammar must inspect the suffix after a complete
        # wrapper or Markdown reference rather than after the path text alone.
        found: list[tuple[int, int, _ParsedFileReference]] = []
        protected_spans = _ProtectedSpanInventory()
        separate_url_reopen_positions: set[int] = set()
        suppressed_recovered_reference_starts: set[int] = set()
        url_clause_spans_by_token_start: dict[int, tuple[tuple[int, int], ...]] = {}
        token_local_clause_spans_by_token_start: dict[int, tuple[tuple[int, int], ...]] = {}
        token_matches = tuple(_NONSPACE_TOKEN.finditer(value))
        line_break_positions = tuple(
            index for index, character in enumerate(value) if character in "\r\n"
        )
        path_separator_positions = tuple(
            index for index, character in enumerate(value) if character in "/\\"
        )
        continuation_boundary_positions = tuple(
            index
            for index, character in enumerate(value)
            if character.isspace()
            or character in _BARE_REFERENCE_FORBIDDEN
            or character in _SEMANTIC_BARE_SEPARATORS
        )
        url_label_candidates: list[tuple[int, int]] = []
        for label_match in _URL_LABEL_TOKEN.finditer(value):
            label_start = label_match.start()
            if label_start > 0 and not (
                value[label_start - 1].isspace()
                or value[label_start - 1] in _REFERENCE_CONTEXT_BOUNDARIES
                or value[label_start - 1] in _SAME_TOKEN_LIST_SEPARATORS
                or value[label_start - 1] in _SEMANTIC_BARE_SEPARATORS
                or value[label_start - 1] in _STRUCTURED_URL_VALUE_OPENERS
            ):
                continue
            label_text = label_match.group()
            if label_text[-1] in "=:：":
                url_label_candidates.append((label_start, label_match.end()))
                continue
            delimiter_start = label_match.end()
            while delimiter_start < len(value) and value[delimiter_start] in " \t":
                delimiter_start += 1
            if (
                delimiter_start < len(value)
                and value[delimiter_start] in "=:："
                and _URL_LABEL_TOKEN.fullmatch(label_text + value[delimiter_start]) is not None
            ):
                url_label_candidates.append((label_start, delimiter_start + 1))

        markdown_references, markdown_spans, malformed_markdown_references = (
            cls._markdown_references(value)
        )
        markdown_destination_by_url_label_start: dict[
            int,
            tuple[int, _MarkdownReference, bool],
        ] = {}
        destination_label_index = 0
        destination_records = sorted(
            (
                *((reference, False) for reference in markdown_references),
                *((reference, True) for reference in malformed_markdown_references),
            ),
            key=lambda record: record[0].span,
        )
        for markdown_reference, malformed in destination_records:
            destination_start = markdown_reference.label_span[1] + 1
            if value[destination_start : destination_start + 1] not in {"(", "["}:
                continue
            while (
                destination_label_index < len(url_label_candidates)
                and url_label_candidates[destination_label_index][0] < destination_start
            ):
                destination_label_index += 1
            while (
                destination_label_index < len(url_label_candidates)
                and url_label_candidates[destination_label_index][0] < markdown_reference.span[1]
            ):
                candidate_start = url_label_candidates[destination_label_index][0]
                markdown_destination_by_url_label_start[candidate_start] = (
                    destination_start,
                    markdown_reference,
                    malformed,
                )
                destination_label_index += 1

        # A wrapper can cross whitespace-token boundaries, so a token-local
        # topology scan cannot by itself prove that a semantic seam sits outside
        # the wrapper. Compute that proof once per physical line, then refine each
        # token's ordinary clause layout with only the globally proven hard seams.
        # List delimiters remain token-local because commas and semicolons can be
        # URI data; they are used globally only to clamp an already recognized URL
        # label to its containing clause.
        line_clause_spans: list[tuple[int, int]] = []
        global_proven_clause_seams: list[int] = []
        structured_url_assignment_envelope_spans: list[tuple[int, int]] = []
        malformed_url_assignment_markdown_references: list[tuple[int, int, int, int]] = []
        recoverable_markdown_url_label_spans: set[tuple[int, int]] = set()
        has_semantic_separator = any(separator in value for separator in _SEMANTIC_BARE_SEPARATORS)
        has_structured_opener = any(opener in value for opener in _STRUCTURED_URL_VALUE_OPENERS)
        needs_shared_line_topology = has_structured_opener and (
            has_semantic_separator or bool(url_label_candidates)
        )
        if needs_shared_line_topology:
            raw_wrapper_url_context: dict[int, bool] = {}
            raw_wrapper_context_bounds: dict[int, tuple[int, int | None, int]] = {}
            raw_uri_owned_action_wrappers: set[int] = set()

            def wrapper_has_raw_url_context(
                wrapper_start: int,
                line_start: int,
            ) -> bool:
                cached = raw_wrapper_url_context.get(wrapper_start)
                if cached is not None:
                    return cached
                context_bounds = raw_wrapper_context_bounds.get(wrapper_start)
                if context_bounds is None:
                    result = False
                else:
                    token_start, preceding_seam, operand_anchor = context_bounds
                    if preceding_seam is not None:
                        seam_candidate_start = preceding_seam + 1
                        while (
                            seam_candidate_start < wrapper_start
                            and value[seam_candidate_start] in " \t"
                        ):
                            seam_candidate_start += 1
                        seam_action_context = value[seam_candidate_start:wrapper_start].rstrip(
                            " \t"
                        )
                        seam_precedes_exact_action = (
                            _PATH_ACTION_PREFIX.fullmatch(seam_action_context) is not None
                            or _CJK_PATH_LOCATION_CONTEXT.fullmatch(seam_action_context) is not None
                        )
                        if seam_precedes_exact_action or preceding_seam >= token_start:
                            token_start = seam_candidate_start
                    immediate_boundary = operand_anchor
                    while (
                        immediate_boundary > token_start and value[immediate_boundary - 1] in " \t"
                    ):
                        immediate_boundary -= 1
                    if (
                        immediate_boundary > token_start
                        and value[immediate_boundary - 1] in _SEMANTIC_BARE_SEPARATORS
                        and not cls._is_escaped(value, immediate_boundary - 1)
                    ):
                        token_start = wrapper_start
                    action_start = supported_action_anchor(
                        wrapper_start,
                        line_start=line_start,
                        operand_anchor=operand_anchor,
                    )
                    if action_start < wrapper_start:
                        token_start = action_start
                    raw_prefix_end = operand_anchor
                    while raw_prefix_end > token_start and value[raw_prefix_end - 1] in " \t":
                        raw_prefix_end -= 1
                    if wrapper_start in raw_uri_owned_action_wrappers or (
                        action_start == wrapper_start
                        and token_start < raw_prefix_end
                        and cls._is_opaque_url_clause(value[token_start:raw_prefix_end])
                    ):
                        result = True
                    else:
                        result = cls._wrapper_has_url_context(
                            value,
                            wrapper_start,
                            [],
                            token_start,
                        )
                raw_wrapper_url_context[wrapper_start] = result
                return result

            physical_line_spans: list[tuple[int, int]] = []
            physical_line_start = 0
            physical_cursor = 0
            while physical_cursor < len(value):
                character = value[physical_cursor]
                if character not in "\r\n":
                    physical_cursor += 1
                    continue
                physical_line_spans.append((physical_line_start, physical_cursor))
                if (
                    character == "\r"
                    and physical_cursor + 1 < len(value)
                    and value[physical_cursor + 1] == "\n"
                ):
                    physical_cursor += 2
                else:
                    physical_cursor += 1
                physical_line_start = physical_cursor
            physical_line_spans.append((physical_line_start, len(value)))

            def supported_action_anchor(
                wrapper_start: int,
                *,
                line_start: int,
                operand_anchor: int | None = None,
            ) -> int:
                """Include one exact bounded action before a structured operand."""

                action_end = wrapper_start if operand_anchor is None else operand_anchor
                while action_end > line_start and value[action_end - 1] in " \t":
                    action_end -= 1
                probe_start = max(line_start, action_end - _MAX_PATH_ACTION_CONTEXT)
                hard_seam: int | None = None
                raw_uri_owner_before_action = False
                for position in range(action_end - 1, probe_start - 1, -1):
                    if value[position] in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT and not cls._is_escaped(
                        value, position
                    ):
                        hard_seam = position
                        break
                if hard_seam is not None:
                    raw_uri_owner_before_action = (
                        value[hard_seam] in ",;"
                        and hard_seam + 1 < action_end
                        and value[hard_seam + 1] not in " \t"
                        and cls._has_explicit_uri_envelope(value[line_start:hard_seam])
                    )
                    candidate_start = hard_seam + 1
                elif probe_start == line_start:
                    candidate_start = line_start
                else:
                    return wrapper_start
                while candidate_start < action_end and value[candidate_start] in " \t":
                    candidate_start += 1
                action_context = value[candidate_start:action_end]
                if (
                    _PATH_ACTION_PREFIX.fullmatch(action_context) is not None
                    or _CJK_PATH_LOCATION_CONTEXT.fullmatch(action_context) is not None
                ):
                    if raw_uri_owner_before_action:
                        raw_uri_owned_action_wrappers.add(wrapper_start)
                        return wrapper_start
                    return candidate_start
                return wrapper_start

            physical_line_label_index = 0
            physical_line_destination_index = 0
            raw_owner_token_index = 0
            raw_uri_owned_markdown_label_spans: set[tuple[int, int]] = set()
            for line_start, line_end in physical_line_spans:
                while (
                    physical_line_label_index < len(url_label_candidates)
                    and url_label_candidates[physical_line_label_index][0] < line_start
                ):
                    physical_line_label_index += 1
                first_line_label = physical_line_label_index
                while (
                    physical_line_label_index < len(url_label_candidates)
                    and url_label_candidates[physical_line_label_index][0] < line_end
                ):
                    physical_line_label_index += 1
                last_line_label = physical_line_label_index

                line_text = value[line_start:line_end]
                line_has_structured_opener = any(
                    opener in line_text for opener in _STRUCTURED_URL_VALUE_OPENERS
                )
                if not line_has_structured_opener:
                    continue
                line_proven_seams: list[int] = []
                if first_line_label != last_line_label or any(
                    separator in line_text for separator in _SEMANTIC_BARE_SEPARATORS
                ):
                    for clause_start, clause_end in cls._same_token_list_clauses(line_text):
                        absolute_clause = (line_start + clause_start, line_start + clause_end)
                        line_clause_spans.append(absolute_clause)
                        if (
                            absolute_clause[1] < line_end
                            and value[absolute_clause[1]] in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT
                            and not cls._is_escaped(value, absolute_clause[1])
                        ):
                            line_proven_seams.append(absolute_clause[1])
                            global_proven_clause_seams.append(absolute_clause[1])

                while (
                    physical_line_destination_index < len(destination_records)
                    and destination_records[physical_line_destination_index][0].span[0] < line_start
                ):
                    physical_line_destination_index += 1
                first_line_destination = physical_line_destination_index
                while (
                    physical_line_destination_index < len(destination_records)
                    and destination_records[physical_line_destination_index][0].span[0] < line_end
                ):
                    physical_line_destination_index += 1
                last_line_destination = physical_line_destination_index

                if first_line_label == last_line_label:
                    continue

                # Mark Markdown references that are lexically owned by a raw URI
                # before any containing ordinary wrapper is allowed to punch a
                # label hole. References and tokens are consumed monotonically;
                # whitespace or a proven top-level clause seam resets ownership.
                raw_owner_gap_start: int | None = None
                raw_owner_token_start: int | None = None
                raw_owner_clause_start: int | None = None
                raw_owner_active = False
                raw_owner_seam_index = 0
                preceding_raw_owner_seam: int | None = None
                raw_owned_wrapper_start: int | None = None
                raw_owned_wrapper_end: int | None = None
                for destination_index in range(
                    first_line_destination,
                    last_line_destination,
                ):
                    markdown_reference, _ = destination_records[destination_index]
                    reference_start = markdown_reference.span[0]
                    while (
                        raw_owner_token_index < len(token_matches)
                        and token_matches[raw_owner_token_index].end() <= reference_start
                    ):
                        raw_owner_token_index += 1
                    if raw_owner_token_index >= len(token_matches):
                        break
                    containing_token = token_matches[raw_owner_token_index]
                    if not (containing_token.start() <= reference_start < containing_token.end()):
                        continue
                    while (
                        raw_owner_seam_index < len(line_proven_seams)
                        and line_proven_seams[raw_owner_seam_index] < reference_start
                    ):
                        preceding_raw_owner_seam = line_proven_seams[raw_owner_seam_index]
                        raw_owner_seam_index += 1
                    clause_start = containing_token.start()
                    if (
                        preceding_raw_owner_seam is not None
                        and preceding_raw_owner_seam >= clause_start
                    ):
                        clause_start = preceding_raw_owner_seam + 1
                    if raw_owned_wrapper_end is not None and (
                        reference_start >= raw_owned_wrapper_end
                        or (
                            preceding_raw_owner_seam is not None
                            and raw_owned_wrapper_start is not None
                            and preceding_raw_owner_seam >= raw_owned_wrapper_start
                        )
                    ):
                        raw_owned_wrapper_start = None
                        raw_owned_wrapper_end = None
                    wrapper_carries_raw_owner = (
                        raw_owned_wrapper_end is not None
                        and reference_start < raw_owned_wrapper_end
                    )
                    if (
                        raw_owner_token_start != containing_token.start()
                        or raw_owner_clause_start != clause_start
                    ):
                        raw_owner_token_start = containing_token.start()
                        raw_owner_clause_start = clause_start
                        raw_owner_gap_start = clause_start
                        raw_owner_active = False
                    assert raw_owner_gap_start is not None
                    operand_start = reference_start
                    operand_probe = reference_start
                    while operand_probe > clause_start and value[operand_probe - 1] in " \t":
                        operand_probe -= 1
                    if (
                        operand_probe > clause_start
                        and value[operand_probe - 1] in _STRUCTURED_URL_VALUE_OPENERS
                        and not cls._is_escaped(value, operand_probe - 1)
                    ):
                        operand_start = operand_probe - 1
                    action_start = supported_action_anchor(
                        operand_start,
                        line_start=line_start,
                    )
                    reference_is_action_owned = action_start < operand_start
                    if reference_is_action_owned and not wrapper_carries_raw_owner:
                        raw_owner_active = False
                        raw_owner_gap_start = reference_start
                    if wrapper_carries_raw_owner:
                        raw_owner_active = True
                    elif (
                        not raw_owner_active
                        and not reference_is_action_owned
                        and raw_owner_gap_start <= reference_start
                        and cls._is_opaque_url_clause(value[raw_owner_gap_start:reference_start])
                    ):
                        raw_owner_active = True
                    if raw_owner_active:
                        raw_uri_owned_markdown_label_spans.add(markdown_reference.label_span)
                        structured_url_assignment_envelope_spans.append(markdown_reference.span)
                        if raw_owned_wrapper_end is None and operand_start < reference_start:
                            candidate_wrapper_end = cls._structured_url_value_span(
                                value,
                                operand_start,
                                line_end=line_end,
                            )[1]
                            if markdown_reference.span[1] <= candidate_wrapper_end:
                                raw_owned_wrapper_start = operand_start
                                raw_owned_wrapper_end = candidate_wrapper_end
                    raw_owner_gap_start = max(
                        raw_owner_gap_start,
                        markdown_reference.span[1],
                    )

                # A URL label can occur after prose inside an already-open outer
                # envelope. Starting the ordinary value scan at the label would
                # then forget that wrapper and could mistake one of its internal
                # hard separators for a top-level repository clause. Record the
                # active *outermost* opener for every label in one forward scan of
                # the physical line. Each resulting envelope is parsed only once,
                # so dense labels and malformed wrappers remain linear.
                active_url_envelopes: list[
                    tuple[int, int, _MarkdownReference | None, bool, int | None, bool]
                ] = []
                wrapper_stack: list[tuple[str, int]] = []
                quote: tuple[str, int] | None = None
                paired_single_quote_openers = cls._paired_single_quote_openers(
                    value,
                    start=line_start,
                    end=line_end,
                )
                backslash_run = 0
                line_label_index = first_line_label
                line_proven_seam_set = set(line_proven_seams)
                preceding_proven_seam: int | None = None
                current_token_start = line_start
                for cursor in range(line_start, line_end):
                    character = value[cursor]
                    if character in _STRUCTURED_URL_VALUE_OPENERS or (
                        character == "!" and cursor + 1 < line_end and value[cursor + 1] == "["
                    ):
                        operand_anchor = (
                            cursor - backslash_run if backslash_run % 2 == 0 else cursor
                        )
                        raw_wrapper_context_bounds[cursor] = (
                            current_token_start,
                            preceding_proven_seam,
                            operand_anchor,
                        )
                    if character.isspace():
                        current_token_start = cursor + 1
                    if cursor in line_proven_seam_set:
                        preceding_proven_seam = cursor
                    while (
                        line_label_index < last_line_label
                        and url_label_candidates[line_label_index][0] == cursor
                    ):
                        natural_outer_start: int | None = None
                        if wrapper_stack:
                            natural_outer_start = wrapper_stack[0][1]
                        elif quote is not None:
                            natural_outer_start = quote[1]
                        natural_outer_anchor = (
                            raw_wrapper_context_bounds.get(
                                natural_outer_start,
                                (natural_outer_start, None, natural_outer_start),
                            )[2]
                            if natural_outer_start is not None
                            else None
                        )
                        markdown_destination = markdown_destination_by_url_label_start.get(cursor)
                        if markdown_destination is not None:
                            (
                                protection_start,
                                destination_reference,
                                malformed_markdown,
                            ) = markdown_destination
                            active_markdown_reference: _MarkdownReference | None = (
                                destination_reference
                            )
                            if (
                                natural_outer_start is not None
                                and natural_outer_start != protection_start
                            ):
                                assert natural_outer_anchor is not None
                                seam_anchor = natural_outer_anchor
                            else:
                                seam_anchor = destination_reference.span[0]
                            containing_outer_start = (
                                natural_outer_start
                                if natural_outer_start is not None
                                and natural_outer_start != protection_start
                                else None
                            )
                            url_context_start = (
                                containing_outer_start
                                if containing_outer_start is not None
                                else destination_reference.span[0]
                            )
                        elif natural_outer_start is not None:
                            protection_start = natural_outer_start
                            assert natural_outer_anchor is not None
                            seam_anchor = natural_outer_anchor
                            active_markdown_reference = None
                            malformed_markdown = False
                            containing_outer_start = None
                            url_context_start = natural_outer_start
                        else:
                            line_label_index += 1
                            continue
                        has_raw_url_context = wrapper_has_raw_url_context(
                            url_context_start,
                            line_start,
                        )
                        while (
                            active_url_envelopes and active_url_envelopes[-1][0] > protection_start
                        ):
                            active_url_envelopes.pop()
                        if (
                            not active_url_envelopes
                            or active_url_envelopes[-1][0] != protection_start
                        ):
                            active_url_envelopes.append(
                                (
                                    protection_start,
                                    seam_anchor,
                                    active_markdown_reference,
                                    malformed_markdown,
                                    containing_outer_start,
                                    has_raw_url_context,
                                )
                            )
                        line_label_index += 1

                    if character == "\\":
                        backslash_run += 1
                        continue
                    escaped = backslash_run % 2 == 1
                    backslash_run = 0
                    if escaped:
                        continue
                    if quote is not None:
                        if character == quote[0]:
                            if cls._apostrophe_is_inner_word(value, cursor):
                                continue
                            quote = None
                        continue
                    if cls._opens_symmetric_quote(
                        value,
                        cursor,
                        scan_start=line_start,
                        paired_single_quote_openers=paired_single_quote_openers,
                    ):
                        quote = (character, cursor)
                        continue
                    if character in _SAME_TOKEN_WRAPPER_PAIRS:
                        if (
                            character == "‘"
                            and cursor not in paired_single_quote_openers
                            and _LEADING_APOSTROPHE_ELISION.match(value, cursor + 1) is not None
                        ):
                            continue
                        wrapper_stack.append((_SAME_TOKEN_WRAPPER_PAIRS[character], cursor))
                        continue
                    if (
                        character in _SAME_TOKEN_WRAPPER_CLOSERS
                        and wrapper_stack
                        and character == wrapper_stack[-1][0]
                    ):
                        if cls._apostrophe_is_inner_word(value, cursor):
                            continue
                        wrapper_stack.pop()

                covered_until = line_start
                containing_outer_wrapper_ends: dict[int, int] = {}
                containing_outer_ends: dict[int, int] = {}
                containing_outer_raw_url_context: dict[int, bool] = {}
                containing_outer_label_holes: dict[int, list[tuple[int, int]]] = {}
                malformed_outer_label_holes: set[tuple[int, int]] = set()
                for (
                    protection_start,
                    seam_anchor,
                    active_markdown_reference,
                    malformed_markdown,
                    containing_outer_start,
                    has_raw_url_context,
                ) in active_url_envelopes:
                    if protection_start < covered_until:
                        continue
                    raw_envelope_span = cls._structured_url_value_span(
                        value,
                        protection_start,
                        line_end=line_end,
                    )
                    if active_markdown_reference is not None:
                        # A Markdown destination has its own grammar boundary. Its
                        # surrounding reference remains ordinary issue syntax, not
                        # part of the nested URL value. Malformed destinations are
                        # already extended to their physical-line boundary.
                        context_end = raw_envelope_span[1]
                    else:
                        context_end = cls._structured_url_value_context_end(
                            value,
                            raw_envelope_span[1],
                            line_end=line_end,
                        )
                        proven_seam_index = bisect_left(
                            line_proven_seams,
                            raw_envelope_span[1],
                        )
                        if proven_seam_index < len(line_proven_seams):
                            context_end = min(
                                context_end,
                                line_proven_seams[proven_seam_index],
                            )
                    envelope_span = (raw_envelope_span[0], context_end)
                    structured_url_assignment_envelope_spans.append(envelope_span)
                    covered_until = max(covered_until, envelope_span[1])

                    if containing_outer_start is not None:
                        containing_outer_end = containing_outer_ends.get(containing_outer_start)
                        if containing_outer_end is None:
                            containing_outer_wrapper_end = cls._structured_url_value_span(
                                value,
                                containing_outer_start,
                                line_end=line_end,
                            )[1]
                            containing_outer_end = cls._structured_url_value_context_end(
                                value,
                                containing_outer_wrapper_end,
                                line_end=line_end,
                            )
                            proven_seam_index = bisect_left(
                                line_proven_seams,
                                containing_outer_wrapper_end,
                            )
                            if proven_seam_index < len(line_proven_seams):
                                containing_outer_end = min(
                                    containing_outer_end,
                                    line_proven_seams[proven_seam_index],
                                )
                            containing_outer_wrapper_ends[containing_outer_start] = (
                                containing_outer_wrapper_end
                            )
                            containing_outer_ends[containing_outer_start] = containing_outer_end
                            containing_outer_raw_url_context[containing_outer_start] = (
                                has_raw_url_context
                            )
                            containing_outer_label_holes[containing_outer_start] = []
                        else:
                            containing_outer_raw_url_context[containing_outer_start] |= (
                                has_raw_url_context
                            )
                        if (
                            active_markdown_reference is not None
                            and malformed_markdown
                            and not active_markdown_reference.image
                            and not has_raw_url_context
                            and active_markdown_reference.label_span
                            not in raw_uri_owned_markdown_label_spans
                        ):
                            malformed_outer_label_holes.add(active_markdown_reference.label_span)

                    if (
                        active_markdown_reference is not None
                        and malformed_markdown
                        and not active_markdown_reference.image
                        and not has_raw_url_context
                        and active_markdown_reference.label_span
                        not in raw_uri_owned_markdown_label_spans
                    ):
                        malformed_url_assignment_markdown_references.append(
                            (
                                active_markdown_reference.span[0],
                                active_markdown_reference.label_span[0],
                                active_markdown_reference.label_span[1],
                                envelope_span[1],
                            )
                        )

                    # The seam immediately before an outermost envelope is
                    # demonstrably outside it even when the envelope never closes.
                    # Preserve the left repository clause without reopening any
                    # path-looking text inside the malformed URL assignment.
                    if (
                        active_markdown_reference is None
                        and seam_anchor > line_start
                        and value[seam_anchor - 1] == "!"
                        and not cls._is_escaped(value, seam_anchor - 1)
                    ):
                        seam_cursor = seam_anchor - 1
                    else:
                        seam_cursor = seam_anchor
                    seam_cursor = supported_action_anchor(
                        seam_cursor,
                        line_start=line_start,
                    )
                    while seam_cursor > line_start and value[seam_cursor - 1] in " \t":
                        seam_cursor -= 1
                    preceding_seam = seam_cursor - 1
                    if (
                        preceding_seam >= line_start
                        and value[preceding_seam] in _SEMANTIC_BARE_SEPARATORS
                        and not cls._is_escaped(value, preceding_seam)
                    ):
                        global_proven_clause_seams.append(preceding_seam)

                    envelope_end = (
                        containing_outer_ends[containing_outer_start]
                        if containing_outer_start is not None
                        else envelope_span[1]
                    )
                    if (
                        envelope_end < line_end
                        and value[envelope_end] in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT
                        and not cls._is_escaped(value, envelope_end)
                        and not (has_raw_url_context and value[envelope_end] in ",;")
                    ):
                        separate_url_reopen_positions.add(envelope_end)
                        global_proven_clause_seams.append(envelope_end)

                # Every complete, non-image Markdown reference directly owned by
                # an ordinary outer envelope is a path-label hole, not just the
                # reference whose destination happened to contain the URL label.
                # Both inventories are ordered, so each destination is assigned at
                # most once even for dense sibling wrappers.
                def outer_reference_has_termination(
                    markdown_reference: _MarkdownReference,
                    containing_outer_start: int,
                    physical_line_end: int = line_end,
                ) -> bool:
                    reference_end = markdown_reference.span[1]
                    if cls._has_reference_termination(
                        value,
                        reference_end,
                        allow_cjk_continuation=True,
                    ):
                        return True
                    slash_cursor = reference_end
                    while slash_cursor < physical_line_end and value[slash_cursor] == "\\":
                        slash_cursor += 1
                    if slash_cursor == reference_end:
                        return False
                    if slash_cursor == physical_line_end:
                        return True
                    expected_closer = _STRUCTURED_URL_VALUE_PAIRS.get(value[containing_outer_start])
                    if expected_closer is None or value[slash_cursor] != expected_closer:
                        return False
                    if (slash_cursor - reference_end) % 2 == 1:
                        return True
                    return cls._has_reference_termination(
                        value,
                        slash_cursor + 1,
                        allow_cjk_continuation=True,
                    )

                outer_reference_index = first_line_destination
                for (
                    containing_outer_start,
                    containing_outer_wrapper_end,
                ) in containing_outer_wrapper_ends.items():
                    while (
                        outer_reference_index < last_line_destination
                        and destination_records[outer_reference_index][0].span[1]
                        <= containing_outer_start
                    ):
                        outer_reference_index += 1
                    reference_index = outer_reference_index
                    outer_is_raw_owned = containing_outer_raw_url_context[containing_outer_start]
                    while reference_index < last_line_destination:
                        markdown_reference, malformed_markdown = destination_records[
                            reference_index
                        ]
                        if markdown_reference.span[0] >= containing_outer_wrapper_end:
                            break
                        label_span = markdown_reference.label_span
                        if (
                            not outer_is_raw_owned
                            and containing_outer_start <= markdown_reference.span[0]
                            and markdown_reference.span[1] <= containing_outer_wrapper_end
                            and not markdown_reference.image
                            and label_span not in raw_uri_owned_markdown_label_spans
                            and outer_reference_has_termination(
                                markdown_reference,
                                containing_outer_start,
                            )
                            and (
                                not malformed_markdown or label_span in malformed_outer_label_holes
                            )
                        ):
                            containing_outer_label_holes[containing_outer_start].append(label_span)
                            recoverable_markdown_url_label_spans.add(label_span)
                        reference_index += 1
                    outer_reference_index = reference_index

                for containing_outer_start, containing_outer_end in containing_outer_ends.items():
                    opaque_cursor = containing_outer_start
                    for label_start, label_end in containing_outer_label_holes[
                        containing_outer_start
                    ]:
                        if opaque_cursor < label_start:
                            structured_url_assignment_envelope_spans.append(
                                (opaque_cursor, label_start)
                            )
                        opaque_cursor = max(opaque_cursor, label_end)
                    if opaque_cursor < containing_outer_end:
                        structured_url_assignment_envelope_spans.append(
                            (opaque_cursor, containing_outer_end)
                        )
        global_proven_clause_seam_positions = tuple(sorted(set(global_proven_clause_seams)))

        def containing_line_clause(position: int) -> tuple[int, int] | None:
            clause_index = bisect_left(line_clause_spans, (position + 1, -1)) - 1
            if clause_index < 0:
                return None
            clause = line_clause_spans[clause_index]
            return clause if clause[0] <= position < clause[1] else None

        def token_clause_spans(match: re.Match[str]) -> tuple[tuple[int, int], ...]:
            cached = url_clause_spans_by_token_start.get(match.start())
            if cached is not None:
                return cached
            base_clauses = cls._same_token_list_clauses(match.group())
            token_local_clause_spans_by_token_start[match.start()] = base_clauses
            first_seam = bisect_left(global_proven_clause_seam_positions, match.start())
            last_seam = bisect_left(global_proven_clause_seam_positions, match.end())
            if first_seam == last_seam:
                url_clause_spans_by_token_start[match.start()] = base_clauses
                return base_clauses

            relative_seams = tuple(
                seam - match.start()
                for seam in global_proven_clause_seam_positions[first_seam:last_seam]
            )
            refined: list[tuple[int, int]] = []
            for clause_start, clause_end in base_clauses:
                cursor = clause_start
                seam_index = bisect_left(relative_seams, clause_start)
                while seam_index < len(relative_seams):
                    seam = relative_seams[seam_index]
                    if seam >= clause_end:
                        break
                    if cursor < seam:
                        refined.append((cursor, seam))
                    cursor = seam + 1
                    seam_index += 1
                if cursor < clause_end:
                    refined.append((cursor, clause_end))
            result = tuple(refined)
            url_clause_spans_by_token_start[match.start()] = result
            return result

        attached_cjk_clause_cache: dict[
            tuple[int, int], tuple[int, _ParsedFileReference] | None
        ] = {}

        def physical_line_end(position: int) -> int:
            line_break_index = bisect_left(line_break_positions, position)
            return (
                line_break_positions[line_break_index]
                if line_break_index < len(line_break_positions)
                else len(value)
            )

        def attached_cjk_clause(
            start: int,
            end: int,
        ) -> tuple[int, _ParsedFileReference] | None:
            clause_span = (start, end)
            if clause_span in attached_cjk_clause_cache:
                return attached_cjk_clause_cache[clause_span]
            result = cls._attached_cjk_ambiguous_reference(
                value,
                start=start,
                end=end,
                line_end=physical_line_end(end),
                path_separator_positions=path_separator_positions,
                continuation_boundary_positions=continuation_boundary_positions,
            )
            attached_cjk_clause_cache[clause_span] = result
            return result

        # Seed syntax that is opaque independently of issue prose, then protect
        # every canonical URL assignment from left to right before any reference
        # parser can accept its value. A preceding URL value may contain text that
        # merely looks like another label, so candidates already inside a protected
        # value are ignored. Dedicated phases keep both insertion runs monotonic.
        for span in cls._html_url_attribute_value_spans(value):
            cls._add_protected_span(protected_spans, span)
        protected_spans.begin_phase()
        for span in sorted(set(structured_url_assignment_envelope_spans)):
            cls._add_protected_span(protected_spans, span)
        protected_spans.begin_phase()
        url_label_starts: list[int] = []
        url_label_token_index = 0
        for label_start, label_end in url_label_candidates:
            label_span = (label_start, label_end)
            if cls._span_starts_in_protected(label_span, protected_spans):
                continue
            while (
                url_label_token_index < len(token_matches)
                and token_matches[url_label_token_index].end() <= label_start
            ):
                url_label_token_index += 1
            value_line_end = physical_line_end(label_end)
            clause_reopen_position: int | None = None
            shared_clause = containing_line_clause(label_start)
            if shared_clause is not None and label_end <= shared_clause[1]:
                value_line_end = min(value_line_end, shared_clause[1])
            if url_label_token_index < len(token_matches):
                containing_token = token_matches[url_label_token_index]
                if containing_token.start() <= label_start and label_end <= containing_token.end():
                    prepass_clause_spans = token_clause_spans(containing_token)
                    relative_label_start = label_start - containing_token.start()
                    clause_index = (
                        bisect_left(prepass_clause_spans, (relative_label_start + 1, -1)) - 1
                    )
                    if clause_index >= 0:
                        clause_start, clause_end = prepass_clause_spans[clause_index]
                        if clause_start <= relative_label_start < clause_end:
                            absolute_clause_end = containing_token.start() + clause_end
                            if absolute_clause_end < containing_token.end():
                                value_line_end = absolute_clause_end
                                clause_reopen_position = absolute_clause_end
            url_label_starts.append(label_start)
            cls._add_protected_span(protected_spans, label_span)
            reopen_position = cls._protect_separate_url_value(
                value,
                value[label_start:label_end],
                label_end,
                protected_spans,
                containing_token_end=label_end,
                line_end=value_line_end,
            )
            if reopen_position is not None:
                separate_url_reopen_positions.add(reopen_position)
            if clause_reopen_position is not None:
                separate_url_reopen_positions.add(clause_reopen_position)
        url_assignment_prepass_protected_spans = protected_spans.copy()
        protected_spans.begin_phase()

        # URL-shaped values must be opaque before Markdown, labels, or wrappers are
        # considered. Otherwise a path-looking substring accepted by an earlier
        # parser branch cannot be retracted later.
        for match in token_matches:
            overlaps = cls._overlapping_spans(match.span(), protected_spans)
            if overlaps and overlaps[0][0] <= match.start() and match.end() <= overlaps[0][1]:
                continue
            raw_token = match.group()
            clause_spans: tuple[tuple[int, int], ...] | None = None
            url_label_end: int | None = None
            if _URL_LABEL_TOKEN.fullmatch(raw_token) is not None:
                url_label_end = len(raw_token)
            elif _URL_LABEL_TOKEN.match(raw_token) is not None:
                delimiter_index = next(
                    (index for index, character in enumerate(raw_token) if character in "=:："),
                    -1,
                )
                candidate_end = delimiter_index + 1
                if (
                    candidate_end > 0
                    and _URL_LABEL_TOKEN.fullmatch(raw_token[:candidate_end]) is not None
                ):
                    url_label_end = candidate_end
            if url_label_end is not None and cls._span_starts_in_protected(
                (match.start(), match.start() + url_label_end),
                protected_spans,
            ):
                # The shared physical-line scan already owns a canonical label
                # inside an outer structured assignment. Reprocessing the token
                # from that inner label would forget the opener in an earlier
                # whitespace token and could protect a valid clause after the
                # envelope's proven closing seam.
                url_label_end = None
            if url_label_end is not None:
                absolute_label_end = match.start() + url_label_end
                if url_label_end < len(raw_token):
                    clause_spans = token_clause_spans(match)
                    first_clause_end = clause_spans[0][1]
                    cls._add_protected_span(
                        protected_spans,
                        (match.start(), match.start() + first_clause_end),
                    )
                    reopen_position = cls._protect_separate_url_value(
                        value,
                        raw_token[:url_label_end],
                        absolute_label_end,
                        protected_spans,
                        containing_token_end=absolute_label_end,
                        line_end=physical_line_end(absolute_label_end),
                    )
                    if reopen_position is not None:
                        separate_url_reopen_positions.add(reopen_position)
                else:
                    cls._add_protected_span(protected_spans, match.span())
                    reopen_position = cls._protect_separate_url_value(
                        value,
                        raw_token,
                        match.end(),
                        protected_spans,
                        containing_token_end=match.end(),
                        line_end=physical_line_end(match.end()),
                    )
                    if reopen_position is not None:
                        separate_url_reopen_positions.add(reopen_position)
                    continue
            if raw_token.startswith(_COMPACT_PATH_SIGIL):
                clause_spans = token_clause_spans(match)
                for compact_clause_start, compact_clause_end in clause_spans:
                    compact_clause = raw_token[compact_clause_start:compact_clause_end]
                    if not compact_clause.startswith(_COMPACT_PATH_SIGIL):
                        continue
                    compact_action_candidate = cls._labeled_reference_candidate(compact_clause)
                    if compact_action_candidate is None:
                        continue
                    compact_path, compact_path_offset = compact_action_candidate
                    compact_path_end = compact_path_offset + len(compact_path)
                    absolute_compact_start = match.start() + compact_clause_start
                    if cls._span_starts_in_protected(
                        (absolute_compact_start, match.start() + compact_clause_end),
                        protected_spans,
                    ):
                        continue
                    if cls._wrapper_has_url_context(
                        value,
                        absolute_compact_start,
                        protected_spans,
                        absolute_compact_start,
                    ):
                        continue
                    if (
                        cls._attached_cjk_location_suffix_span(
                            compact_clause,
                            compact_path_end,
                            line_end=len(compact_clause),
                        )
                        is None
                    ):
                        continue
                    found.append(
                        (
                            absolute_compact_start + compact_path_offset,
                            absolute_compact_start + compact_path_end,
                            _ParsedFileReference(compact_path, True),
                        )
                    )
                    # The explicit compact operand is authoritative, but action
                    # prose after it remains opaque. This prevents a URL fragment
                    # or Markdown construct in that suffix from becoming a second
                    # repository target.
                    cls._add_protected_span(
                        protected_spans,
                        (
                            absolute_compact_start,
                            match.start() + compact_clause_end,
                        ),
                    )
            folded_raw_token = raw_token.casefold()
            could_contain_supported_suffix = any(
                suffix in folded_raw_token for suffix in _SUPPORTED_REFERENCE_SUFFIXES
            )
            if could_contain_supported_suffix and cls._has_opaque_same_token_reference_continuation(
                raw_token
            ):
                # The token-wide probe keeps the overwhelmingly common safe path
                # allocation-free. Only a token with an actual opaque continuation
                # needs the wrapper-aware split and per-clause confirmation.
                clause_spans = cls._same_token_list_clauses(raw_token)
                for clause_start, clause_end in clause_spans:
                    clause_text = raw_token[clause_start:clause_end]
                    if not cls._has_opaque_same_token_reference_continuation(clause_text):
                        continue
                    absolute_clause_start = match.start() + clause_start
                    absolute_clause_end = match.start() + clause_end
                    if cls._spans_overlap(
                        (absolute_clause_start, absolute_clause_end),
                        protected_spans,
                    ):
                        continue
                    attached_clause = attached_cjk_clause(
                        absolute_clause_start,
                        absolute_clause_end,
                    )
                    if (
                        attached_clause is not None
                        and cls._location_ambiguity_precedes_opaque_suffix(
                            value,
                            attached_clause[1],
                        )
                    ):
                        continue
                    # Poison only the malformed top-level clause. A completed value
                    # followed by `;Update:...` or another explicit list item must be
                    # able to reopen without reviving any part of the rejected clause.
                    cls._add_protected_span(
                        protected_spans,
                        (absolute_clause_start, absolute_clause_end),
                    )
            if raw_token.startswith(_COMPACT_PATH_SIGIL):
                # A leading compact label is parsed before URL-shape heuristics;
                # explicit URL-label/wrapper context is still rejected below. Keep
                # the clause layout so a valid target after a hard semantic seam
                # cannot inherit URL context from the rejected clause.
                continue
            has_url_envelope_marker = (
                "/" in raw_token
                or "?" in raw_token
                or "#" in raw_token
                or "=" in raw_token
                or ":" in raw_token
                or "：" in raw_token
            )
            has_clause_separator = has_url_envelope_marker and any(
                separator in raw_token for separator in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT
            )
            if has_clause_separator:
                if clause_spans is None:
                    clause_spans = token_clause_spans(match)
                for clause_start, clause_end in clause_spans:
                    absolute_clause_start = match.start() + clause_start
                    absolute_clause_end = match.start() + clause_end
                    analysis_start = cls._unprotected_clause_start(
                        absolute_clause_start,
                        absolute_clause_end,
                        protected_spans,
                        value,
                    )
                    if analysis_start >= absolute_clause_end or not cls._is_opaque_url_clause(
                        value[analysis_start:absolute_clause_end]
                    ):
                        continue
                    if (
                        attached_cjk_clause(
                            analysis_start,
                            absolute_clause_end,
                        )
                        is not None
                    ):
                        continue
                    cls._add_protected_span(
                        protected_spans,
                        (analysis_start, absolute_clause_end),
                    )
            if (
                has_url_envelope_marker
                and any(dot in raw_token for dot in _IDNA_DOT_CHARACTERS)
                and ("/" in raw_token or "?" in raw_token or "#" in raw_token)
            ):
                if clause_spans is None:
                    clause_spans = token_clause_spans(match)
                for clause_start, clause_end in clause_spans:
                    absolute_clause_start = match.start() + clause_start
                    absolute_clause_end = match.start() + clause_end
                    analysis_start = cls._unprotected_clause_start(
                        absolute_clause_start,
                        absolute_clause_end,
                        protected_spans,
                        value,
                    )
                    clause_text = value[analysis_start:absolute_clause_end]
                    if not clause_text:
                        continue
                    if (
                        attached_cjk_clause(
                            analysis_start,
                            absolute_clause_end,
                        )
                        is not None
                    ):
                        continue
                    for span_start, _ in cls._idna_url_spans(clause_text):
                        # Once an IDNA authority/path is established, keep the rest
                        # of its lexical clause opaque. This also fails closed when
                        # an unbalanced wrapper makes every separator non-top-level.
                        cls._add_protected_span(
                            protected_spans,
                            (
                                analysis_start + span_start,
                                absolute_clause_end,
                            ),
                        )

        # A structured URL value may begin in a separate whitespace token. The
        # token-local splitter then cannot see the URL label to the left, so an
        # immediately following bare path needs one bounded recovery probe at
        # the already proven protected-span seam. Keep the probe capped at the
        # repository-reference limit instead of repartitioning every URL token.
        reopen_positions = tuple(sorted(separate_url_reopen_positions))
        reopen_index = 0
        for match in token_matches:
            while (
                reopen_index < len(reopen_positions)
                and reopen_positions[reopen_index] < match.start()
            ):
                reopen_index += 1
            while (
                reopen_index < len(reopen_positions)
                and reopen_positions[reopen_index] < match.end()
            ):
                reopen_position = reopen_positions[reopen_index]
                reopen_index += 1
                recovered_candidate_start = reopen_position + 1
                probe_end = min(match.end(), recovered_candidate_start + 512)
                probe = value[recovered_candidate_start:probe_end]
                probe_segments = cls._bare_reference_segments(probe)
                if not probe_segments or probe_segments[0][0] != 0:
                    continue
                _, segment_end = probe_segments[0]
                segment_text = probe[:segment_end]
                if segment_text.startswith(_COMPACT_PATH_SIGIL):
                    # Compact operands are owned by the labeled-reference branch,
                    # which strips the sigil and validates its action suffix. The
                    # generic seam recovery must not emit a second `@path` alias.
                    continue
                bare_candidate = cls._bare_reference_candidate(segment_text)
                if bare_candidate is None:
                    continue
                if any(
                    character in _ACTION_WRAPPER_OPENERS or character in _REFERENCE_WRAPPER_CLOSERS
                    for character in bare_candidate
                ):
                    # Wrapped and Markdown operands belong to their dedicated
                    # parsers, which retain the surrounding action grammar.
                    continue
                if cls._is_opaque_url_clause(segment_text):
                    # A semantic seam ends the preceding structured value, but
                    # it does not turn a second URL-shaped token into a repository
                    # path. Keep the recovered segment opaque just as the ordinary
                    # URL pass would when parsing it in isolation.
                    suppressed_recovered_reference_starts.add(recovered_candidate_start)
                    continue
                recovered_attached_cjk_reference = attached_cjk_clause(
                    recovered_candidate_start,
                    recovered_candidate_start + segment_end,
                )
                if recovered_attached_cjk_reference is None:
                    recovered_candidate = bare_candidate
                    if "/" not in recovered_candidate:
                        suppressed_recovered_reference_starts.add(recovered_candidate_start)
                        continue
                    recovered_candidate_end = recovered_candidate_start + len(recovered_candidate)
                    recovered_ambiguous_cjk_prefix = cls._attached_cjk_prefix(recovered_candidate)
                    recovered_ambiguous_cjk_suffix_span = None
                else:
                    recovered_candidate_end, recovered_attached_reference = (
                        recovered_attached_cjk_reference
                    )
                    recovered_candidate = recovered_attached_reference.path
                    recovered_ambiguous_cjk_prefix = (
                        recovered_attached_reference.ambiguous_cjk_prefix
                    )
                    recovered_ambiguous_cjk_suffix_span = (
                        recovered_attached_reference.ambiguous_cjk_suffix_span
                    )
                if not cls._has_reference_termination(
                    value,
                    recovered_candidate_end,
                    allow_cjk_continuation=True,
                ):
                    suppressed_recovered_reference_starts.add(recovered_candidate_start)
                    continue
                recovered_target_eligible = cls._has_path_action_context(
                    value,
                    recovered_candidate_start,
                    recovered_candidate_end,
                ) or ("/" in recovered_candidate and recovered_ambiguous_cjk_prefix is None)
                found.append(
                    (
                        recovered_candidate_start,
                        recovered_candidate_end,
                        _ParsedFileReference(
                            recovered_candidate,
                            target_eligible=recovered_target_eligible,
                            ambiguous_cjk_prefix=(
                                None
                                if recovered_target_eligible
                                else recovered_ambiguous_cjk_prefix
                            ),
                            ambiguous_cjk_suffix_span=(
                                None
                                if recovered_target_eligible
                                else recovered_ambiguous_cjk_suffix_span
                            ),
                        ),
                    )
                )
                # The normal bare pass sees the seam character and candidate in
                # one coarse token. Its separator-prefixed alias is discarded at
                # convergence by the exact proven seam position, without inserting
                # earlier spans into the already-sorted protection inventory.

        def containing_clause_start(match: re.Match[str], position: int) -> int:
            clause_spans = url_clause_spans_by_token_start.get(match.start())
            clause_start_position = match.start()
            if clause_spans is not None:
                relative_position = position - match.start()
                clause_index = (
                    bisect_left(
                        clause_spans,
                        (relative_position + 1, -1),
                    )
                    - 1
                )
                if clause_index >= 0:
                    clause_start, clause_end = clause_spans[clause_index]
                    if clause_start <= relative_position < clause_end:
                        clause_start_position = match.start() + clause_start

            shared_line_clause = containing_line_clause(position)
            if shared_line_clause is not None:
                clause_start_position = max(
                    clause_start_position,
                    shared_line_clause[0],
                )

            reopen_index = bisect_left(reopen_positions, position) - 1
            if reopen_index < 0:
                return clause_start_position
            reopen_position = reopen_positions[reopen_index]
            line_break_index = bisect_left(line_break_positions, position)
            physical_line_start = (
                line_break_positions[line_break_index - 1] + 1 if line_break_index > 0 else 0
            )
            url_label_index = bisect_left(url_label_starts, position) - 1
            if (
                url_label_index >= 0
                and url_label_starts[url_label_index] > reopen_position
                and url_label_starts[url_label_index] >= physical_line_start
            ):
                return url_label_starts[url_label_index]
            if reopen_position < physical_line_start:
                return clause_start_position
            return max(clause_start_position, reopen_position + 1)

        non_markdown_protected_spans = protected_spans.copy()
        protected_spans.begin_phase()
        for span in markdown_spans:
            cls._add_protected_span(protected_spans, span)
        malformed_markdown_token_index = 0
        for (
            markdown_start,
            label_start,
            label_end,
            malformed_end,
        ) in sorted(set(malformed_url_assignment_markdown_references)):
            label_span = (label_start, label_end)
            if cls._spans_overlap(label_span, url_assignment_prepass_protected_spans):
                continue
            while (
                malformed_markdown_token_index < len(token_matches)
                and token_matches[malformed_markdown_token_index].end() <= markdown_start
            ):
                malformed_markdown_token_index += 1
            if malformed_markdown_token_index >= len(token_matches):
                break
            containing_token = token_matches[malformed_markdown_token_index]
            if not (containing_token.start() <= markdown_start < containing_token.end()):
                continue
            if (
                label_span not in recoverable_markdown_url_label_spans
                and cls._wrapper_has_url_context(
                    value,
                    markdown_start,
                    url_assignment_prepass_protected_spans,
                    containing_clause_start(containing_token, markdown_start),
                )
            ):
                continue
            if (
                label_span not in recoverable_markdown_url_label_spans
                and not cls._has_reference_termination(
                    value,
                    malformed_end,
                    allow_cjk_continuation=True,
                )
            ):
                continue
            candidate = cls._reference_candidate(value[label_start:label_end])
            if candidate is not None and cls._candidate_has_unambiguous_shape(
                candidate,
                allow_spaced_root=True,
            ):
                found.append(
                    (
                        label_start,
                        malformed_end,
                        _ParsedFileReference(candidate, True),
                    )
                )
        markdown_token_index = 0
        for markdown_reference in markdown_references:
            if markdown_reference.image:
                continue
            label_is_recoverable = (
                markdown_reference.label_span in recoverable_markdown_url_label_spans
            )
            if cls._spans_overlap(
                markdown_reference.label_span,
                non_markdown_protected_spans,
            ) and (
                not label_is_recoverable
                or cls._spans_overlap(
                    markdown_reference.label_span,
                    url_assignment_prepass_protected_spans,
                )
            ):
                continue
            while (
                markdown_token_index < len(token_matches)
                and token_matches[markdown_token_index].end() <= markdown_reference.span[0]
            ):
                markdown_token_index += 1
            if markdown_token_index >= len(token_matches):
                break
            containing_token = token_matches[markdown_token_index]
            if not label_is_recoverable and cls._wrapper_has_url_context(
                value,
                markdown_reference.span[0],
                protected_spans,
                containing_clause_start(
                    containing_token,
                    markdown_reference.span[0],
                ),
            ):
                continue
            if not label_is_recoverable and not cls._has_reference_termination(
                value,
                markdown_reference.span[1],
                allow_cjk_continuation=True,
            ):
                continue
            label_start, label_end = markdown_reference.label_span
            candidate = cls._reference_candidate(value[label_start:label_end])
            if candidate is not None and cls._candidate_has_unambiguous_shape(
                candidate,
                allow_spaced_root=True,
            ):
                found.append(
                    (
                        label_start,
                        markdown_reference.span[1],
                        _ParsedFileReference(candidate, True),
                    )
                )

        protected_spans.begin_phase()
        bare_segment_layouts: list[tuple[tuple[int, int], ...]] = []
        for match in token_matches:
            raw_token = match.group()
            shared_clauses: tuple[tuple[int, int], ...] | None = None
            if raw_token.startswith(_COMPACT_PATH_SIGIL):
                shared_clauses = token_clause_spans(match)
            elif global_proven_clause_seam_positions:
                first_shared_seam = bisect_left(
                    global_proven_clause_seam_positions,
                    match.start(),
                )
                if (
                    first_shared_seam < len(global_proven_clause_seam_positions)
                    and global_proven_clause_seam_positions[first_shared_seam] < match.end()
                ):
                    shared_clauses = token_clause_spans(match)
            if raw_token.startswith(_COMPACT_PATH_SIGIL) or shared_clauses is not None:
                assert shared_clauses is not None
                bare_segments = tuple(
                    (clause_start + segment_start, clause_start + segment_end)
                    for clause_start, clause_end in shared_clauses
                    for segment_start, segment_end in cls._bare_reference_segments(
                        raw_token[clause_start:clause_end]
                    )
                )
            else:
                bare_segments = cls._bare_reference_segments(raw_token)
            bare_segment_layouts.append(bare_segments)
            for segment_start, segment_end in bare_segments:
                segment_text = raw_token[segment_start:segment_end]
                segment_span = (
                    match.start() + segment_start,
                    match.start() + segment_end,
                )
                if cls._spans_overlap(segment_span, protected_spans):
                    continue
                labeled_candidate = cls._labeled_reference_candidate(segment_text)
                if labeled_candidate is None:
                    if segment_text.startswith(_COMPACT_PATH_SIGIL):
                        cls._add_protected_span(protected_spans, segment_span)
                    continue
                if not cls._has_reference_termination(
                    value,
                    segment_span[1],
                    allow_cjk_continuation=True,
                ):
                    # A compact/labeled candidate rejected against its original
                    # token boundary must not be reinterpreted by the weaker
                    # bare-reference pass after Unicode punctuation splitting.
                    cls._add_protected_span(protected_spans, segment_span)
                    continue
                candidate, candidate_offset = labeled_candidate
                if cls._wrapper_has_url_context(
                    value,
                    segment_span[0],
                    protected_spans,
                    containing_clause_start(match, segment_span[0]),
                ):
                    cls._add_protected_span(protected_spans, segment_span)
                    continue
                candidate_start = segment_span[0] + candidate_offset
                found.append(
                    (
                        candidate_start,
                        candidate_start + len(candidate),
                        _ParsedFileReference(candidate, True),
                    )
                )
                cls._add_protected_span(protected_spans, segment_span)

        protected_spans.begin_phase()
        wrapper_spans: list[tuple[int, int, int, int]] = []
        for opener, closer in _REFERENCE_WRAPPERS:
            if opener not in value or closer not in value:
                continue
            wrapper_spans.extend(cls._wrapper_spans(value, opener, closer))
        token_index = 0
        for start, content_start, end, span_end in sorted(wrapper_spans):
            while token_index < len(token_matches) and token_matches[token_index].end() <= start:
                token_index += 1
            if token_index >= len(token_matches):
                break
            containing_token = token_matches[token_index]
            clause_token_start = containing_clause_start(containing_token, start)
            span = (start, span_end)
            if cls._spans_overlap(span, protected_spans):
                continue
            if end - content_start > 500 or not cls._could_end_with_reference_suffix(
                value,
                content_start,
                end,
            ):
                continue
            if any(
                character.isspace() and character not in " \t"
                for character in value[content_start:end]
            ):
                cls._add_protected_span(protected_spans, span)
                continue
            if cls._wrapper_has_invalid_left_attachment(
                value,
                start,
                clause_token_start,
            ):
                continue
            if not cls._has_reference_termination(
                value,
                span[1],
                allow_cjk_continuation=True,
            ) and not cls._has_html_tag_value_termination(value, start, span[1]):
                continue
            candidate = cls._reference_candidate(value[content_start:end])
            if candidate is None or not cls._candidate_has_unambiguous_shape(
                candidate,
                allow_spaced_root=True,
            ):
                continue
            url_context = cls._wrapper_has_url_context(
                value,
                start,
                protected_spans,
                clause_token_start,
            )
            if url_context:
                cls._add_protected_span(protected_spans, span)
                continue
            found.append((content_start, span_end, _ParsedFileReference(candidate, True)))
            cls._add_protected_span(protected_spans, span)

        protected_spans.begin_phase()
        for match in token_matches:
            raw_token = match.group()
            if (
                "/" not in raw_token
                and "?" not in raw_token
                and "#" not in raw_token
                and ":" not in raw_token
                and _URL_LABEL_TOKEN.fullmatch(raw_token) is None
            ):
                continue
            url_clause_spans = url_clause_spans_by_token_start.get(
                match.start(), ((0, len(raw_token)),)
            )
            url_segments = (
                (clause_start, segment)
                for clause_start, clause_end in url_clause_spans
                for segment in _URL_TOKEN_SEGMENT.finditer(raw_token[clause_start:clause_end])
            )
            for clause_start, segment in url_segments:
                segment_span = (
                    match.start() + clause_start + segment.start(),
                    match.start() + clause_start + segment.end(),
                )
                segment_text = segment.group()
                if _PATH_ACTION_PREFIX.fullmatch(segment_text) is not None:
                    continue
                overlaps = cls._overlapping_spans(segment_span, protected_spans)
                line_end = physical_line_end(segment_span[1])
                attached_cjk_reference = (
                    None
                    if overlaps
                    else cls._attached_cjk_ambiguous_reference(
                        value,
                        start=segment_span[0],
                        end=segment_span[1],
                        line_end=line_end,
                        path_separator_positions=path_separator_positions,
                        continuation_boundary_positions=continuation_boundary_positions,
                    )
                )
                if attached_cjk_reference is not None:
                    # A canonical attached CJK path seam is fail-closed before
                    # URL heuristics. A later slash, query marker, fragment, or
                    # dotted action operand cannot erase an ambiguity already
                    # established at the start of this token.
                    continue
                if not overlaps and cls._looks_like_url_token(segment_text):
                    cls._add_protected_span(protected_spans, segment_span)
                    cls._protect_separate_url_value(
                        value,
                        segment_text,
                        segment_span[1],
                        protected_spans,
                        containing_token_end=match.end(),
                        line_end=line_end,
                    )
                    continue
                gap_start = segment_span[0]
                for protected_start, protected_end in (
                    *overlaps,
                    (segment_span[1], segment_span[1]),
                ):
                    if gap_start < protected_start:
                        gap_text = value[gap_start:protected_start]
                        if _PATH_ACTION_PREFIX.fullmatch(
                            gap_text
                        ) is None and cls._looks_like_url_token(gap_text):
                            cls._add_protected_span(
                                protected_spans,
                                (gap_start, segment_span[1]),
                            )
                            cls._protect_separate_url_value(
                                value,
                                value[gap_start : segment_span[1]],
                                segment_span[1],
                                protected_spans,
                                containing_token_end=match.end(),
                                line_end=line_end,
                            )
                            break
                    gap_start = max(gap_start, protected_end)

        for match, bare_segments in zip(token_matches, bare_segment_layouts, strict=True):
            raw_token = match.group()
            for segment_start, segment_end in bare_segments:
                segment_span = (match.start() + segment_start, match.start() + segment_end)
                if cls._spans_overlap(segment_span, protected_spans):
                    continue
                if not cls._has_reference_termination(
                    value,
                    segment_span[1],
                    allow_cjk_continuation=True,
                ):
                    continue
                line_break_index = bisect_left(line_break_positions, segment_span[1])
                line_end = (
                    line_break_positions[line_break_index]
                    if line_break_index < len(line_break_positions)
                    else len(value)
                )
                attached_cjk_reference = cls._attached_cjk_ambiguous_reference(
                    value,
                    start=segment_span[0],
                    end=segment_span[1],
                    line_end=line_end,
                    path_separator_positions=path_separator_positions,
                    continuation_boundary_positions=continuation_boundary_positions,
                )
                if attached_cjk_reference is None:
                    candidate = cls._bare_reference_candidate(raw_token[segment_start:segment_end])
                    if candidate is None:
                        continue
                    raw_segment = raw_token[segment_start:segment_end]
                    candidate_offset = raw_segment.find(candidate)
                    candidate_start = segment_span[0] + max(0, candidate_offset)
                    candidate_end = candidate_start + len(candidate)
                    ambiguous_cjk_prefix = cls._attached_cjk_prefix(candidate)
                    ambiguous_cjk_suffix_span = None
                    if ambiguous_cjk_prefix is not None:
                        operand_start = candidate_start + len(ambiguous_cjk_prefix)
                        operand = cls._compact_replay_candidate(
                            _COMPACT_PATH_SIGIL + value[operand_start : match.end()]
                        )
                        if operand is None:
                            continue
                        candidate = ambiguous_cjk_prefix + operand
                        candidate_end = candidate_start + len(candidate)
                        if cls._has_attached_path_continuation(
                            value,
                            candidate_end,
                            line_end=line_end,
                            path_separator_positions=path_separator_positions,
                            continuation_boundary_positions=continuation_boundary_positions,
                        ):
                            continue
                        if candidate_end < line_end:
                            ambiguous_cjk_suffix_span = (candidate_end, line_end)
                else:
                    candidate_end, parsed_attached_reference = attached_cjk_reference
                    candidate = parsed_attached_reference.path
                    candidate_start = segment_span[0]
                    ambiguous_cjk_prefix = parsed_attached_reference.ambiguous_cjk_prefix
                    ambiguous_cjk_suffix_span = parsed_attached_reference.ambiguous_cjk_suffix_span
                has_explicit_action_context = cls._has_path_action_context(
                    value,
                    candidate_start,
                    candidate_end,
                )
                if (
                    ambiguous_cjk_prefix in _CJK_PATH_LOCATION_PREFIXES
                    and ambiguous_cjk_suffix_span is None
                ):
                    ambiguous_cjk_suffix_span = cls._attached_cjk_location_suffix_span(
                        value,
                        candidate_end,
                        line_end=line_end,
                    )
                target_eligible = has_explicit_action_context or (
                    "/" in candidate and ambiguous_cjk_prefix is None
                )
                found.append(
                    (
                        candidate_start,
                        candidate_end,
                        _ParsedFileReference(
                            candidate,
                            target_eligible=target_eligible,
                            ambiguous_cjk_prefix=(
                                None if target_eligible else ambiguous_cjk_prefix
                            ),
                            ambiguous_cjk_suffix_span=(
                                None if target_eligible else ambiguous_cjk_suffix_span
                            ),
                        ),
                    )
                )

        references: list[_ParsedFileReference] = []
        indexes_by_path: dict[str, int] = {}
        for reference_start, action_context_end, parsed_reference in sorted(
            found, key=lambda item: item[0]
        ):
            if (
                reference_start in separate_url_reopen_positions
                or reference_start in suppressed_recovered_reference_starts
            ):
                continue
            if parsed_reference.target_eligible and "/" not in parsed_reference.path:
                action_start = reference_start
                if action_start > 0 and value[action_start - 1] in _ACTION_WRAPPER_OPENERS:
                    action_start -= 1
                resolved_target_eligible = cls._has_path_action_context(
                    value,
                    action_start,
                    action_context_end,
                )
                parsed_reference = _ParsedFileReference(
                    parsed_reference.path,
                    resolved_target_eligible,
                    (None if resolved_target_eligible else parsed_reference.ambiguous_cjk_prefix),
                    (
                        None
                        if resolved_target_eligible
                        else parsed_reference.ambiguous_cjk_suffix_span
                    ),
                )
            existing_index = indexes_by_path.get(parsed_reference.path)
            if existing_index is None:
                indexes_by_path[parsed_reference.path] = len(references)
                references.append(parsed_reference)
            elif (
                parsed_reference.target_eligible and not references[existing_index].target_eligible
            ):
                references[existing_index] = _ParsedFileReference(parsed_reference.path, True)
            elif (
                references[existing_index].ambiguous_cjk_prefix is not None
                and parsed_reference.ambiguous_cjk_prefix is None
            ):
                references[existing_index] = parsed_reference
            elif (
                references[existing_index].ambiguous_cjk_suffix_span is None
                and parsed_reference.ambiguous_cjk_suffix_span is not None
            ):
                references[existing_index] = parsed_reference
        return tuple(references)

    @classmethod
    @lru_cache(maxsize=16)
    def _bare_reference_segments(cls, value: str) -> tuple[tuple[int, int], ...]:
        segments: list[tuple[int, int]] = []
        for base in _BARE_TOKEN_SEGMENT.finditer(value):
            separator_indexes = [
                index
                for index in range(base.start(), base.end())
                if value[index] in _SEMANTIC_BARE_SEPARATORS
                or (
                    base.start() == 0
                    and value.startswith(_COMPACT_PATH_SIGIL)
                    and value[index] in _COMPACT_CLAUSE_SEPARATORS
                )
            ]
            recognized: set[int] = set()
            for position, separator_index in enumerate(separator_indexes):
                left_start = base.start() if position == 0 else separator_indexes[position - 1] + 1
                right_end = (
                    base.end()
                    if position + 1 == len(separator_indexes)
                    else separator_indexes[position + 1]
                )
                left = value[left_start:separator_index]
                right = value[separator_index + 1 : right_end]
                if value[
                    separator_index
                ] in _SEMANTIC_BARE_SEPARATORS and cls._semantic_clause_separator_is_recognized(
                    left, right
                ):
                    recognized.add(separator_index)
                    continue
                compact_clause_action = next(
                    (prefix for prefix in _CJK_PATH_ACTION_PREFIXES if right.startswith(prefix)),
                    None,
                )
                if (
                    value[separator_index] in _COMPACT_CLAUSE_SEPARATORS
                    and cls._labeled_reference_candidate(left) is not None
                    and compact_clause_action is not None
                    and cls._bare_reference_candidate(right[len(compact_clause_action) :])
                    is not None
                ):
                    recognized.add(separator_index)
                    continue
                left_candidate = cls._bare_reference_candidate(left)
                if left_candidate is not None and (
                    cls._bare_reference_candidate(right) is not None
                    or _ENGLISH_CLAUSE_CONTINUATION.match(right) is not None
                ):
                    recognized.add(separator_index)
            cursor = base.start()
            for separator_index in separator_indexes:
                if separator_index not in recognized:
                    continue
                if cursor < separator_index:
                    segments.append((cursor, separator_index))
                cursor = separator_index + 1
            if cursor < base.end():
                segments.append((cursor, base.end()))
        return tuple(segments)

    @classmethod
    def _markdown_references(
        cls,
        value: str,
    ) -> tuple[
        tuple[_MarkdownReference, ...],
        tuple[tuple[int, int], ...],
        tuple[_MarkdownReference, ...],
    ]:
        """Return complete references, opaque spans, and malformed destinations."""

        bracket_pairs = cls._balanced_delimiter_pairs(value, "[", "]")
        line_break_positions = tuple(
            index
            for index, character in enumerate(value)
            if character == "\r" or (character == "\n" and (index == 0 or value[index - 1] != "\r"))
        )

        def physical_line_boundary(start: int) -> tuple[int, int]:
            line_break_index = bisect_left(line_break_positions, start)
            if line_break_index >= len(line_break_positions):
                return len(value), len(value)
            line_end = line_break_positions[line_break_index]
            next_line_start = line_end + 1
            if (
                value[line_end] == "\r"
                and next_line_start < len(value)
                and value[next_line_start] == "\n"
            ):
                next_line_start += 1
            return line_end, next_line_start

        definition_spans: list[tuple[int, int]] = []
        for match in _MARKDOWN_REFERENCE_DEFINITION_START.finditer(value):
            opener = match.end() - 1
            closer = bracket_pairs.get(opener)
            if closer is None or closer + 1 >= len(value) or value[closer + 1] != ":":
                continue
            line_end, continuation_start = physical_line_boundary(closer + 2)
            definition_end = line_end
            destination_line = value[closer + 2 : definition_end]
            destination_found = cls._is_markdown_definition_destination_line(destination_line)
            if line_end < len(value):
                if not destination_line.strip():
                    continuation_end, after_continuation = physical_line_boundary(
                        continuation_start
                    )
                    continuation = value[continuation_start:continuation_end]
                    if cls._is_markdown_definition_destination_line(continuation):
                        definition_end = continuation_end
                        destination_found = True
                        continuation_start = after_continuation
                if destination_found and continuation_start < len(value):
                    title_end, _ = physical_line_boundary(continuation_start)
                    title_line = value[continuation_start:title_end]
                    if cls._is_markdown_definition_title_line(title_line):
                        definition_end = title_end
            if destination_found:
                definition_spans.append((match.start(), definition_end))

        references: list[_MarkdownReference] = []
        malformed_references: list[_MarkdownReference] = []
        protected_spans = list(definition_spans)
        cursor = 0
        while cursor < len(value):
            image = (
                value[cursor] == "!"
                and not cls._is_escaped(value, cursor)
                and cursor + 1 < len(value)
                and value[cursor + 1] == "["
            )
            opener = cursor + 1 if image else cursor
            if value[opener : opener + 1] != "[" or cls._is_escaped(value, opener):
                cursor += 1
                continue
            if cls._spans_overlap((cursor, opener + 1), definition_spans):
                cursor += 1
                continue
            label_closer = bracket_pairs.get(opener)
            if label_closer is None:
                cursor += 1
                continue

            span_end: int | None = None
            suffix_start = label_closer + 1
            if suffix_start < len(value) and value[suffix_start] == "(":
                destination_closer = cls._markdown_destination_closer(value, suffix_start)
                if destination_closer is not None:
                    span_end = destination_closer + 1
                else:
                    malformed_end, _ = physical_line_boundary(suffix_start + 1)
                    malformed_references.append(
                        _MarkdownReference(
                            span=(cursor, malformed_end),
                            label_span=(opener + 1, label_closer),
                            image=image,
                        )
                    )
                    protected_spans.append((cursor, malformed_end))
                    cursor = max(cursor + 1, malformed_end)
                    continue
            elif suffix_start < len(value) and value[suffix_start] == "[":
                destination_closer = bracket_pairs.get(suffix_start)
                if destination_closer is not None:
                    span_end = destination_closer + 1
                else:
                    malformed_end, _ = physical_line_boundary(suffix_start + 1)
                    malformed_references.append(
                        _MarkdownReference(
                            span=(cursor, malformed_end),
                            label_span=(opener + 1, label_closer),
                            image=image,
                        )
                    )
                    protected_spans.append((cursor, malformed_end))
                    cursor = max(cursor + 1, malformed_end)
                    continue
            elif image:
                span_end = label_closer + 1

            if span_end is None:
                cursor = label_closer + 1
                continue
            span = (cursor, span_end)
            references.append(
                _MarkdownReference(
                    span=span,
                    label_span=(opener + 1, label_closer),
                    image=image,
                )
            )
            protected_spans.append(span)
            cursor = span_end

        protected_spans.sort()
        return tuple(references), tuple(protected_spans), tuple(malformed_references)

    @staticmethod
    def _is_markdown_definition_destination_line(value: str) -> bool:
        content = value.lstrip(" \t")
        if len(value) - len(content) > 3 or not content:
            return False
        if content.startswith("<"):
            destination_end = content.find(">", 1)
            if destination_end < 0:
                return False
            remainder = content[destination_end + 1 :].strip()
        else:
            destination_end = next(
                (index for index, character in enumerate(content) if character.isspace()),
                len(content),
            )
            remainder = content[destination_end:].strip()
        return not remainder or PlanBuilder._is_complete_markdown_title(remainder)

    @staticmethod
    def _is_markdown_definition_title_line(value: str) -> bool:
        content = value.lstrip(" \t")
        return (
            len(value) - len(content) <= 3
            and bool(content)
            and PlanBuilder._is_complete_markdown_title(content)
        )

    @staticmethod
    def _is_complete_markdown_title(value: str) -> bool:
        closer_by_opener = {'"': '"', "'": "'", "(": ")"}
        closer = closer_by_opener.get(value[0])
        return closer is not None and len(value) >= 2 and value.endswith(closer)

    @classmethod
    def _markdown_destination_closer(
        cls,
        value: str,
        opener: int,
        *,
        line_end: int | None = None,
    ) -> int | None:
        depth = 1
        quote: str | None = None
        inside_angle_destination = False
        backslash_run = 0
        cursor = opener + 1
        scan_end = len(value) if line_end is None else line_end
        paired_single_quote_openers = cls._paired_single_quote_openers(
            value,
            start=cursor,
            end=scan_end,
        )
        while cursor < scan_end:
            character = value[cursor]
            if character in "\r\n":
                return None
            if character == "\\":
                backslash_run += 1
                cursor += 1
                continue
            escaped = backslash_run % 2 == 1
            backslash_run = 0
            if escaped:
                cursor += 1
                continue
            if inside_angle_destination:
                if character == ">":
                    inside_angle_destination = False
                cursor += 1
                continue
            if quote is not None:
                if character == quote:
                    if not cls._apostrophe_is_inner_word(value, cursor):
                        quote = None
                cursor += 1
                continue
            if character == "<" and depth == 1:
                inside_angle_destination = True
            elif character in {'"', "'"} and cls._opens_symmetric_quote(
                value,
                cursor,
                scan_start=opener,
                paired_single_quote_openers=paired_single_quote_openers,
            ):
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    return cursor
            cursor += 1
        return None

    @staticmethod
    def _markdown_reference_destination_closer(
        value: str,
        opener: int,
        *,
        line_end: int,
    ) -> int | None:
        depth = 1
        backslash_run = 0
        cursor = opener + 1
        while cursor < line_end:
            character = value[cursor]
            if character == "\\":
                backslash_run += 1
                cursor += 1
                continue
            escaped = backslash_run % 2 == 1
            backslash_run = 0
            if not escaped:
                if character == "[":
                    depth += 1
                elif character == "]":
                    depth -= 1
                    if depth == 0:
                        return cursor
            cursor += 1
        return None

    @staticmethod
    def _balanced_delimiter_pairs(value: str, opener: str, closer: str) -> dict[int, int]:
        stack: list[int] = []
        pairs: dict[int, int] = {}
        for index, character in enumerate(value):
            if character == opener and not PlanBuilder._is_escaped(value, index):
                stack.append(index)
            elif character == closer and not PlanBuilder._is_escaped(value, index) and stack:
                pairs[stack.pop()] = index
        return pairs

    @staticmethod
    def _wrapper_spans(
        value: str,
        opener: str,
        closer: str,
    ) -> tuple[tuple[int, int, int, int], ...]:
        spans: list[tuple[int, int, int, int]] = []
        if opener == closer:
            previous: int | None = None
            for index, character in enumerate(value):
                if character != opener or PlanBuilder._is_escaped(value, index):
                    continue
                if previous is not None:
                    spans.append((previous, previous + 1, index, index + 1))
                previous = index
            return tuple(spans)

        stack: list[int] = []
        for index, character in enumerate(value):
            if character == opener and not PlanBuilder._is_escaped(value, index):
                stack.append(index)
            elif character == closer and not PlanBuilder._is_escaped(value, index) and stack:
                start = stack.pop()
                spans.append((start, start + 1, index, index + 1))
        return tuple(spans)

    @staticmethod
    def _is_escaped(value: str, index: int) -> bool:
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and value[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        return backslashes % 2 == 1

    @staticmethod
    def _could_end_with_reference_suffix(value: str, start: int, end: int) -> bool:
        trimmed_end = end
        while trimmed_end > start and value[trimmed_end - 1].isspace():
            trimmed_end -= 1
        return any(
            value[max(start, trimmed_end - len(suffix)) : trimmed_end].casefold() == suffix
            for suffix in _SUPPORTED_REFERENCE_SUFFIXES
        )

    @classmethod
    def _wrapper_has_invalid_left_attachment(
        cls,
        value: str,
        wrapper_start: int,
        token_start: int,
    ) -> bool:
        """Reject a wrapper that is only a valid-looking suffix of one malformed token."""

        if wrapper_start <= token_start:
            return False
        preceding_character = value[wrapper_start - 1]
        if not (
            preceding_character.isascii()
            and (preceding_character.isalnum() or preceding_character in "._-")
        ):
            return False
        # A non-space token cannot carry an unbounded natural-language action
        # prefix. Keep this check constant-time for dense malformed wrappers.
        prefix_start = max(token_start, wrapper_start - 64)
        prefix = cls._strip_trailing_context_openers(value[prefix_start:wrapper_start]).lstrip(
            " \t"
        )
        return _PATH_ACTION_PREFIX.fullmatch(cls._context_suffix(prefix)) is None

    @classmethod
    def _wrapper_has_url_context(
        cls,
        value: str,
        wrapper_start: int,
        protected_spans: _ProtectedSpans,
        token_start: int,
    ) -> bool:
        bounded_by_protected_span = False
        bounded_by_clause = (
            token_start > 0
            and value[token_start - 1] in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT
            and not cls._is_escaped(value, token_start - 1)
        )
        previous_span = cls._previous_protected_span(protected_spans, wrapper_start)
        if previous_span is not None:
            if token_start < previous_span[1] <= wrapper_start:
                token_start = previous_span[1]
                bounded_by_protected_span = True
            elif (
                previous_span[1] < token_start
                and value[previous_span[1]] in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT
                and not cls._is_escaped(value, previous_span[1])
                and not value[previous_span[1] + 1 : token_start].strip(" \t")
            ):
                bounded_by_clause = True
        same_token_prefix = value[token_start:wrapper_start]
        boundary_index = cls._reference_context_boundary(same_token_prefix)
        has_internal_clause_boundary = boundary_index >= 0
        bounded_by_clause = bounded_by_clause or has_internal_clause_boundary
        if has_internal_clause_boundary:
            token_start += boundary_index + 1
            same_token_prefix = same_token_prefix[boundary_index + 1 :]
        path_context_prefix = cls._strip_trailing_context_openers(same_token_prefix)
        while (
            path_context_prefix
            and path_context_prefix[-1] in _ACTION_WRAPPER_OPENERS
            and not cls._is_escaped(path_context_prefix, len(path_context_prefix) - 1)
        ):
            path_context_prefix = path_context_prefix[:-1]
        if same_token_prefix:
            exact_path_action = (
                _PATH_ACTION_PREFIX.fullmatch(path_context_prefix) is not None
                or _CJK_PATH_LOCATION_CONTEXT.fullmatch(path_context_prefix) is not None
            )
            if exact_path_action:
                return False
            if cls._looks_like_url_token(
                same_token_prefix
            ) or same_token_prefix.casefold().startswith(("url=", "url:")):
                return True

        if bounded_by_protected_span or bounded_by_clause:
            return False
        context_start = (
            token_start if same_token_prefix and not path_context_prefix else wrapper_start
        )
        preceding_end = context_start
        newline_count = 0
        while preceding_end > 0 and value[preceding_end - 1].isspace():
            preceding_end -= 1
            if value[preceding_end] in "\r\n":
                newline_count += 1
        if newline_count > 1:
            return False
        if newline_count and not cls._has_multiline_html_url_attribute_context(
            value, wrapper_start
        ):
            return False
        if preceding_end == 0:
            return False
        while True:
            previous_start = preceding_end
            while previous_start > 0 and not value[previous_start - 1].isspace():
                previous_start -= 1
            unprotected_previous_start = cls._unprotected_clause_start(
                previous_start,
                preceding_end,
                protected_spans,
                value,
            )
            raw_previous_token = cls._strip_trailing_context_openers(
                value[unprotected_previous_start:preceding_end]
            )
            previous_token = cls._top_level_semantic_action_context(raw_previous_token)
            if previous_token is None:
                previous_token = cls._context_suffix(raw_previous_token)
            if previous_token:
                break
            if raw_previous_token != previous_token:
                return False
            preceding_end = cls._rstrip_inline_whitespace(value, previous_start)
            if preceding_end == 0 or value[preceding_end - 1] in "\r\n":
                return False
        if _PATH_ACTION_PREFIX.fullmatch(previous_token) is not None:
            return False
        if previous_token.endswith(("=", ":", "：")) and cls._looks_like_url_token(previous_token):
            return True
        if previous_token.casefold() in {"url", "url=", "url:", "url："}:
            return True
        if previous_token not in {"=", ":", "："}:
            return False
        before_equals_end = cls._rstrip_inline_whitespace(value, previous_start)
        if before_equals_end == 0 or value[before_equals_end - 1] in "\r\n":
            return False
        url_start = before_equals_end
        while url_start > 0 and not value[url_start - 1].isspace():
            url_start -= 1
        return cls._looks_like_url_token(cls._context_suffix(value[url_start:before_equals_end]))

    @staticmethod
    def _strip_trailing_context_openers(value: str) -> str:
        end = len(value)
        while end > 0 and value[end - 1] in _WRAPPER_CONTEXT_OPENERS:
            end -= 1
        return value[:end]

    @classmethod
    def _context_suffix(cls, value: str) -> str:
        boundary_index = cls._reference_context_boundary(value)
        return value[boundary_index + 1 :]

    @staticmethod
    def _has_multiline_html_url_attribute_context(value: str, wrapper_start: int) -> bool:
        window_start = max(0, wrapper_start - 500)
        prefix = value[window_start:wrapper_start]
        tag_start = prefix.rfind("<")
        if tag_start < 0 or tag_start < prefix.rfind(">"):
            return False
        return (
            re.search(
                r"(?i)(?<![a-z0-9_-])(?:href|src)\s*=\s*$",
                prefix[tag_start + 1 :],
            )
            is not None
        )

    @classmethod
    def _has_path_action_context(cls, value: str, start: int, end: int) -> bool:
        """Recognize a bounded action in the same line as a root path."""

        window_start = max(0, start - _MAX_PATH_ACTION_CONTEXT)
        line_start = (
            max(
                value.rfind("\n", window_start, start),
                value.rfind("\r", window_start, start),
            )
            + 1
        )
        if line_start == 0:
            line_start = window_start

        prefix = cls._strip_trailing_context_openers(value[line_start:start])
        raw_action_context = cls._context_suffix(prefix)
        has_trusted_left_boundary = (
            window_start == 0 or line_start > window_start or len(raw_action_context) < len(prefix)
        )
        semantic_boundary = max(
            (prefix.rfind(separator) for separator in _SEMANTIC_BARE_SEPARATORS),
            default=-1,
        )
        if semantic_boundary >= 0:
            semantic_action_context = prefix[semantic_boundary + 1 :].lstrip(" \t")
            if (
                _PATH_ACTION_PREFIX.fullmatch(semantic_action_context) is not None
                or _CJK_PATH_LOCATION_CONTEXT.fullmatch(semantic_action_context) is not None
            ):
                raw_action_context = semantic_action_context
                has_trusted_left_boundary = True
        ascii_sentence_boundary = None
        for match in _ASCII_SENTENCE_BOUNDARY.finditer(prefix):
            ascii_sentence_boundary = match
        if ascii_sentence_boundary is not None and ascii_sentence_boundary.end() > len(
            prefix
        ) - len(raw_action_context):
            raw_action_context = prefix[ascii_sentence_boundary.end() :]
            has_trusted_left_boundary = True
        action_context = raw_action_context.lstrip(" \t")
        if has_trusted_left_boundary and _PATH_ACTION_PREFIX.fullmatch(action_context) is not None:
            return True

        location_context = raw_action_context.lstrip(" \t")
        if (
            not has_trusted_left_boundary
            or _CJK_PATH_LOCATION_CONTEXT.fullmatch(location_context) is None
        ):
            return False

        return cls._attached_cjk_location_suffix_span(value, end) is not None

    @staticmethod
    def _attached_cjk_location_suffix_span(
        value: str,
        path_end: int,
        *,
        line_end: int | None = None,
    ) -> tuple[int, int] | None:
        probe_end = min(len(value), path_end + _MAX_PATH_ACTION_CONTEXT)
        if line_end is None:
            following_line_breaks = tuple(
                position
                for separator in ("\n", "\r")
                if (position := value.find(separator, path_end, probe_end)) >= 0
            )
            line_end = min(following_line_breaks, default=probe_end)
        probe_end = min(probe_end, line_end)
        suffix_probe = value[path_end:probe_end]
        cursor = 0
        while cursor < len(suffix_probe) and suffix_probe[cursor] in " \t":
            cursor += 1
        if cursor < len(suffix_probe) and suffix_probe[cursor] in "中内里的":
            cursor += 1
            while cursor < len(suffix_probe) and suffix_probe[cursor] in " \t":
                cursor += 1
        for action_prefix in _CJK_PATH_ACTION_PREFIXES:
            if suffix_probe.startswith(action_prefix, cursor):
                return (path_end, line_end)
        return None

    @classmethod
    def _location_ambiguity_precedes_opaque_suffix(
        cls,
        value: str,
        reference: _ParsedFileReference,
    ) -> bool:
        """Keep an earlier location ambiguity when later prose resembles a query."""

        if reference.ambiguous_cjk_prefix not in _CJK_PATH_LOCATION_PREFIXES:
            return False
        suffix_span = reference.ambiguous_cjk_suffix_span
        if suffix_span is None:
            return False
        suffix = value[slice(*suffix_span)]
        cursor = 0
        while cursor < len(suffix) and suffix[cursor] in " \t":
            cursor += 1
        if cursor < len(suffix) and suffix[cursor] in "中内里的":
            cursor += 1
            while cursor < len(suffix) and suffix[cursor] in " \t":
                cursor += 1
        action_prefix = next(
            (prefix for prefix in _CJK_PATH_ACTION_PREFIXES if suffix.startswith(prefix, cursor)),
            None,
        )
        if action_prefix is None:
            return False
        remainder = suffix[cursor + len(action_prefix) :]
        for marker in "?!":
            marker_index = remainder.find(marker)
            if marker_index <= 0:
                continue
            nested_candidate = cls._reference_candidate(remainder[:marker_index])
            prose_tail = remainder[marker_index + 1 :]
            if (
                nested_candidate is not None
                and cls._candidate_has_unambiguous_shape(
                    nested_candidate,
                    allow_spaced_root=True,
                )
                and prose_tail
                and "\u3400" <= prose_tail[0] <= "\u9fff"
                and "/" not in prose_tail
                and "\\" not in prose_tail
            ):
                return True
        return False

    @classmethod
    def _html_url_attribute_value_spans(cls, value: str) -> tuple[tuple[int, int], ...]:
        """Return opaque href/src spans, failing closed for incomplete tags."""

        spans: list[tuple[int, int]] = []
        cursor = 0
        while cursor < len(value):
            tag_start = value.find("<", cursor)
            if tag_start < 0:
                break
            name_start = tag_start + 1
            if name_start < len(value) and value[name_start] == "/":
                name_start += 1
            if name_start >= len(value) or not (
                value[name_start].isascii() and value[name_start].isalpha()
            ):
                cursor = tag_start + 1
                continue

            tag_end, tag_complete = cls._html_tag_extent(value, name_start)

            index = name_start + 1
            has_url_attribute_assignment = False
            while index < tag_end and (
                value[index].isascii() and (value[index].isalnum() or value[index] in "_:-")
            ):
                index += 1
            while index < tag_end:
                while index < tag_end and (value[index].isspace() or value[index] == "/"):
                    index += 1
                attribute_start = index
                while index < tag_end and not value[index].isspace() and value[index] not in "=/>":
                    index += 1
                if index == attribute_start:
                    index += 1
                    continue
                attribute = value[attribute_start:index].casefold()
                while index < tag_end and value[index].isspace():
                    index += 1
                if index >= tag_end or value[index] != "=":
                    continue
                if attribute in {"href", "src"}:
                    has_url_attribute_assignment = True
                index += 1
                while index < tag_end and value[index].isspace():
                    index += 1
                value_start = index
                if index >= tag_end:
                    break
                if value[index] in {'"', "'"}:
                    quote = value[index]
                    index += 1
                    while index < tag_end and value[index] != quote:
                        index += 1
                    if index < tag_end:
                        index += 1
                else:
                    while index < tag_end and not value[index].isspace():
                        index += 1
                if attribute in {"href", "src"} and index > value_start:
                    spans.append((value_start, index))
            if not tag_complete and has_url_attribute_assignment:
                spans.append((tag_start, tag_end))
            cursor = tag_end + 1 if tag_complete else tag_end
        return tuple(spans)

    @staticmethod
    def _html_tag_extent(value: str, start: int) -> tuple[int, bool]:
        """Return the tag body end and whether a real closing bracket was observed."""

        quote: str | None = None
        cursor = start
        while cursor < len(value):
            character = value[cursor]
            if quote is not None:
                if character == quote:
                    quote = None
            elif character in {'"', "'"}:
                quote = character
            elif character == ">":
                return cursor, True
            elif character == "<":
                return cursor, False
            cursor += 1
        return len(value), False

    @classmethod
    def _has_html_tag_value_termination(cls, value: str, start: int, end: int) -> bool:
        tag_start = value.rfind("<", 0, start)
        if tag_start < 0 or tag_start < value.rfind(">", 0, start):
            return False
        cursor = end
        while cursor < len(value) and value[cursor].isspace():
            cursor += 1
        if cursor < len(value) and value[cursor] == "/":
            cursor += 1
            while cursor < len(value) and value[cursor].isspace():
                cursor += 1
        return cursor < len(value) and value[cursor] == ">"

    @classmethod
    def _reference_context_boundary(cls, value: str) -> int:
        if len(value) <= _MAX_PATH_ACTION_CONTEXT:
            return cls._cached_reference_context_boundary(value)
        return cls._uncached_reference_context_boundary(value)

    @classmethod
    @lru_cache(maxsize=64)
    def _cached_reference_context_boundary(cls, value: str) -> int:
        return cls._uncached_reference_context_boundary(value)

    @classmethod
    def _uncached_reference_context_boundary(cls, value: str) -> int:
        cjk_boundary = max(
            (value.rfind(boundary) for boundary in _REFERENCE_CONTEXT_BOUNDARIES),
            default=-1,
        )
        ascii_boundary = max(
            (value.rfind(boundary) for boundary in _ASCII_REFERENCE_CONTEXT_BOUNDARIES),
            default=-1,
        )
        if ascii_boundary <= cjk_boundary:
            return cjk_boundary
        preceding = value[:ascii_boundary]
        if cls._ends_with_complete_labeled_url_value(preceding):
            return ascii_boundary
        if cls._looks_like_url_token(preceding[cjk_boundary + 1 :]):
            return cjk_boundary
        return ascii_boundary

    @classmethod
    def _ends_with_complete_labeled_url_value(cls, value: str) -> bool:
        closer_to_opener = {
            '"': '"',
            "'": "'",
            "`": "`",
            "”": "“",
            "’": "‘",
            "」": "「",
            "』": "『",
            ")": "(",
            "）": "（",
            "]": "[",
            "】": "【",
            "}": "{",
        }
        trimmed = value.rstrip()
        if not trimmed:
            return False
        opener = closer_to_opener.get(trimmed[-1])
        if opener is None:
            return False
        complete_spans = [
            span
            for span in cls._wrapper_spans(trimmed, opener, trimmed[-1])
            if span[3] == len(trimmed)
        ]
        if not complete_spans:
            return False
        opener_index = min(span[0] for span in complete_spans)
        label_and_delimiter = trimmed[:opener_index].rstrip()
        if not label_and_delimiter or label_and_delimiter[-1] not in "=:：":
            return False
        label_text = label_and_delimiter[:-1].rstrip()
        if not label_text:
            return False
        label = label_text.rsplit(maxsplit=1)[-1]
        if _URL_LABEL_TOKEN.fullmatch(label) is not None:
            return True
        return cls._nested_url_label_chain_end(
            label_and_delimiter,
            0,
            line_end=len(label_and_delimiter),
        ) == len(label_and_delimiter)

    @staticmethod
    def _rstrip_inline_whitespace(value: str, end: int) -> int:
        cursor = end
        while cursor > 0 and value[cursor - 1] not in "\r\n" and value[cursor - 1].isspace():
            cursor -= 1
        return cursor

    @classmethod
    def _protect_separate_url_value(
        cls,
        value: str,
        token: str,
        token_end: int,
        protected_spans: _ProtectedSpans,
        *,
        containing_token_end: int,
        line_end: int,
    ) -> int | None:
        if token_end < containing_token_end:
            # This URL-shaped segment ended at a precomputed clause boundary
            # inside the current non-whitespace token. Searching globally from
            # here would rediscover and rescan the entire remaining token for
            # every segment, and the remainder is not a separate URL value.
            return None
        stripped_token = token.rstrip(_REFERENCE_TRAILING_PUNCTUATION_TEXT)
        context_token = cls._strip_trailing_context_openers(stripped_token)
        if context_token.endswith("=") and len(context_token) < len(stripped_token):
            structured_start = token_end - len(token) + len(context_token)
            structured_span = cls._structured_url_value_span(
                value,
                structured_start,
                line_end=line_end,
            )
            protected_span = (
                structured_span[0],
                cls._structured_url_value_context_end(
                    value,
                    structured_span[1],
                    line_end=line_end,
                ),
            )
            cls._add_protected_span(
                protected_spans,
                protected_span,
            )
            return cls._url_value_reopen_position(value, protected_span[1], line_end)
        if (
            token_end < line_end
            and not value[token_end].isspace()
            and value[token_end] in _STRUCTURED_URL_VALUE_OPENERS
            and (context_token.endswith("=") or token.casefold().endswith((":", "：")))
        ):
            structured_span = cls._structured_url_value_span(
                value,
                token_end,
                line_end=line_end,
            )
            protected_span = (
                structured_span[0],
                cls._structured_url_value_context_end(
                    value,
                    structured_span[1],
                    line_end=line_end,
                ),
            )
            cls._add_protected_span(protected_spans, protected_span)
            return cls._url_value_reopen_position(value, protected_span[1], line_end)
        next_match = _NONSPACE_TOKEN.search(value, token_end, line_end)
        if next_match is None:
            return None
        value_match: re.Match[str] | None
        if context_token.endswith("="):
            value_match = next_match
        elif next_match.group() in {"=", ":", "："}:
            cls._add_protected_span(protected_spans, next_match.span())
            value_match = _NONSPACE_TOKEN.search(value, next_match.end(), line_end)
            if value_match is None:
                return None
        elif token.casefold().endswith((":", "：")):
            value_match = next_match
        else:
            return None
        if value_match is None:
            return None

        nested_label_end = cls._nested_url_label_chain_end(
            value,
            value_match.start(),
            line_end=line_end,
        )
        if nested_label_end is not None:
            cls._add_protected_span(
                protected_spans,
                (value_match.start(), nested_label_end),
            )
            value_match = _NONSPACE_TOKEN.search(value, nested_label_end, line_end)
            if value_match is None:
                return None

        value_span = cls._structured_url_value_span(
            value,
            value_match.start(),
            line_end=line_end,
        )
        structured_value_end = (
            value_span[1] if value[value_match.start()] in _STRUCTURED_URL_VALUE_OPENERS else None
        )
        value_token = (
            value[value_match.start() : value_match.end()]
            if value_match.end() - value_match.start() <= _MAX_PATH_ACTION_CONTEXT
            else None
        )
        if value_token is not None and (
            _PATH_ACTION_PREFIX.fullmatch(value_token) is not None
            or _CJK_PATH_LOCATION_CONTEXT.fullmatch(value_token) is not None
        ):
            labeled_value = _NONSPACE_TOKEN.search(value, value_match.end(), line_end)
            if labeled_value is not None:
                labeled_value_span = cls._structured_url_value_span(
                    value,
                    labeled_value.start(),
                    line_end=line_end,
                )
                value_span = (
                    value_span[0],
                    labeled_value_span[1],
                )
                if value[labeled_value.start()] in _STRUCTURED_URL_VALUE_OPENERS:
                    structured_value_end = labeled_value_span[1]
        if structured_value_end is not None:
            value_span = (
                value_span[0],
                cls._structured_url_value_context_end(
                    value,
                    structured_value_end,
                    line_end=line_end,
                ),
            )
        cls._add_protected_span(protected_spans, value_span)
        return cls._url_value_reopen_position(value, value_span[1], line_end)

    @classmethod
    def _url_value_reopen_position(
        cls,
        value: str,
        span_end: int,
        line_end: int,
    ) -> int | None:
        if (
            span_end < line_end
            and value[span_end] in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT
            and not cls._is_escaped(value, span_end)
        ):
            return span_end
        return None

    @classmethod
    def _nested_url_label_chain_end(
        cls,
        value: str,
        start: int,
        *,
        line_end: int,
    ) -> int | None:
        """Return the final delimiter in a leading chain of nested URL labels."""

        cursor = start
        found = False
        while cursor < line_end:
            while cursor < line_end and value[cursor].isspace():
                cursor += 1
            label_probe_end = min(line_end, cursor + _MAX_URL_LABEL_LENGTH + 1)
            label_end = cursor
            while (
                label_end < label_probe_end
                and not value[label_end].isspace()
                and value[label_end] not in "=:："
            ):
                label_end += 1
            if label_end == cursor or label_end - cursor > _MAX_URL_LABEL_LENGTH:
                break
            delimiter_start = label_end
            while delimiter_start < line_end and value[delimiter_start].isspace():
                delimiter_start += 1
            if delimiter_start >= line_end or value[delimiter_start] not in "=:：":
                break
            candidate = value[cursor:label_end] + value[delimiter_start]
            if _URL_LABEL_TOKEN.fullmatch(candidate) is None:
                break
            cursor = delimiter_start + 1
            found = True
        return cursor if found else None

    @classmethod
    def _apostrophe_is_prose(
        cls,
        value: str,
        index: int,
        *,
        scan_start: int = 0,
    ) -> bool:
        """Distinguish English apostrophes from issue-syntax quote delimiters."""

        if value[index] not in {"'", "’"}:
            return False
        if index > scan_start and value[index - 1].isalnum():
            context_start = max(scan_start, index - _MAX_PATH_ACTION_CONTEXT)
            bounded_prefix = value[context_start:index]
            candidate_starts = [0]
            candidate_starts.extend(
                position + 1
                for position, character in enumerate(bounded_prefix)
                if character in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT
                and not cls._is_escaped(bounded_prefix, position)
            )
            for candidate_start in reversed(candidate_starts):
                action_context = bounded_prefix[candidate_start:].lstrip(" \t")
                if (
                    _PATH_ACTION_PREFIX.fullmatch(action_context) is not None
                    or _CJK_PATH_LOCATION_CONTEXT.fullmatch(action_context) is not None
                ):
                    return False
            return True
        return _LEADING_APOSTROPHE_ELISION.match(value, index + 1) is not None

    @staticmethod
    def _apostrophe_is_lexically_inner(value: str, index: int) -> bool:
        """Return whether one apostrophe is unambiguously English word syntax."""

        if value[index] not in {"'", "’"}:
            return False
        if (
            index > 0
            and index + 1 < len(value)
            and value[index - 1].isalnum()
            and value[index + 1].isalnum()
        ):
            return True
        return _LEADING_APOSTROPHE_ELISION.match(value, index + 1) is not None

    @staticmethod
    @lru_cache(maxsize=8)
    def _single_quote_roles(value: str) -> _SingleQuoteRoles:
        """Classify ambiguous apostrophes once in an escape- and line-aware pass.

        A possessive-looking apostrophe inside an active quote is not its closer
        when a URL label and a later closer remain in the same physical-line and
        structural-wrapper context. The same proof lets a leading English elision
        act as a real quote opener only when it encloses a URL assignment. Keeping
        the result issue-wide means nested Markdown and URL scanners reuse one
        linear topology pass instead of rescanning every remaining line suffix.
        """

        if "'" not in value and "‘" not in value and "’" not in value:
            return _SingleQuoteRoles(frozenset(), frozenset())

        url_label_starts: set[int] = set()
        for label_match in _URL_LABEL_TOKEN.finditer(value):
            label_start = label_match.start()
            if label_start > 0 and not (
                value[label_start - 1].isspace()
                or value[label_start - 1] in _REFERENCE_CONTEXT_BOUNDARIES
                or value[label_start - 1] in _SAME_TOKEN_LIST_SEPARATORS
                or value[label_start - 1] in _SEMANTIC_BARE_SEPARATORS
                or value[label_start - 1] in _STRUCTURED_URL_VALUE_OPENERS
            ):
                continue
            label_text = label_match.group()
            if label_text[-1] in "=:：":
                url_label_starts.add(label_start)
                continue
            delimiter_start = label_match.end()
            while delimiter_start < len(value) and value[delimiter_start] in " \t":
                delimiter_start += 1
            if (
                delimiter_start < len(value)
                and value[delimiter_start] in "=:："
                and _URL_LABEL_TOKEN.fullmatch(label_text + value[delimiter_start]) is not None
            ):
                url_label_starts.add(label_start)

        # Group URL labels and apostrophes by physical line, structural wrapper,
        # and quote family. Wrapper nodes receive stable identifiers so a mark
        # inside a completed Markdown destination cannot pair with prose outside.
        events: dict[tuple[int, int, str], list[tuple[int, bool, str, int]]] = {}
        physical_line_events: dict[int, list[tuple[int, str, str]]] = {}
        quote_token_starts: dict[int, int] = {}
        context_clause_epochs: dict[tuple[int, int], int] = {(0, 0): 0}
        wrapper_stack: list[tuple[str, int]] = []
        context_id = 0
        next_context_id = 1
        line_id = 0
        token_start = 0
        other_quote: tuple[str, int] | None = None
        backslash_run = 0
        previous_was_cr = False
        for index, character in enumerate(value):
            if character in "\r\n":
                if not (character == "\n" and previous_was_cr):
                    line_id += 1
                previous_was_cr = character == "\r"
                wrapper_stack.clear()
                context_id = 0
                other_quote = None
                backslash_run = 0
                token_start = index + 1
                context_clause_epochs.setdefault((line_id, context_id), 0)
                continue
            previous_was_cr = False
            if character == "\\":
                backslash_run += 1
                continue
            escaped = backslash_run % 2 == 1
            backslash_run = 0
            if escaped:
                continue
            if character.isspace():
                token_start = index + 1

            clause_epoch = context_clause_epochs.setdefault((line_id, context_id), 0)
            if index in url_label_starts:
                for quote_family in ("ascii", "curly"):
                    events.setdefault((line_id, context_id, quote_family), []).append(
                        (index, True, character, clause_epoch)
                    )
                physical_line_events.setdefault(line_id, []).append((index, "url", character))
            if character == "'":
                quote_token_starts[index] = token_start
                events.setdefault((line_id, context_id, "ascii"), []).append(
                    (index, False, character, clause_epoch)
                )
                physical_line_events.setdefault(line_id, []).append((index, "quote", character))
            elif character in {"‘", "’"}:
                quote_token_starts[index] = token_start
                events.setdefault((line_id, context_id, "curly"), []).append(
                    (index, False, character, clause_epoch)
                )
                physical_line_events.setdefault(line_id, []).append((index, "quote", character))
            if character in _SAME_TOKEN_WRAPPER_CLOSERS:
                physical_line_events.setdefault(line_id, []).append(
                    (index, "wrapper_closer", character)
                )

            if character in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT:
                context_clause_epochs[(line_id, context_id)] = clause_epoch + 1
                if character in _SEMANTIC_BARE_SEPARATORS:
                    token_start = index + 1

            if other_quote is not None:
                if character == other_quote[0]:
                    context_id = other_quote[1]
                    other_quote = None
                continue
            if character in {'"', "`"}:
                parent_context = context_id
                context_id = next_context_id
                next_context_id += 1
                context_clause_epochs[(line_id, context_id)] = 0
                other_quote = (character, parent_context)
                continue
            if character in _SAME_TOKEN_WRAPPER_PAIRS and character != "‘":
                parent_context = context_id
                context_id = next_context_id
                next_context_id += 1
                context_clause_epochs[(line_id, context_id)] = 0
                wrapper_stack.append((_SAME_TOKEN_WRAPPER_PAIRS[character], parent_context))
                continue
            if (
                character in _SAME_TOKEN_WRAPPER_CLOSERS
                and character != "’"
                and wrapper_stack
                and character == wrapper_stack[-1][0]
            ):
                _, context_id = wrapper_stack.pop()

        paired_openers: set[int] = set()
        deferred_closers: set[int] = set()
        completed_quote_marks: set[int] = set()

        def has_compact_reference_between(start: int, end: int) -> bool:
            marker = value.find(_COMPACT_PATH_SIGIL, start, end)
            while marker >= 0:
                probe_end = min(
                    end,
                    marker + 1 + 500 + _MAX_REFERENCE_INVALID_SUFFIX_LENGTH,
                )
                if PlanBuilder._labeled_reference_candidate(value[marker:probe_end]) is not None:
                    return True
                marker = value.find(_COMPACT_PATH_SIGIL, marker + 1, end)
            return False

        def quote_pair_has_raw_owner(opener: int) -> bool:
            prefix = value[quote_token_starts.get(opener, opener) : opener].strip()
            if not prefix:
                return False
            url_label = _URL_LABEL_TOKEN.match(prefix)
            return bool(
                (
                    url_label is not None
                    and url_label.end() == len(prefix)
                    and prefix[url_label.end() - 1] in "=:："
                )
                or PlanBuilder._has_explicit_uri_envelope(prefix)
            )

        for (_, _, quote_family), context_events in events.items():
            last_url_by_epoch: dict[int, int] = {}
            completed_pair_by_epoch: dict[int, tuple[int, int]] = {}
            for position, is_url, _character, clause_epoch in context_events:
                if is_url:
                    last_url_by_epoch[clause_epoch] = position

            active_opener: int | None = None
            active_epoch: int | None = None
            elision_closer: int | None = None
            for position, is_url, character, clause_epoch in context_events:
                if active_epoch != clause_epoch:
                    active_opener = None
                    active_epoch = clause_epoch
                    elision_closer = None
                if is_url:
                    continue
                if elision_closer == position:
                    elision_closer = None
                    continue
                leading_elision = _LEADING_APOSTROPHE_ELISION.match(value, position + 1)
                if leading_elision is not None:
                    elision_closer = leading_elision.end()
                    continue
                if PlanBuilder._apostrophe_is_lexically_inner(value, position):
                    continue
                if quote_family == "curly":
                    if character == "‘":
                        if active_opener is None:
                            active_opener = position
                        continue
                    if active_opener is None:
                        continue
                elif active_opener is None:
                    if not PlanBuilder._apostrophe_is_prose(value, position):
                        active_opener = position
                    continue
                completed_pair_by_epoch[clause_epoch] = (active_opener, position)
                active_opener = None
            for clause_epoch, (opener, closer) in completed_pair_by_epoch.items():
                last_url = last_url_by_epoch.get(clause_epoch)
                if last_url is None or closer >= last_url:
                    continue
                if not quote_pair_has_raw_owner(opener) and has_compact_reference_between(
                    closer + 1, last_url
                ):
                    completed_quote_marks.add(closer)

        for (_, _, _quote_family), context_events in events.items():
            context_next_quote: int | None = None
            context_next_url: int | None = None
            context_next_url_epoch: int | None = None
            context_quote_after_url: int | None = None
            for position, is_url, character, clause_epoch in reversed(context_events):
                if is_url:
                    context_next_url = position
                    context_next_url_epoch = clause_epoch
                    context_quote_after_url = context_next_quote
                    continue
                has_url_envelope = (
                    context_next_url is not None
                    and context_next_url_epoch == clause_epoch
                    and context_quote_after_url is not None
                    and position < context_next_url < context_quote_after_url
                )
                lexically_inner = PlanBuilder._apostrophe_is_lexically_inner(value, position)
                if (
                    has_url_envelope
                    and character in {"'", "’"}
                    and not lexically_inner
                    and position not in completed_quote_marks
                ):
                    deferred_closers.add(position)
                if (
                    has_url_envelope
                    and _LEADING_APOSTROPHE_ELISION.match(value, position + 1) is not None
                ):
                    paired_openers.add(position)
                if not lexically_inner:
                    context_next_quote = position

        # A leading elision immediately inside a structured wrapper may contain
        # apparent wrapper closers as ordinary quoted text. The structural pass
        # above deliberately keeps contexts strict, so add only the narrow proof
        # where a URL label is followed by a quote and then the wrapper's expected
        # closer on the same physical line. This recovers complete quoted values
        # without pairing a malformed Markdown destination with later prose.
        for physical_events in physical_line_events.values():
            physical_next_quote: int | None = None
            physical_next_url: int | None = None
            physical_quote_after_url: int | None = None
            next_wrapper_closer: dict[str, int] = {}
            for position, event_kind, character in reversed(physical_events):
                if event_kind == "wrapper_closer":
                    next_wrapper_closer[character] = position
                    continue
                if event_kind == "url":
                    physical_next_url = position
                    physical_quote_after_url = physical_next_quote
                    continue
                if (
                    character == "'"
                    and physical_next_url is not None
                    and physical_quote_after_url is not None
                    and position < physical_next_url < physical_quote_after_url
                    and _LEADING_APOSTROPHE_ELISION.match(value, position + 1) is not None
                    and position > 0
                    and value[position - 1] in _SAME_TOKEN_WRAPPER_PAIRS
                ):
                    expected_closer = _SAME_TOKEN_WRAPPER_PAIRS[value[position - 1]]
                    sibling_closer = next_wrapper_closer.get(expected_closer)
                    if sibling_closer is not None and sibling_closer < physical_next_url:
                        physical_next_quote = position
                        continue
                    closer_cursor = physical_quote_after_url + 1
                    closer_limit = min(len(value), closer_cursor + 33)
                    while closer_cursor < closer_limit and value[closer_cursor] in " \t":
                        closer_cursor += 1
                    if closer_cursor < closer_limit and value[closer_cursor] == expected_closer:
                        paired_openers.add(position)
                if not PlanBuilder._apostrophe_is_lexically_inner(value, position):
                    physical_next_quote = position

        return _SingleQuoteRoles(
            paired_openers=frozenset(paired_openers),
            deferred_closers=frozenset(deferred_closers),
        )

    @classmethod
    def _apostrophe_is_inner_word(cls, value: str, index: int) -> bool:
        """Keep prose apostrophes and deferred URL-envelope marks from closing."""

        if value[index] not in {"'", "’"}:
            return False
        return (
            cls._apostrophe_is_lexically_inner(value, index)
            or index in cls._single_quote_roles(value).deferred_closers
        )

    @classmethod
    def _paired_single_quote_openers(
        cls,
        value: str,
        *,
        start: int = 0,
        end: int | None = None,
    ) -> frozenset[int]:
        """Return shared opener roles; bounds are consumption, not pairing, limits."""

        del start, end
        return cls._single_quote_roles(value).paired_openers

    @classmethod
    def _opens_symmetric_quote(
        cls,
        value: str,
        index: int,
        *,
        scan_start: int = 0,
        paired_single_quote_openers: frozenset[int] | None = None,
    ) -> bool:
        character = value[index]
        if (
            character == "'"
            and paired_single_quote_openers is not None
            and index in paired_single_quote_openers
        ):
            return True
        return character in _SAME_TOKEN_SYMMETRIC_QUOTES and not (
            character == "'" and cls._apostrophe_is_prose(value, index, scan_start=scan_start)
        )

    @classmethod
    def _structured_url_value_span(
        cls,
        value: str,
        start: int,
        *,
        line_end: int,
    ) -> tuple[int, int]:
        first = value[start]
        if first not in _STRUCTURED_URL_VALUE_PAIRS:
            value_match = _URL_TOKEN_SEGMENT.match(value, start, line_end)
            if value_match is None:
                return start, start
            reset_seam = cls._top_level_url_label_seam(value_match.group())
            if reset_seam is not None:
                return start, start + reset_seam
            # A separate URL value can share a non-whitespace token with a
            # later, explicit repository clause. Reuse the wrapper-aware clause
            # splitter so only a proven top-level semantic seam reopens parsing;
            # URI punctuation and separators inside balanced quotes remain
            # opaque. This is bounded to the one already matched token and keeps
            # the early URL-protection pass linear for dense malformed input.
            clause_spans = cls._same_token_list_clauses(value_match.group())
            if clause_spans and clause_spans[0][0] == 0:
                return start, start + clause_spans[0][1]
            return value_match.span()

        if first in _SAME_TOKEN_SYMMETRIC_QUOTES:
            backslash_run = 0
            cursor = start + 1
            while cursor < line_end:
                character = value[cursor]
                if character == "\\":
                    backslash_run += 1
                    cursor += 1
                    continue
                escaped = backslash_run % 2 == 1
                backslash_run = 0
                if (
                    character == first
                    and not escaped
                    and not cls._apostrophe_is_inner_word(value, cursor)
                ):
                    return start, cursor + 1
                cursor += 1
            return start, cursor

        stack: list[str] = [_STRUCTURED_URL_VALUE_PAIRS[first]]
        quote: str | None = None
        backslash_run = 0
        cursor = start + 1
        paired_single_quote_openers = cls._paired_single_quote_openers(
            value,
            start=cursor,
            end=line_end,
        )
        while cursor < line_end:
            character = value[cursor]
            if character == "\\":
                backslash_run += 1
                cursor += 1
                continue
            escaped = backslash_run % 2 == 1
            backslash_run = 0
            if escaped:
                cursor += 1
                continue
            if quote is not None:
                if character == quote:
                    if cls._apostrophe_is_inner_word(value, cursor):
                        cursor += 1
                        continue
                    quote = None
            elif cls._opens_symmetric_quote(
                value,
                cursor,
                scan_start=start,
                paired_single_quote_openers=paired_single_quote_openers,
            ):
                quote = character
            elif character in _SAME_TOKEN_WRAPPER_PAIRS:
                stack.append(_SAME_TOKEN_WRAPPER_PAIRS[character])
            elif character in _SAME_TOKEN_WRAPPER_CLOSERS:
                if not stack or character != stack[-1]:
                    if cls._apostrophe_is_prose(value, cursor, scan_start=start):
                        cursor += 1
                        continue
                    return start, line_end
                if cls._apostrophe_is_inner_word(value, cursor):
                    cursor += 1
                    continue
                stack.pop()
                if not stack:
                    span_end = cursor + 1
                    if first == "[" and value[span_end : span_end + 1] in {"(", "["}:
                        destination_closer = (
                            cls._markdown_destination_closer(
                                value,
                                span_end,
                                line_end=line_end,
                            )
                            if value[span_end] == "("
                            else cls._markdown_reference_destination_closer(
                                value,
                                span_end,
                                line_end=line_end,
                            )
                        )
                        if destination_closer is not None:
                            return start, destination_closer + 1
                        return start, line_end
                    return start, span_end
            cursor += 1
        return start, cursor

    @classmethod
    def _top_level_url_label_seam(cls, value: str) -> int | None:
        """Return a proven hard seam whose right clause starts a URL assignment."""

        stack: list[str] = []
        quote: str | None = None
        backslash_run = 0
        paired_single_quote_openers = cls._paired_single_quote_openers(value)
        for index, character in enumerate(value):
            if character == "\\":
                backslash_run += 1
                continue
            escaped = backslash_run % 2 == 1
            backslash_run = 0
            if escaped:
                continue
            if quote is not None:
                if character == quote:
                    if cls._apostrophe_is_inner_word(value, index):
                        continue
                    quote = None
                continue
            if cls._opens_symmetric_quote(
                value,
                index,
                paired_single_quote_openers=paired_single_quote_openers,
            ):
                quote = character
                continue
            if character in _SAME_TOKEN_WRAPPER_PAIRS:
                stack.append(_SAME_TOKEN_WRAPPER_PAIRS[character])
                continue
            if character in _SAME_TOKEN_WRAPPER_CLOSERS:
                if not stack or character != stack[-1]:
                    if cls._apostrophe_is_prose(value, index):
                        continue
                    return None
                if cls._apostrophe_is_inner_word(value, index):
                    continue
                stack.pop()
                continue
            if stack or character not in _SEMANTIC_BARE_SEPARATORS:
                continue
            right = value[index + 1 :].lstrip(" \t")
            if not right:
                continue
            if (
                cls._nested_url_label_chain_end(right, 0, line_end=len(right)) is not None
                or _URL_LABEL_TOKEN.fullmatch(right) is not None
            ):
                return index
        return None

    @classmethod
    def _structured_url_value_context_end(
        cls,
        value: str,
        start: int,
        *,
        line_end: int,
    ) -> int:
        """Keep a completed URL envelope opaque until a proven top-level clause seam."""
        if start >= line_end:
            return start

        stack: list[str] = []
        quote: str | None = None
        backslash_run = 0
        paired_single_quote_openers = cls._paired_single_quote_openers(
            value,
            start=start,
            end=line_end,
        )
        for index in range(start, line_end):
            character = value[index]
            if character == "\\":
                backslash_run += 1
                continue
            escaped = backslash_run % 2 == 1
            backslash_run = 0
            if escaped:
                continue
            if quote is not None:
                if character == quote:
                    if cls._apostrophe_is_inner_word(value, index):
                        continue
                    quote = None
                continue
            if cls._opens_symmetric_quote(
                value,
                index,
                paired_single_quote_openers=paired_single_quote_openers,
            ):
                quote = character
                continue
            if character in _SAME_TOKEN_WRAPPER_PAIRS:
                stack.append(_SAME_TOKEN_WRAPPER_PAIRS[character])
                continue
            if character in _SAME_TOKEN_WRAPPER_CLOSERS:
                if not stack or character != stack[-1]:
                    if cls._apostrophe_is_prose(value, index):
                        continue
                    return line_end
                if cls._apostrophe_is_inner_word(value, index):
                    continue
                stack.pop()
                continue
            if not stack and (
                character in _SAME_TOKEN_LIST_SEPARATORS or character in _SEMANTIC_BARE_SEPARATORS
            ):
                return index
        return line_end

    @classmethod
    def _bare_reference_candidate(cls, value: str) -> str | None:
        candidate_text = value.rstrip(_BARE_TRAILING_PUNCTUATION)
        if 0 < len(candidate_text) <= 500 and not any(
            character in _BARE_REFERENCE_FORBIDDEN for character in candidate_text
        ):
            candidate = cls._reference_candidate(candidate_text)
            if candidate is not None and cls._candidate_has_unambiguous_shape(
                candidate,
                allow_spaced_root=False,
            ):
                return candidate

        folded = value.casefold()
        for suffix in _SUPPORTED_REFERENCE_SUFFIXES:
            search_from = 0
            while True:
                suffix_start = folded.find(suffix, search_from)
                if suffix_start < 0:
                    break
                suffix_end = suffix_start + len(suffix)
                search_from = suffix_end
                if suffix_end > 500:
                    break
                if suffix_end >= len(value):
                    continue
                boundary = value[suffix_end]
                is_cjk_boundary = "\u3400" <= boundary <= "\u9fff"
                if not (is_cjk_boundary or boundary in "，。；：！？…⋯．"):
                    continue
                if is_cjk_boundary and not value[suffix_end:].startswith(_CJK_BARE_PROSE_PREFIXES):
                    continue
                if boundary in "，。；：！？…⋯．":
                    cursor = suffix_end
                    while cursor < len(value) and value[cursor] in "，。；：！？…⋯．":
                        cursor += 1
                    if cursor < len(value) and not "\u3400" <= value[cursor] <= "\u9fff":
                        continue
                candidate_text = value[:suffix_end]
                if not candidate_text or any(
                    character in _BARE_REFERENCE_FORBIDDEN for character in candidate_text
                ):
                    continue
                candidate = cls._reference_candidate(candidate_text)
                if candidate is not None and cls._candidate_has_unambiguous_shape(
                    candidate,
                    allow_spaced_root=False,
                ):
                    return candidate
        return None

    @classmethod
    def _same_token_list_clauses(cls, value: str) -> tuple[tuple[int, int], ...]:
        """Split only top-level issue clauses while preserving real URI data."""

        if not value:
            return ()
        if not any(separator in value for separator in _SAME_TOKEN_CLAUSE_SEPARATOR_TEXT):
            return ((0, len(value)),)

        stack: list[str] = []
        quote: str | None = None
        backslash_run = 0
        invalid_topology = False
        separator_candidates: list[tuple[int, str]] = []
        paired_single_quote_openers = cls._paired_single_quote_openers(value)
        for index, character in enumerate(value):
            if character == "\\":
                backslash_run += 1
                continue
            escaped = backslash_run % 2 == 1
            backslash_run = 0
            if escaped:
                continue
            if quote is not None:
                if character == quote:
                    if cls._apostrophe_is_inner_word(value, index):
                        continue
                    quote = None
                continue
            if cls._opens_symmetric_quote(
                value,
                index,
                paired_single_quote_openers=paired_single_quote_openers,
            ):
                quote = character
                continue
            if character in _SAME_TOKEN_WRAPPER_PAIRS:
                stack.append(_SAME_TOKEN_WRAPPER_PAIRS[character])
                continue
            if character in _SAME_TOKEN_WRAPPER_CLOSERS:
                if not stack or character != stack[-1]:
                    if cls._apostrophe_is_prose(value, index):
                        continue
                    invalid_topology = True
                    continue
                if cls._apostrophe_is_inner_word(value, index):
                    continue
                stack.pop()
                continue
            if not stack and (
                character in _SAME_TOKEN_LIST_SEPARATORS or character in _SEMANTIC_BARE_SEPARATORS
            ):
                separator_candidates.append((index, character))

        # A separator after an unclosed, mismatched, or unterminated envelope is
        # not demonstrably top-level. Keep the entire token fail-closed.
        if invalid_topology or stack or quote is not None:
            return ((0, len(value)),)
        if not separator_candidates:
            return ((0, len(value)),)

        semantic_boundaries: set[int] = set()
        for position, (separator_index, character) in enumerate(separator_candidates):
            if character not in _SEMANTIC_BARE_SEPARATORS:
                continue
            left_start = 0 if position == 0 else separator_candidates[position - 1][0] + 1
            right_end = (
                len(value)
                if position + 1 == len(separator_candidates)
                else separator_candidates[position + 1][0]
            )
            if cls._semantic_clause_separator_is_recognized(
                value[left_start:separator_index],
                value[separator_index + 1 : right_end],
            ):
                semantic_boundaries.add(separator_index)

        recognized_boundaries: set[int] = set()
        current_clause_start = 0
        opaque_ascii_clause_start: int | None = None
        for separator_index, character in separator_candidates:
            if separator_index in semantic_boundaries:
                recognized_boundaries.add(separator_index)
                current_clause_start = separator_index + 1
                continue
            if character not in _SAME_TOKEN_LIST_SEPARATORS:
                continue
            if character in ",;":
                if opaque_ascii_clause_start == current_clause_start:
                    continue
                clause_prefix = value[current_clause_start:separator_index]
                if cls._has_explicit_uri_envelope(
                    clause_prefix
                ) and not cls._ends_with_complete_labeled_url_value(clause_prefix):
                    opaque_ascii_clause_start = current_clause_start
                    continue
            recognized_boundaries.add(separator_index)
            current_clause_start = separator_index + 1
        if not recognized_boundaries:
            return ((0, len(value)),)

        clauses: list[tuple[int, int]] = []
        clause_start = 0
        for separator_index, _ in separator_candidates:
            if separator_index not in recognized_boundaries:
                continue
            if clause_start < separator_index:
                clauses.append((clause_start, separator_index))
            clause_start = separator_index + 1
        if clause_start < len(value):
            clauses.append((clause_start, len(value)))
        return tuple(clauses)

    @classmethod
    def _top_level_semantic_action_context(cls, value: str) -> str | None:
        """Return an exact action suffix after a proven top-level hard seam."""

        if not any(separator in value for separator in _SEMANTIC_BARE_SEPARATORS):
            return None
        stack: list[str] = []
        quote: str | None = None
        backslash_run = 0
        invalid_topology = False
        semantic_boundary = -1
        paired_single_quote_openers = cls._paired_single_quote_openers(value)
        for index, character in enumerate(value):
            if character == "\\":
                backslash_run += 1
                continue
            escaped = backslash_run % 2 == 1
            backslash_run = 0
            if escaped:
                continue
            if quote is not None:
                if character == quote:
                    if cls._apostrophe_is_inner_word(value, index):
                        continue
                    quote = None
                continue
            if cls._opens_symmetric_quote(
                value,
                index,
                paired_single_quote_openers=paired_single_quote_openers,
            ):
                quote = character
                continue
            if character in _SAME_TOKEN_WRAPPER_PAIRS:
                stack.append(_SAME_TOKEN_WRAPPER_PAIRS[character])
                continue
            if character in _SAME_TOKEN_WRAPPER_CLOSERS:
                if not stack or character != stack[-1]:
                    if cls._apostrophe_is_prose(value, index):
                        continue
                    invalid_topology = True
                    continue
                if cls._apostrophe_is_inner_word(value, index):
                    continue
                stack.pop()
                continue
            if not stack and character in _SEMANTIC_BARE_SEPARATORS:
                semantic_boundary = index

        if invalid_topology or stack or quote is not None or semantic_boundary < 0:
            return None
        action_context = value[semantic_boundary + 1 :].lstrip(" \t")
        if (
            _PATH_ACTION_PREFIX.fullmatch(action_context) is not None
            or _CJK_PATH_LOCATION_CONTEXT.fullmatch(action_context) is not None
        ):
            return action_context
        return None

    @classmethod
    def _semantic_clause_left_is_recognized(cls, left: str) -> bool:
        url_label = _URL_LABEL_TOKEN.match(left)
        return bool(
            cls._bare_reference_candidate(left) is not None
            or cls._labeled_reference_candidate(left) is not None
            or (url_label is not None and left[url_label.end() - 1] in "=:：")
            or cls._has_opaque_same_token_reference_continuation(left)
            or cls._idna_url_spans(left)
            or cls._looks_like_url_token(left)
        )

    @classmethod
    def _partial_cjk_location_reference_envelope(cls, value: str) -> bool:
        """Recognize a location operand whose required action may follow by space."""

        if len(value) > 1_024:
            return False
        location_prefix = next(
            (
                prefix
                for prefix in _CJK_PATH_LOCATION_PREFIXES_BY_INITIAL.get(value[:1], ())
                if value.startswith(prefix)
            ),
            None,
        )
        if location_prefix is None:
            return False
        operand_start = len(location_prefix)
        operand = value[operand_start:]
        if cls._complete_wrapped_reference_candidate(operand) is not None:
            return True
        markdown_references, _, _ = cls._markdown_references(value)
        for markdown_reference in markdown_references:
            if markdown_reference.image or markdown_reference.span != (
                operand_start,
                len(value),
            ):
                continue
            label_start, label_end = markdown_reference.label_span
            candidate = cls._reference_candidate(value[label_start:label_end])
            if candidate is not None and cls._candidate_has_unambiguous_shape(
                candidate,
                allow_spaced_root=True,
            ):
                return True
        return False

    @classmethod
    @lru_cache(maxsize=32)
    def _semantic_clause_separator_is_recognized(cls, left: str, right: str) -> bool:
        """Recognize a hard semantic seam without splitting an ordinary filename."""

        left = left.strip()
        right = right.strip()
        if not right:
            return False
        if cls._complete_url_assignment_envelope(right):
            return cls._semantic_clause_left_is_recognized(left)
        if (
            cls._nested_url_label_chain_end(right, 0, line_end=len(right)) is not None
            or _URL_LABEL_TOKEN.fullmatch(right) is not None
        ):
            return True
        if cls._explicit_reference_envelope_candidate(right) is not None:
            return True
        if (
            _PATH_ACTION_PREFIX.fullmatch(right) is not None
            or _CJK_PATH_LOCATION_CONTEXT.fullmatch(right) is not None
            or cls._partial_cjk_location_reference_envelope(right)
        ):
            return cls._semantic_clause_left_is_recognized(left)
        if _ENGLISH_CLAUSE_CONTINUATION.match(right) is not None and any(
            suffix in right.casefold() for suffix in _SUPPORTED_REFERENCE_SUFFIXES
        ):
            return True

        right_candidate = cls._bare_reference_candidate(right)
        if right_candidate is None:
            if cls._has_explicit_uri_envelope(right) and (
                cls._bare_reference_candidate(left) is not None
                or cls._explicit_reference_envelope_candidate(left) is not None
            ):
                return True
            return False
        return cls._semantic_clause_left_is_recognized(left)

    @classmethod
    def _complete_url_assignment_envelope(cls, value: str) -> bool:
        """Recognize a complete wrapper whose label begins a URL assignment."""

        stripped = value.strip()
        structured_start = 0
        label_start = 1
        if stripped.startswith("!["):
            structured_start = 1
            label_start = 2
        elif not stripped or stripped[0] not in _STRUCTURED_URL_VALUE_OPENERS:
            return False
        if (
            cls._nested_url_label_chain_end(
                stripped,
                label_start,
                line_end=min(
                    len(stripped),
                    label_start + _MAX_URL_LABEL_LENGTH + 33,
                ),
            )
            is None
        ):
            return False
        return cls._structured_url_value_span(
            stripped,
            structured_start,
            line_end=len(stripped),
        ) == (structured_start, len(stripped))

    @classmethod
    def _complete_wrapped_reference_candidate(cls, value: str) -> str | None:
        for opener, closer in _REFERENCE_WRAPPERS:
            if not (value.startswith(opener) and value.endswith(closer)):
                continue
            candidate = cls._reference_candidate(value[len(opener) : -len(closer)])
            if candidate is not None and cls._candidate_has_unambiguous_shape(
                candidate,
                allow_spaced_root=True,
            ):
                return candidate
            break
        return None

    @classmethod
    def _explicit_reference_envelope_candidate(cls, value: str) -> str | None:
        """Recognize a complete, explicit reference clause without recursive parsing."""

        labeled_candidate = cls._labeled_reference_candidate(value)
        if labeled_candidate is not None:
            return labeled_candidate[0]
        wrapped_candidate = cls._complete_wrapped_reference_candidate(value)
        if wrapped_candidate is not None:
            return wrapped_candidate

        cjk_action = next(
            (
                prefix
                for prefix in _CJK_PATH_ACTION_PREFIXES_BY_INITIAL.get(value[:1], ())
                if value.startswith(prefix)
            ),
            None,
        )
        if cjk_action is not None:
            action_candidate = cls._labeled_reference_candidate(
                cjk_action + ":" + value[len(cjk_action) :]
            )
            if action_candidate is not None:
                return action_candidate[0]

        # Reference operands are bounded to 500 characters. Keep the additional
        # action/location/Markdown envelope probe bounded as well.
        if len(value) > 1_024:
            return None

        for opener, closer in _REFERENCE_WRAPPERS:
            if not value.endswith(closer):
                continue
            opener_index = value.find(opener)
            if opener_index <= 0:
                continue
            candidate = cls._complete_wrapped_reference_candidate(value[opener_index:])
            action_context = value[:opener_index].rstrip("= \t")
            if candidate is not None and _PATH_ACTION_PREFIX.fullmatch(action_context) is not None:
                return candidate

        location_prefix = next(
            (
                prefix
                for prefix in _CJK_PATH_LOCATION_PREFIXES_BY_INITIAL.get(value[:1], ())
                if value.startswith(prefix)
            ),
            None,
        )
        if location_prefix is not None:
            remainder_start = len(location_prefix)
            remainder = value[remainder_start:]
            for opener, closer in _REFERENCE_WRAPPERS:
                if not remainder.startswith(opener):
                    continue
                complete_span = next(
                    (
                        span
                        for span in cls._wrapper_spans(remainder, opener, closer)
                        if span[0] == 0
                    ),
                    None,
                )
                if complete_span is None:
                    break
                candidate = cls._reference_candidate(remainder[complete_span[1] : complete_span[2]])
                wrapper_end = remainder_start + complete_span[3]
                if (
                    candidate is not None
                    and cls._candidate_has_unambiguous_shape(
                        candidate,
                        allow_spaced_root=True,
                    )
                    and cls._attached_cjk_location_suffix_span(
                        value,
                        wrapper_end,
                        line_end=len(value),
                    )
                    is not None
                ):
                    return candidate
                break

        markdown_references, _, _ = cls._markdown_references(value)
        for markdown_reference in markdown_references:
            if markdown_reference.image:
                continue
            markdown_start, markdown_end = markdown_reference.span
            action_context = value[:markdown_start].rstrip("= \t")
            location_context = value[:markdown_start].rstrip(" \t")
            is_complete_bare_markdown = markdown_start == 0 and markdown_end == len(value)
            is_complete_action_markdown = (
                markdown_end == len(value)
                and _PATH_ACTION_PREFIX.fullmatch(action_context) is not None
            )
            is_complete_location_markdown = (
                location_context in _CJK_PATH_LOCATION_PREFIXES
                and cls._attached_cjk_location_suffix_span(
                    value,
                    markdown_end,
                    line_end=len(value),
                )
                is not None
            )
            if not (
                is_complete_bare_markdown
                or is_complete_action_markdown
                or is_complete_location_markdown
            ):
                continue
            label_start, label_end = markdown_reference.label_span
            candidate = cls._reference_candidate(value[label_start:label_end])
            if candidate is not None and cls._candidate_has_unambiguous_shape(
                candidate,
                allow_spaced_root=True,
            ):
                return candidate
        return None

    @classmethod
    def _has_explicit_uri_envelope(cls, value: str) -> bool:
        """Return whether ASCII comma/semicolon remain data in a real URI token."""

        stripped = value.strip()
        if not stripped or stripped.startswith(_COMPACT_PATH_SIGIL):
            return False
        url_label = _URL_LABEL_TOKEN.match(stripped)
        if url_label is not None and stripped[url_label.end() - 1] in "=:：":
            remainder = stripped[url_label.end() :].lstrip("([{<\"'“‘「『【（")
            scheme = _URI_SCHEME.match(remainder)
            return bool(
                scheme is not None
                or "//" in remainder[:2048]
                or remainder.translate(_IDNA_DOT_TRANSLATION).casefold().startswith("www.")
                or (
                    any(marker in remainder for marker in "?#")
                    and cls._looks_like_url_token(remainder)
                )
            )

        scheme = _URI_SCHEME.search(stripped)
        if scheme is not None:
            scheme_prefix = stripped[: scheme.end()]
            if _PATH_ACTION_PREFIX.fullmatch(scheme_prefix) is not None:
                return False
            return True
        return cls._looks_like_url_token(stripped)

    @classmethod
    def _is_opaque_url_clause(cls, value: str) -> bool:
        """Keep URL values opaque without hiding a repository action clause."""

        stripped = value.strip()
        if not stripped or stripped.startswith(_COMPACT_PATH_SIGIL):
            return False
        if cls._labeled_reference_candidate(stripped) is not None:
            return False
        if cls._explicit_reference_envelope_candidate(stripped) is not None:
            return False
        url_label = _URL_LABEL_TOKEN.match(stripped)
        if url_label is not None and stripped[url_label.end() - 1] in "=:：":
            return True
        return cls._has_explicit_uri_envelope(stripped)

    @classmethod
    def _has_opaque_same_token_reference_continuation(cls, value: str) -> bool:
        """Reject suffix/query fragments that would otherwise escape their envelope."""

        if len(value) <= _MAX_CACHED_OPAQUE_TOKEN_LENGTH:
            return cls._cached_opaque_same_token_reference_continuation(value)
        return cls._uncached_opaque_same_token_reference_continuation(value)

    @classmethod
    @lru_cache(maxsize=16)
    def _cached_opaque_same_token_reference_continuation(cls, value: str) -> bool:
        return cls._uncached_opaque_same_token_reference_continuation(value)

    @classmethod
    def _uncached_opaque_same_token_reference_continuation(cls, value: str) -> bool:
        saw_supported_suffix = False
        value_length = len(value)
        checked_punctuation_run_end = 0
        for index, character in enumerate(value):
            suffix_probe = value[
                max(0, index - _MAX_SUPPORTED_REFERENCE_SUFFIX_LENGTH) : index
            ].casefold()
            if suffix_probe.endswith(_SUPPORTED_REFERENCE_SUFFIXES):
                saw_supported_suffix = True
                continuation_start = index
                while (
                    continuation_start < value_length
                    and value[continuation_start] in _REFERENCE_WRAPPER_CLOSERS
                ):
                    continuation_start += 1
                if continuation_start < value_length:
                    folded_continuation = (
                        normalize(
                            "NFKC",
                            value[
                                continuation_start : continuation_start
                                + _MAX_REFERENCE_INVALID_SUFFIX_LENGTH
                            ],
                        )
                        .translate(_IDNA_DOT_TRANSLATION)
                        .casefold()
                    )
                    if folded_continuation.startswith(_REFERENCE_INVALID_SUFFIXES):
                        return True

                    continuation_marker = value[continuation_start]
                    normalized_marker = normalize("NFKC", continuation_marker)
                    remainder_start = continuation_start + 1
                    if normalized_marker in {"?", "!", ":"} and remainder_start < value_length:
                        normalized_first = normalize("NFKC", value[remainder_start])[0]
                        cjk_action = next(
                            (
                                prefix
                                for prefix in _CJK_PATH_ACTION_PREFIXES
                                if value.startswith(prefix, remainder_start)
                            ),
                            None,
                        )
                        reopens_cjk_path_clause = (
                            cjk_action is not None
                            and cls._bare_reference_candidate(
                                value[
                                    remainder_start + len(cjk_action) : min(
                                        value_length,
                                        remainder_start + len(cjk_action) + 501,
                                    )
                                ]
                            )
                            is not None
                        )
                        if not reopens_cjk_path_clause and (
                            continuation_marker.isascii()
                            or (
                                normalized_first.isascii()
                                and (normalized_first.isalnum() or normalized_first in "_./\\")
                            )
                        ):
                            return True
            if not saw_supported_suffix:
                continue

            if character in _IDNA_DOT_CHARACTERS:
                if index < checked_punctuation_run_end:
                    continue
                cursor = index + 1
                while cursor < value_length and value[cursor] in _REFERENCE_TRAILING_PUNCTUATION:
                    cursor += 1
                checked_punctuation_run_end = cursor
                folded_suffix = normalize(
                    "NFKC",
                    value[cursor : cursor + _MAX_REFERENCE_INVALID_SUFFIX_LENGTH],
                ).casefold()
                if folded_suffix.startswith(_REFERENCE_INVALID_SUFFIXES) or (
                    "." + folded_suffix
                ).startswith(_REFERENCE_INVALID_SUFFIXES):
                    return True
        return False

    @staticmethod
    def _has_attached_path_continuation(
        value: str,
        path_end: int,
        *,
        line_end: int,
        path_separator_positions: tuple[int, ...],
        continuation_boundary_positions: tuple[int, ...],
    ) -> bool:
        """Return whether a later slash still belongs to this path operand."""

        cursor = path_end
        if cursor < line_end and value[cursor].isspace():
            return False
        separator_index = bisect_left(path_separator_positions, cursor)
        if (
            separator_index >= len(path_separator_positions)
            or path_separator_positions[separator_index] >= line_end
        ):
            return False
        separator = path_separator_positions[separator_index]

        if cursor == separator:
            return True

        grammar_cursor = cursor
        if value[grammar_cursor] in "中内里的":
            grammar_cursor += 1
            while grammar_cursor < separator and value[grammar_cursor] in " \t":
                grammar_cursor += 1
        matched_action = next(
            (
                prefix
                for prefix in _CJK_PATH_ACTION_PREFIXES
                if value.startswith(prefix, grammar_cursor, separator)
            ),
            None,
        )
        if matched_action is not None:
            action_end = grammar_cursor + len(matched_action)
            while action_end < separator and value[action_end] in " \t":
                action_end += 1
            # `中修改src/a.py` has complete action grammar before the separator;
            # `修改/bar.py` does not, so the latter remains one path operand.
            return action_end == separator

        # A continuous repository-name fragment before `/` is still part of
        # the path. Whitespace or prose/URL punctuation establishes a boundary.
        boundary_index = bisect_left(continuation_boundary_positions, cursor)
        return (
            boundary_index >= len(continuation_boundary_positions)
            or continuation_boundary_positions[boundary_index] >= separator
        )

    @classmethod
    def _attached_cjk_ambiguous_reference(
        cls,
        value: str,
        *,
        start: int,
        end: int,
        line_end: int,
        path_separator_positions: tuple[int, ...],
        continuation_boundary_positions: tuple[int, ...],
    ) -> tuple[int, _ParsedFileReference] | None:
        """Choose the same complete endpoint used by a compact ``@path`` replay."""

        if start >= end:
            return None
        possible_prefixes = _CJK_PATH_AMBIGUITY_PREFIXES_BY_INITIAL.get(value[start], ())
        matched_prefix = next(
            (prefix for prefix in possible_prefixes if value.startswith(prefix, start, end)),
            None,
        )
        if matched_prefix is None:
            return None

        operand_start = start + len(matched_prefix)
        candidate_limit = min(end, start + 500)
        folded_segment = value[start:candidate_limit].casefold()
        candidate_ends: set[int] = set()
        relative_operand_start = operand_start - start
        for suffix in _SUPPORTED_REFERENCE_SUFFIXES:
            suffix_start = folded_segment.find(suffix, relative_operand_start)
            while suffix_start >= 0:
                candidate_ends.add(start + suffix_start + len(suffix))
                suffix_start = folded_segment.find(suffix, suffix_start + len(suffix))

        viable_candidates: list[tuple[int, str]] = []
        for candidate_end in sorted(candidate_ends):
            candidate = cls._reference_candidate(value[start:candidate_end])
            if candidate != value[start:candidate_end]:
                continue
            operand = value[operand_start:candidate_end]
            if cls._reference_candidate(operand) != operand:
                continue
            if cls._has_attached_path_continuation(
                value,
                candidate_end,
                line_end=line_end,
                path_separator_positions=path_separator_positions,
                continuation_boundary_positions=continuation_boundary_positions,
            ):
                continue

            suffix_span: tuple[int, int] | None = (candidate_end, line_end)
            if matched_prefix in _CJK_PATH_LOCATION_PREFIXES:
                suffix_span = cls._attached_cjk_location_suffix_span(
                    value,
                    candidate_end,
                    line_end=line_end,
                )
                if suffix_span is None:
                    if not cls._has_reference_termination(value, candidate_end):
                        continue
                    suffix_span = (candidate_end, line_end) if candidate_end < line_end else None
                return (
                    candidate_end,
                    _ParsedFileReference(
                        candidate,
                        target_eligible=False,
                        ambiguous_cjk_prefix=matched_prefix,
                        ambiguous_cjk_suffix_span=suffix_span,
                    ),
                )

            viable_candidates.append((candidate_end, candidate))
            if not (
                cls._has_reference_termination(value, candidate_end)
                or cls._attached_cjk_location_suffix_span(
                    value,
                    candidate_end,
                    line_end=line_end,
                )
                is not None
            ):
                continue
            return (
                candidate_end,
                _ParsedFileReference(
                    candidate,
                    target_eligible=False,
                    ambiguous_cjk_prefix=matched_prefix,
                    ambiguous_cjk_suffix_span=(candidate_end, line_end),
                ),
            )

        if viable_candidates:
            candidate_end, candidate = viable_candidates[-1]
            if cls._has_reference_termination(
                value,
                candidate_end,
                allow_cjk_continuation=True,
            ):
                return (
                    candidate_end,
                    _ParsedFileReference(
                        candidate,
                        target_eligible=False,
                        ambiguous_cjk_prefix=matched_prefix,
                        ambiguous_cjk_suffix_span=(candidate_end, line_end),
                    ),
                )
        return None

    @staticmethod
    def _attached_cjk_prefix(candidate: str) -> str | None:
        if not candidate:
            return None
        for marker in _CJK_PATH_AMBIGUITY_PREFIXES_BY_INITIAL.get(candidate[0], ()):
            if candidate.startswith(marker) and candidate[len(marker) :]:
                return marker
        return None

    @staticmethod
    def _has_cjk_path_continuation(value: str) -> bool:
        folded = value.casefold()
        for suffix in _SUPPORTED_REFERENCE_SUFFIXES:
            suffix_start = folded.find(suffix)
            while suffix_start >= 0:
                suffix_end = suffix_start + len(suffix)
                if suffix_end < len(value) and value[suffix_end] in "中内里的":
                    return True
                suffix_start = folded.find(suffix, suffix_end)
        return False

    @staticmethod
    @lru_cache(maxsize=64)
    def _reference_candidate(value: str) -> str | None:
        candidate = value.strip()
        if any(character.isspace() and character not in " \t" for character in candidate):
            return None
        if not candidate.casefold().endswith(_SUPPORTED_REFERENCE_SUFFIXES):
            return None
        if _URI_SCHEME.match(candidate) is not None:
            return None
        try:
            return validate_repository_path(candidate)
        except ValueError:
            return None

    @staticmethod
    def _candidate_has_unambiguous_shape(
        candidate: str,
        *,
        allow_spaced_root: bool,
    ) -> bool:
        has_whitespace = any(character.isspace() for character in candidate)
        if not has_whitespace:
            return allow_spaced_root or _PROSE_PATH_PREFIX.search(candidate) is None
        if _PROSE_PATH_PREFIX.search(candidate) is not None:
            return False
        if "/" in candidate:
            first_segment = candidate.split("/", 1)[0]
            if not any(character.isspace() for character in first_segment):
                return True
            return allow_spaced_root and _PROSE_PATH_PREFIX.search(candidate) is None
        return allow_spaced_root and _PROSE_PATH_PREFIX.search(candidate) is None

    @classmethod
    def _labeled_reference_candidate(cls, value: str) -> tuple[str, int] | None:
        if value.startswith(_COMPACT_PATH_SIGIL):
            candidate_limit = min(len(value), len(_COMPACT_PATH_SIGIL) + 500)
            folded = value[:candidate_limit].casefold()
            path_separator_positions = tuple(
                index for index, character in enumerate(value) if character in "/\\"
            )
            continuation_boundary_positions = tuple(
                index
                for index, character in enumerate(value)
                if character.isspace()
                or character in _BARE_REFERENCE_FORBIDDEN
                or character in _SEMANTIC_BARE_SEPARATORS
            )
            candidate_ends: set[int] = set()
            for suffix in _SUPPORTED_REFERENCE_SUFFIXES:
                suffix_start = folded.find(suffix, len(_COMPACT_PATH_SIGIL))
                while suffix_start >= 0:
                    candidate_ends.add(suffix_start + len(suffix))
                    suffix_start = folded.find(suffix, suffix_start + len(suffix))
            viable_candidates: list[tuple[int, str]] = []
            for candidate_end in sorted(candidate_ends):
                candidate = cls._reference_candidate(
                    value[len(_COMPACT_PATH_SIGIL) : candidate_end]
                )
                if candidate is None:
                    continue
                if cls._has_attached_path_continuation(
                    value,
                    candidate_end,
                    line_end=len(value),
                    path_separator_positions=path_separator_positions,
                    continuation_boundary_positions=continuation_boundary_positions,
                ):
                    continue
                viable_candidates.append((candidate_end, candidate))
                if (
                    cls._has_reference_termination(value, candidate_end)
                    or cls._attached_cjk_location_suffix_span(
                        value,
                        candidate_end,
                        line_end=len(value),
                    )
                    is not None
                ):
                    return candidate, len(_COMPACT_PATH_SIGIL)
            if viable_candidates:
                candidate_end, candidate = viable_candidates[-1]
                if cls._has_reference_termination(
                    value,
                    candidate_end,
                    allow_cjk_continuation=True,
                ):
                    return candidate, len(_COMPACT_PATH_SIGIL)
            return None

        match = _LABELED_REFERENCE.fullmatch(value)
        if match is None:
            return None
        raw_candidate = match.group("value").rstrip(_BARE_TRAILING_PUNCTUATION)
        candidate_text = raw_candidate
        for opener, closer in _REFERENCE_WRAPPERS:
            if raw_candidate.startswith(opener) and raw_candidate.endswith(closer):
                candidate_text = raw_candidate[len(opener) : -len(closer)]
                break
        candidate = cls._reference_candidate(candidate_text)
        if candidate is None:
            return None
        return candidate, match.start("value") + raw_candidate.find(candidate_text)

    @classmethod
    def _compact_replay_candidate(cls, value: str) -> str | None:
        """Select the first explicit target exactly as a standalone compact replay does."""

        if not value.startswith(_COMPACT_PATH_SIGIL) or any(
            character.isspace() for character in value
        ):
            return None
        for coarse_segment in _BARE_TOKEN_SEGMENT.finditer(value):
            for semantic_start, semantic_end in cls._bare_reference_segments(
                coarse_segment.group()
            ):
                segment_start = coarse_segment.start() + semantic_start
                segment_end = coarse_segment.start() + semantic_end
                if segment_start != 0:
                    return None
                labeled_candidate = cls._labeled_reference_candidate(value[:segment_end])
                if labeled_candidate is None or not cls._has_reference_termination(
                    value,
                    segment_end,
                    allow_cjk_continuation=True,
                ):
                    return None
                return labeled_candidate[0]
        return None

    @classmethod
    def _looks_like_url_token(cls, value: str) -> bool:
        if cls._has_cjk_action_path_after_idna_dot(value):
            return False
        if value and normalize("NFKC", value[0]) in _COMPACT_CLAUSE_SEPARATORS:
            cjk_action = next(
                (prefix for prefix in _CJK_PATH_ACTION_PREFIXES if value.startswith(prefix, 1)),
                None,
            )
            if (
                cjk_action is not None
                and cls._bare_reference_candidate(value[1 + len(cjk_action) :]) is not None
            ):
                return False
        value = value.translate(_IDNA_DOT_TRANSLATION)
        if _URL_LABEL_TOKEN.fullmatch(value) is not None:
            return True
        has_query_or_fragment = "?" in value or "#" in value
        has_url_delimiter = "/" in value or has_query_or_fragment
        if (
            (":" in value and _URI_SCHEME.search(value))
            or "//" in value
            or value.lstrip("([{<\"'“‘「『【（").startswith(("?", "#"))
            or (has_query_or_fragment and cls._has_url_query_or_fragment(value))
        ):
            return True
        if not has_url_delimiter:
            return False
        authority_probe = value[:2048]
        if not any(delimiter in authority_probe for delimiter in "/?#"):
            # The issue itself is bounded, but URL regexes intentionally inspect
            # only a smaller prefix. If one same-token prefix exhausts that probe
            # before its delimiter, treat the full token as opaque instead of
            # letting a later wrapper or Markdown label become a repository path.
            return len(value) > len(authority_probe)
        potential_authority = (
            "." in authority_probe
            or "[" in authority_probe
            or "localhost" in authority_probe.casefold()
        )
        return bool(
            (potential_authority and _HOST_WITH_URL_DELIMITER.search(authority_probe))
            or ("@" in authority_probe and _EMAIL_WITH_URL_DELIMITER.search(authority_probe))
            or (potential_authority and _BARE_AUTHORITY_WITH_DELIMITER.search(authority_probe))
            or (
                "/" in authority_probe
                and has_query_or_fragment
                and _SINGLE_LABEL_AUTHORITY_QUERY.search(authority_probe)
            )
        )

    @staticmethod
    def _has_url_query_or_fragment(value: str) -> bool:
        marker = _URL_QUERY_OR_FRAGMENT.search(value)
        if marker is None:
            return False
        return "/" in value[: marker.start()] or "=" in value[marker.end() :]

    @classmethod
    def _idna_url_spans(cls, value: str) -> tuple[tuple[int, int], ...]:
        if not any(dot in value for dot in _IDNA_DOT_CHARACTERS) or not any(
            delimiter in value for delimiter in "/?#"
        ):
            return ()

        spans: list[tuple[int, int]] = []
        value_length = len(value)
        cursor = 0
        authority_start: int | None = None
        label_length = 0
        saw_dot = False
        saw_idna_dot = False

        while cursor < value_length:
            character = value[cursor]
            if not character.isspace() and character not in _IDNA_AUTHORITY_FORBIDDEN:
                if authority_start is None:
                    authority_start = cursor
                label_length += 1
                cursor += 1
                continue

            if character in _IDNA_AUTHORITY_DOTS:
                if authority_start is not None and label_length:
                    saw_dot = True
                    saw_idna_dot = saw_idna_dot or character in _IDNA_DOT_CHARACTERS
                    label_length = 0
                else:
                    authority_start = None
                    label_length = 0
                    saw_dot = False
                    saw_idna_dot = False
                cursor += 1
                continue

            delimiter_index: int | None = None
            has_complete_authority = (
                authority_start is not None and label_length > 0 and saw_dot and saw_idna_dot
            )
            if has_complete_authority and character in "/?#":
                delimiter_index = cursor
            elif has_complete_authority and character == ":":
                port_end = cursor + 1
                while port_end < value_length and value[port_end].isdigit():
                    port_end += 1
                if port_end > cursor + 1 and port_end < value_length and value[port_end] in "/?#":
                    delimiter_index = port_end

            if delimiter_index is None or authority_start is None:
                authority_start = None
                label_length = 0
                saw_dot = False
                saw_idna_dot = False
                cursor += 1
                continue

            authority_end = delimiter_index + 1
            valid_start = (
                authority_start == 0
                or value[max(0, authority_start - 2) : authority_start] == "//"
                or value[authority_start - 1].isspace()
                or value[authority_start - 1] in "=:：<,，、;；！？…⋯"
            )
            if not valid_start:
                cursor = authority_end
                authority_start = None
                label_length = 0
                saw_dot = False
                saw_idna_dot = False
                continue

            end = authority_end
            while (
                end < value_length
                and not value[end].isspace()
                and value[end] not in _REFERENCE_CONTEXT_BOUNDARIES
            ):
                end += 1
            if cls._has_cjk_action_path_after_idna_dot(
                value,
                start=authority_start,
                end=end,
            ):
                cursor = end
                authority_start = None
                label_length = 0
                saw_dot = False
                saw_idna_dot = False
                continue
            spans.append((authority_start, end))
            cursor = end
            authority_start = None
            label_length = 0
            saw_dot = False
            saw_idna_dot = False
        return tuple(spans)

    @classmethod
    def _has_cjk_action_path_after_idna_dot(
        cls,
        value: str,
        *,
        start: int = 0,
        end: int | None = None,
    ) -> bool:
        scan_end = len(value) if end is None else end
        for index in range(start, scan_end):
            character = value[index]
            if character not in _IDNA_DOT_CHARACTERS:
                continue

            remainder_start = index + 1
            lookahead_end = min(scan_end, remainder_start + _MAX_CJK_ACTION_LOOKAHEAD)
            has_action_prefix = value.startswith(
                _CJK_PATH_ACTION_PREFIXES,
                remainder_start,
                lookahead_end,
            )
            has_location_prefix = any(
                value.startswith(prefix, remainder_start, lookahead_end)
                and cls._has_cjk_path_continuation(
                    value[remainder_start + len(prefix) : lookahead_end]
                )
                for prefix in _CJK_PATH_LOCATION_PREFIXES
            )
            if not (has_action_prefix or has_location_prefix):
                continue

            remainder = value[remainder_start:lookahead_end]
            if cls._bare_reference_candidate(
                remainder
            ) is not None and not cls._has_explicit_uri_context_before(value, index):
                return True
        return False

    @classmethod
    def _has_explicit_uri_context_before(cls, value: str, end: int) -> bool:
        boundary_characters = _REFERENCE_CONTEXT_BOUNDARIES - _IDNA_DOT_CHARACTERS
        lookbehind_start = max(0, end - _MAX_URI_CONTEXT_LOOKBEHIND)
        context_start = (
            max(
                (value.rfind(boundary, lookbehind_start, end) for boundary in boundary_characters),
                default=lookbehind_start - 1,
            )
            + 1
        )
        context_start = max(context_start, lookbehind_start)
        prefix = value[context_start:end].strip()
        if not prefix or cls._ends_with_complete_labeled_url_value(prefix):
            return False
        if "//" in prefix or "www." in prefix.translate(_IDNA_DOT_TRANSLATION).casefold():
            return True
        scheme = _URI_SCHEME.search(prefix)
        if scheme is not None and _PATH_ACTION_PREFIX.fullmatch(prefix[: scheme.end()]) is None:
            return True
        for delimiter in "=:：":
            delimiter_index = prefix.find(delimiter)
            if delimiter_index < 0:
                continue
            label = prefix[: delimiter_index + 1].rsplit(maxsplit=1)[-1]
            if _URL_LABEL_TOKEN.fullmatch(label) is not None:
                return True
        return False

    @classmethod
    def _unprotected_clause_start(
        cls,
        start: int,
        end: int,
        protected_spans: _ProtectedSpans,
        value: str,
    ) -> int:
        overlaps = cls._overlapping_spans((start, end), protected_spans)
        if (
            overlaps
            and overlaps[0][0] <= start < overlaps[0][1]
            and overlaps[0][1] < end
            and (
                value[overlaps[0][1]] in _SAME_TOKEN_LIST_SEPARATORS
                or value[overlaps[0][1]] in _SEMANTIC_BARE_SEPARATORS
            )
            and not cls._is_escaped(value, overlaps[0][1])
        ):
            start = overlaps[0][1] + 1
        return start

    @staticmethod
    def _spans_overlap(
        candidate: tuple[int, int],
        spans: _ProtectedSpans,
    ) -> bool:
        if isinstance(spans, _ProtectedSpanInventory):
            return spans.overlaps(candidate)
        index = bisect_left(spans, (candidate[0], -1))
        if index > 0 and candidate[0] < spans[index - 1][1]:
            return True
        return index < len(spans) and candidate[1] > spans[index][0]

    @staticmethod
    def _span_starts_in_protected(
        candidate: tuple[int, int],
        spans: _ProtectedSpans,
    ) -> bool:
        if isinstance(spans, _ProtectedSpanInventory):
            return spans.starts_in_protected(candidate)
        index = bisect_left(spans, (candidate[0], -1))
        if index > 0:
            previous = spans[index - 1]
            if previous[0] <= candidate[0] < previous[1]:
                return True
        return index < len(spans) and spans[index][0] == candidate[0]

    @staticmethod
    def _overlapping_spans(
        candidate: tuple[int, int],
        spans: _ProtectedSpans,
    ) -> tuple[tuple[int, int], ...]:
        if isinstance(spans, _ProtectedSpanInventory):
            return spans.overlapping(candidate)
        index = bisect_left(spans, (candidate[0], -1))
        if index > 0 and spans[index - 1][1] > candidate[0]:
            index -= 1
        overlaps: list[tuple[int, int]] = []
        while index < len(spans) and spans[index][0] < candidate[1]:
            if spans[index][1] > candidate[0]:
                overlaps.append(spans[index])
            index += 1
        return tuple(overlaps)

    @staticmethod
    def _add_protected_span(
        spans: _ProtectedSpans,
        span: tuple[int, int],
    ) -> None:
        if isinstance(spans, _ProtectedSpanInventory):
            spans.add(span)
            return
        if not spans or span[0] >= spans[-1][1]:
            spans.append(span)
            return
        start, end = span
        index = bisect_left(spans, (start, -1))
        if index > 0 and spans[index - 1][1] > start:
            index -= 1
            start = min(start, spans[index][0])
            end = max(end, spans[index][1])
            spans.pop(index)
        while index < len(spans) and spans[index][0] < end:
            start = min(start, spans[index][0])
            end = max(end, spans[index][1])
            spans.pop(index)
        spans.insert(index, (start, end))

    @staticmethod
    def _previous_protected_span(
        spans: _ProtectedSpans,
        position: int,
    ) -> tuple[int, int] | None:
        if isinstance(spans, _ProtectedSpanInventory):
            return spans.previous(position)
        index = bisect_left(spans, (position, -1))
        return spans[index - 1] if index > 0 else None

    @staticmethod
    def _has_reference_termination(
        value: str,
        end: int,
        *,
        allow_cjk_continuation: bool = False,
    ) -> bool:
        if end >= len(value) or value[end].isspace():
            return True
        folded_remaining = normalize(
            "NFKC",
            value[end : end + _MAX_REFERENCE_INVALID_SUFFIX_LENGTH],
        ).casefold()
        if folded_remaining.startswith(_REFERENCE_INVALID_SUFFIXES):
            return False
        if allow_cjk_continuation and "\u3400" <= value[end] <= "\u9fff":
            return True
        cursor = end
        while cursor < len(value) and value[cursor] in _REFERENCE_TRAILING_PUNCTUATION:
            cursor += 1
        if cursor == end:
            return False
        if (
            value[end] == "."
            and cursor < len(value)
            and (value[cursor].isascii() and (value[cursor].isalnum() or value[cursor] == "_"))
        ):
            return False
        folded_after_punctuation = normalize(
            "NFKC",
            value[cursor : cursor + _MAX_REFERENCE_INVALID_SUFFIX_LENGTH],
        ).casefold()
        if folded_after_punctuation.startswith(_REFERENCE_INVALID_SUFFIXES):
            return False
        return True

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
        explicit_target: tuple[str, bool] | None,
        low_confidence: bool,
    ) -> list[FileReference]:
        if explicit_target is not None:
            path, exists = explicit_target
            if exists:
                document = next(document for document in source_documents if document.path == path)
                return [
                    self._reference(
                        document,
                        evidence_by_path,
                        action=FileAction.MODIFY,
                        reason="Issue-explicit Python implementation path backed by evidence.",
                    )
                ]
            anchor = source_documents[0] if source_documents else fallback_document
            return [
                FileReference(
                    path=path,
                    action=FileAction.CREATE,
                    exists=False,
                    reason=(
                        "The Issue explicitly requests this safe repository-relative Python "
                        "implementation path, which is absent from the inspected tree."
                    ),
                    evidence_ids=[evidence_by_path[anchor.path].id],
                )
            ]
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
        package_name = self._inferred_package_name(snapshot.repository.name)
        path = f"src/{package_name}/feature.py"
        self._assert_creatable_path(snapshot, path)
        return [
            FileReference(
                path=path,
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
        explicit_target: tuple[str, bool] | None,
        low_confidence: bool,
    ) -> list[FileReference]:
        if explicit_target is not None:
            path, exists = explicit_target
            if exists:
                document = next(document for document in test_documents if document.path == path)
                return [
                    self._reference(
                        document,
                        evidence_by_path,
                        action=FileAction.MODIFY,
                        reason="Issue-explicit regression-test path backed by evidence.",
                    )
                ]
            anchor = (
                test_documents[0]
                if test_documents
                else (config_documents[0] if config_documents else fallback_document)
            )
            return [
                FileReference(
                    path=path,
                    action=FileAction.CREATE,
                    exists=False,
                    reason=(
                        "The Issue explicitly requests this safe repository-relative regression "
                        "test path, which is absent from the inspected tree."
                    ),
                    evidence_ids=[evidence_by_path[anchor.path].id],
                )
            ]
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
        path = f"tests/test_{issue_slug}.py"
        self._assert_creatable_path(snapshot, path)
        return [
            FileReference(
                path=path,
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
    def _inferred_package_name(repository_name: str) -> str:
        package_name = _NON_IDENTIFIER.sub("_", repository_name.casefold()).strip("_")
        package_name = package_name or "change"
        if not package_name.isidentifier() or keyword.iskeyword(package_name):
            package_name = f"repopilot_{package_name}"
        if not package_name.isidentifier() or keyword.iskeyword(package_name):
            raise AssertionError("inferred Python package name must be importable")
        return package_name

    @staticmethod
    def _verification_intents(
        evidence: list[EvidenceItem],
    ) -> list[VerificationIntent]:
        """Create intents only from declarations already bound to evidence windows."""

        intents: list[VerificationIntent] = []

        for tool in ("pytest", "ruff", "mypy"):
            allowed_kinds = (
                {
                    VerificationDeclarationKind.COMMAND,
                    VerificationDeclarationKind.CONFIGURATION,
                }
                if tool == "pytest"
                else {VerificationDeclarationKind.COMMAND}
            )
            declaring_evidence = next(
                (
                    item
                    for item in evidence
                    if any(
                        declaration.tool == tool
                        and declaration.kind in allowed_kinds
                        and declaration.arguments == []
                        for declaration in item.declared_tools
                    )
                ),
                None,
            )
            if declaring_evidence is None:
                continue
            intents.append(
                VerificationIntent(
                    tool=tool,
                    arguments=[],
                    evidence_ids=[declaring_evidence.id],
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
        multi_target_categories: tuple[EvidenceCategory, ...],
        verification_needs_human_input: bool,
    ) -> list[str]:
        categories = {document.category for document in snapshot.documents}
        risks: list[str] = []

        if verification_needs_human_input:
            risks.append(
                "No evidence-backed Python test runner was observed; verification needs human "
                "input before a future execution stage."
            )

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
        if multi_target_categories:
            category_names = " and ".join(category.value for category in multi_target_categories)
            risks.append(
                "M0 emits only the first explicit target per category; additional "
                f"{category_names} paths were audited for repository evidence but remain "
                "deferred multi-file scope."
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
        self._validate_inspected_repository(request, snapshot)
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

    @staticmethod
    def _validate_inspected_repository(
        request: CreatePlanRequest,
        snapshot: RepositorySnapshot,
    ) -> None:
        requested = request.repository
        inspected = snapshot.repository
        identity_drifted = (
            inspected.url != requested.url
            or inspected.owner != requested.owner
            or inspected.name != requested.name
            or (requested.ref is not None and inspected.ref != requested.ref)
        )
        if identity_drifted:
            raise RepositoryUpstreamError(
                "repository inspector returned an inconsistent repository identity"
            )
