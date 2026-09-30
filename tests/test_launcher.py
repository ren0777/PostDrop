"""The launcher: a new requirement must reach an existing virtual environment."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAT_BYTES = (ROOT / "Start ArtDrop.bat").read_bytes()
BAT = BAT_BYTES.decode("utf-8")


def _check_requirements():
    spec = importlib.util.spec_from_file_location(
        "check_requirements", ROOT / "tools" / "check_requirements.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_launcher_updates_an_existing_environment():
    assert "tools\\check_requirements.py" in BAT
    assert "Updating ArtDrop's components" in BAT
    # the browser download only happens after the components are current
    assert BAT.index("Updating ArtDrop's components") < BAT.index("playwright install chromium")


def test_the_launcher_records_the_hash_after_installing():
    assert "check_requirements.py write" in BAT


def test_the_bat_keeps_windows_line_endings():
    assert b"\r\n" in BAT_BYTES
    assert BAT_BYTES.replace(b"\r\n", b"").count(b"\n") == 0


def test_the_hash_follows_requirements(tmp_path, monkeypatch):
    helper = _check_requirements()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("playwright\n", encoding="utf-8")
    monkeypatch.setattr(helper, "REQUIREMENTS", requirements)
    monkeypatch.setattr(helper, "MARKER", tmp_path / ".venv" / ".requirements-hash")

    assert helper.main([]) == 0  # no environment yet: the first-run path installs everything
    (tmp_path / ".venv").mkdir()
    assert helper.main([]) == 1  # an older environment without a hash must be updated
    assert helper.main(["write"]) == 0
    assert helper.main([]) == 0  # now it is up to date
    requirements.write_text("playwright\nhttpx\n", encoding="utf-8")
    assert helper.main([]) == 1  # a new requirement forces the update
