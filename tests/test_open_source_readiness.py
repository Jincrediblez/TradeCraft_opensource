import json
import re
import struct
from datetime import date
from pathlib import Path

from app.demo_data import ensure_demo_workspace, seed_demo


ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {
    ".css", ".env", ".html", ".ini", ".js", ".json", ".md", ".py",
    ".sh", ".toml", ".txt", ".yaml", ".yml",
}


def image_dimensions(path: Path):
    payload = path.read_bytes()
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", payload[16:24])
    if payload.startswith(b"\xff\xd8"):
        offset = 2
        while offset < len(payload):
            if payload[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(payload) and payload[offset] == 0xFF:
                offset += 1
            marker = payload[offset]
            offset += 1
            if marker in {0xD8, 0xD9}:
                continue
            segment_length = int.from_bytes(payload[offset : offset + 2], "big")
            if marker in range(0xC0, 0xC4):
                height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
                width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
                return width, height
            offset += segment_length
    raise AssertionError(f"Unsupported image format: {path}")


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
        "README.md", "TradeCraft_User_Manual.md", "TradeCraft_User_Manual.pdf",
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


def test_feature_guide_images_match_the_retina_desktop_capture():
    image_dir = ROOT / "docs" / "images" / "feature-guide"
    images = sorted(image_dir.glob("*.png"))
    assert len(images) == 24
    assert not list(image_dir.glob("*.jpg"))
    assert {image_dimensions(path) for path in images} == {(3452, 2358)}


def test_documentation_screenshot_references_exist():
    for document in (ROOT / "README.md", ROOT / "TradeCraft_User_Manual.md"):
        references = re.findall(
            r"!\[[^]]*\]\((docs/images/feature-guide/[^)]+)\)",
            document.read_text(encoding="utf-8"),
        )
        assert references
        assert all((ROOT / reference).is_file() for reference in references)


def test_ai_prompt_is_english_only():
    prompts = {path.name for path in (ROOT / "prompts").glob("critique_audit*")}
    assert prompts == {"critique_audit_en.md"}


def test_locale_catalogs_have_matching_stable_keys():
    english = json.loads((ROOT / "static" / "locales" / "en.json").read_text(encoding="utf-8"))
    chinese = json.loads((ROOT / "static" / "locales" / "zh-CN.json").read_text(encoding="utf-8"))
    assert nested_keys(english) == nested_keys(chinese)

    placeholders = re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")

    def walk(left, right):
        for key, value in left.items():
            peer = right[key]
            if isinstance(value, dict):
                walk(value, peer)
            elif isinstance(value, str):
                assert set(placeholders.findall(value)) == set(placeholders.findall(peer)), key

    walk(english, chinese)


def test_repository_is_english_only_outside_the_chinese_ui_catalog():
    allowed = {
        ROOT / "static" / "locales" / "zh-CN.json",
        ROOT / "tests" / "test_i18n_demo.py",
    }
    han = re.compile(r"[\u3400-\u9fff]")
    for path in public_text_files():
        if path in allowed:
            continue
        assert not han.search(path.read_text(encoding="utf-8", errors="ignore")), path


def test_legacy_localized_document_paths_are_removed():
    chinese_manual = "TradeCraft_\u7cfb\u7edf\u529f\u80fd\u624b\u518c_zh-CN.pdf"
    for path in (
        ROOT / "README.en.md",
        ROOT / "TradeCraft_User_Manual_en.md",
        ROOT / "TradeCraft_User_Manual_en.pdf",
        ROOT / chinese_manual,
        ROOT / "docs" / "images" / "feature-guide-en",
    ):
        assert not path.exists(), path


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
