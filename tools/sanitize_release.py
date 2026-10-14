"""Sanitize export text and fixture identities, preserving functional dates.

Private replacement rules may be supplied from outside the exported tree.
This module never ships those rules or original values in its report.
"""

from __future__ import annotations

import ast
import base64
from datetime import datetime
import io
import json
from pathlib import Path
import re
import runpy
import sys
import textwrap
import tokenize


REFERENCE_DATE = "2026-09-09 00:00:00"
DATE_PATTERN = re.compile(r"(?<!\d)20\d{2}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?(?!\d)")
CAPTURE_PATTERN = re.compile(r"(?<!\d)20\d{6}[_-]\d{6}(?!\d|_sample_)")
MONTH_PATTERN = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(?:20\d{2}|\d{1,2}(?:,?\s+20\d{2})?)\b")
HOME_PATTERN = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s`\"']+(?:[\\/][^\s`\"'<>]+)?")


def editorial_dates(text: str, reference: datetime) -> str:
    def replace_date(match):
        old = match.group()
        if len(old) == 10:
            return reference.strftime("%Y-%m-%d")
        separator = "T" if "T" in old else " "
        suffix = "Z" if old.endswith("Z") else "+00:00" if re.search(r"[+-]\d{2}:\d{2}$", old) else ""
        return reference.strftime("%Y-%m-%d" + separator + "%H:%M:%S") + suffix
    return MONTH_PATTERN.sub(reference.strftime("%Y-%m-%d"), DATE_PATTERN.sub(replace_date, text))


def sanitize_python_dates(text: str, reference: datetime) -> str:
    """Change comments/docstrings, keeping executable datetime/expiry literals."""
    tree = ast.parse(text)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(first.value.lineno)
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    edits = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT or (token.type == tokenize.STRING and token.start[0] in docstrings):
            updated = editorial_dates(token.string, reference)
            if updated != token.string:
                edits.append((offsets[token.start[0] - 1] + token.start[1],
                              offsets[token.end[0] - 1] + token.end[1], updated))
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    # This is descriptive audit metadata, not a gameplay or expiry timestamp.
    text = re.sub(r'("snapshot_saved_at"\s*:\s*")[^"]+(")',
                  lambda m: m[1] + reference.isoformat() + '+00:00' + m[2], text)
    return text


def anonymize_roster(project: Path) -> bool:
    path = project / "tests/fixtures/captured_character_selection.py"
    if not path.is_file():
        return False
    # Load only this export, regardless of the directory name or installed packages.
    saved = {name: module for name, module in sys.modules.items()
             if name == "dso_server" or name.startswith("dso_server.")}
    for name in saved:
        del sys.modules[name]
    try:
        runpy.run_path(str(project / "run.py"))["load_package"]()
        from dso_server.protocol.character_selection import decode_character_selection, encode_character_selection
        fixture = runpy.run_path(str(path))
        model = decode_character_selection(fixture["CHARACTER_SELECTION_PAYLOAD"], fixture["CHARACTER_SELECTION_BIT_LENGTH"])
        active_index = next((i for i, c in enumerate(model.characters) if c.character_id == model.active_character_id), 0)
        for index, character in enumerate(model.characters, 1):
            character.name = f"Example{index:03d}"
            character.character_id = 200000000 + index
            character.unknown_u32_a = 0
        model.active_character_id = model.characters[active_index].character_id if model.characters else 0
        model.header_field_b = 200000003
        model.locale = "EN"
        payload, bit_length = encode_character_selection(model)
        assert encode_character_selection(decode_character_selection(payload, bit_length)) == (payload, bit_length)
    finally:
        for name in list(sys.modules):
            if name == "dso_server" or name.startswith("dso_server."):
                del sys.modules[name]
        sys.modules.update(saved)
    content = path.read_text(encoding="utf-8")
    encoded = "\n".join(textwrap.wrap(base64.b64encode(payload).decode("ascii"), 96))
    content = re.sub(r'_PAYLOAD_B64 = """.*?"""', '_PAYLOAD_B64 = """\n' + encoded + '\n"""', content, flags=re.S)
    content = re.sub(r'CHARACTER_SELECTION_BIT_LENGTH = \d+', f'CHARACTER_SELECTION_BIT_LENGTH = {bit_length}', content)
    first = min(1232, len(payload))
    content = re.sub(r'CAPTURED_FRAGMENT_LENGTHS = \([^\n]+\)', f'CAPTURED_FRAGMENT_LENGTHS = ({first}, {len(payload) - first})', content)
    content = re.sub(r'from captured session [0-9/]+', 'with synthetic names and identifiers', content)
    content = re.sub(r'as session \d+', 'as the anonymized reference fixture', content)
    path.write_text(content, encoding="utf-8")
    return True


def sanitize_release(project: Path, reference_date: str = REFERENCE_DATE, *, rules_path: Path | None = None) -> dict:
    project = project.resolve()
    reference = datetime.strptime(reference_date, "%Y-%m-%d %H:%M:%S")
    rules = json.loads(rules_path.read_text(encoding="utf-8")) if rules_path and rules_path.is_file() else {}
    files = sorted(p for p in project.rglob("*") if p.is_file() and p.name not in ("RELEASE_MANIFEST.json", "SANITIZATION.json"))
    if any(p.is_symlink() or not p.resolve().is_relative_to(project) for p in files):
        raise ValueError("Sanitization targets must stay in the export")
    originals = {path: path.read_text(encoding="utf-8-sig") for path in files}
    captures = sorted({m.group() for content in originals.values() for m in CAPTURE_PATTERN.finditer(content)})
    capture_rules = {old: reference.strftime("%Y%m%d_%H%M%S") + f"_sample_{index:02d}"
                     for index, old in enumerate(captures, 1)}
    changed = []
    for path, original in originals.items():
        content = HOME_PATTERN.sub(lambda m: "logs/" + re.split(r"[\\/]+", m.group())[-1], original)
        for old, new in (*rules.items(), *capture_rules.items()):
            content = content.replace(old, new)
        relative = path.relative_to(project).as_posix()
        if path.suffix in (".md", ".docs", ".txt"):
            content = editorial_dates(content, reference)
        elif path.suffix == ".py":
            content = sanitize_python_dates(content, reference)
        elif path.suffix == ".json" and relative.startswith("tests/fixtures/"):
            document = json.loads(content)
            def scrub(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in ("timestamp", "snapshot_saved_at", "description", "source") and isinstance(child, str):
                            value[key] = editorial_dates(child, reference)
                        elif key in ("character_id", "account_id") and isinstance(child, int):
                            value[key] = 200000001
                        else:
                            scrub(child)
                elif isinstance(value, list):
                    for child in value:
                        scrub(child)
            scrub(document)
            content = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        destination = path
        for old, new in rules.items():
            relative = relative.replace(old, new)
        destination = project / relative
        if destination != path and destination.exists():
            raise ValueError("Sanitized filename would overwrite another file")
        if content != original or destination != path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            if destination != path:
                path.unlink()
            changed.append(relative)
    if anonymize_roster(project):
        changed.append("tests/fixtures/captured_character_selection.py")
    # Private search terms are never stored in the export or report.
    for path in project.rglob("*"):
        if not path.is_file() or path.name in ("RELEASE_MANIFEST.json", "SANITIZATION.json"):
            continue
        content = path.read_text(encoding="utf-8-sig")
        if any(old in content or old in path.name for old in rules):
            raise ValueError(f"Identifying source text remains in {path.relative_to(project)}")
    report = {
        "reference_date": reference_date,
        "scope": "Personal labels, user paths, public endpoint examples, captured roster identities and editorial/capture metadata",
        "changed_files": sorted(set(changed)),
        "dates_preserved": "Executable test dates, membership expiry boundaries, wire-format timestamps and relative protocol timings",
        "archive_timestamps": "Normalized to reference_date; manifest generation time remains actual",
        "note": "Editorial reference dates are normalized examples, not original capture or execution timestamps.",
    }
    (project / "SANITIZATION.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
