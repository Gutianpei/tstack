#!/usr/bin/env bash
# tstack installer for macOS and Linux. Safe to run again. Usage: ./install.sh [--dry-run]
# Needs bash 3.2+, python3 3.9+, git. herdr and the agent CLI (default: Claude Code) are used at run time.
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  "") ;;
  -h|--help) sed -n '2,3p' "$0" | sed 's/^# //'; exit 0 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac
command -v python3 >/dev/null 2>&1 || { echo "tstack: python3 is required" >&2; exit 1; }
CONFIG="${TSTACK_CONFIG:-$HOME/.config/tstack/config.json}"
KIT="$KIT" CONFIG="$CONFIG" DRY="$DRY" exec python3 - <<'PY'
import json, os, re, shutil, subprocess, sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit("tstack needs Python 3.9 or newer")
kit, config_file, dry = Path(os.environ["KIT"]), Path(os.environ["CONFIG"]), os.environ["DRY"] == "1"
sys.path.insert(0, str(kit / "lib"))
from tstack import config  # noqa: E402

def say(msg): print(("[dry run] " if dry else "") + msg, flush=True)

# 1. settings
if not config_file.exists():
    raw = json.loads((kit / "config.example.json").read_text())
    raw["user"] = os.environ.get("USER", "you")
    say(f"write settings {config_file}")
    if not dry:
        config.save_raw(raw, config_file)
else:
    raw = config.load_raw(config_file)
    say(f"keep settings {config_file}")
values = config._merge(config.DEFAULTS, raw)
memory, data, bin_dir = config.path("memory_dir", values), config.path("data_dir", values), config.path("bin_dir", values)
keys = {"USER": str(values["user"]), "MEMORY_DIR": str(memory), "DATA_DIR": str(data), "KIT_DIR": str(kit)}
def fill(text):
    for k, v in keys.items():
        text = text.replace("{{" + k + "}}", v)
    return text

# 2. memory repo
if not (memory / "nodes").exists():
    say(f"create memory repo {memory}")
    if not dry:
        shutil.copytree(str(kit / "memory-template"), str(memory), dirs_exist_ok=True)
        (memory / "README.md").write_text(fill((memory / "README.md").read_text()))
        subprocess.run(["git", "init", "-q", str(memory)], check=True)
        subprocess.run(["git", "-C", str(memory), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(memory), "commit", "-q", "-m", "tstack memory: start"], check=True)
else:
    say(f"keep memory repo {memory}")
say(f"set {memory} core.hooksPath -> {kit}/git-hooks")
if not dry:
    subprocess.run(["git", "-C", str(memory), "config", "core.hooksPath", str(kit / "git-hooks")], check=True)

# 3. swarm data folder
say(f"swarm init at {data}")
if not dry:
    subprocess.run([sys.executable, str(kit / "bin" / "swarm"), "init"], check=True,
                   env={**os.environ, "TSTACK_CONFIG": str(config_file)})

# 3b. server module (lib/tstack/server.py): write the storage rule that the CLAUDE.md block points to
say(f"write storage rule {data}/rules/storage.md")
if not dry:
    subprocess.run([sys.executable, str(kit / "bin" / "swarm"), "storage", "--write-rule"], check=True,
                   stdout=subprocess.DEVNULL, env={**os.environ, "TSTACK_CONFIG": str(config_file)})

# 4. command link
link = bin_dir / "swarm"
if link.is_symlink() or not link.exists():
    say(f"link {link} -> {kit}/bin/swarm")
    if not dry:
        bin_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(kit / "bin" / "swarm")
else:
    say(f"WARNING: {link} exists and is not a link; left alone")
if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
    say(f"WARNING: {bin_dir} is not on PATH; add it in your shell profile (~/.zshrc or ~/.bashrc)")

# 5-7 are Claude Code specific: global instructions, skills, permissions.
agent = config.section("agent", values)
if agent.get("herdr_kind") != "claude":
    say(f"agent is {agent.get('herdr_kind')!r}, not claude: skip ~/.claude instructions, skills and permissions; "
        "give your agent CLI the same rules from claude/CLAUDE.md by hand")
else:
    # 5. global instructions, between markers
    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    block = fill((kit / "claude" / "CLAUDE.md").read_text()).strip() + "\n"
    old = claude_md.read_text() if claude_md.exists() else ""
    pattern = re.compile(r"<!-- tstack:begin.*?<!-- tstack:end -->\n?", re.S)
    new = pattern.sub(lambda _: block, old) if pattern.search(old) else (old.rstrip() + "\n\n" + block if old.strip() else block)
    say(f"{'update' if new != old else 'keep'} tstack block in {claude_md}")
    if not dry and new != old:
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(new)

    # 6. skills
    for skill in ("resume", "curate"):
        target = Path.home() / ".claude" / "skills" / skill / "SKILL.md"
        text = fill((kit / "claude" / "skills" / skill / "SKILL.md").read_text())
        if target.exists() and "tstack" not in target.read_text():
            say(f"WARNING: {target} exists and is not tstack's; left alone")
            continue
        say(f"install skill {target}")
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    # 6b. rules (every claude/rules/*.md) and the plain-english skill, into ~/.claude (upkeep: agent rules)
    extra = {**keys, "MANAGER_MODEL": str(agent.get("manager_model") or "your manager model"),
             "WORKER_MODEL": str(agent.get("worker_model") or "your worker model")}
    def fill_more(text):
        for k, v in extra.items():
            text = text.replace("{{" + k + "}}", v)
        return text
    plans = [(Path.home() / ".claude" / "rules" / rule.name, rule, "tstack rule") for rule in sorted((kit / "claude" / "rules").glob("*.md")) if rule.name != "storage.md"]  # storage.md: filled by `swarm storage --write-rule` (3b)
    plans.append((Path.home() / ".claude" / "skills" / "plain-english" / "SKILL.md", kit / "claude" / "skills" / "plain-english" / "SKILL.md", "tstack skill"))
    for target, source, tag in plans:
        if target.exists() and tag not in target.read_text():
            say(f"WARNING: {target} exists and is not tstack's; left alone")
            continue
        say(f"install {tag.split()[1]} {target}")
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fill_more(source.read_text()))

    # 7. let agents run swarm and memory commands without a prompt each time
    settings_file = Path.home() / ".claude" / "settings.json"
    cc = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    allow = cc.setdefault("permissions", {}).setdefault("allow", [])
    wanted = ["Bash(swarm:*)", "Bash(herdr agent list:*)", "Bash(herdr agent get:*)", "Bash(herdr agent read:*)",
              f"Bash(git -C {memory}:*)"]
    add = [rule for rule in wanted if rule not in allow]
    say(f"allow in {settings_file}: {', '.join(add)}" if add else f"keep permissions in {settings_file}")
    if add and not dry:
        allow.extend(add)
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(cc, indent=2) + "\n")

# 8. herdr settings: append whole sections the user lacks
herdr_cfg = Path(os.environ.get("HERDR_CONFIG_PATH") or Path.home() / ".config" / "herdr" / "config.toml")
mine = herdr_cfg.read_text() if herdr_cfg.exists() else ""
chunks = re.split(r"(?m)^(?=\[)", (kit / "herdr" / "config.toml").read_text())
missing = [c for c in chunks[1:] if c.splitlines()[0].strip() not in {l.strip() for l in mine.splitlines()}]
if missing:
    say(f"add herdr sections to {herdr_cfg}: {', '.join(c.splitlines()[0] for c in missing)}")
    if not dry:
        herdr_cfg.parent.mkdir(parents=True, exist_ok=True)
        herdr_cfg.write_text(mine.rstrip() + "\n\n" + "\n".join(c.rstrip() + "\n" for c in missing))
        if shutil.which("herdr"):
            subprocess.run(["herdr", "server", "reload-config"], capture_output=True)
else:
    say("herdr settings already have every tstack section")

if not shutil.which(str(agent.get("cmd") or "claude")):
    say(f"note: agent CLI {agent.get('cmd')!r} is not on PATH yet")
if not shutil.which("herdr"):
    say("note: herdr is not on PATH yet; the swarm needs it, memory works without it")
print("Dry run: nothing written." if dry else
      "Done. Next: swarm config check, then swarm add-manager overall --role overall --dry-run")
PY
