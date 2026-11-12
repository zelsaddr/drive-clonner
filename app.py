"""Gdrive Clonner — entry point.

The UI is built with pywebview + Tailwind (see `webui.py` and `static/index.html`).
This module only starts that UI. Run with:

    python app.py
    # or
    python webui.py
"""

from webui import main


if __name__ == "__main__":
    main()