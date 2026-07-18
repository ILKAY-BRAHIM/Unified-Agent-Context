"""Skill discovery and frontmatter parsing (Phase 2).

`.agents/skills/*.md` is the single source of truth. Claude Code gets a native
mirror (sync.py); Codex gets the same content over MCP via skill_load (D6).

Progressive disclosure is the whole point: `skill_list` returns names and
descriptions only. Bodies load on demand. Never inline bodies into the
instructions file — that defeats the reason this layer exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

SKILLS_RELDIR = Path(".agents/skills")
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
VALID_NAME = re.compile(r"\A[a-z0-9][a-z0-9-]*\Z")


class SkillError(ValueError):
    """Malformed skill. Always names the file — a silent skip means a skill
    the agent never triggers and you never find out why."""


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path

    def index_entry(self) -> dict:
        """What skill_list returns. Deliberately body-free."""
        return {"name": self.name, "description": self.description}


def parse_skill(path: Path) -> Skill:
    text = path.read_text()
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise SkillError(
            f"{path}: missing YAML frontmatter. A skill must start with a '---' block "
            f"containing 'name' and 'description'."
        )

    raw, body = match.groups()
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillError(f"{path}: frontmatter is not valid YAML: {exc}") from exc

    if not isinstance(meta, dict):
        raise SkillError(f"{path}: frontmatter must be a YAML mapping, got {type(meta).__name__}.")

    name = meta.get("name")
    description = meta.get("description")
    if not name or not str(name).strip():
        raise SkillError(f"{path}: frontmatter is missing 'name'.")
    if not description or not str(description).strip():
        raise SkillError(
            f"{path}: frontmatter is missing 'description'. The description is what makes the "
            f"skill trigger — it must say *when* to use the skill, not just what it is."
        )

    name = str(name).strip()
    if not VALID_NAME.match(name):
        raise SkillError(
            f"{path}: skill name {name!r} must be lowercase kebab-case "
            f"(it becomes a directory name in .claude/skills/)."
        )
    if name != path.stem:
        raise SkillError(
            f"{path}: frontmatter name {name!r} does not match the filename {path.stem!r}. "
            f"Keep them the same so skills are findable."
        )

    return Skill(name=name, description=str(description).strip(), body=body.strip(), path=path)


def skills_dir(root: Path) -> Path:
    return root / SKILLS_RELDIR


def discover(root: Path) -> list[Skill]:
    """All skills, sorted by name. Raises on the first malformed file."""
    directory = skills_dir(root)
    if not directory.is_dir():
        return []
    return [parse_skill(p) for p in sorted(directory.glob("*.md"))]


def load(root: Path, name: str) -> Skill:
    path = skills_dir(root) / f"{name}.md"
    if not path.exists():
        available = ", ".join(s.name for s in discover(root)) or "none"
        raise SkillError(f"No skill named {name!r}. Available: {available}.")
    return parse_skill(path)


def render_index(skills: list[Skill]) -> str:
    """The cheap always-in-context index."""
    if not skills:
        return "(no skills defined)"
    return "\n".join(f"- {s.name}: {s.description}" for s in skills)


# --- importing Claude's own skills ------------------------------------------


def claude_skills_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def discover_claude_skills() -> list[Skill]:
    """Claude Code's user skills — the ones that exist as files.

    Only `~/.claude/skills/<name>/SKILL.md` can be imported. Claude ships many
    more (deep-research, dataviz, verify, code-review …) compiled into its
    binary; those aren't on disk, so there is nothing to copy, and several of
    them are about Claude's own features and would mean nothing to Codex.
    """
    directory = claude_skills_dir()
    if not directory.is_dir():
        return []

    found = []
    for path in sorted(directory.glob("*/SKILL.md")):
        try:
            skill = parse_skill_named(path, path.parent.name)
        except SkillError:
            # Claude's format is close to ours but not identical; skip rather
            # than fail the whole import over one file.
            continue
        found.append(skill)
    return found


def parse_skill_named(path: Path, name: str) -> Skill:
    """Parse a SKILL.md whose name comes from its directory, not its filename."""
    text = path.read_text()
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise SkillError(f"{path}: missing YAML frontmatter.")
    raw, body = match.groups()
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{path}: frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillError(f"{path}: frontmatter must be a YAML mapping.")

    description = str(meta.get("description", "")).strip()
    if not description:
        raise SkillError(f"{path}: frontmatter is missing 'description'.")
    return Skill(name=str(meta.get("name", name)).strip() or name,
                 description=description, body=body.strip(), path=path)


def import_from_claude(root: Path, overwrite: bool = False) -> tuple[list[str], list[str]]:
    """Copy Claude's user skills into `.agents/skills/` so both agents get them.

    Returns (imported, skipped). A copy — not a symlink — because the skill then
    belongs to the project: it gets committed, reviewed, and edited for both
    agents rather than silently tracking your personal Claude setup.
    """
    directory = skills_dir(root)
    directory.mkdir(parents=True, exist_ok=True)

    imported: list[str] = []
    skipped: list[str] = []
    for skill in discover_claude_skills():
        target = directory / f"{skill.name}.md"
        if target.exists() and not overwrite:
            skipped.append(skill.name)
            continue
        target.write_text(
            f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n{skill.body}\n"
        )
        imported.append(skill.name)
    return imported, skipped
