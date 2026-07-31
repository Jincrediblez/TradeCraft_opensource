import json
import re
from datetime import date
from pathlib import Path

from app.demo_data import ensure_demo_workspace, seed_demo


ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {
    ".css", ".env", ".html", ".ini", ".js", ".json", ".md", ".py",
    ".sh", ".toml", ".txt", ".yaml", ".yml",
}


def public_text_files():
    excluded = {
        ".git", ".claude", ".codex", ".venv", "cache", "data",
        "Library", "logs", "pet-upgrades",
    }
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in excluded for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"LICENSE", "NOTICE", "Makefile"}:
            yield path


def nested_keys(payload, prefix=""):
    keys = set()
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else key
        keys.add(path)
        if isinstance(value, dict):
            keys.update(nested_keys(value, path))
    return keys


def test_required_open_source_documents_exist():
    for name in (
        "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "SECURITY.md",
        "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "DISCLAIMER.md",
        "README.md", "README.en.md",
    ):
        assert (ROOT / name).is_file(), name


def test_local_workspace_outputs_are_ignored():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in (".env", "data/", "cache/", "Library/", "*.log", ".claude/", ".codex/"):
        assert entry in ignore
    assert "!.env.example" in ignore


def test_removed_personalized_modules_have_no_public_references():
    tokens = [
        "know" + "ledge_loader",
        "principle_" + "alignment",
        "playbook_" + "matcher",
        "concept_" + "tracker",
        "kow" + "ledge/",
    ]
    corpus = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in public_text_files())
    for token in tokens:
        assert token not in corpus


def test_public_text_has_no_macos_user_absolute_path():
    marker = "/" + "Users" + "/"
    for path in public_text_files():
        assert marker not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_public_text_has_no_ibkr_account_identifier():
    pattern = re.compile(r"\bU\d{7,10}\b")
    for path in public_text_files():
        assert not pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path


def test_browser_dependencies_are_pinned_and_local():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/lightweight-charts.standalone.production.js" in html
    assert "/static/vendor/d3.min.js" in html
    assert "unpkg.com" not in html
    assert (ROOT / "static" / "vendor" / "LICENSE.lightweight-charts").is_file()
    assert (ROOT / "static" / "vendor" / "LICENSE.d3").is_file()


def test_ai_prompts_are_locale_specific():
    prompts = {path.name for path in (ROOT / "prompts").glob("critique_audit*")}
    assert prompts == {"critique_audit_en.md", "critique_audit_zh-CN.md"}


def test_locale_catalogs_have_matching_stable_keys():
    english = json.loads((ROOT / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    chinese = json.loads((ROOT / "static" / "locales" / "zh-CN.json").read_text(encoding="utf-8"))
    stable_sections = ("nav", "settings", "watchlist", "demo", "api")
    assert nested_keys({key: english[key] for key in stable_sections}) == nested_keys(
        {key: chinese[key] for key in stable_sections}
    )


def test_demo_contains_current_and_prior_year_periods(tmp_path):
    status = seed_demo(tmp_path)
    periods = set(status["periods"])
    assert f"{date.today().year}YTD" in periods
    assert str(date.today().year - 1) in periods
    assert "ALL" in periods


def test_real_workspace_is_never_auto_seeded(tmp_path):
    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "broker-export.csv").write_text("synthetic fixture marker", encoding="utf-8")
    status = ensure_demo_workspace(tmp_path)
    assert status["active"] is False
    assert status["reason"] == "real-data-present"
    assert not (tmp_path / "data" / "state" / "tradecraft_demo.json").exists()
