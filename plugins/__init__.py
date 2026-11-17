# -*- coding: utf-8 -*-
"""
Board Release - KiCad PCB editor action plugin.

This file is deliberately tiny and boring, because it is the ONE file KiCad
caches. Everything that you will actually want to tinker with lives in
kicad_release.py (the engine) and action_plugin.py (the dialog), and both of
those are re-imported from disk every single time you click the toolbar button.

So the edit loop is:

    save the file  ->  click the button  ->  your new code runs

No "Refresh Plugins", no KiCad restart, no reinstall.

You only need to restart KiCad if you edit THIS file (or swap the icon).

Where the code is loaded from, in order:
    1. the BOARD_RELEASE_SRC environment variable, if it points at a folder
    2. a file called dev_source.txt sitting next to this one, containing a path
       (write it with:  python kicad_release.py --dev-link <plugin folder>)
    3. this folder - the normal installed case
"""

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

ENGINE_NAME = "board_release_engine"
UI_NAME = "board_release_ui"


# --------------------------------------------------------------------------- #
#  hot reload plumbing
# --------------------------------------------------------------------------- #

def source_dir() -> str:
    """Where to load the engine and dialog from this time round."""
    env = os.environ.get("BOARD_RELEASE_SRC", "").strip().strip('"')
    if env and os.path.isdir(env) and os.path.isfile(os.path.join(env, "kicad_release.py")):
        return env

    link = os.path.join(_HERE, "dev_source.txt")
    if os.path.isfile(link):
        try:
            with open(link, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip().strip('"')
                    if not line or line.startswith("#"):
                        continue
                    line = os.path.expanduser(os.path.expandvars(line))
                    if os.path.isdir(line) and os.path.isfile(
                            os.path.join(line, "kicad_release.py")):
                        return line
        except Exception:
            pass
    return _HERE


def _load_module(path: str, modname: str):
    """Import a file as `modname`, replacing any previously loaded copy."""
    import importlib.util
    sys.modules.pop(modname, None)
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def load_fresh():
    """Re-read engine + dialog from disk. Returns (engine, ui, source_dir)."""
    src = source_dir()
    engine = _load_module(os.path.join(src, "kicad_release.py"), ENGINE_NAME)
    ui = _load_module(os.path.join(src, "action_plugin.py"), UI_NAME)
    return engine, ui, src


# --------------------------------------------------------------------------- #
#  registration
# --------------------------------------------------------------------------- #

try:
    import pcbnew
    import wx

    ICON = os.path.join(_HERE, "icon.png")

    class BoardReleaseActionPlugin(pcbnew.ActionPlugin):
        # Some KiCad builds call defaults() from the base __init__, others expect
        # the attributes set in __init__ directly. Doing both covers 6 to 10.
        def __init__(self):
            self.defaults()

        def defaults(self):
            self.name = "Board Release..."
            self.category = "Fabrication outputs"
            self.description = (
                "Generate the schematic PDF, board PDF, interactive BOM and "
                "JLCPCB production files in one go, into a versioned archive folder.")
            self.pcbnew_icon_support = hasattr(self, "show_toolbar_button")
            self.show_toolbar_button = True
            if os.path.isfile(ICON):
                self.icon_file_name = ICON
                self.dark_icon_file_name = ICON

        def Run(self):
            # Loop so the dialog's "Reload code" button can come straight back
            # with freshly imported modules.
            while True:
                try:
                    engine, ui, src = load_fresh()
                except Exception:
                    wx.MessageBox(
                        "The Board Release code failed to load.\n\n"
                        "Fix the error and click the button again - you do not\n"
                        "need to refresh plugins or restart KiCad.\n\n"
                        + traceback.format_exc(),
                        "Board Release - load error", wx.ICON_ERROR)
                    return
                try:
                    if ui.launch(engine, src) != "reload":
                        return
                except Exception:
                    wx.MessageBox(traceback.format_exc(),
                                  "Board Release - error", wx.ICON_ERROR)
                    return

    BoardReleaseActionPlugin().register()

except Exception:                                    # pragma: no cover
    sys.stderr.write("[Board Release] plugin failed to load:\n")
    sys.stderr.write(traceback.format_exc())
