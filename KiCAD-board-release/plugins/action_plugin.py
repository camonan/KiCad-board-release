# -*- coding: utf-8 -*-
"""
Board Release - the toolbar button and its dialog.

The dialog is a thin front end: every decision about what to run and how lives
in kicad_release.py, which also works standalone from a terminal.
"""

from __future__ import annotations

import datetime as _dt
import io
import os
import sys
import subprocess
import threading
import traceback

import pcbnew
import wx

# The engine is injected by launch() so that __init__.py can hand us a freshly
# re-imported copy on every click. The import below is only a fallback for
# running this file directly, or for an older loader.
core = None
try:
    from . import kicad_release as core        # type: ignore
except Exception:
    try:
        import kicad_release as core           # type: ignore
    except Exception:
        core = sys.modules.get("board_release_engine")


ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
RELOAD = "reload"
_RELOAD_MODAL_CODE = 4321

# A scratch module that survives a hot reload, so the dialog can come back with
# whatever you had typed and ticked. The loader only discards the engine and UI
# modules, never this one.
import types
_HOLDER = sys.modules.setdefault("board_release_state",
                                 types.ModuleType("board_release_state"))
if not hasattr(_HOLDER, "remember"):
    _HOLDER.remember = {}
_REMEMBER: dict = _HOLDER.remember


# --------------------------------------------------------------------------- #

class ReleaseDialog(wx.Dialog):
    def __init__(self, parent, project, cfg: dict, src_dir: str = ""):
        super().__init__(parent, title=f"{core.APP_NAME} {core.__version__}",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.project = project
        self.cfg = cfg
        self.src_dir = src_dir
        self.worker: threading.Thread | None = None
        self.proc = None
        self.out_dir = ""
        self._timer = None
        self._alive = True        # False once the dialog is on its way out

        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)

        # ---- header ------------------------------------------------------ #
        head = wx.StaticText(panel, label=project.title or project.stem)
        f = head.GetFont()
        f.SetPointSize(f.GetPointSize() + 2)
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        head.SetFont(f)
        outer.Add(head, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

        sub = wx.StaticText(panel, label=project.dir)
        sub.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        outer.Add(sub, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        # ---- revision + archive ------------------------------------------ #
        grid = wx.FlexGridSizer(3, 3, 6, 8)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(panel, label="Revision"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.rev = wx.TextCtrl(panel, value=project.rev or "")
        self.rev.SetHint("e.g. C, 1.2, A1")
        grid.Add(self.rev, 1, wx.EXPAND)
        src = wx.StaticText(panel, label=f"from {project.rev_source}"
                            if project.rev_source else "not found - type one")
        src.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        grid.Add(src, 0, wx.ALIGN_CENTER_VERTICAL)

        grid.Add(wx.StaticText(panel, label="Prefix"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.prefix = wx.TextCtrl(panel, value=cfg.get("rev_prefix", ""))
        self.prefix.SetHint("blank = folder is just the revision")
        grid.Add(self.prefix, 1, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label='e.g. "Rev" -> RevC'), 0,
                 wx.ALIGN_CENTER_VERTICAL)

        grid.Add(wx.StaticText(panel, label="Archive"), 0, wx.ALIGN_CENTER_VERTICAL)
        # Always show the real folder for THIS project; what gets saved is the
        # portable form (blank = default, or relative to the project).
        self.archive = wx.TextCtrl(
            panel, value=core.resolve_archive_root(cfg.get("archive_root", ""),
                                                   project.dir, project.stem))
        self.archive.SetHint(f"blank = <project>/{core.ARCHIVE_DIRNAME}")
        grid.Add(self.archive, 1, wx.EXPAND)
        browse = wx.Button(panel, label="Browse...")
        browse.Bind(wx.EVT_BUTTON, self.on_browse)
        grid.Add(browse, 0)

        outer.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        self.dest = wx.StaticText(panel, label="")
        self.dest.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        outer.Add(self.dest, 0, wx.ALL, 12)

        # ---- step checklist ---------------------------------------------- #
        box = wx.StaticBoxSizer(wx.VERTICAL, panel, "What to generate")
        self.checks = wx.CheckListBox(
            panel, choices=[label for _, label in core.STEP_ORDER])
        for i, (key, _) in enumerate(core.STEP_ORDER):
            self.checks.Check(i, bool(cfg["steps"].get(key)))
        self.checks.SetMinSize((520, 200))
        box.Add(self.checks, 1, wx.EXPAND | wx.ALL, 6)

        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (("All", self.on_all), ("None", self.on_none),
                               ("Save as defaults", self.on_save)):
            b = wx.Button(panel, label=label)
            b.Bind(wx.EVT_BUTTON, handler)
            row.Add(b, 0, wx.RIGHT, 6)
        row.AddStretchSpacer()
        self.tools = wx.Button(panel, label="Check tools")
        self.tools.SetToolTip("Test kicad-cli, KiCad's Python and "
                              "InteractiveHtmlBom, and say what is wrong.")
        self.tools.Bind(wx.EVT_BUTTON, self.on_tools)
        row.Add(self.tools, 0)
        box.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        outer.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        # ---- log ---------------------------------------------------------- #
        self.log_ctrl = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.log_ctrl.SetMinSize((520, 180))
        self.log_ctrl.SetFont(wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE)))
        outer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 12)

        self.gauge = wx.Gauge(panel, range=100)
        self.gauge.Hide()
        outer.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        # ---- buttons ------------------------------------------------------ #
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_open = wx.Button(panel, label="Open folder")
        self.btn_open.Enable(False)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open)
        btns.Add(self.btn_open, 0, wx.RIGHT, 6)
        self.btn_reload = wx.Button(panel, label="Reload code")
        self.btn_reload.SetToolTip(
            "Re-read kicad_release.py and action_plugin.py from disk and reopen "
            "this dialog. Use after editing them - no need to refresh plugins "
            "or restart KiCad.")
        self.btn_reload.Bind(wx.EVT_BUTTON, self.on_reload)
        btns.Add(self.btn_reload, 0, wx.RIGHT, 6)
        btns.AddStretchSpacer()
        self.btn_dry = wx.Button(panel, label="Preview")
        self.btn_dry.Bind(wx.EVT_BUTTON, lambda e: self.start(dry=True))
        btns.Add(self.btn_dry, 0, wx.RIGHT, 6)
        self.btn_go = wx.Button(panel, wx.ID_OK, "Generate")
        self.btn_go.SetDefault()
        self.btn_go.Bind(wx.EVT_BUTTON, lambda e: self.start(dry=False))
        btns.Add(self.btn_go, 0, wx.RIGHT, 6)
        self.btn_close = wx.Button(panel, wx.ID_CANCEL, "Close")
        btns.Add(self.btn_close, 0)
        outer.Add(btns, 0, wx.EXPAND | wx.ALL, 12)

        panel.SetSizer(outer)
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(s)
        self.SetMinSize(self.GetSize())

        # restore whatever was on screen before a "Reload code"
        if _REMEMBER:
            self.rev.SetValue(_REMEMBER.get("rev", self.rev.GetValue()))
            self.prefix.SetValue(_REMEMBER.get("prefix", self.prefix.GetValue()))
            self.archive.SetValue(_REMEMBER.get("archive", self.archive.GetValue()))
            for i, (key, _) in enumerate(core.STEP_ORDER):
                if key in _REMEMBER.get("steps", {}):
                    self.checks.Check(i, bool(_REMEMBER["steps"][key]))
            _REMEMBER.clear()

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.rev.Bind(wx.EVT_TEXT, lambda e: self.refresh_dest())
        self.prefix.Bind(wx.EVT_TEXT, lambda e: self.refresh_dest())
        self.archive.Bind(wx.EVT_TEXT, lambda e: self.refresh_dest())
        self.refresh_dest()

        self.write("Reminder: save the board (Ctrl+S) and update the PCB from the")
        self.write("schematic (F8) before generating, so the outputs match.")
        self.write("")
        self.write(self.code_stamp())
        self.write("")

    # -- helpers ----------------------------------------------------------- #

    def code_stamp(self) -> str:
        """So you can see at a glance that your edit actually got picked up."""
        src = self.src_dir or os.path.dirname(os.path.abspath(__file__))
        eng = os.path.join(src, "kicad_release.py")
        try:
            when = _dt.datetime.fromtimestamp(os.path.getmtime(eng))
            stamp = when.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            stamp = "?"
        return f"Code loaded from {src}\n  kicad_release.py last saved {stamp}"

    def current_cfg(self) -> dict:
        cfg = dict(self.cfg)
        cfg["archive_root"] = core.portable_archive_root(self.archive.GetValue(),
                                                         self.project.dir)
        cfg["rev_prefix"] = self.prefix.GetValue()
        cfg["steps"] = {key: self.checks.IsChecked(i)
                        for i, (key, _) in enumerate(core.STEP_ORDER)}
        return cfg

    def refresh_dest(self) -> None:
        try:
            runner = core.ReleaseRunner(self.project, self.current_cfg(),
                                        self.rev.GetValue().strip() or "X",
                                        core.Log(quiet=True), dry_run=True)
            self.dest.SetLabel("Output:  " + runner.out_root
                               + "      files named " + runner.base_name() + "_*")
        except Exception as e:
            self.dest.SetLabel(f"Output:  (cannot resolve - {e})")

    def write(self, msg: str) -> None:
        # Posting to a destroyed dialog is a hard crash, not an exception.
        if not self._alive:
            return
        wx.CallAfter(self._write, msg)

    def _write(self, msg: str) -> None:
        if not self._alive:
            return
        try:
            self.log_ctrl.AppendText(msg + "\n")
        except Exception:
            pass

    # -- events ------------------------------------------------------------ #

    def on_browse(self, _evt) -> None:
        with wx.DirDialog(self, "Choose the archive root folder",
                          defaultPath=self.archive.GetValue() or self.project.dir,
                          style=wx.DD_DEFAULT_STYLE) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.archive.SetValue(dlg.GetPath())

    def on_all(self, _evt) -> None:
        for i in range(self.checks.GetCount()):
            self.checks.Check(i, True)

    def on_none(self, _evt) -> None:
        for i in range(self.checks.GetCount()):
            self.checks.Check(i, False)

    def on_save(self, _evt) -> None:
        cfg = self.current_cfg()
        choice = wx.MessageBox(
            "Save these settings for THIS project only?\n\n"
            "Yes  -  write .board_release.json next to the project\n"
            "No   -  save as your defaults for every project",
            "Save defaults", wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION, self)
        if choice == wx.CANCEL:
            return
        path = (os.path.join(self.project.dir, core.CONFIG_BASENAME)
                if choice == wx.YES else core.user_config_path())
        try:
            core.save_config(cfg, path)          # only non-default values
            self.cfg = cfg
            self.write(f"Saved settings -> {path}")
        except Exception as e:
            wx.MessageBox(str(e), "Could not save", wx.ICON_ERROR, self)

    def on_tools(self, _evt) -> None:
        """Run the same checks as `--doctor`, into the log pane."""
        self.log_ctrl.SetValue("")
        cfg = self.current_cfg()
        self.tools.Enable(False)

        def work():
            import contextlib
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    core.doctor(cfg, self.project)
            except Exception:
                buf.write(traceback.format_exc())
            for line in buf.getvalue().splitlines():
                self.write(line)
            wx.CallAfter(self.tools.Enable, True)

        threading.Thread(target=work, daemon=True).start()

    def on_close(self, evt) -> None:
        if self.worker and self.worker.is_alive():
            wx.MessageBox("The release is still running. Wait for it to finish.",
                          core.APP_NAME, wx.ICON_INFORMATION, self)
            evt.Veto()
            return
        self.shutdown()
        evt.Skip()

    def shutdown(self) -> None:
        """Stop anything that could fire after this dialog is gone."""
        self._alive = False
        if self._timer:
            try:
                self._timer.Stop()
            except Exception:
                pass
            self._timer = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def on_open(self, _evt) -> None:
        if self.out_dir:
            core.open_in_file_manager(self.out_dir)

    def on_reload(self, _evt) -> None:
        if self.worker and self.worker.is_alive():
            wx.MessageBox("Wait for the current run to finish first.",
                          core.APP_NAME, wx.ICON_INFORMATION, self)
            return
        # Persist what is on screen so the reopened dialog looks identical.
        _REMEMBER.update({
            "rev": self.rev.GetValue(),
            "prefix": self.prefix.GetValue(),
            "archive": self.archive.GetValue(),
            "steps": {key: self.checks.IsChecked(i)
                      for i, (key, _) in enumerate(core.STEP_ORDER)},
        })
        self.EndModal(_RELOAD_MODAL_CODE)

    # -- the run ----------------------------------------------------------- #

    def release_argv(self, cfg: dict, rev: str, selected: list, dry: bool):
        """
        The release runs as a SEPARATE PROCESS.

        kicad_release.py is standard-library only, so the child needs no pcbnew
        and shares nothing with KiCad. Whatever it does - including launching
        InteractiveHtmlBom - cannot reach back into this process.
        """
        src = self.src_dir or os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(src, "kicad_release.py")
        if not os.path.isfile(script):
            return None
        py = core.runner_python(cfg.get("kicad_cli") or core.find_kicad_cli())
        if not py:
            return None
        argv = [py, "-u", script, self.project.pro,
                "--rev", rev,
                "--rev-prefix", cfg.get("rev_prefix", "") or "",
                "--steps", ",".join(selected),
                "--force", "--no-open"]
        # hand the child an absolute folder: on the command line a relative
        # --archive means relative to the current directory, not the project
        argv += ["--archive", core.resolve_archive_root(
            cfg.get("archive_root", ""), self.project.dir, self.project.stem)]
        if dry:
            argv.append("--dry-run")
        return argv

    def stream(self, argv: list) -> None:
        """Run the child and pump its output into the log pane."""
        self.write("  Running as a separate process - KiCad is not involved.")
        self.write("")
        try:
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                cwd=self.project.dir, **core._no_window())
        except Exception as e:
            self.write(f"  could not start the release process: {e}")
            return
        try:
            for line in self.proc.stdout:
                self.write(line.rstrip("\r\n"))
        finally:
            try:
                self.proc.wait(timeout=30)
            except Exception:
                pass
        if self.proc.returncode not in (0, 1):
            self.write(f"  (release process exited with code {self.proc.returncode})")
        self.proc = None

    def start(self, dry: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        rev = self.rev.GetValue().strip()
        if not rev:
            wx.MessageBox("Enter a revision - it becomes the archive folder name.",
                          "Revision needed", wx.ICON_WARNING, self)
            self.rev.SetFocus()
            return
        cfg = self.current_cfg()
        selected = [k for k, _ in core.STEP_ORDER if cfg["steps"].get(k)]
        if not selected:
            wx.MessageBox("Nothing is ticked.", "Nothing to do", wx.ICON_WARNING, self)
            return

        # Remember where this project files its releases - saved on Preview as
        # well as Generate. Stored relative to the project (blank for the default
        # <project>/archive), so copying or moving the project keeps it right.
        try:
            portable = core.portable_archive_root(self.archive.GetValue(),
                                                  self.project.dir)
            core.update_project_config(self.project.dir,
                                       archive_root=portable,
                                       rev_prefix=self.prefix.GetValue())
            self.cfg["archive_root"] = portable
            self.cfg["rev_prefix"] = self.prefix.GetValue()
        except Exception:
            pass

        # resolve the destination locally for the overwrite prompt and the
        # Open folder button; this is pure path arithmetic, nothing is written
        preview = core.ReleaseRunner(self.project, cfg, rev,
                                     core.Log(quiet=True), dry_run=True)
        self.out_dir = preview.out_root
        argv = self.release_argv(cfg, rev, selected, dry)

        if not dry and os.path.isdir(self.out_dir) and os.listdir(self.out_dir):
            if wx.MessageBox(
                    f"{self.out_dir}\n\nalready exists and is not empty.\n\n"
                    "Overwrite its contents?",
                    "Folder exists", wx.YES_NO | wx.ICON_WARNING, self) != wx.YES:
                return

        self.log_ctrl.SetValue("")
        for c in (self.btn_go, self.btn_dry, self.btn_close, self.checks,
                  self.rev, self.prefix, self.archive, self.tools, self.btn_reload):
            c.Enable(False)
        self.gauge.Show()
        self.gauge.Pulse()
        self.Layout()

        def work():
            try:
                if argv:
                    self.stream(argv)
                else:
                    # No interpreter found to spawn with. Fall back to running
                    # in this thread - safe now that nothing in the engine
                    # imports pcbnew or wx, but noted so it is not a surprise.
                    self.write("  (no interpreter found to spawn - running in-thread)")
                    core.ReleaseRunner(
                        self.project, cfg, rev,
                        core.Log(sink=self.write, quiet=True), dry_run=dry).run(selected)
            except Exception:
                self.write(traceback.format_exc())
            if self._alive:
                wx.CallAfter(self.finished, dry)

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._pulse()

    def _pulse(self) -> None:
        # Keep the handle. An unstopped CallLater that fires after the dialog
        # is destroyed touches freed memory.
        if self._alive and self.worker and self.worker.is_alive():
            try:
                self.gauge.Pulse()
            except Exception:
                return
            self._timer = wx.CallLater(120, self._pulse)
        else:
            self._timer = None

    def finished(self, dry: bool) -> None:
        if not self._alive:
            return
        if self._timer:
            try:
                self._timer.Stop()
            except Exception:
                pass
            self._timer = None
        self.gauge.Hide()
        self.Layout()
        for c in (self.btn_go, self.btn_dry, self.btn_close, self.checks,
                  self.rev, self.prefix, self.archive, self.tools, self.btn_reload):
            c.Enable(True)
        self.btn_open.Enable(not dry and os.path.isdir(self.out_dir))
        if not dry and self.cfg.get("open_folder_when_done", True) \
                and os.path.isdir(self.out_dir):
            core.open_in_file_manager(self.out_dir)


# --------------------------------------------------------------------------- #

def launch(engine=None, src_dir: str = "") -> str | None:
    """
    Entry point called by __init__.py on every button click.

    `engine` is a freshly imported kicad_release module - taking it as an
    argument is what lets the loader hand us new code without KiCad having to
    re-import this package.

    Returns "reload" if the user asked for the code to be re-read.
    """
    global core
    if engine is not None:
        core = engine
    if core is None:
        wx.MessageBox("The Board Release engine could not be imported.",
                      "Board Release", wx.ICON_ERROR)
        return None

    try:
        board = pcbnew.GetBoard()
        board_path = board.GetFileName() if board else ""
        if not board_path or not os.path.isfile(board_path):
            wx.MessageBox("Save the board to disk first.", core.APP_NAME,
                          wx.ICON_WARNING)
            return None

        project = core.find_project(os.path.dirname(board_path))
        if project is None:
            wx.MessageBox(
                "No .kicad_pro found next to this board.\n\n"
                "Board Release works on a saved KiCad project.",
                core.APP_NAME, wx.ICON_WARNING)
            return None

        cfg = core.load_config(project.dir)
        parent = wx.FindWindowByName("PcbFrame") or wx.GetActiveWindow()
        dlg = ReleaseDialog(parent, project, cfg, src_dir)
        rc = dlg.ShowModal()
        dlg.shutdown()
        wx.SafeYield()            # let queued log events drain before teardown
        dlg.Destroy()
        return RELOAD if rc == _RELOAD_MODAL_CODE else None
    except Exception:
        wx.MessageBox(traceback.format_exc(), core.APP_NAME + " - error",
                      wx.ICON_ERROR)
        return None
