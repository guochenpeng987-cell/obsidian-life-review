#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Obsidian Vault -> GitHub review mirror + audit pack builder
Standard library only.
Usage:
  python scripts/build_audit_pack.py --config config.json
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple, Iterable, Optional

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
CJK_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_WORD_RE = re.compile(r"[A-Za-z0-9_'-]+")


@dataclass
class NoteRecord:
    src_path: Path
    rel_path: str
    repo_path: Path
    modified_at: dt.datetime
    size_bytes: int
    text: str
    note_type: str
    tags: str
    word_count_estimate: int
    links_out: int
    links_in: int = 0
    broken_targets: List[str] = None
    duplicate_stem: bool = False
    is_orphan: bool = False
    empty_note: bool = False


def load_config(config_path: Path) -> dict:
    with config_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def normalize_rel(path_str: str) -> str:
    if not path_str:
        return ""
    s = path_str.replace("\\", "/").strip().strip("/")
    return str(PurePosixPath(s))


def is_under(path_str: str, prefix: str) -> bool:
    path_str = normalize_rel(path_str)
    prefix = normalize_rel(prefix)
    return path_str == prefix or path_str.startswith(prefix + "/")


def should_manage(rel_path: str, cfg: dict) -> bool:
    rel = normalize_rel(rel_path)
    parts = rel.split("/")
    top = parts[0] if parts else ""
    managed_top = set(cfg.get("managed_top_level_dirs", []))
    managed_root_files = set(cfg.get("managed_root_files", []))
    if managed_top:
        if top in managed_top:
            return True
        if rel in managed_root_files:
            return True
        return False
    return True


def should_exclude(rel_path: str, cfg: dict) -> bool:
    rel = normalize_rel(rel_path)
    for d in cfg.get("exclude_dirs", []):
        if is_under(rel, d):
            return True
    for patt in cfg.get("exclude_globs", []):
        if fnmatch.fnmatch(rel, patt):
            return True
    return False


def include_extension(path: Path, cfg: dict) -> bool:
    return path.suffix.lower() in {ext.lower() for ext in cfg.get("include_extensions", [".md"])}


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if len(lines) < 3:
        return {}, text
    try:
        end_idx = lines[1:].index("---") + 1
    except ValueError:
        return {}, text
    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])
    data: Dict[str, str] = {}
    current_key = None
    list_buffer: List[str] = []
    for raw in fm_lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if re.match(r"^\s*-\s+", line) and current_key:
            item = re.sub(r"^\s*-\s+", "", line).strip().strip('"')
            list_buffer.append(item)
            continue
        if ":" in line:
            if current_key and list_buffer:
                data[current_key] = ", ".join(list_buffer)
                list_buffer = []
            key, val = line.split(":", 1)
            current_key = key.strip()
            data[current_key] = val.strip().strip('"')
        else:
            continue
    if current_key and list_buffer:
        data[current_key] = ", ".join(list_buffer)
    return data, body


def estimate_word_count(text: str) -> int:
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_WORD_RE.findall(text))
    return cjk + latin


def extract_internal_links(text: str) -> List[str]:
    targets = []
    for match in WIKILINK_RE.findall(text):
        target = match.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target)
    for _, url in MD_LINK_RE.findall(text):
        if url.startswith("http://") or url.startswith("https://") or url.startswith("mailto:"):
            continue
        target = url.split("#", 1)[0].strip()
        if not target:
            continue
        if target.endswith(".md"):
            target = target[:-3]
        targets.append(target)
    return targets


def resolve_link(target: str, path_map: Dict[str, str], stem_map: Dict[str, List[str]]) -> Optional[str]:
    t = normalize_rel(target)
    if t.endswith(".md"):
        t = t[:-3]
    # exact path without extension
    if t in path_map:
        return path_map[t]
    # suffix path match
    for k, v in path_map.items():
        if k.endswith("/" + t):
            return v
    stem = Path(t).name
    matches = stem_map.get(stem, [])
    if len(matches) == 1:
        return matches[0]
    return None


def build_tree_lines(paths: List[str]) -> List[str]:
    root = {}
    for rel in sorted(paths):
        parts = rel.split("/")
        node = root
        for part in parts:
            node = node.setdefault(part, {})
    lines: List[str] = []

    def walk(node: dict, prefix: str = ""):
        keys = sorted(node.keys(), key=lambda x: (not x.endswith(".md"), x))
        for idx, key in enumerate(keys):
            is_last = idx == len(keys) - 1
            connector = "└── " if is_last else "├── "
            lines.append(prefix + connector + key)
            new_prefix = prefix + ("    " if is_last else "│   ")
            walk(node[key], new_prefix)

    walk(root)
    return lines


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n[已截断，超出长度限制]"


def collect_notes(src_root: Path, repo_root: Path, cfg: dict) -> List[NoteRecord]:
    notes: List[NoteRecord] = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        current = Path(dirpath)
        rel_dir = normalize_rel(os.path.relpath(current, src_root))
        if rel_dir == ".":
            rel_dir = ""
        # mutate dirnames in-place to skip excluded
        kept_dirs = []
        for d in dirnames:
            rel_d = normalize_rel("/".join(p for p in [rel_dir, d] if p))
            if should_exclude(rel_d, cfg):
                continue
            if not should_manage(rel_d, cfg):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in filenames:
            src_path = current / fname
            rel_path = normalize_rel("/".join(p for p in [rel_dir, fname] if p))
            if not should_manage(rel_path, cfg):
                continue
            if should_exclude(rel_path, cfg):
                continue
            if not include_extension(src_path, cfg):
                continue

            try:
                text = src_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = src_path.read_text(encoding="utf-8", errors="replace")
            fm, body = parse_frontmatter(text)
            note_type = fm.get("类型", "").strip()
            tags = fm.get("标签", "").strip()
            record = NoteRecord(
                src_path=src_path,
                rel_path=rel_path,
                repo_path=repo_root / rel_path,
                modified_at=dt.datetime.fromtimestamp(src_path.stat().st_mtime),
                size_bytes=src_path.stat().st_size,
                text=text,
                note_type=note_type or "",
                tags=tags or "",
                word_count_estimate=estimate_word_count(body),
                links_out=len(extract_internal_links(text)),
                broken_targets=[],
                empty_note=estimate_word_count(body) < 30
            )
            notes.append(record)
    return sorted(notes, key=lambda n: n.rel_path)


def sync_mirror(notes: List[NoteRecord], repo_root: Path, cfg: dict) -> None:
    expected = set()
    for note in notes:
        note.repo_path.parent.mkdir(parents=True, exist_ok=True)
        note.repo_path.write_text(note.text, encoding="utf-8")
        expected.add(note.repo_path.resolve())

    # delete stale mirrored markdown files under managed top-level dirs
    managed_dirs = cfg.get("managed_top_level_dirs", [])
    for top in managed_dirs:
        target_dir = repo_root / top
        if not target_dir.exists():
            continue
        for path in target_dir.rglob("*"):
            if path.is_file() and include_extension(path, cfg):
                if path.resolve() not in expected:
                    path.unlink()

    # clean empty dirs under managed top-level dirs
    for top in managed_dirs:
        target_dir = repo_root / top
        if not target_dir.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(target_dir, topdown=False):
            p = Path(dirpath)
            if p == target_dir:
                continue
            if not any(p.iterdir()):
                p.rmdir()


def enrich_links(notes: List[NoteRecord]) -> None:
    path_map: Dict[str, str] = {}
    stem_map: Dict[str, List[str]] = {}
    for note in notes:
        no_ext = normalize_rel(note.rel_path[:-3] if note.rel_path.endswith(".md") else note.rel_path)
        path_map[no_ext] = note.rel_path
        stem = Path(no_ext).name
        stem_map.setdefault(stem, []).append(note.rel_path)

    inbound_count: Dict[str, int] = {note.rel_path: 0 for note in notes}
    duplicate_stems = {stem for stem, vals in stem_map.items() if len(vals) > 1}

    for note in notes:
        targets = extract_internal_links(note.text)
        for target in targets:
            resolved = resolve_link(target, path_map, stem_map)
            if resolved:
                inbound_count[resolved] += 1
            else:
                note.broken_targets.append(target)

    for note in notes:
        stem = Path(note.rel_path).stem
        note.links_in = inbound_count.get(note.rel_path, 0)
        note.duplicate_stem = stem in duplicate_stems
        note.is_orphan = note.links_in == 0 and note.links_out == 0


def backup_current_audit(audit_current: Path, audit_history: Path) -> None:
    if not audit_current.exists():
        return
    files = [p for p in audit_current.iterdir() if p.is_file()]
    if not files:
        return
    timestamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = audit_history / timestamp
    target.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, target / f.name)


def is_core(note: NoteRecord, cfg: dict) -> bool:
    name_set = set(cfg.get("core_file_names", []))
    if Path(note.rel_path).name in name_set:
        return True
    for key in cfg.get("core_path_keywords", []):
        if normalize_rel(key) in normalize_rel(note.rel_path):
            return True
    return False


def generate_tree(notes: List[NoteRecord], out_path: Path) -> None:
    lines = build_tree_lines([n.rel_path for n in notes])
    content = "# 当前目录树\n\n生成时间：{time}\n\n```text\n{tree}\n```\n".format(
        time=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        tree="\n".join(lines) if lines else "(空)"
    )
    out_path.write_text(content, encoding="utf-8")


def generate_inventory(notes: List[NoteRecord], out_path: Path) -> None:
    fieldnames = [
        "path", "type", "modified_at", "size_bytes", "word_count_estimate",
        "tags", "links_out", "links_in", "broken_links_count", "empty_note", "duplicate_stem"
    ]
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for n in notes:
            writer.writerow({
                "path": n.rel_path,
                "type": n.note_type,
                "modified_at": n.modified_at.strftime("%Y-%m-%d %H:%M:%S"),
                "size_bytes": n.size_bytes,
                "word_count_estimate": n.word_count_estimate,
                "tags": n.tags,
                "links_out": n.links_out,
                "links_in": n.links_in,
                "broken_links_count": len(n.broken_targets),
                "empty_note": "yes" if n.empty_note else "no",
                "duplicate_stem": "yes" if n.duplicate_stem else "no",
            })


def generate_core_notes(notes: List[NoteRecord], out_path: Path, cfg: dict) -> None:
    max_chars = int(cfg.get("max_core_note_chars", 30000))
    selected = [n for n in notes if is_core(n, cfg)]
    selected.sort(key=lambda x: x.rel_path)
    parts = ["# 系统骨架笔记合集", "", f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    if not selected:
        parts.append("（未匹配到核心笔记，请检查 config.json 中的 core_file_names / core_path_keywords）")
    for n in selected:
        parts.extend([
            f"## FILE: {n.rel_path}",
            f"- 修改时间：{n.modified_at.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "```markdown",
            truncate_text(n.text, max_chars),
            "```",
            "",
            "---",
            ""
        ])
    out_path.write_text("\n".join(parts), encoding="utf-8")


def generate_changed_notes(notes: List[NoteRecord], out_path: Path, cfg: dict) -> None:
    hours = int(cfg.get("changed_within_hours", 24))
    max_chars = int(cfg.get("max_changed_note_chars", 30000))
    threshold = dt.datetime.now() - dt.timedelta(hours=hours)
    changed = [n for n in notes if n.modified_at >= threshold]
    changed.sort(key=lambda x: x.modified_at, reverse=True)
    parts = ["# 最近变动笔记合集", "", f"时间窗口：最近 {hours} 小时", ""]
    if not changed:
        parts.append("（最近时间窗口内没有变动笔记）")
    for n in changed:
        parts.extend([
            f"## FILE: {n.rel_path}",
            f"- 修改时间：{n.modified_at.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "```markdown",
            truncate_text(n.text, max_chars),
            "```",
            "",
            "---",
            ""
        ])
    out_path.write_text("\n".join(parts), encoding="utf-8")


def generate_link_health(notes: List[NoteRecord], out_path: Path) -> None:
    fieldnames = [
        "path", "is_orphan", "broken_links_count", "broken_targets",
        "empty_note", "duplicate_stem", "links_out", "links_in"
    ]
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for n in notes:
            writer.writerow({
                "path": n.rel_path,
                "is_orphan": "yes" if n.is_orphan else "no",
                "broken_links_count": len(n.broken_targets),
                "broken_targets": " | ".join(n.broken_targets),
                "empty_note": "yes" if n.empty_note else "no",
                "duplicate_stem": "yes" if n.duplicate_stem else "no",
                "links_out": n.links_out,
                "links_in": n.links_in,
            })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to config.json")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    cfg = load_config(config_path)

    src_root = Path(cfg["vault_path"]).expanduser().resolve()
    repo_root = Path(cfg["review_repo_path"]).expanduser().resolve()

    if not src_root.exists():
        raise SystemExit(f"Vault path does not exist: {src_root}")
    if not repo_root.exists():
        raise SystemExit(f"Review repo path does not exist: {repo_root}")

    notes = collect_notes(src_root, repo_root, cfg)
    enrich_links(notes)
    sync_mirror(notes, repo_root, cfg)

    audit_current = repo_root / "_audit" / "current"
    audit_history = repo_root / "_audit" / "history"
    audit_current.mkdir(parents=True, exist_ok=True)
    audit_history.mkdir(parents=True, exist_ok=True)

    backup_current_audit(audit_current, audit_history)

    generate_tree(notes, audit_current / "00_tree.md")
    generate_inventory(notes, audit_current / "01_inventory.csv")
    generate_core_notes(notes, audit_current / "02_core_notes.md", cfg)
    generate_changed_notes(notes, audit_current / "03_changed_notes.md", cfg)
    generate_link_health(notes, audit_current / "04_link_health.csv")

    print("Done.")
    print(f"Vault: {src_root}")
    print(f"Review repo: {repo_root}")
    print(f"Notes mirrored: {len(notes)}")
    print(f"Audit files: {audit_current}")


if __name__ == "__main__":
    main()
