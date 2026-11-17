#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Load the single recurring upstream review class list with bounded safe YAML."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

CLASS_FILE = Path(__file__).resolve().parents[1] / ".config" / "upstream-review-classes.yaml"
FIELDS = {"id", "hits", "pull_requests", "definition", "question", "check"}
CLASS_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", re.ASCII)
CHECK = re.compile(r"(?:upstream-lint|variant-sweep):[a-z][a-z0-9-]*|script:[\w./-]+", re.ASCII)


def sibling_module(name: str) -> ModuleType:
    """Load a trusted helper by resolved sibling path under isolated Python."""
    path = Path(__file__).resolve().with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_ffmpeg_{name}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plain_text(value: object) -> bool:
    """Accept trimmed printable ASCII text that fits one Markdown table cell."""
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and value.isascii()
        and value.isprintable()
        and "|" not in value
    )


def _is_integer(value: object) -> bool:
    """Accept integers while refusing boolean values."""
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_pull_requests(requests: object, label: str) -> None:
    """Require nonempty, unique, strictly ascending positive pull request numbers."""
    if not isinstance(requests, list) or not requests:
        raise ValueError(f"{label}: missing or invalid pull_requests")
    if any(not _is_integer(number) or number < 1 for number in requests):
        raise ValueError(f"{label}: invalid pull request number")
    if len(requests) != len(set(requests)):
        raise ValueError(f"{label}: duplicate pull request number")
    if requests != sorted(requests):
        raise ValueError(f"{label}: pull_requests must be strictly ascending")


def validate_item(item: object, path: Path) -> dict:
    """Validate class fields, ascending pull requests, and a table-safe question ending in ?."""
    if not isinstance(item, dict) or not FIELDS - {"check"} <= item.keys() <= FIELDS:
        raise ValueError(f"{path}: invalid class fields")
    if not isinstance(item["id"], str) or not CLASS_ID.fullmatch(item["id"]):
        raise ValueError(f"{path}: invalid class id")
    label = f"{path}: {item['id']}"
    if not _is_integer(item["hits"]) or item["hits"] < 2:
        raise ValueError(f"{label}: hits must be an integer >= 2")
    _validate_pull_requests(item["pull_requests"], label)
    for field in ("definition", "question"):
        if not plain_text(item[field]):
            raise ValueError(f"{label}: invalid {field}")
    if not item["question"].endswith("?"):
        raise ValueError(f"{label}: question must end in ?")
    if "check" in item and (
        not isinstance(item["check"], str) or not CHECK.fullmatch(item["check"])
    ):
        raise ValueError(f"{label}: invalid check")
    return item


def load_classes(path: Path = CLASS_FILE) -> list[dict]:
    """Read a nonempty class list, refusing malformed input and duplicate ids."""
    try:
        document = sibling_module("lane_brief").read_yaml(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: cannot load review classes: {error}") from error
    if not isinstance(document, dict) or document.keys() != {"classes"}:
        raise ValueError(f"{path}: expected classes mapping")
    items = document["classes"]
    if not isinstance(items, list) or not items:
        raise ValueError(f"{path}: expected nonempty classes list")
    result = []
    seen = set()
    for raw_item in items:
        item = validate_item(raw_item, path)
        if item["id"] in seen:
            raise ValueError(f"{path}: duplicate class id {item['id']}")
        seen.add(item["id"])
        result.append(item)
    return result
