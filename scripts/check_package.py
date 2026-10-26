"""Check portable packaging and obvious accidental disclosures; not a security guarantee."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".venv", "outputs", "inputs"}
ALLOW = {".md", ".py", ".json", ".csv", ".txt", ".yml", ".yaml"}
PATTERNS = {
    "machine_absolute_path": r"/(?:Users|home|mnt|Volumes|private|etc|tmp)/[A-Za-z0-9_\u4e00-\u9fff]",
    "windows_absolute_path": r"\b[A-Z]:[\\/][A-Za-z0-9_]",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE" + r" KEY-----",
    "github_token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b",
    "cloud_access_key": r"\b(?:AKIA|AKID)[A-Za-z0-9]{16,}\b",
    "email_address": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "private_ipv4": r"\b(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.(?:[0-9]{1,3}\.){1,2}[0-9]{1,3}\b",
}


def main():
    errors, count = [], 0
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(p in SKIP for p in relative.parts):
            continue
        if path.is_symlink():
            errors.append(f"{relative}: symlink not allowed in published package")
            continue
        if not path.is_file():
            continue
        count += 1
        if path.suffix not in ALLOW and path.name not in {"LICENSE", ".gitignore"}:
            errors.append(f"{relative}: unexpected file type")
        if path.stat().st_size > 1_000_000:
            errors.append(f"{relative}: unexpectedly large file")
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            errors.append(f"{relative}: not text")
            continue
        for name, pattern in PATTERNS.items():
            if re.search(pattern, text):
                errors.append(f"{relative}: {name}")
        if path.suffix == ".json":
            try:
                json.loads(text)
            except ValueError:
                errors.append(f"{relative}: invalid JSON")
        if path.suffix == ".md":
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                if link.startswith(("https://", "http://", "#")):
                    continue
                target = (path.parent / link.split("#", 1)[0]).resolve()
                if not target.is_relative_to(ROOT) or not target.exists():
                    errors.append(f"{relative}: broken or external local link: {link}")
    entry = ROOT / "skills" / "market-evidence" / "SKILL.md"
    source = entry.read_text()
    if not source.startswith("---\n") or "\n---\n" not in source[4:]:
        errors.append("SKILL.md: missing frontmatter")
    else:
        metadata = source.split("---", 2)[1]
        name = re.search(r"^name: ([a-z0-9-]{1,64})$", metadata, re.M)
        desc = re.search(r"^description: (.+)$", metadata, re.M)
        if not name or name.group(1) != entry.parent.name:
            errors.append("SKILL.md: invalid name")
        if not desc or len(desc.group(1)) > 1024:
            errors.append("SKILL.md: invalid description")
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Package checks passed: {count} source files; links, metadata, JSON and portability checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
