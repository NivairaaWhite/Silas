"""Launch the Streamlit UI."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    try:
        import streamlit.web.cli as stcli
    except ImportError:
        print("Streamlit is required: pip install streamlit")
        sys.exit(1)
    ui_path = str(Path(__file__).resolve().parent / "ui.py")
    sys.argv = ["streamlit", "run", ui_path, "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    stcli.main()


if __name__ == "__main__":
    main()
