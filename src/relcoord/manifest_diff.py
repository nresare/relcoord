# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
"""Render a manifest-builder diff as a GitHub pull request comment.

Ported from the ``diffcomment`` tool in bktools, which produced the same comment
from a manifest-builder run in CI. The git access goes through dulwich rather
than the ``git`` binary, because relcoord's image does not ship one: instead of
staging the generated output and reading ``git diff --cached``, the diff is taken
between the commit a manifests checkout was cloned at and the commit
manifest-builder created on top of it.

Every generated manifest carries a deploy-id annotation, and a change that
touches a shared label or annotation rewrites every manifest that has it. Those
repeated metadata-only changes drown the interesting part of a diff, so they are
summarized and dropped from the diff the comment carries, while the comment
points at the full diff that the response also returns.

Generated ``checksum/`` annotations are dropped the same way: they hash a
ConfigMap or Secret so that a workload restarts when its config changes, so they
say nothing the change to that config does not already say in the same diff.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import cast

import yaml
from dulwich.diff_tree import TreeChange, TreeEntry, tree_changes
from dulwich.diffstat import diffstat
from dulwich.object_store import tree_lookup_path
from dulwich.objects import Blob, ObjectID
from dulwich.patch import write_tree_diff
from dulwich.repo import Repo
from manifest_builder import __version__ as MANIFEST_BUILDER_VERSION

logger = logging.getLogger(__name__)

# GitHub rejects a longer issue comment body.
MAX_COMMENT_CHARS = 65_536
DEPLOY_ID_METADATA_PATH = ("metadata", "annotations", "noa.re/deploy-id")
# Annotations under this prefix hash the config a workload mounts, so that the
# workload restarts when the config changes. Their value is derived from a change
# the same diff already shows, wherever in a manifest they sit.
CHECKSUM_ANNOTATION_PREFIX = "checksum/"
# A label or annotation that changed on this many manifests is reported as one
# summary line instead of once per manifest.
METADATA_SUMMARY_THRESHOLD = 2
NO_CHANGES_MESSAGE = "The generated output is the same before and after this change"
COMMENT_MARKER_PREFIX = "relcoord:manifest-diff"

_DIFF_FILE_HEADER = re.compile(r"^diff --git a/(?P<old>.*) b/(?P<new>.*)$")
_HUNK_HEADER = re.compile(r"^@@ -(?P<start>\d+)(?:,\d+)? \+\d+(?:,\d+)? @@$")
# git's default funcname heuristic for a file with no configured diff driver.
_HUNK_CONTEXT_LINE = re.compile(r"^[A-Za-z_$]")


@dataclass(frozen=True)
class ManifestDiff:
    """The diff for one manifests repository, in the forms a comment needs."""

    stat: str
    diff: str
    summary: str = ""
    filtered_diff: str | None = None


@dataclass(frozen=True)
class DiffSection:
    """One manifests repository's diff, with the heading to render it under.

    ``heading`` is ``None`` when a comment covers a single repository, which is
    the usual case and renders without a heading at all.
    """

    heading: str | None
    diff: ManifestDiff


@dataclass(frozen=True)
class CommentBody:
    body: str
    omitted_context_diff: bool


def manifest_diff(
    repo: Repo, old_tree: ObjectID | None, new_tree: ObjectID
) -> ManifestDiff:
    """Diff two trees of a manifests checkout the way the comment wants it."""
    raw_diff = _tree_diff(repo, old_tree, new_tree)
    summary, filtered_diff = smart_manifest_diff(repo, old_tree, new_tree, raw_diff)
    return ManifestDiff(
        stat=_diff_stat(raw_diff),
        diff=raw_diff,
        summary=summary,
        filtered_diff=filtered_diff,
    )


def _tree_diff(repo: Repo, old_tree: ObjectID | None, new_tree: ObjectID) -> str:
    output = BytesIO()
    write_tree_diff(output, repo.object_store, old_tree, new_tree)
    raw_diff = output.getvalue().decode(errors="replace")
    return _annotate_hunk_headers(repo, old_tree, raw_diff)


def _annotate_hunk_headers(repo: Repo, old_tree: ObjectID | None, raw_diff: str) -> str:
    """Label each hunk header with its enclosing key, the way git does.

    git ends a hunk header with the nearest preceding line of the pre-image that
    starts with a letter, an underscore or a dollar sign, which in a manifest is
    the top-level YAML key the hunk sits under. dulwich writes bare hunk headers,
    and the metadata filtering needs that label to resolve a changed line whose
    own hunk does not reach up to ``metadata:``.
    """
    annotated: list[str] = []
    old_lines: list[str] = []
    for line in raw_diff.splitlines():
        file_header = _DIFF_FILE_HEADER.match(line)
        if file_header is not None:
            old_lines = _tree_file_lines(repo, old_tree, file_header.group("old"))
            annotated.append(line)
            continue
        hunk_header = _HUNK_HEADER.match(line)
        if hunk_header is not None:
            context = _hunk_context(old_lines, int(hunk_header.group("start")))
            annotated.append(line if context is None else f"{line} {context}")
            continue
        annotated.append(line)
    return "\n".join(annotated) + ("\n" if raw_diff.endswith("\n") else "")


def _tree_file_lines(repo: Repo, tree: ObjectID | None, path: str) -> list[str]:
    if tree is None:
        return []
    try:
        _mode, sha = tree_lookup_path(
            repo.object_store.__getitem__, tree, path.encode()
        )
    except KeyError:
        return []
    blob = repo.object_store[sha]
    if not isinstance(blob, Blob):
        return []
    return blob.data.decode(errors="replace").splitlines()


def _hunk_context(old_lines: list[str], start: int) -> str | None:
    for line in reversed(old_lines[: max(start - 1, 0)]):
        if _HUNK_CONTEXT_LINE.match(line):
            return line.rstrip()
    return None


def _diff_stat(raw_diff: str) -> str:
    """Render the ``git diff --stat`` equivalent for a diff."""
    if not raw_diff.strip():
        return ""
    lines = raw_diff.encode().split(b"\n")
    return diffstat(lines).decode(errors="replace") + "\n"


def smart_manifest_diff(
    repo: Repo, old_tree: ObjectID | None, new_tree: ObjectID, raw_diff: str
) -> tuple[str, str | None]:
    """Summarize repeated metadata changes and drop them from the diff.

    Returns the summary and the diff to show in the comment, where the diff is
    ``None`` when nothing was worth filtering out.
    """
    try:
        metadata_changes = summarize_metadata_changes(repo, old_tree, new_tree)
    except yaml.YAMLError, KeyError, OSError:
        logger.exception("Failed to summarize manifest metadata changes")
        return "", None

    summary_paths = {
        change.path
        for change, count in metadata_changes.items()
        if count >= METADATA_SUMMARY_THRESHOLD
    }
    suppress_paths = {DEPLOY_ID_METADATA_PATH, *summary_paths}
    filtered_diff = filter_metadata_hunks(raw_diff, suppress_paths)
    summary = render_metadata_summary(metadata_changes)
    if filtered_diff == raw_diff and not summary:
        return "", None
    return summary, filtered_diff


@dataclass(frozen=True)
class MetadataChange:
    section: str
    key: str
    old: str | None
    new: str | None

    @property
    def path(self) -> tuple[str, str, str]:
        return ("metadata", self.section, self.key)


def summarize_metadata_changes(
    repo: Repo, old_tree: ObjectID | None, new_tree: ObjectID
) -> Counter[MetadataChange]:
    """Count, per label and annotation, how many manifests changed it."""
    changes: Counter[MetadataChange] = Counter()
    for change in tree_changes(repo.object_store, old_tree, new_tree):
        if not _is_yaml_change(change):
            continue
        new_content = _blob_text(repo, change.new)
        if new_content is None:
            continue
        old_content = _blob_text(repo, change.old) or ""
        changes.update(compare_manifest_metadata(old_content, new_content))
    return changes


def _is_yaml_change(change: TreeChange) -> bool:
    entry = change.new if change.new is not None else change.old
    if entry is None or entry.path is None:
        return False
    return entry.path.decode(errors="replace").endswith((".yaml", ".yml"))


def _blob_text(repo: Repo, entry: TreeEntry | None) -> str | None:
    if entry is None or entry.sha is None:
        return None
    blob = repo.object_store[entry.sha]
    if not isinstance(blob, Blob):
        return None
    return blob.data.decode(errors="replace")


def compare_manifest_metadata(
    old_content: str, new_content: str
) -> Counter[MetadataChange]:
    old_docs = parse_yaml_documents(old_content)
    new_docs = parse_yaml_documents(new_content)
    changes: Counter[MetadataChange] = Counter()
    for index, new_doc in enumerate(new_docs):
        old_doc = old_docs[index] if index < len(old_docs) else {}
        changes.update(compare_document_metadata(old_doc, new_doc))
    return changes


def parse_yaml_documents(content: str) -> list[object]:
    if not content.strip():
        return []
    return list(yaml.safe_load_all(content))


def compare_document_metadata(
    old_doc: object, new_doc: object
) -> Counter[MetadataChange]:
    changes: Counter[MetadataChange] = Counter()
    old_metadata = mapping_value(old_doc, "metadata")
    new_metadata = mapping_value(new_doc, "metadata")
    for section in ("labels", "annotations"):
        old_values = string_mapping_value(old_metadata, section)
        new_values = string_mapping_value(new_metadata, section)
        for key in old_values.keys() | new_values.keys():
            if ("metadata", section, key) == DEPLOY_ID_METADATA_PATH:
                continue
            if is_checksum_key(key):
                continue
            old = old_values.get(key)
            new = new_values.get(key)
            if old != new:
                changes[MetadataChange(section, key, old, new)] += 1
    return changes


def mapping_value(value: object, key: str) -> dict[object, object]:
    if not isinstance(value, Mapping):
        return {}
    mapping = cast(Mapping[object, object], value)
    child = mapping.get(key)
    if not isinstance(child, Mapping):
        return {}
    child_mapping = cast(Mapping[object, object], child)
    return {child_key: child_value for child_key, child_value in child_mapping.items()}


def string_mapping_value(value: object, key: str) -> dict[str, str]:
    mapping = mapping_value(value, key)
    return {
        str(child_key): str(child_value) for child_key, child_value in mapping.items()
    }


def render_metadata_summary(metadata_changes: Counter[MetadataChange]) -> str:
    lines = []
    for change, count in sorted(
        metadata_changes.items(),
        key=lambda item: (
            item[0].section,
            item[0].key,
            item[0].old or "",
            item[0].new or "",
        ),
    ):
        if count < METADATA_SUMMARY_THRESHOLD:
            continue
        name = "Label" if change.section == "labels" else "Annotation"
        if change.old is None:
            description = f"{name} `{change.key}` was added with `{change.new}`"
        elif change.new is None:
            description = f"{name} `{change.key}` was removed"
        else:
            description = (
                f"{name} `{change.key}` changed from `{change.old}` to `{change.new}`"
            )
        lines.append(f"- {description} on {count} manifests.")
    return "\n".join(lines)


def filter_metadata_hunks(
    raw_diff: str, suppress_paths: set[tuple[str, str, str]]
) -> str:
    files = split_diff_files(raw_diff)
    filtered_files: list[str] = []
    for file_lines in files:
        filtered = filter_file_hunks(file_lines, suppress_paths)
        if filtered:
            filtered_files.extend(filtered)
    return "\n".join(filtered_files).rstrip("\n")


def split_diff_files(raw_diff: str) -> list[list[str]]:
    files: list[list[str]] = []
    current: list[str] = []
    for line in raw_diff.splitlines():
        if line.startswith("diff --git ") and current:
            files.append(current)
            current = []
        current.append(line)
    if current:
        files.append(current)
    return files


def filter_file_hunks(
    file_lines: list[str], suppress_paths: set[tuple[str, str, str]]
) -> list[str]:
    header: list[str] = []
    hunks: list[list[str]] = []
    current_hunk: list[str] | None = None
    for line in file_lines:
        if line.startswith("@@ "):
            if current_hunk is not None:
                hunks.append(current_hunk)
            current_hunk = [line]
        elif current_hunk is None:
            header.append(line)
        else:
            current_hunk.append(line)
    if current_hunk is not None:
        hunks.append(current_hunk)

    kept_hunks: list[list[str]] = []
    for hunk in hunks:
        if hunk_is_suppressed_metadata(hunk, suppress_paths):
            continue
        filtered_hunk = filter_suppressed_metadata_lines(hunk, suppress_paths)
        if hunk_has_changed_lines(filtered_hunk):
            kept_hunks.append(filtered_hunk)

    if not kept_hunks:
        return []
    return [*header, *[line for hunk in kept_hunks for line in hunk]]


def is_checksum_key(key: str) -> bool:
    return key.startswith(CHECKSUM_ANNOTATION_PREFIX)


def suppressed_yaml_paths(
    hunk: list[str], suppress_paths: set[tuple[str, str, str]]
) -> set[tuple[str, ...]]:
    """Work out which of a hunk's changed paths are noise to be dropped.

    A path is noise when it names a suppressed key, and so is a path whose every
    changed child is noise: an ``annotations:`` line a manifest only gained to
    carry a deploy-id goes the same way the deploy-id itself does.
    """
    changed_paths = changed_yaml_paths(hunk)
    suppressed = {
        path
        for path in changed_paths
        if path in suppress_paths or (path and is_checksum_key(path[-1]))
    }
    for path in sorted(changed_paths - suppressed, key=len, reverse=True):
        held = {
            other
            for other in changed_paths
            if len(other) > len(path) and other[: len(path)] == path
        }
        if held and held <= suppressed:
            suppressed.add(path)
    return suppressed


def hunk_is_suppressed_metadata(
    hunk: list[str], suppress_paths: set[tuple[str, str, str]]
) -> bool:
    changed_paths = changed_yaml_paths(hunk)
    if not changed_paths:
        return False
    return changed_paths <= suppressed_yaml_paths(hunk, suppress_paths)


def changed_yaml_paths(hunk: list[str]) -> set[tuple[str, ...]]:
    old_stack = hunk_header_yaml_stack(hunk[0])
    new_stack = hunk_header_yaml_stack(hunk[0])
    changed_paths: set[tuple[str, ...]] = set()
    for line in hunk[1:]:
        if not line:
            continue
        marker = line[0]
        if marker not in " +-":
            continue
        content = line[1:]
        if marker == " ":
            yaml_mapping_path(content, old_stack)
            yaml_mapping_path(content, new_stack)
            continue
        stack = old_stack if marker == "-" else new_stack
        path = yaml_mapping_path(content, stack)
        if path:
            changed_paths.add(path)
    return changed_paths


def filter_suppressed_metadata_lines(
    hunk: list[str], suppress_paths: set[tuple[str, str, str]]
) -> list[str]:
    suppressed_paths = suppressed_yaml_paths(hunk, suppress_paths)
    suppressed_keys = {path[-1] for path in suppress_paths}
    old_stack = hunk_header_yaml_stack(hunk[0])
    new_stack = hunk_header_yaml_stack(hunk[0])
    filtered = [hunk[0]]
    for line in hunk[1:]:
        if not line:
            filtered.append(line)
            continue
        marker = line[0]
        if marker not in " +-":
            filtered.append(line)
            continue
        content = line[1:]
        if marker == " ":
            yaml_mapping_path(content, old_stack)
            yaml_mapping_path(content, new_stack)
            filtered.append(line)
            continue
        stack = old_stack if marker == "-" else new_stack
        path = yaml_mapping_path(content, stack)
        key = yaml_mapping_key(content)
        if (
            path in suppressed_paths
            or (key is not None and is_checksum_key(key))
            or (hunk_header_yaml_stack(hunk[0]) and key in suppressed_keys)
        ):
            continue
        filtered.append(line)
    return filtered


def hunk_has_changed_lines(hunk: list[str]) -> bool:
    return any(line.startswith(("+", "-")) for line in hunk[1:])


def hunk_header_yaml_stack(header: str) -> list[tuple[int, str]]:
    """Read the mapping key a hunk header names as its context, if any.

    dulwich writes bare hunk headers, so this only finds something for a diff
    that came from somewhere else; the mapping keys in a hunk's own context
    lines are what usually establishes the path of a changed line.
    """
    match = re.match(r"^@@ .* @@\s+([^:\s][^:]*)\s*:\s*$", header)
    if not match:
        return []
    return [(0, match.group(1).strip().strip("\"'"))]


def yaml_mapping_key(line: str) -> str | None:
    match = re.match(r"^\s*([^:#][^:]*):(?:\s.*)?$", line)
    if not match:
        return None
    return match.group(1).strip().strip("\"'")


def yaml_mapping_path(line: str, stack: list[tuple[int, str]]) -> tuple[str, ...]:
    match = re.match(r"^(\s*)([^:#][^:]*):(?:\s.*)?$", line)
    if not match:
        return tuple(key for _, key in stack)

    indent = len(match.group(1))
    key = match.group(2).strip().strip("\"'")
    while stack and stack[-1][0] >= indent:
        stack.pop()
    path = tuple([key for _, key in stack] + [key])
    stack.append((indent, key))
    return path


@dataclass(frozen=True)
class _RenderedSection:
    lines: tuple[str, ...]
    context_lines: tuple[str, ...]
    diff_was_filtered: bool
    empty: bool


def comment_marker() -> str:
    """The hidden line a comment carries so a later diff can find and edit it."""
    return f"<!-- {COMMENT_MARKER_PREFIX} -->"


def build_comment_body(
    sections: Sequence[DiffSection],
    *,
    full_diff_reference: str,
    marker: str | None = None,
) -> CommentBody:
    """Render the comment for one or more manifests repository diffs.

    The context diff is dropped altogether when a comment carrying it would be
    longer than GitHub accepts; ``omitted_context_diff`` reports whether the
    comment leaves the reader needing the full diff.
    """
    preamble = [marker, ""] if marker is not None else []
    metadata = [f"manifest-builder version: `{MANIFEST_BUILDER_VERSION}`"]
    rendered = [_render_section(section, full_diff_reference) for section in sections]
    if all(section.empty for section in rendered):
        return CommentBody(
            body="\n".join([*preamble, NO_CHANGES_MESSAGE, "", *metadata]),
            omitted_context_diff=False,
        )

    diff_was_filtered = any(section.diff_was_filtered for section in rendered)
    with_context = [
        line
        for section in rendered
        for line in (*section.lines, *section.context_lines)
    ]
    body_with_context = "\n".join([*preamble, *with_context, "", *metadata])
    if len(body_with_context) <= MAX_COMMENT_CHARS:
        return CommentBody(
            body=body_with_context, omitted_context_diff=diff_was_filtered
        )

    too_large = (
        "_The full context diff is too large for a GitHub comment and "
        f"has been {full_diff_reference}._"
    )
    without_context: list[str] = []
    for section in rendered:
        without_context.extend(section.lines)
        if section.context_lines:
            without_context.extend(["", too_large])
    return CommentBody(
        body="\n".join([*preamble, *without_context, "", *metadata]),
        omitted_context_diff=True,
    )


def _render_section(section: DiffSection, full_diff_reference: str) -> _RenderedSection:
    diff = section.diff
    stat = diff.stat.rstrip()
    context_diff = (
        diff.filtered_diff if diff.filtered_diff is not None else diff.diff
    ).strip()
    summary = diff.summary.strip()
    heading = [f"### {section.heading}", ""] if section.heading is not None else []

    if not stat and not context_diff and not summary:
        return _RenderedSection(
            lines=tuple([*heading, NO_CHANGES_MESSAGE] if heading else []),
            context_lines=(),
            diff_was_filtered=False,
            empty=True,
        )

    lines = list(heading)
    if stat:
        stat_fence = markdown_fence(stat)
        lines.extend([stat_fence, stat, stat_fence])

    if summary:
        lines.extend(["", "Metadata changes:", "", summary])

    diff_was_filtered = (
        diff.filtered_diff is not None and diff.filtered_diff != diff.diff
    )
    if diff_was_filtered:
        lines.extend(
            [
                "",
                (
                    "_Repeated metadata-only changes have been summarized or omitted. "
                    f"The full diff has been {full_diff_reference}._"
                ),
            ]
        )

    context_lines: list[str] = []
    if context_diff:
        context_lines = [
            "",
            f"{markdown_fence(context_diff)}diff",
            context_diff,
            markdown_fence(context_diff),
        ]

    return _RenderedSection(
        lines=tuple(lines),
        context_lines=tuple(context_lines),
        diff_was_filtered=diff_was_filtered,
        empty=False,
    )


def markdown_fence(text: str) -> str:
    """Return a fence long enough to hold text that has backticks of its own."""
    longest_backtick_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", text)), default=0
    )
    return "`" * max(3, longest_backtick_run + 1)
