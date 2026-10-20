"""Maintainer-built, public-entry browser dependency; embedded in the single Tool."""

from pathlib import Path

_EMBEDDED_SHELL = ""


def shell_asset() -> str:
    if _EMBEDDED_SHELL:
        return _EMBEDDED_SHELL
    path = Path(__file__).resolve().parents[2] / "build" / "browser-shell.js"
    if not path.is_file():
        raise RuntimeError("Run npm ci and npm run build before building the Tool")
    return path.read_text()
