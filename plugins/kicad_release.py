#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Board Release  -  one-shot release package generator for KiCad projects.

Everything you normally do by hand when you cut a board revision, in one go,
filed into <project>/archive/<revision>/ (or wherever archive_root points):

    01_Documentation   schematic PDF (all pages) + PCB PDF (copper + silkscreen)
    02_Manufacturing   gerbers.zip, drills, BOM.csv, positions.csv
                       ...and the whole folder zipped, ready to send to the fab
    03_Interactive_BOM interactive HTML BOM
    04_Reports         ERC / DRC (optional)
    05_Source          source snapshot zip
    06_3D              STEP model (optional)

Front ends
    python kicad_release.py                     -> interactive checklist menu
    python kicad_release.py --yes               -> run saved defaults, no prompts
    python kicad_release.py --doctor            -> test every external tool
    (and)  the "Board Release" toolbar button inside the KiCad PCB editor

Design notes
    * stdlib only, no pip installs
    * only ONE external plugin is used (InteractiveHtmlBom); manufacturing files
      are produced by kicad-cli directly, so there is no dependency on the
      JLCPCB Fabrication Toolkit
    * every kicad-cli flag is probed against --help before use, so this works
      across KiCad 8 / 9 / 10 without edits
    * nothing is overwritten without asking

MIT licence.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import fnmatch
import glob
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile

__version__ = "1.1.0"
APP_NAME = "Board Release"
CONFIG_BASENAME = ".board_release.json"

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


# --------------------------------------------------------------------------- #
#  console helpers
# --------------------------------------------------------------------------- #

def _unicode_ok() -> bool:
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "✓✗─".encode(enc)
        return True
    except Exception:
        return False


_U = _unicode_ok()
TICK = "[✓]" if _U else "[x]"
CROSS = "[✗]" if _U else "[!]"
BLANK = "[ ]"
RULE = "─" * 74 if _U else "-" * 74


class Log:
    """Collects output so the GUI can show it and the archive can keep a copy."""

    def __init__(self, sink=None, quiet: bool = False):
        self.lines: list[str] = []
        self.sink = sink
        self.quiet = quiet

    def __call__(self, msg: str = "") -> None:
        self.lines.append(msg)
        if self.sink:
            try:
                self.sink(msg)
            except Exception:
                pass
        if not self.quiet:
            try:
                print(msg, flush=True)
            except UnicodeEncodeError:
                print(msg.encode("ascii", "replace").decode("ascii"), flush=True)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _slug(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = re.sub(r"\s+", "_", text)
    return text.strip("._ ") or "unnamed"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _q(s: str) -> str:
    return f'"{s}"' if " " in s else s


def _no_window() -> dict:
    """Stop Windows flashing a console window for every child process."""
    if not IS_WIN:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": si, "creationflags": 0x08000000}   # CREATE_NO_WINDOW


# --------------------------------------------------------------------------- #
#  locating KiCad and its plugins
# --------------------------------------------------------------------------- #

def kicad_version_key(path: str) -> tuple:
    """
    Sort key for a KiCad install path.

    Plain string sorting is wrong here: "9.0" sorts AFTER "10.0", so a machine
    with both installed would silently get the older kicad-cli - and an older
    kicad-cli cannot read a project saved by a newer KiCad ("Failed to load
    schematic").
    """
    best = (0, 0)
    for part in re.split(r"[\\/]", path):
        m = re.fullmatch(r"(\d+)\.(\d+)", part)
        if m:
            best = max(best, (int(m.group(1)), int(m.group(2))))
    return best


def find_kicad_cli(hint: str | None = None) -> str | None:
    if hint:
        if os.path.isfile(hint):
            return hint
        w = shutil.which(hint)
        if w:
            return w

    # Running inside KiCad? Then that install's kicad-cli is the right one -
    # it is guaranteed to understand the file formats this KiCad just wrote.
    for base in _kicad_roots_from_pcbnew():
        for cand in (os.path.join(base, "kicad-cli.exe"),
                     os.path.join(base, "kicad-cli"),
                     os.path.join(base, "bin", "kicad-cli.exe"),
                     os.path.join(base, "bin", "kicad-cli")):
            if os.path.isfile(cand):
                return cand

    found = shutil.which("kicad-cli")
    if found:
        return found

    pats: list[str] = []
    if IS_WIN:
        pats += [r"C:\Program Files\KiCad\*\bin\kicad-cli.exe",
                 r"C:\Program Files (x86)\KiCad\*\bin\kicad-cli.exe",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs\KiCad\*\bin\kicad-cli.exe")]
    elif IS_MAC:
        pats += ["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                 "/Applications/KiCad*/KiCad.app/Contents/MacOS/kicad-cli",
                 os.path.expanduser("~/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")]
    else:
        pats += ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
                 "/snap/bin/kicad.kicad-cli",
                 os.path.expanduser("~/.local/bin/kicad-cli")]

    hits: list[str] = []
    for p in pats:
        hits.extend(glob.glob(p))
    if hits:
        hits.sort(key=kicad_version_key)      # newest KiCad, numerically
        return hits[-1]

    if shutil.which("flatpak"):
        try:
            out = subprocess.run(["flatpak", "list", "--app", "--columns=application"],
                                 capture_output=True, text=True, timeout=20,
                                 **_no_window()).stdout
            if "org.kicad.KiCad" in out:
                return "flatpak:org.kicad.KiCad"
        except Exception:
            pass
    return None


def all_kicad_clis() -> list[str]:
    """Every kicad-cli on this machine, newest first - for the doctor."""
    pats = []
    if IS_WIN:
        pats = [r"C:\Program Files\KiCad\*\bin\kicad-cli.exe",
                r"C:\Program Files (x86)\KiCad\*\bin\kicad-cli.exe"]
    elif IS_MAC:
        pats = ["/Applications/KiCad*/KiCad.app/Contents/MacOS/kicad-cli"]
    else:
        pats = ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]
    hits = []
    for p in pats:
        hits.extend(glob.glob(p))
    w = shutil.which("kicad-cli")
    if w and w not in hits:
        hits.append(w)
    return sorted(set(hits), key=kicad_version_key, reverse=True)


def cli_argv(kicad_cli: str) -> list[str]:
    if kicad_cli.startswith("flatpak:"):
        return ["flatpak", "run", "--command=kicad-cli", kicad_cli.split(":", 1)[1]]
    return [kicad_cli]


def _kicad_roots_from_pcbnew() -> list[str]:
    """
    If pcbnew is importable, walk up from its file to find the KiCad install.
    This is by far the most reliable way to locate KiCad's own Python, which is
    what InteractiveHtmlBom needs.
    """
    try:
        import pcbnew
        start = os.path.dirname(os.path.abspath(pcbnew.__file__))
    except Exception:
        return []
    out = []
    d = start
    for _ in range(7):
        out.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return out


def _is_python_exe(p: str) -> bool:
    return os.path.basename(p).lower().startswith("python")


def python_candidates(kicad_cli: str | None) -> list[str]:
    """Every interpreter worth testing for `import pcbnew`, best first."""
    out: list[str] = []

    def add(p: str) -> None:
        if p and p not in out:
            out.append(p)

    # 1. we may already BE a suitable interpreter
    try:
        import pcbnew  # noqa: F401
        if _is_python_exe(sys.executable):
            add(sys.executable)
    except Exception:
        pass

    # 2. derived from where pcbnew actually lives  (the reliable one)
    for base in _kicad_roots_from_pcbnew():
        for rel in (("python.exe",), ("python3",), ("bin", "python.exe"),
                    ("bin", "python3"), ("bin", "python")):
            add(os.path.join(base, *rel))
        for pat in ("Frameworks/Python.framework/Versions/*/bin/python3",
                    "python/bin/python3"):
            for hit in sorted(glob.glob(os.path.join(base, *pat.split("/")))):
                add(hit)

    # 3. derived from kicad-cli's location
    if kicad_cli and not kicad_cli.startswith("flatpak:"):
        bindir = os.path.dirname(os.path.abspath(kicad_cli))
        root = os.path.dirname(bindir)
        add(os.path.join(bindir, "python.exe"))
        add(os.path.join(bindir, "python3"))
        for hit in sorted(glob.glob(os.path.join(
                root, "Frameworks", "Python.framework", "Versions", "*", "bin", "python3"))):
            add(hit)

    # 4. well-known install locations
    if IS_WIN:
        for hit in sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\python.exe"),
                          key=kicad_version_key, reverse=True):
            add(hit)
    if IS_MAC:
        for hit in sorted(glob.glob("/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                                    "Python.framework/Versions/*/bin/python3")):
            add(hit)

    # 5. plain system pythons (fine on Linux where pcbnew is a system package)
    if _is_python_exe(sys.executable):
        add(sys.executable)
    add(shutil.which("python3") or "")
    add(shutil.which("python") or "")
    return [p for p in out if p]


def runner_python(kicad_cli: str | None = None) -> str | None:
    """
    Any working interpreter. kicad_release.py is standard library only, so this
    does NOT need pcbnew - it only has to exist. Used to run the release in a
    subprocess so nothing can destabilise KiCad.
    """
    for c in python_candidates(kicad_cli):
        if os.path.isfile(c):
            return c
    return shutil.which("python3") or shutil.which("python")


def probe_python(path: str) -> tuple[bool, str]:
    """Can this interpreter import pcbnew? Returns (ok, detail)."""
    if not os.path.isfile(path):
        return False, "not found"
    try:
        r = subprocess.run([path, "-c",
                            "import pcbnew, sys; print(pcbnew.GetBuildVersion())"],
                           capture_output=True, text=True, timeout=90, **_no_window())
    except Exception as e:
        return False, str(e)
    if r.returncode == 0:
        return True, (r.stdout or "").strip()
    err = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
    return False, (err[-1] if err else f"exit {r.returncode}")


def find_kicad_python(kicad_cli: str | None, hint: str | None = None) -> str | None:
    if hint and probe_python(hint)[0]:
        return hint
    for c in python_candidates(kicad_cli):
        if probe_python(c)[0]:
            return c
    return None


def documents_dirs() -> list[str]:
    """
    Every plausible Documents folder, best first.

    This matters more than it looks. On Windows, Documents is often
    redirected into OneDrive or Dropbox, and the plain Users/<you>/Documents folder
    is left behind holding an older copy of the KiCad tree. Picking the wrong
    one means finding a plugin built for a KiCad version you are not running.
    """
    out: list[str] = []

    def add(p):
        if p and p not in out:
            out.append(p)

    if IS_WIN:
        try:
            import winreg
            for key in (r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                        val, _ = winreg.QueryValueEx(k, "Personal")
                        add(os.path.expandvars(str(val)))
                except OSError:
                    pass
        except Exception:
            pass
        od = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        if od:
            add(os.path.join(od, "Documents"))
    add(os.path.join(os.path.expanduser("~"), "Documents"))
    return [d for d in out if os.path.isdir(d)]


def kicad_plugin_dirs() -> list[str]:
    dirs: list[str] = []
    # KiCad sets these for its own scripting, and a subprocess inherits them,
    # so inside KiCad this is the authoritative answer.
    for var in ("KICAD_PLUGIN_PATH", "KICAD10_3RD_PARTY", "KICAD9_3RD_PARTY",
                "KICAD8_3RD_PARTY"):
        env = os.environ.get(var)
        if not env:
            continue
        for part in env.split(os.pathsep):
            dirs.append(part)
            dirs.append(os.path.join(part, "plugins"))

    home = os.path.expanduser("~")
    if IS_WIN:
        roots = [os.path.join(d, "KiCad") for d in documents_dirs()]
        roots.append(os.path.join(os.environ.get("APPDATA", ""), "kicad"))
    elif IS_MAC:
        roots = [os.path.join(home, "Documents", "KiCad"),
                 os.path.join(home, "Library", "Application Support", "kicad")]
    else:
        roots = [os.path.join(home, ".local", "share", "kicad"),
                 os.path.join(home, ".var", "app", "org.kicad.KiCad", "data", "kicad")]

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for ver in sorted(os.listdir(root), key=lambda v: kicad_version_key(v),
                          reverse=True):
            base = os.path.join(root, ver)
            if os.path.isdir(base):
                dirs.append(os.path.join(base, "3rdparty", "plugins"))
                dirs.append(os.path.join(base, "scripting", "plugins"))

    dirs.append(os.path.join(home, ".kicad_plugins"))
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def load_assembly_module():
    """
    Import assembly_bom.py from beside this file, fresh every time so it hot
    reloads with everything else.
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assembly_bom.py")
    if not os.path.isfile(path):
        return None
    sys.modules.pop("board_release_assembly", None)
    spec = importlib.util.spec_from_file_location("board_release_assembly", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["board_release_assembly"] = mod
    spec.loader.exec_module(mod)
    return mod


def find_ibom_candidates(hint: str | None = None,
                         prefer: tuple | None = None) -> list[str]:
    """
    Every InteractiveHtmlBom copy on this machine, best first.

    Previously this returned the SHORTEST path, which on a machine with two
    Documents trees reliably picked the stale copy under an older KiCad. An
    iBOM built for KiCad 9 cannot be imported by KiCad 10's Python, so that
    choice guaranteed failure. Rank by matching the running KiCad instead.
    """
    hits: list[str] = []
    if hint and os.path.isfile(hint):
        hits.append(hint)
    for d in kicad_plugin_dirs():
        hits.extend(glob.glob(os.path.join(d, "generate_interactive_bom.py")))
        for depth in ("*", "*/*", "*/*/*"):
            hits.extend(glob.glob(os.path.join(d, depth, "generate_interactive_bom.py")))
    w = shutil.which("generate_interactive_bom")
    if w:
        hits.append(w)

    seen, uniq = set(), []
    for h in hits:
        k = os.path.normcase(os.path.abspath(h))
        if k not in seen:
            seen.add(k)
            uniq.append(h)

    def rank(p: str):
        v = kicad_version_key(p)
        exact = 0 if (prefer and v == prefer) else 1
        return (exact, -v[0], -v[1], len(p))

    uniq.sort(key=rank)
    return uniq


def find_ibom(hint: str | None = None, prefer: tuple | None = None) -> str | None:
    hits = find_ibom_candidates(hint, prefer)
    return hits[0] if hits else None


# --------------------------------------------------------------------------- #
#  kicad-cli wrapper with capability probing
# --------------------------------------------------------------------------- #

class KiCadCli:
    """
    Flags are passed as GROUPS - each group is [flag] or [flag, value...].
    If the flag is not supported by this KiCad build the whole group is dropped.
    The positional input file is passed separately so it can never be eaten by
    a dropped flag.
    """

    def __init__(self, exe: str, log: Log):
        self.exe = exe
        self.log = log
        self._help: dict[str, str] = {}
        self.version = self._version()

    def _version(self) -> str:
        try:
            r = subprocess.run(cli_argv(self.exe) + ["version"], capture_output=True,
                               text=True, timeout=60, **_no_window())
            txt = (r.stdout or r.stderr or "").strip()
            return txt.splitlines()[0] if txt else "unknown"
        except Exception as e:
            return f"unavailable ({e})"

    @property
    def major(self) -> int:
        m = re.match(r"(\d+)", self.version or "")
        return int(m.group(1)) if m else 0

    def help_for(self, subcmd: tuple[str, ...]) -> str:
        key = " ".join(subcmd)
        if key not in self._help:
            try:
                r = subprocess.run(cli_argv(self.exe) + list(subcmd) + ["--help"],
                                   capture_output=True, text=True, timeout=60,
                                   **_no_window())
                self._help[key] = (r.stdout or "") + (r.stderr or "")
            except Exception:
                self._help[key] = ""
        return self._help[key]

    def supports(self, subcmd: tuple[str, ...], flag: str) -> bool:
        return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])",
                         self.help_for(subcmd)) is not None

    def has_subcommand(self, parent: tuple[str, ...], name: str) -> bool:
        return re.search(rf"^\s*{re.escape(name)}\b", self.help_for(parent), re.M) is not None

    def run(self, subcmd: tuple[str, ...], flags: list[list[str]],
            positional: str | None = None, cwd: str | None = None,
            timeout: int = 1800, quiet_drop: bool = False) -> tuple[bool, str]:
        argv: list[str] = []
        for group in flags:
            if not group:
                continue
            name = group[0]
            if name.startswith("-") and not self.supports(subcmd, name):
                if not quiet_drop:
                    self.log(f"      note: this KiCad build has no {name} - skipping it")
                continue
            argv.extend(group)
        if positional:
            argv.append(positional)

        cmd = cli_argv(self.exe) + list(subcmd) + argv
        self.log("      $ " + " ".join(_q(c) for c in cmd))
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, **_no_window())
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s"
        except Exception as e:
            return False, str(e)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out


# --------------------------------------------------------------------------- #
#  project discovery, title block, text variables
# --------------------------------------------------------------------------- #

def project_text_vars(pro_path: str) -> dict:
    """KiCad projects can define ${VARS} used inside title blocks."""
    try:
        with open(pro_path, encoding="utf-8", errors="ignore") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    tv = data.get("text_variables") or {}
    return {str(k): str(v) for k, v in tv.items() if isinstance(v, (str, int, float))}


def expand_vars(text: str, variables: dict) -> str:
    if not text or "${" not in text:
        return text or ""
    return re.sub(r"\$\{([^}]+)\}",
                  lambda m: variables.get(m.group(1), m.group(0)), text)


def parse_title_block(path: str | None) -> dict:
    """Pull title / rev / company / date from a .kicad_sch or .kicad_pcb."""
    out: dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            head = fh.read(300_000)
    except Exception:
        return out
    m = re.search(r"\(title_block\b(.*?)\n\s*\)\s*\n", head, re.S)
    block = m.group(1) if m else head[:30_000]
    for key in ("title", "rev", "company", "date"):
        mm = re.search(rf'\(\s*{key}\s+"((?:[^"\\]|\\.)*)"\s*\)', block)
        if mm:
            out[key] = mm.group(1).replace('\\"', '"').strip()
    return out


# Where to look for the revision, in order. The BOARD is the default: it is the
# thing being released, and its title block is what the fab and the assembler
# see on the fabrication drawing.
DEFAULT_REVISION_ORDER = ("board", "schematic", "project")


# Field names that usually hold a part number, in the order we prefer them.
# Anything here that the schematic actually defines gets carried into the BOM,
# the interactive BOM and the assembly BOM.
PART_NUMBER_FIELDS = [
    "MPN", "MfrPN", "MFP", "Mfr Part Number", "Manufacturer Part Number",
    "Manufacturer", "Mfr", "MFR", "MFG", "Vendor",
    "LCSC", "LCSC Part #", "LCSC Part Number", "JLCPCB Part #",
    "Digikey", "Digi-Key", "DigiKey PN", "Mouser", "Mouser PN",
    "Part Number", "PartNumber", "PN", "SKU", "OrderCode", "Order Code",
]

# KiCad's own built-ins; never treated as user fields
BUILTIN_FIELDS = {"Reference", "Value", "Footprint", "Datasheet", "Description"}


def schematic_field_names(sch_paths) -> list[str]:
    """
    Every user-defined symbol field name used anywhere in the schematic.

    Read straight out of the .kicad_sch files. Guessing a field name (the way
    an earlier version hardcoded "LCSC") produces an empty column on every
    project that calls it something else.
    """
    found: list[str] = []
    seen = set()
    for path in sch_paths or []:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except Exception:
            continue
        for name in re.findall(r'\(\s*property\s+"((?:[^"\\]|\\.)*)"', text):
            name = name.replace('\\"', '"').strip()
            if not name or name in BUILTIN_FIELDS or name.startswith("ki_"):
                continue
            key = name.lower()
            if key not in seen:
                seen.add(key)
                found.append(name)
    return found


class Project:
    def __init__(self, pro_path: str, revision_from=DEFAULT_REVISION_ORDER):
        self.pro = os.path.abspath(pro_path)
        self.dir = os.path.dirname(self.pro)
        self.stem = os.path.splitext(os.path.basename(self.pro))[0]
        self.vars = project_text_vars(self.pro)
        self.revision_from = tuple(revision_from)

        self.sch = self._preferred(".kicad_sch")
        self.pcb = self._preferred(".kicad_pcb")

        self.title = self._resolve("title", ("TITLE",)) or self.stem
        self.company = self._resolve("company", ("COMPANY",))
        self.date = self._resolve("date", ("ISSUE_DATE", "DATE"))
        self.rev = ""
        self.rev_source = ""
        self.resolve_revision(self.revision_from)

    def resolve_revision(self, order=None) -> str:
        """
        (Re)read the revision. Called again once the config is loaded, so
        "revision_from" can change the search order without reopening files.
        """
        if order:
            self.revision_from = tuple(order)
        self._rev_source = ""
        self.rev = self._resolve("rev", ("REVISION", "REV", "Revision", "Rev"))
        self.rev_source = self._rev_source
        return self.rev

    def _preferred(self, ext: str) -> str | None:
        p = os.path.join(self.dir, self.stem + ext)
        if os.path.isfile(p):
            return p
        hits = sorted(glob.glob(os.path.join(self.dir, "*" + ext)))
        return hits[0] if hits else None

    def all_sheets(self) -> list[str]:
        """Every .kicad_sch in the project, root first."""
        out = [self.sch] if self.sch else []
        for p in sorted(glob.glob(os.path.join(self.dir, "**", "*.kicad_sch"),
                                  recursive=True)):
            if p not in out:
                out.append(p)
        return [p for p in out if p]

    def _sheets(self, key: str = "") -> list[str]:
        """Files to search, in the configured order."""
        board = [self.pcb] if self.pcb else []
        sch: list[str] = []
        if self.sch:
            sch.append(self.sch)                       # root sheet first
        for p in sorted(glob.glob(os.path.join(self.dir, "**", "*.kicad_sch"),
                                  recursive=True)):
            if p not in sch:
                sch.append(p)

        # only the revision honours the configured order; title/company/date
        # are the same everywhere and read best from the schematic
        order = self.revision_from if key == "rev" else ("schematic", "board")
        out: list[str] = []
        for src in order:
            out.extend(board if src == "board" else sch if src == "schematic" else [])
        return [p for p in out if p]

    def _resolve(self, key: str, var_names: tuple[str, ...]) -> str:
        """
        Look for the field in the root sheet, then any other sheet, then the
        board, expanding ${VARS} as we go. Falls back to the project's own text
        variables, which is where a lot of people actually keep the revision.
        """
        self._rev_source = getattr(self, "_rev_source", "")
        if key == "rev" and "project" == self.revision_from[0]:
            for name in var_names:
                if self.vars.get(name, "").strip():
                    self._rev_source = f"project variable ${{{name}}}"
                    return self.vars[name].strip()
        for path in self._sheets(key):
            raw = parse_title_block(path).get(key, "")
            val = expand_vars(raw, self.vars).strip()
            if val and "${" not in val:
                if key == "rev":
                    what = "board" if path == self.pcb else "schematic"
                    self._rev_source = f"{what} title block ({os.path.basename(path)})"
                return val
        for name in var_names:
            if self.vars.get(name, "").strip():
                if key == "rev":
                    self._rev_source = f"project variable ${{{name}}}"
                return self.vars[name].strip()
        return ""

    def __str__(self):
        return f"{self.stem} ({self.pro})"


def find_project(start: str | None = None) -> Project | None:
    start = os.path.abspath(start or os.getcwd())
    if os.path.isfile(start):
        if start.endswith(".kicad_pro"):
            return Project(start)
        start = os.path.dirname(start)
    d = start
    for _ in range(6):
        hits = sorted(glob.glob(os.path.join(d, "*.kicad_pro")))
        if hits:
            return Project(hits[0])
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


# --------------------------------------------------------------------------- #
#  board layer table
# --------------------------------------------------------------------------- #

LAYER_ALIASES = {
    "F.Silkscreen": ("F.SilkS",), "B.Silkscreen": ("B.SilkS",),
    "F.SilkS": ("F.Silkscreen",), "B.SilkS": ("B.Silkscreen",),
    "User.Comments": ("Cmts.User",), "Cmts.User": ("User.Comments",),
    "User.Drawings": ("Dwgs.User",), "Dwgs.User": ("User.Drawings",),
    "F.Adhesive": ("F.Adhes",), "B.Adhesive": ("B.Adhes",),
    "User.Eco1": ("Eco1.User",), "User.Eco2": ("Eco2.User",),
}


def board_layers(pcb_path: str | None) -> list[str]:
    """
    Enabled layers, in board order. KiCad only writes enabled layers into the
    (layers ...) table, so this is also the real stackup.
    """
    if not pcb_path or not os.path.isfile(pcb_path):
        return []
    try:
        with open(pcb_path, encoding="utf-8", errors="ignore") as fh:
            txt = fh.read(600_000)
    except Exception:
        return []
    m = re.search(r"\n\s*\(layers\s*\n(.*?)\n\s*\)\s*\n", txt, re.S)
    if not m:
        return []
    # Scan the whole block rather than line by line - the file format makes no
    # promise about one layer per line, and a missed layer silently drops a
    # page from the board PDF or a file from the gerber set.
    names: list[str] = []
    for canonical, friendly in re.findall(
            r'\(\s*\d+\s+"?([\w.]+)"?\s+\w+(?:\s+"([^"]+)")?', m.group(1)):
        for nm in (canonical, friendly):
            if nm and nm not in names:
                names.append(nm)
    return names


def _present(wanted: str, present: set) -> bool:
    return wanted in present or any(a in present for a in LAYER_ALIASES.get(wanted, ()))


def copper_layers(present: list[str]) -> list[str]:
    """F.Cu, In1.Cu ... InN.Cu, B.Cu - in stackup order."""
    inner = sorted((l for l in present if re.fullmatch(r"In\d+\.Cu", l)),
                   key=lambda s: int(re.findall(r"\d+", s)[0]))
    out = []
    if "F.Cu" in present:
        out.append("F.Cu")
    out += inner
    if "B.Cu" in present:
        out.append("B.Cu")
    return out or ["F.Cu", "B.Cu"]


def silkscreen_layers(present: list[str]) -> list[str]:
    out = []
    for want in ("F.Silkscreen", "B.Silkscreen"):
        if _present(want, set(present)):
            out.append(want)
    return out or ["F.Silkscreen", "B.Silkscreen"]


# Layers offered to the fab. Anything with nothing drawn on it is pruned after
# export, so a generous list here costs nothing.
GERBER_CANDIDATES = [
    "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask", "F.Paste", "B.Paste",
    "Edge.Cuts", "F.Fab", "B.Fab", "F.Adhesive", "B.Adhesive",
    "User.1", "User.2", "User.3", "User.4",
]


# --------------------------------------------------------------------------- #
#  configuration
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
#  archive location
# --------------------------------------------------------------------------- #

# Blank archive_root = <project>/archive. Relative paths are relative to the
# PROJECT folder (not the current directory), so one saved value works for
# every board. {project_dir} and {project} may also be used in the path.
ARCHIVE_DIRNAME = "archive"


def default_archive_root(project_dir: str) -> str:
    """<project>/archive - or an existing <project>/Archive from older versions."""
    legacy = os.path.join(project_dir, "Archive")
    new = os.path.join(project_dir, ARCHIVE_DIRNAME)
    if os.path.isdir(legacy) and not os.path.isdir(new):
        return legacy
    return new


def resolve_archive_root(value: str, project_dir: str, project_stem: str = "") -> str:
    """Turn a configured archive_root into an absolute folder for this project."""
    value = (value or "").strip()
    if not value:
        return os.path.abspath(default_archive_root(project_dir))
    value = value.replace("{project_dir}", project_dir)
    if project_stem:
        value = value.replace("{project}", _slug(project_stem))
    value = os.path.expanduser(os.path.expandvars(value))
    if not os.path.isabs(value):
        value = os.path.join(project_dir, value)
    return os.path.abspath(value)


def portable_archive_root(value: str, project_dir: str) -> str:
    """
    The form of archive_root worth SAVING: blank when it is the default, a path
    relative to the project when it lives inside it, otherwise as given. This is
    what stops one board's absolute path being carried into the next project.
    """
    value = (value or "").strip()
    if not value or not project_dir:
        return value
    full = resolve_archive_root(value, project_dir)
    proj = os.path.abspath(project_dir)
    if os.path.normcase(full) == os.path.normcase(os.path.abspath(default_archive_root(proj))):
        return ""
    try:
        rel = os.path.relpath(full, proj)
    except ValueError:                   # different drive on Windows
        return value
    if rel == os.curdir or rel.startswith(os.pardir):
        return value
    return rel.replace(os.sep, "/")


def _is_fixed_path(value: str) -> bool:
    """An absolute archive_root with nothing project-specific in it."""
    value = (value or "").strip()
    if not value or "{project" in value:
        return False
    return os.path.isabs(os.path.expanduser(os.path.expandvars(value)))


DEFAULT_CONFIG = {
    # blank = <project>/archive. Relative = inside the project folder.
    "archive_root": "",
    # "" = no prefix, so the folder is just  Archive/C/.  Set to "Rev" for RevC.
    "rev_prefix": "",
    # where the revision comes from, in order: "board", "schematic", "project"
    "revision_from": ["board", "schematic", "project"],
    # tokens: {rev} {revlabel} {project} {title} {date} {year} {month} {day}
    #         {company} {user}
    "folder_template": "{revlabel}",
    "file_name_template": "{project}_{revlabel}",

    "subfolders": {
        "docs":          "01_Documentation",
        "manufacturing": "02_Manufacturing",
        "ibom":          "03_Assembly_BOM",
        "reports":       "04_Reports",
        "source":        "05_Source",
        "models":        "06_3D",
    },

    "steps": {
        "sch_pdf":       True,
        "pcb_pdf":       True,
        "manufacturing": True,
        "assembly_bom":  True,
        "ibom":          False,      # superseded by assembly_bom; no plugin needed
        "erc":           False,
        "drc":           False,
        "step":          False,
        "source_zip":    True,
        "manifest":      True,
    },

    # --- documentation ---------------------------------------------------- #
    # empty = every copper layer plus both silkscreens, in that order
    "pcb_pdf_layers": [],
    "pcb_pdf_extra_args": ["--mode-multipage", "--include-border-title"],
    "sch_pdf_extra_args": [],

    # --- manufacturing ---------------------------------------------------- #
    # empty = copper layers from the board + GERBER_CANDIDATES, empties pruned
    "gerber_layers": [],
    "gerber_extra_args": ["--use-drill-file-origin", "--no-x2"],
    "drill_extra_args": ["--format", "excellon", "--excellon-units", "mm",
                         "--excellon-separate-th", "--generate-map",
                         "--map-format", "gerberx2", "--drill-origin", "plot"],
    "pos_extra_args": ["--format", "csv", "--units", "mm", "--side", "both",
                       "--exclude-dnp", "--use-drill-file-origin"],
    # Blank = work the columns out from the fields the schematic defines.
    # Set them to override completely.
    "bom_fields": "",
    "bom_labels": "",
    # Candidates considered to be part numbers, in preference order. Only the
    # ones your schematic actually uses are included.
    "part_number_fields": [],
    # Force an exact list instead of auto-detection.
    "bom_extra_fields": [],
    "bom_group_by": "Value,Footprint",
    "bom_exclude_dnp": True,
    "prune_empty_gerbers": True,
    "zip_manufacturing": True,

    # --- interactive bom -------------------------------------------------- #
    "assembly_bom_dark": True,
    "assembly_bom_include_dnp": False,
    "ibom_extra_args": ["--no-browser", "--dark-mode", "--layer-view", "FB"],

    # --- 3D --------------------------------------------------------------- #
    "step_extra_args": ["--subst-models", "--no-dnp"],

    "open_folder_when_done": True,
    "kicad_cli": "",
    "kicad_python": "",
    "ibom_script": "",
}


def user_config_path() -> str:
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                            "BoardRelease", "config.json")
    if IS_MAC:
        return os.path.expanduser("~/Library/Application Support/BoardRelease/config.json")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "board_release", "config.json")


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# Values that older versions wrote into saved configs as if they were the
# user's choice. They were only ever defaults, and leaving them in place means
# a config saved months ago silently overrides a later fix.
STALE_DEFAULTS = {
    "bom_fields": {"Value,Reference,Footprint,${QUANTITY},LCSC"},
    "bom_labels": {"Comment,Designator,Footprint,Quantity,LCSC Part #"},
}


def _drop_stale(cfg: dict) -> dict:
    for key, bad in STALE_DEFAULTS.items():
        if str(cfg.get(key, "")).strip() in bad:
            cfg[key] = ""
    return cfg


def load_config(project_dir: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user_path = user_config_path()
    for path in (user_path,
                 os.path.join(project_dir, CONFIG_BASENAME) if project_dir else None):
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as e:
                print(f"  warning: could not read {path}: {e}")
                continue
            if not isinstance(data, dict):
                continue
            # A fixed absolute folder in the USER-WIDE config would send every
            # board to the same place. Unless the folder names are per-project
            # ({project} in the path or folder_template), ignore it and use the
            # project's own archive folder.
            if path == user_path and _is_fixed_path(data.get("archive_root", "")):
                template = data.get("folder_template", cfg.get("folder_template", ""))
                if "{project" not in str(template):
                    data = dict(data)
                    data.pop("archive_root", None)
            cfg = _deep_merge(cfg, data)
    return _drop_stale(cfg)


def update_project_config(project_dir: str, **values) -> str | None:
    """
    Merge a few keys into the project's own .board_release.json.

    Deliberately surgical: it rewrites only the keys given and leaves the rest
    of the file alone. Dumping the whole merged config here would bake today's
    defaults into the file and stop future versions from changing them.
    """
    if not project_dir or not os.path.isdir(project_dir):
        return None
    path = os.path.join(project_dir, CONFIG_BASENAME)
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            return None                  # unreadable or hand-edited: don't clobber it
    changed = False
    for k, v in values.items():
        if data.get(k) != v:
            data[k] = v
            changed = True
    if not changed:
        return path
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        return None
    return path


# Always worth recording even when it matches the default - these are the
# things the dialog owns and the user expects to stick.
ALWAYS_SAVE = ("steps", "archive_root", "rev_prefix")


def save_config(cfg: dict, where: str, full: bool = False) -> str:
    """
    Write a config file.

    By default only values that DIFFER from the built-in defaults are written.
    Dumping the whole merged config freezes today's defaults into the file, so a
    later version can never improve them - that is exactly how a saved config
    went on forcing a hardcoded "LCSC" BOM column long after the code had
    learned to detect field names properly.
    """
    data = dict(cfg) if full else {
        k: v for k, v in cfg.items()
        if k in ALWAYS_SAVE or v != DEFAULT_CONFIG.get(k)
    }
    _drop_stale(data)
    os.makedirs(os.path.dirname(where) or ".", exist_ok=True)
    with open(where, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return where


# --------------------------------------------------------------------------- #
#  gerber content detection
# --------------------------------------------------------------------------- #

GERBER_EXTS = (".gbr", ".gbl", ".gtl", ".gbs", ".gts", ".gbo", ".gto", ".gbp",
               ".gtp", ".gm1", ".gko", ".gba", ".gta", ".g1", ".g2", ".g3", ".g4",
               ".gp1", ".gp2")

# D01 = draw, D03 = flash. D10+ are aperture selects, so the regex must not
# match those: D0*[13]* only matches D1 D3 D01 D03, never D13 or D10.
_DRAW_RE = re.compile(r"D0*[13]\*")


def gerber_has_content(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            txt = fh.read()
    except Exception:
        return True                      # if in doubt, keep the file
    if "G36*" in txt:                    # region fill
        return True
    return bool(_DRAW_RE.search(txt))


def layer_from_gerber_name(path: str, board_stem: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    if name.lower().startswith(board_stem.lower()):
        name = name[len(board_stem):].lstrip("-_")
    return name.replace("_", ".") or os.path.basename(path)


# --------------------------------------------------------------------------- #
#  the release run
# --------------------------------------------------------------------------- #

class StepResult:
    def __init__(self, key: str, label: str):
        self.key = key
        self.label = label
        self.ok: bool | None = None
        self.message = ""
        self.detail = ""
        self.files: list[str] = []
        self.skipped = False


STEP_ORDER = [
    ("sch_pdf",       "Schematic PDF - all pages"),
    ("pcb_pdf",       "PCB PDF - copper, silkscreen + fab layers"),
    ("manufacturing", "Manufacturing package - gerbers, drills, BOM, positions"),
    ("assembly_bom",  "Interactive assembly BOM (built in, no plugin)"),
    ("ibom",          "Interactive HTML BOM (needs the InteractiveHtmlBom plugin)"),
    ("erc",           "ERC report"),
    ("drc",           "DRC report"),
    ("step",          "3D model - STEP"),
    ("source_zip",    "Source snapshot .zip"),
    ("manifest",      "Release manifest + log"),
]
STEP_LABELS = dict(STEP_ORDER)


class ReleaseRunner:
    def __init__(self, project: Project, cfg: dict, rev: str, log: Log,
                 dry_run: bool = False, stop_on_error: bool = False):
        self.p = project
        self.cfg = cfg
        self.rev = rev
        self.log = log
        self.dry = dry_run
        self.stop_on_error = stop_on_error
        self.results: list[StepResult] = []
        self.started = _dt.datetime.now()

        self.cli_exe = cfg.get("kicad_cli") or find_kicad_cli()
        self.cli = KiCadCli(self.cli_exe, log) if self.cli_exe else None
        self.py = cfg.get("kicad_python") or ""
        self.out_root = self.resolve_out_dir()

    # -- naming ------------------------------------------------------------ #

    @property
    def rev_source(self) -> str:
        """Where this revision came from - blank if the user supplied it."""
        return self.p.rev_source if (self.rev and self.rev == self.p.rev) else "typed in"

    @property
    def revlabel(self) -> str:
        return f"{self.cfg.get('rev_prefix', '')}{self.rev}"

    def tokens(self) -> dict:
        now = _dt.datetime.now()
        return {
            "rev": _slug(self.rev) or "X",
            "revlabel": _slug(self.revlabel) or "X",
            "project": _slug(self.p.stem),
            "title": _slug(self.p.title),
            "date": now.strftime("%Y-%m-%d"),
            "year": now.strftime("%Y"),
            "month": now.strftime("%m"),
            "day": now.strftime("%d"),
            "company": _slug(self.p.company),
            "user": _slug(os.environ.get("USERNAME") or os.environ.get("USER") or ""),
        }

    def resolve_out_dir(self) -> str:
        root = resolve_archive_root(self.cfg.get("archive_root", ""), self.p.dir, self.p.stem)
        try:
            rel = self.cfg["folder_template"].format(**self.tokens())
        except KeyError as e:
            self.log(f"  warning: unknown token {e} in folder_template - using {{revlabel}}")
            rel = self.tokens()["revlabel"]
        return os.path.abspath(os.path.join(root, rel))

    def base_name(self) -> str:
        try:
            return _slug(self.cfg["file_name_template"].format(**self.tokens()))
        except KeyError:
            return _slug(f"{self.p.stem}_{self.revlabel}")

    def sub(self, key: str) -> str:
        d = os.path.join(self.out_root, self.cfg["subfolders"].get(key, key))
        if not self.dry:
            os.makedirs(d, exist_ok=True)
        return d

    # -- plumbing ---------------------------------------------------------- #

    def part_fields(self) -> list[str]:
        """Part-number fields this project actually defines, best first."""
        explicit = self.cfg.get("bom_extra_fields")
        if explicit:
            return list(explicit)
        present = schematic_field_names(self.p.all_sheets())
        if not present:
            return []
        by_lower = {f.lower(): f for f in present}
        wanted = self.cfg.get("part_number_fields") or PART_NUMBER_FIELDS
        out = []
        for cand in wanted:
            real = by_lower.get(cand.lower())
            if real and real not in out:
                out.append(real)
        return out

    def _need_cli(self, r: StepResult) -> bool:
        if self.cli is None:
            r.ok = False
            r.message = "kicad-cli not found - run --doctor, or set kicad_cli in the config"
            return False
        return True

    def _record(self, r: StepResult, produced: list[str] | None = None) -> None:
        if produced:
            for f in produced:
                if os.path.exists(f) and f not in r.files:
                    r.files.append(f)
        self.results.append(r)
        if r.skipped:
            self.log(f"   {BLANK} skipped - {r.message}")
        elif r.ok:
            self.log(f"   {TICK} {r.label}" + (f"   {r.message}" if r.message else "")
                     + (f"   [{len(r.files)} file(s)]" if r.files else ""))
        else:
            self.log(f"   {CROSS} {r.label}: {r.message}")
            for line in (r.detail or "").splitlines()[:8]:
                self.log(f"        {line}")

    def _prepare(self, expected: str) -> float:
        """
        Delete whatever is at the target path, then note the time.

        Without this, a file left by a previous run makes a failed step look
        successful - which is exactly how "Failed to load schematic" got
        reported as a pass. Comparing timestamps is not reliable enough at
        one-second filesystem granularity; removing the target is.
        """
        try:
            if os.path.isdir(expected):
                shutil.rmtree(expected, ignore_errors=True)
            elif os.path.exists(expected):
                os.remove(expected)
        except OSError as e:
            self.log(f"      could not clear {os.path.basename(expected)}: {e}")
        return _dt.datetime.now().timestamp()

    def _collect(self, expected: str, since: float, pattern: str = "*.pdf") -> list[str]:
        """
        Return the files this step actually produced, and make sure they end up
        in the folder we asked for.

        kicad-cli does not always put a file exactly where you asked - a
        hierarchical schematic can produce several - so rather than trusting the
        path, look for anything matching that was written during this step and
        move it into place.
        """
        dest_dir = os.path.dirname(os.path.abspath(expected))
        stem, ext = os.path.splitext(os.path.basename(expected))

        def fresh(p: str) -> bool:
            """Written by THIS step - a leftover from a previous run is not proof."""
            try:
                return os.path.getmtime(p) + 2 >= since and os.path.getsize(p) > 0
            except OSError:
                return False

        # kicad-cli sometimes treats --output as a folder to write into rather
        # than the file to write (pcb export pdf --mode-multipage does this),
        # leaving  .../Name_PCB.pdf/Board.pdf .  Flatten it back to one file.
        if os.path.isdir(expected):
            inner = [p for p in sorted(glob.glob(os.path.join(expected, "*" + ext)))
                     if os.path.isfile(p)]
            if len(inner) == 1:
                tmp = expected + ".unwrap"
                shutil.move(inner[0], tmp)
                shutil.rmtree(expected, ignore_errors=True)
                os.replace(tmp, expected)
                self.log(f"      kicad-cli made a folder called "
                         f"{os.path.basename(expected)} - unwrapped it to a single file")
            elif inner:
                for p in inner:
                    shutil.move(p, os.path.join(dest_dir, stem + "_" + os.path.basename(p)))
                shutil.rmtree(expected, ignore_errors=True)
                self.log(f"      kicad-cli wrote {len(inner)} files into a folder"
                         f" - moved them into {os.path.basename(dest_dir)}")

        # the target was cleared before the run, so simply existing is proof
        found = [expected] if (os.path.isfile(expected)
                               and os.path.getsize(expected) > 0) else []

        # In the destination folder only this step's own files count - an
        # earlier step in the same run may have just written a sibling.
        searches = [(dest_dir, stem + "*" + ext), (self.p.dir, pattern)]
        for d, pat in searches:
            for p in glob.glob(os.path.join(d, pat)):
                if not os.path.isfile(p) or not fresh(p):
                    continue
                if os.path.abspath(p) not in [os.path.abspath(f) for f in found]:
                    found.append(p)

        out: list[str] = []
        for p in found:
            if os.path.dirname(os.path.abspath(p)) == dest_dir:
                out.append(p)
                continue
            target = os.path.join(dest_dir, os.path.basename(p))
            try:
                shutil.move(p, target)
                self.log(f"      kicad-cli wrote {os.path.basename(p)} beside the project"
                         f" - moved it into {os.path.basename(dest_dir)}")
                out.append(target)
            except Exception as e:
                self.log(f"      could not move {p}: {e}")
                out.append(p)
        return out

    def _judge(self, r: StepResult, produced: list[str], ok: bool, msg: str,
               noun: str = "file") -> None:
        """
        Decide pass/fail on the ARTIFACT, not the exit code.

        kicad-cli returns non-zero for plenty of things that are not failures -
        a missing symbol library, a substituted font, ERC violations - while
        still writing a perfectly good file. If the file is there, the step
        worked; the non-zero exit becomes a warning you can read in the log.
        """
        good = [p for p in produced if os.path.isfile(p) and os.path.getsize(p) > 0]
        if good:
            r.ok = True
            if not ok and msg.strip():
                r.message = "written, but kicad-cli reported a warning (see log)"
                self.log("      note: kicad-cli exited non-zero but the "
                         f"{noun} was written. It said:")
                for line in msg.strip().splitlines()[:6]:
                    self.log("        " + line)
        else:
            r.ok = False
            r.message = f"no {noun} was produced"
            r.detail = msg
        r.files = good

    def python(self) -> str | None:
        if self.py and probe_python(self.py)[0]:
            return self.py
        found = find_kicad_python(self.cli_exe, self.cfg.get("kicad_python") or None)
        if found:
            self.py = found
        return found

    # ---------------------------------------------------------------- steps #

    def step_sch_pdf(self) -> None:
        r = StepResult("sch_pdf", STEP_LABELS["sch_pdf"])
        if not self._need_cli(r):
            return self._record(r)
        if not self.p.sch:
            r.skipped, r.message = True, "no schematic file"
            return self._record(r)
        out = os.path.join(self.sub("docs"), self.base_name() + "_Schematic.pdf")
        if self.dry:
            r.ok, r.message = True, f"(dry) {out}"
            return self._record(r)
        since = self._prepare(out)
        flags = [["--output", out]] + [[a] for a in self.cfg.get("sch_pdf_extra_args") or []]
        ok, msg = self.cli.run(("sch", "export", "pdf"), flags, self.p.sch)
        self._judge(r, self._collect(out, since, "*.pdf"), ok, msg, "PDF")
        self._record(r)

    def _pcb_pdf_layers(self) -> list[str]:
        if self.cfg.get("pcb_pdf_layers"):
            return list(self.cfg["pcb_pdf_layers"])
        present = board_layers(self.p.pcb)
        pset = set(present)
        # Fab layers go in for the assembly house - they carry the drawings
        # and notes that are deliberately kept off the silkscreen.
        fab = [l for l in ("F.Fab", "B.Fab") if _present(l, pset)]
        return copper_layers(present) + silkscreen_layers(present) + fab

    def step_pcb_pdf(self) -> None:
        r = StepResult("pcb_pdf", STEP_LABELS["pcb_pdf"])
        if not self._need_cli(r):
            return self._record(r)
        if not self.p.pcb:
            r.skipped, r.message = True, "no board file"
            return self._record(r)
        layers = self._pcb_pdf_layers()
        out = os.path.join(self.sub("docs"), self.base_name() + "_PCB.pdf")
        if self.dry:
            r.ok, r.message = True, f"(dry) {out}  [{len(layers)} pages: {', '.join(layers)}]"
            return self._record(r)
        since = self._prepare(out)
        flags = [["--output", out], ["--layers", ",".join(layers)]] \
            + [[a] for a in self.cfg.get("pcb_pdf_extra_args") or []]
        ok, msg = self.cli.run(("pcb", "export", "pdf"), flags, self.p.pcb)
        self._judge(r, self._collect(out, since, "*.pdf"), ok, msg, "PDF")
        if r.ok and not r.message:
            r.message = f"{len(layers)} pages: {', '.join(layers)}"
        self._record(r)

    # ------------------------------------------------------- manufacturing #

    def _gerber_layer_list(self) -> list[str]:
        if self.cfg.get("gerber_layers"):
            return list(self.cfg["gerber_layers"])
        present = board_layers(self.p.pcb)
        pset = set(present)
        out = copper_layers(present)
        for want in GERBER_CANDIDATES:
            if _present(want, pset) and want not in out:
                out.append(want)
        return out

    def step_manufacturing(self) -> None:
        r = StepResult("manufacturing", STEP_LABELS["manufacturing"])
        if not self._need_cli(r):
            return self._record(r)
        if not self.p.pcb:
            r.skipped, r.message = True, "no board file"
            return self._record(r)

        mfg = self.sub("manufacturing")
        base = self.base_name()
        layers = self._gerber_layer_list()

        # versions before 2.1 put this at the archive root; clean up the stray
        legacy = os.path.join(self.out_root, base + "_Manufacturing.zip")
        if not self.dry and os.path.isfile(legacy):
            try:
                os.remove(legacy)
                self.log("      removed the old top-level Manufacturing.zip")
            except OSError:
                pass

        if self.dry:
            r.ok = True
            r.message = (f"(dry) {mfg}  gerbers[{', '.join(layers)}] + drills "
                         f"+ BOM.csv + positions.csv, zipped")
            return self._record(r)

        produced: list[str] = []
        problems: list[str] = []
        fatal: list[str] = []

        # --- gerbers into a scratch folder, then prune, then zip ---------- #
        gdir = os.path.join(mfg, "_gerbers_tmp")
        shutil.rmtree(gdir, ignore_errors=True)
        os.makedirs(gdir, exist_ok=True)

        flags = [["--output", gdir + os.sep], ["--layers", ",".join(layers)]]
        flags += self._grouped(self.cfg.get("gerber_extra_args") or [])
        ok, msg = self.cli.run(("pcb", "export", "gerbers"), flags, self.p.pcb)
        gerber_files = [os.path.join(gdir, f) for f in sorted(os.listdir(gdir))] \
            if os.path.isdir(gdir) else []
        if not gerber_files:
            fatal.append("gerber export produced no files")
            r.detail = msg

        # prune the layers with nothing drawn on them
        kept, dropped, extras = [], [], []
        for f in gerber_files:
            if not os.path.isfile(f):
                continue
            if f.lower().endswith((".gbrjob", ".json")):
                extras.append("job file")     # a manifest, not a layer - always keep
                continue
            lname = layer_from_gerber_name(f, self.p.stem)
            is_gerber = f.lower().endswith(GERBER_EXTS)
            if (self.cfg.get("prune_empty_gerbers", True) and is_gerber
                    and "edge" not in lname.lower()
                    and not gerber_has_content(f)):
                dropped.append(lname)
                os.remove(f)
            else:
                kept.append(lname)
        if dropped:
            self.log(f"      empty, not sent to the fab: {', '.join(sorted(dropped))}")
        if kept:
            self.log(f"      layers included ({len(kept)}): {', '.join(sorted(kept))}"
                     + (f"  + {', '.join(extras)}" if extras else ""))
        elif gerber_files:
            fatal.append("every gerber layer came back empty")

        # --- drill files into the same folder ----------------------------- #
        dflags = [["--output", gdir + os.sep]] + self._grouped(
            self.cfg.get("drill_extra_args") or [])
        dok, dmsg = self.cli.run(("pcb", "export", "drill"), dflags, self.p.pcb)
        drills = [f for f in (os.listdir(gdir) if os.path.isdir(gdir) else [])
                  if f.lower().endswith((".drl", ".txt", ".nc"))]
        if not (dok and drills):
            fatal.append("drill export failed - the fab cannot build without drill files")
            r.detail = (r.detail + "\n" + dmsg).strip()

        # --- zip the gerber folder ---------------------------------------- #
        gzip_path = os.path.join(mfg, base + "_Gerbers.zip")
        contents = sorted(os.listdir(gdir)) if os.path.isdir(gdir) else []
        if contents:
            with zipfile.ZipFile(gzip_path, "w", zipfile.ZIP_DEFLATED) as z:
                for f in contents:
                    z.write(os.path.join(gdir, f), f)
            produced.append(gzip_path)
            self.log(f"      {len(contents)} files -> {os.path.basename(gzip_path)}")
        else:
            fatal.append("no gerber or drill files to zip")
        shutil.rmtree(gdir, ignore_errors=True)

        # --- BOM ----------------------------------------------------------- #
        bom = os.path.join(mfg, base + "_BOM.csv")
        if self._export_bom(bom):
            produced.append(bom)
        else:
            problems.append("BOM could not be generated")

        # --- positions ------------------------------------------------------ #
        pos = os.path.join(mfg, base + "_Positions.csv")
        if self._export_positions(pos):
            produced.append(pos)
        else:
            problems.append("positions file could not be generated")

        # --- zip the whole manufacturing folder ----------------------------- #
        if self.cfg.get("zip_manufacturing", True) and produced:
            zip_name = base + "_Manufacturing.zip"
            mzip = os.path.join(mfg, zip_name)
            # Build it outside the folder first - it lives in the folder it
            # archives, so zipping in place would try to include itself.
            tmp_zip = mzip + ".building"
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
                for f in sorted(os.listdir(mfg)):
                    full = os.path.join(mfg, f)
                    if not os.path.isfile(full):
                        continue
                    if f == zip_name or f.endswith(".building"):
                        continue          # never nest a previous run's archive
                    z.write(full, f)
            os.replace(tmp_zip, mzip)
            produced.append(mzip)
            self.log(f"      ready to send -> {self.cfg['subfolders']['manufacturing']}"
                     f"{os.sep}{zip_name}")

        if fatal:
            r.ok = False
            r.message = "; ".join(fatal)
        elif problems:
            r.ok = True
            r.message = f"{len(kept)} layers, but: " + "; ".join(problems)
        else:
            r.ok = True
            r.message = f"{len(kept)} layers, {len(drills)} drill file(s), BOM + positions"
        self._record(r, produced)

    @staticmethod
    def _grouped(flat: list[str]) -> list[list[str]]:
        """["--a","1","--b"] -> [["--a","1"],["--b"]] so unknown flags drop cleanly."""
        groups: list[list[str]] = []
        for tok in flat:
            if tok.startswith("-"):
                groups.append([tok])
            elif groups:
                groups[-1].append(tok)
            else:
                groups.append([tok])
        return groups

    def _bom_columns(self) -> tuple:
        """
        Work out the CSV columns, and sanity-check any configured list.

        A configured column that names a field the schematic does not define is
        dropped - kicad-cli would happily emit it as a blank column - and any
        detected part-number field that is missing gets appended. That way a
        config saved by an older version cannot quietly suppress the real part
        numbers.
        """
        detected = self.part_fields()
        known = {f.lower() for f in schematic_field_names(self.p.all_sheets())}
        builtin = {b.lower() for b in BUILTIN_FIELDS}

        configured = (self.cfg.get("bom_fields") or "").strip()
        if not configured:
            if detected:
                self.log(f"      part number fields found: {', '.join(detected)}")
            else:
                self.log("      no part-number fields found in the schematic")
            return (",".join(["Value", "Reference", "Footprint", "${QUANTITY}"] + detected),
                    ",".join(["Comment", "Designator", "Footprint", "Quantity"] + detected))

        f_in = [x.strip() for x in configured.split(",") if x.strip()]
        l_in = [x.strip() for x in (self.cfg.get("bom_labels") or "").split(",")]
        while len(l_in) < len(f_in):
            l_in.append(f_in[len(l_in)])

        keep_f, keep_l, dropped = [], [], []
        for f, l in zip(f_in, l_in):
            if f.startswith("${") or f.lower() in builtin or f.lower() in known or not known:
                keep_f.append(f)
                keep_l.append(l or f)
            else:
                dropped.append(f)
        have = {f.lower() for f in keep_f}
        added = [d for d in detected if d.lower() not in have]
        keep_f += added
        keep_l += added

        if dropped:
            self.log(f"      configured BOM column(s) not in this schematic, "
                     f"dropped: {', '.join(dropped)}")
        if added:
            self.log(f"      part number fields added: {', '.join(added)}")
        if not dropped and not added and detected:
            self.log(f"      BOM columns from config: {', '.join(keep_f)}")
        return ",".join(keep_f), ",".join(keep_l)

    def _export_bom(self, out: str) -> bool:
        """
        Fab-friendly BOM.

        The columns are built from the fields the schematic actually defines,
        rather than a hardcoded guess. kicad-cli happily accepts a field name
        that does not exist and emits a blank column for it, so a wrong guess
        fails silently - which is exactly what used to happen.
        """
        if self.cli and self.cli.has_subcommand(("sch", "export"), "bom") and self.p.sch:
            fields, labels = self._bom_columns()

            group = self.cfg.get("bom_group_by") or "Value,Footprint"
            attempts = [
                [["--output", out], ["--fields", fields], ["--labels", labels],
                 ["--group-by", group],
                 ["--exclude-dnp"] if self.cfg.get("bom_exclude_dnp", True) else []],
                # same columns, no grouping/dnp options in case this build chokes
                [["--output", out], ["--fields", fields], ["--labels", labels]],
                # last resort: whatever kicad-cli does by default
                [["--output", out]],
            ]
            for i, flags in enumerate(attempts):
                ok, msg = self.cli.run(("sch", "export", "bom"),
                                       [f for f in flags if f], self.p.sch,
                                       quiet_drop=(i > 0))
                if ok and os.path.isfile(out) and os.path.getsize(out) > 0:
                    if i:
                        self.log("      (BOM fell back to a simpler field set)")
                    return True
            self.log("      kicad-cli could not export a BOM - deriving one from the board")
        return self._bom_from_board(out)

    def _bom_from_board(self, out: str) -> bool:
        """
        Fallback BOM built from the position export, so KiCad 8 users (no
        `sch export bom`) still get something usable.
        """
        tmp = out + ".pos.tmp"
        if not self._export_positions(tmp, normalise=False):
            return False
        rows: dict[tuple, list] = {}
        try:
            with open(tmp, newline="", encoding="utf-8", errors="ignore") as fh:
                for row in csv.DictReader(fh):
                    key = (row.get("Val") or row.get("Value") or "",
                           row.get("Package") or row.get("Footprint") or "")
                    rows.setdefault(key, []).append(row.get("Ref") or row.get("Designator") or "")
        except Exception:
            return False
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not rows:
            return False
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Comment", "Designator", "Footprint", "Quantity"])
            for (val, pkg), refs in sorted(rows.items()):
                w.writerow([val, ",".join(sorted(refs)), pkg, len(refs)])
        return True

    def _export_positions(self, out: str, normalise: bool = True) -> bool:
        if not (self.cli and self.p.pcb):
            return False
        flags = [["--output", out]] + self._grouped(self.cfg.get("pos_extra_args") or [])
        ok, msg = self.cli.run(("pcb", "export", "pos"), flags, self.p.pcb)
        if not (ok and os.path.isfile(out)):
            return False
        if normalise:
            self._normalise_positions(out)
        return True

    @staticmethod
    def _normalise_positions(path: str) -> None:
        """
        kicad-cli writes Ref,Val,Package,PosX,PosY,Rot,Side.
        Rename to the headers assembly houses expect, and title-case the side.
        """
        rename = {"Ref": "Designator", "Val": "Comment", "Package": "Footprint",
                  "PosX": "Mid X", "PosY": "Mid Y", "Rot": "Rotation", "Side": "Layer"}
        try:
            with open(path, newline="", encoding="utf-8", errors="ignore") as fh:
                rows = list(csv.reader(fh))
        except Exception:
            return
        if not rows:
            return
        rows[0] = [rename.get(c.strip(), c.strip()) for c in rows[0]]
        try:
            side = rows[0].index("Layer")
        except ValueError:
            side = -1
        if side >= 0:
            for row in rows[1:]:
                if len(row) > side:
                    row[side] = row[side].strip().capitalize()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)

    # ------------------------------------------------------------- iBOM ---- #

    def step_assembly_bom(self) -> None:
        r = StepResult("assembly_bom", STEP_LABELS["assembly_bom"])
        if not self.p.pcb:
            r.skipped, r.message = True, "no board file"
            return self._record(r)
        out = os.path.join(self.sub("ibom"), self.base_name() + "_AssemblyBOM.html")
        if self.dry:
            r.ok, r.message = True, f"(dry) {out}"
            return self._record(r)

        mod = load_assembly_module()
        if mod is None:
            r.ok = False
            r.message = "assembly_bom.py is missing from the Board Release folder"
            return self._record(r)
        try:
            self._prepare(out)
            fields = self.part_fields()
            if fields:
                self.log(f"      part number fields: {', '.join(fields)}")
            mod.build(self.p.pcb, out,
                      meta={"project": self.p.stem, "rev": self.rev,
                            "title": self.p.title,
                            "generated": self.started.strftime("%Y-%m-%d %H:%M")},
                      include_dnp=bool(self.cfg.get("assembly_bom_include_dnp")),
                      dark=bool(self.cfg.get("assembly_bom_dark", True)),
                      extra_fields=fields, sch_paths=self.p.all_sheets())
        except Exception as e:
            import traceback
            r.ok, r.message, r.detail = False, f"could not read the board: {e}", traceback.format_exc()
            return self._record(r)

        if os.path.isfile(out) and os.path.getsize(out) > 0:
            board = mod.parse_board(self.p.pcb)
            rows = mod.bom_rows(board, bool(self.cfg.get("assembly_bom_include_dnp")),
                                extra_fields=fields,
                                sch_fields=mod.schematic_fields_by_ref(self.p.all_sheets())
                                if fields else None)
            r.ok = True
            r.message = (f"{len(rows)} line items, {sum(x['qty'] for x in rows)} parts, "
                         f"{len(board.footprints)} footprints drawn")
        else:
            r.ok, r.message = False, "no HTML was written"
        self._record(r, [out])

    def _netlist_xml(self) -> str | None:
        """
        Export a KiCad XML netlist to a temp file.

        iBOM reads extra fields from footprint properties, which only hold the
        schematic's fields if "Update PCB from Schematic" copied them. Handing
        it the netlist as well means the part numbers are there either way.
        """
        if not (self.cli and self.p.sch):
            return None
        if not self.cli.has_subcommand(("sch", "export"), "netlist"):
            return None
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="board_release_")
        self._tmp_dirs = getattr(self, "_tmp_dirs", [])
        self._tmp_dirs.append(tmpdir)
        path = os.path.join(tmpdir, self.base_name() + "_netlist.xml")
        ok, _ = self.cli.run(("sch", "export", "netlist"),
                             [["--output", path], ["--format", "kicadxml"]],
                             self.p.sch, quiet_drop=True)
        return path if (ok and os.path.isfile(path)) else None

    def _ibom_help(self, py: str, script: str) -> str:
        """iBOM's own --help, so we never pass a flag an older build lacks."""
        key = (py, script)
        if not hasattr(self, "_ibom_help_cache"):
            self._ibom_help_cache = {}
        if key not in self._ibom_help_cache:
            try:
                r = subprocess.run([py, script, "--help"], capture_output=True,
                                   text=True, timeout=120, **_no_window())
                self._ibom_help_cache[key] = (r.stdout or "") + (r.stderr or "")
            except Exception:
                self._ibom_help_cache[key] = ""
        return self._ibom_help_cache[key]

    def _ibom_args(self, dest: str, name: str, help_text: str,
                   extra_data: str | None) -> list:
        args = ["--dest-dir", dest, "--name-format", name]
        args += list(self.cfg.get("ibom_extra_args") or [])
        if "--no-browser" not in args:
            args.append("--no-browser")

        given = set(args)
        fields = self.part_fields()
        if fields and "--show-fields" not in given and "--extra-fields" not in given:
            shown = ",".join(["Value", "Footprint"] + fields)
            if "--show-fields" in help_text:
                args += ["--show-fields", shown]
                if "--group-fields" in help_text and "--group-fields" not in given:
                    args += ["--group-fields", shown]
            elif "--extra-fields" in help_text:
                args += ["--extra-fields", ",".join(fields)]
            if "--normalize-field-case" in help_text:
                args.append("--normalize-field-case")
        if extra_data and "--extra-data-file" not in given:
            if "--extra-data-file" in help_text:
                args += ["--extra-data-file", extra_data]
            elif "--netlist-file" in help_text:
                args += ["--netlist-file", extra_data]
        return args + [self.p.pcb]

    def step_ibom(self) -> None:
        """
        Run InteractiveHtmlBom as a SEPARATE PROCESS. Always.

        An earlier version fell back to executing iBOM inside this interpreter
        when the subprocess failed. Inside KiCad that meant running wx and
        pcbnew calls on a worker thread, which corrupts the process and takes
        KiCad down a moment later. There is no in-process path any more, and
        there should never be one again.
        """
        r = StepResult("ibom", STEP_LABELS["ibom"])
        if not self.p.pcb:
            r.skipped, r.message = True, "no board file"
            return self._record(r)

        running = kicad_version_key(self.cli_exe or "") if self.cli_exe else None
        if running == (0, 0):
            running = None
        scripts = find_ibom_candidates(self.cfg.get("ibom_script") or None, running)
        if not scripts:
            r.ok = False
            r.message = "InteractiveHtmlBom not found"
            r.detail = ("Install it from KiCad's Plugin and Content Manager, or set\n"
                        "\"ibom_script\" in the config to generate_interactive_bom.py.\n"
                        "Searched:\n  " + "\n  ".join(kicad_plugin_dirs()))
            return self._record(r)

        dest = self.sub("ibom")
        name = self.base_name() + "_iBOM"
        if self.dry:
            r.ok, r.message = True, f"(dry) {os.path.join(dest, name + '.html')}"
            return self._record(r)

        extra_data = self._netlist_xml()
        if extra_data:
            self.log("      exported a netlist so iBOM can see the schematic fields")

        pys = [p for p in python_candidates(self.cli_exe) if os.path.isfile(p)]
        if self.cfg.get("kicad_python"):
            pys.insert(0, self.cfg["kicad_python"])
        if not pys:
            r.ok = False
            r.message = "no Python interpreter found to run InteractiveHtmlBom"
            return self._record(r)

        if len(scripts) > 1:
            self.log(f"      {len(scripts)} copies of InteractiveHtmlBom found; "
                     f"trying the one matching KiCad {running[0]}.{running[1]}"
                     if running else f"      {len(scripts)} copies found")

        # Pair each iBOM copy with interpreters, matching KiCad versions first.
        pairs = []
        for script in scripts:
            sv = kicad_version_key(script)
            for py in sorted(pys, key=lambda p: kicad_version_key(p) != sv):
                if (py, script) not in pairs:
                    pairs.append((py, script))

        errors: list[str] = []
        for py, script in pairs[:6]:
            self._prepare(os.path.join(dest, name + ".html"))
            help_text = self._ibom_help(py, script) if script.endswith(".py") else ""
            args = self._ibom_args(dest, name, help_text, extra_data)
            argv = [py, script] + args if script.endswith(".py") else [script] + args
            ok, msg = self._run_tool(argv, cwd=self.p.dir, timeout=900)
            hits = sorted(glob.glob(os.path.join(dest, name + "*.html")))
            if hits:
                r.ok, r.message = True, os.path.basename(hits[0])
                return self._record(r, hits)
            why = (msg or "").strip().splitlines()
            tail = why[-1] if why else f"exit code {0 if ok else 1}"
            self.log(f"      no output from that pair - {tail[:160]}")
            errors.append(f"python: {py}\nscript: {script}\n{(msg or '')[-500:]}")

        r.ok = False
        r.message = "InteractiveHtmlBom did not produce a file"
        r.detail = ("Tried these combinations:\n\n" + "\n\n".join(errors)
                    + "\n\nMost often this means the iBOM copy was installed under a "
                      "different KiCad version than the one you are running. Install it "
                      "from the Plugin and Content Manager inside the KiCad you use.")
        self._record(r)

    def _run_tool(self, argv: list[str], cwd: str | None = None,
                  timeout: int = 1200) -> tuple[bool, str]:
        self.log("      $ " + " ".join(_q(a) for a in argv))
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, env=env, **_no_window())
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s"
        except FileNotFoundError as e:
            return False, str(e)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()

    # ------------------------------------------------------------ reports -- #

    def step_erc(self) -> None:
        r = StepResult("erc", STEP_LABELS["erc"])
        if not self._need_cli(r):
            return self._record(r)
        if not self.p.sch:
            r.skipped, r.message = True, "no schematic file"
            return self._record(r)
        out = os.path.join(self.sub("reports"), self.base_name() + "_ERC.rpt")
        if self.dry:
            r.ok, r.message = True, f"(dry) {out}"
            return self._record(r)
        ok, msg = self.cli.run(("sch", "erc"),
                               [["--output", out], ["--severity-error"],
                                ["--severity-warning"], ["--format", "report"]],
                               self.p.sch)
        r.ok = os.path.isfile(out)
        r.message = "violations present - read the report" if (not ok and r.ok) else ""
        r.detail = "" if r.ok else msg
        self._record(r, [out])

    def step_drc(self) -> None:
        r = StepResult("drc", STEP_LABELS["drc"])
        if not self._need_cli(r):
            return self._record(r)
        if not self.p.pcb:
            r.skipped, r.message = True, "no board file"
            return self._record(r)
        out = os.path.join(self.sub("reports"), self.base_name() + "_DRC.rpt")
        if self.dry:
            r.ok, r.message = True, f"(dry) {out}"
            return self._record(r)
        ok, msg = self.cli.run(("pcb", "drc"),
                               [["--output", out], ["--schematic-parity"],
                                ["--severity-error"], ["--severity-warning"],
                                ["--format", "report"]],
                               self.p.pcb)
        r.ok = os.path.isfile(out)
        r.message = "violations present - read the report" if (not ok and r.ok) else ""
        r.detail = "" if r.ok else msg
        self._record(r, [out])

    def step_step(self) -> None:
        r = StepResult("step", STEP_LABELS["step"])
        if not self._need_cli(r):
            return self._record(r)
        if not self.p.pcb:
            r.skipped, r.message = True, "no board file"
            return self._record(r)
        out = os.path.join(self.sub("models"), self.base_name() + ".step")
        if self.dry:
            r.ok, r.message = True, f"(dry) {out}"
            return self._record(r)
        since = self._prepare(out)
        flags = [["--output", out], ["--force"]] + self._grouped(
            self.cfg.get("step_extra_args") or [])
        ok, msg = self.cli.run(("pcb", "export", "step"), flags, self.p.pcb, timeout=2400)
        self._judge(r, self._collect(out, since, "*.step"), ok, msg, "STEP model")
        self._record(r)

    # ------------------------------------------------------------- source -- #

    SOURCE_GLOBS = ("*.kicad_pro", "*.kicad_sch", "*.kicad_pcb", "*.kicad_dru",
                    "*.kicad_prl", "*.kicad_sym", "*.kicad_wks", "*.pretty",
                    "fp-lib-table", "sym-lib-table", "*.step", "*.stp", "*.wrl",
                    "*.net", "*.csv", "*.md", "*.txt")
    SOURCE_EXCLUDE_DIRS = ("-backups", "backups", "fp-info-cache", "__pycache__",
                           ".git", "production", "gerbers", "bom", "Archive", "archive")

    def step_source_zip(self) -> None:
        r = StepResult("source_zip", STEP_LABELS["source_zip"])
        out = os.path.join(self.sub("source"), self.base_name() + "_source.zip")
        if self.dry:
            r.ok, r.message = True, f"(dry) {out}"
            return self._record(r)
        archive_abs = os.path.abspath(self.out_root)
        count = 0
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for dirpath, dirnames, filenames in os.walk(self.p.dir):
                    if os.path.abspath(dirpath).startswith(archive_abs):
                        dirnames[:] = []
                        continue
                    dirnames[:] = [d for d in dirnames
                                   if d not in self.SOURCE_EXCLUDE_DIRS and not d.startswith(".")]
                    for fn in filenames:
                        if any(fnmatch.fnmatch(fn, p) for p in self.SOURCE_GLOBS):
                            full = os.path.join(dirpath, fn)
                            z.write(full, os.path.relpath(full, self.p.dir))
                            count += 1
        except Exception as e:
            r.ok, r.message = False, str(e)
            return self._record(r)
        r.ok = count > 0
        r.message = f"{count} file(s)"
        self._record(r, [out])

    # ----------------------------------------------------------- manifest -- #

    def step_manifest(self) -> None:
        r = StepResult("manifest", STEP_LABELS["manifest"])
        if self.dry:
            r.ok, r.message = True, "(dry)"
            return self._record(r)

        files: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(self.out_root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in sorted(filenames):
                if fn.startswith("release_manifest") or fn == "release_log.txt":
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    files.append({"path": os.path.relpath(full, self.out_root).replace("\\", "/"),
                                  "bytes": os.path.getsize(full),
                                  "sha256": _sha256(full)})
                except Exception:
                    pass

        data = {
            "tool": f"{APP_NAME} {__version__}",
            "generated": self.started.isoformat(timespec="seconds"),
            "host": platform.node(),
            "os": f"{platform.system()} {platform.release()}",
            "kicad_cli": self.cli_exe or "",
            "kicad_version": self.cli.version if self.cli else "",
            "project": {"name": self.p.stem, "title": self.p.title,
                        "revision": self.rev, "revision_source": self.rev_source,
                        "company": self.p.company, "title_block_date": self.p.date,
                        "project_file": self.p.pro, "schematic": self.p.sch or "",
                        "board": self.p.pcb or ""},
            "git": _git_info(self.p.dir),
            "steps": [{"key": s.key, "label": s.label,
                       "status": "skipped" if s.skipped else ("ok" if s.ok else "failed"),
                       "message": s.message, "detail": s.detail,
                       "files": [os.path.relpath(f, self.out_root).replace("\\", "/")
                                 for f in s.files]} for s in self.results],
            "files": files,
        }
        jpath = os.path.join(self.out_root, "release_manifest.json")
        tpath = os.path.join(self.out_root, "release_manifest.txt")
        with open(jpath, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

        lines = [f"{APP_NAME} {__version__}", RULE,
                 f"Project    : {self.p.title}  ({self.p.stem})",
                 f"Revision   : {self.rev}"
                 + (f"   ({self.rev_source})" if self.rev_source else ""),
                 f"Company    : {self.p.company or '-'}",
                 f"Generated  : {self.started.strftime('%Y-%m-%d %H:%M:%S')} on {platform.node()}",
                 f"KiCad      : {self.cli.version if self.cli else 'n/a'}"]
        g = data["git"]
        if g.get("commit"):
            lines.append(f"Git        : {g.get('branch', '?')} @ {g['commit'][:10]}"
                         + ("  (uncommitted changes present)" if g.get("dirty") else ""))
        lines += ["", "Steps", RULE]
        for s in self.results:
            mark = "skip" if s.skipped else ("ok  " if s.ok else "FAIL")
            lines.append(f"  [{mark}] {s.label}" + (f"   - {s.message}" if s.message else ""))
        lines += ["", f"Files ({len(files)})", RULE]
        for f in files:
            lines.append(f"  {f['path']}")
            lines.append(f"      {f['bytes']:>12,} bytes   sha256 {f['sha256'][:16]}...")
        with open(tpath, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        r.ok, r.message = True, f"{len(files)} file(s) catalogued"
        self._record(r, [jpath, tpath])

    # -------------------------------------------------------------- driver #

    DISPATCH = {
        "sch_pdf": step_sch_pdf, "pcb_pdf": step_pcb_pdf,
        "manufacturing": step_manufacturing, "ibom": step_ibom,
        "assembly_bom": step_assembly_bom,
        "erc": step_erc, "drc": step_drc, "step": step_step,
        "source_zip": step_source_zip, "manifest": step_manifest,
    }

    def run(self, selected: list[str]) -> bool:
        self.log("")
        self.log(RULE)
        self.log(f" {APP_NAME} {__version__}")
        self.log(RULE)
        self.log(f" Project    : {self.p.title}  [{self.p.stem}]")
        self.log(f" Revision   : {self.rev or '(none)'}"
                 + (f"   ({self.rev_source})" if self.rev_source else ""))
        self.log(f" KiCad      : {self.cli.version if self.cli else 'kicad-cli NOT FOUND'}"
                 + (f"   ({self.cli_exe})" if self.cli_exe else ""))
        self.log(f" Destination: {self.out_root}")
        for warn in version_mismatch_warnings(self.cli):
            self.log(f" {CROSS} {warn}")
        if self.dry:
            self.log(" Mode       : DRY RUN - nothing will be written")
        self.log(RULE)

        if not self.dry:
            os.makedirs(self.out_root, exist_ok=True)

        ordered = [k for k, _ in STEP_ORDER if k in selected and k != "manifest"]
        if "manifest" in selected:
            ordered.append("manifest")

        for key in ordered:
            self.log("")
            self.log(f" > {STEP_LABELS.get(key, key)}")
            try:
                self.DISPATCH[key](self)
            except Exception as e:
                import traceback
                res = StepResult(key, STEP_LABELS.get(key, key))
                res.ok, res.message, res.detail = False, f"unexpected error: {e}", traceback.format_exc()
                self._record(res)
            if self.stop_on_error and self.results and self.results[-1].ok is False:
                self.log("\n Stopping - --stop-on-error was set.")
                break

        ok = sum(1 for r in self.results if r.ok)
        bad = [r for r in self.results if r.ok is False]
        skipped = sum(1 for r in self.results if r.skipped)
        elapsed = (_dt.datetime.now() - self.started).total_seconds()

        self.log("")
        self.log(RULE)
        self.log(f" Done in {elapsed:.1f}s   -   {ok} ok, {len(bad)} failed, {skipped} skipped")
        for r in bad:
            self.log(f"   {CROSS} {r.label}: {r.message}")
        if bad:
            self.log("   Run  python kicad_release.py --doctor  to test the external tools.")
        self.log(f" {self.out_root}")
        self.log(RULE)

        for d in getattr(self, "_tmp_dirs", []):
            shutil.rmtree(d, ignore_errors=True)

        if not self.dry:
            try:
                with open(os.path.join(self.out_root, "release_log.txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write(self.log.text())
            except Exception:
                pass
        return not bad


def version_mismatch_warnings(cli) -> list[str]:
    """
    Catch the nastiest multi-install failure: an older kicad-cli trying to read
    a project the newer KiCad just saved. It reports "Failed to load schematic"
    and nothing else, which is a horrible thing to debug.
    """
    out: list[str] = []
    if cli is None:
        return out
    try:
        import pcbnew
        running = str(pcbnew.GetBuildVersion())
    except Exception:
        return out
    rm = re.match(r"(\d+)\.(\d+)", running.lstrip("v"))
    cm = re.match(r"(\d+)\.(\d+)", (cli.version or "").lstrip("v"))
    if not (rm and cm):
        return out
    if (int(rm.group(1)), int(rm.group(2))) != (int(cm.group(1)), int(cm.group(2))):
        out.append(f"WARNING: you are running KiCad {running.strip()} but kicad-cli "
                   f"is {cli.version}.")
        out.append(f"         {cli.exe}")
        out.append("         An older kicad-cli cannot read a newer project and will")
        out.append("         say only 'Failed to load schematic'. Set \"kicad_cli\" in")
        out.append("         the config to the matching version's kicad-cli.")
    return out


def _git_info(path: str) -> dict:
    if not shutil.which("git"):
        return {}

    def g(*a):
        try:
            r = subprocess.run(["git", "-C", path] + list(a), capture_output=True,
                               text=True, timeout=20, **_no_window())
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    commit = g("rev-parse", "HEAD")
    if not commit:
        return {}
    return {"commit": commit, "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(g("status", "--porcelain"))}


def open_in_file_manager(path: str) -> None:
    try:
        if IS_WIN:
            os.startfile(path)            # noqa: S606
        elif IS_MAC:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  doctor - find out exactly why a tool is failing
# --------------------------------------------------------------------------- #

def doctor(cfg: dict, project: Project | None = None) -> int:
    print("\n" + RULE)
    print(f"  {APP_NAME} {__version__} - tool check")
    print(RULE)
    print(f"  Python running this script : {sys.executable}")
    print(f"  Platform                   : {platform.system()} {platform.release()}")

    problems = 0

    # --- kicad-cli ------------------------------------------------------- #
    cli_path = cfg.get("kicad_cli") or find_kicad_cli()

    installs = all_kicad_clis()
    if len(installs) > 1:
        print("\n  More than one KiCad is installed:")
        for i in installs:
            print(f"    {'-> ' if i == cli_path else '   '}{i}")
        print("    The one marked -> is what Board Release will use. If your project")
        print("    was saved by a newer KiCad than that, set \"kicad_cli\" in the config.")

    print("\n  kicad-cli")
    if not cli_path:
        problems += 1
        print(f"    {CROSS} not found. Searched PATH and the standard install folders.")
        print("        Set \"kicad_cli\" in the config to the full path.")
        cli = None
    else:
        cli = KiCadCli(cli_path, Log(quiet=True))
        good = not cli.version.startswith("unavailable")
        print(f"    {TICK if good else CROSS} {cli_path}")
        print(f"        version {cli.version}")
        for warn in version_mismatch_warnings(cli):
            problems += 1
            print("    " + warn)
        if not good:
            problems += 1
        else:
            for sub, name in ((("sch", "export"), "bom"),):
                have = cli.has_subcommand(sub, name)
                print(f"        {'has' if have else 'NO'} `{' '.join(sub)} {name}`"
                      + ("" if have else "  (KiCad 9+ only - BOM falls back to the board)"))

    # --- python with pcbnew ------------------------------------------------ #
    print("\n  Python that can import pcbnew   (needed by InteractiveHtmlBom)")
    chosen = None
    tried = 0
    for cand in python_candidates(cli_path):
        if chosen is not None:
            continue
        if not os.path.isfile(cand):
            continue                       # don't list interpreters that aren't there
        tried += 1
        ok, detail = probe_python(cand)
        if ok:
            chosen = cand
            print(f"    {TICK} {cand}")
            print(f"        reports pcbnew {detail}")
        else:
            print(f"    {CROSS} {cand}")
            print(f"        {detail}")
        if tried >= 8:
            break
    if chosen is None:
        problems += 1
        print(f"    {CROSS} none of the above could import pcbnew.")
        print("        Set \"kicad_python\" in the config, e.g.")
        print(r"        C:\Program Files\KiCad\9.0\bin\python.exe")

    # --- interactive bom --------------------------------------------------- #
    print("\n  InteractiveHtmlBom")
    running = kicad_version_key(cli_path or "") if cli_path else None
    if running == (0, 0):
        running = None
    all_ibom = find_ibom_candidates(cfg.get("ibom_script") or None, running)
    ibom = all_ibom[0] if all_ibom else None
    if len(all_ibom) > 1:
        print(f"    {len(all_ibom)} copies installed - the first is the one used:")
        for i in all_ibom:
            v = kicad_version_key(i)
            print(f"      {'-> ' if i == ibom else '   '}{i}   (KiCad {v[0]}.{v[1]})")
    if not ibom:
        problems += 1
        print(f"    {CROSS} generate_interactive_bom.py not found. Searched:")
        for d in kicad_plugin_dirs():
            print(f"        {d}")
        print("        Install it from KiCad's Plugin and Content Manager.")
    else:
        print(f"    {TICK} {ibom}")
        if chosen:
            try:
                r = subprocess.run([chosen, ibom, "--help"], capture_output=True,
                                   text=True, timeout=120, **_no_window())
                if r.returncode == 0:
                    print(f"    {TICK} runs correctly under {os.path.basename(chosen)}")
                else:
                    problems += 1
                    tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
                    print(f"    {CROSS} it errors when run:")
                    for line in tail[-6:]:
                        print(f"        {line}")
                    if any("wx" in l for l in tail):
                        print("        -> missing wxPython in that interpreter")
                    if any("jsonschema" in l for l in tail):
                        print("        -> missing jsonschema; install it into KiCad's Python:")
                        print(f"           \"{chosen}\" -m pip install jsonschema")
            except Exception as e:
                problems += 1
                print(f"    {CROSS} could not run it: {e}")

    # --- project ------------------------------------------------------------ #
    if project:
        print("\n  Project")
        print(f"    file      : {project.pro}")
        print(f"    schematic : {project.sch or '(none)'}")
        print(f"    board     : {project.pcb or '(none)'}")
        print(f"    title     : {project.title}")
        print(f"    revision  : {project.rev or '(EMPTY)'}"
              + (f"   from {project.rev_source}" if project.rev_source else ""))
        if not project.rev:
            print("        No revision found. Set it in Schematic Editor >")
            print("        File > Page Settings > Revision, or add a REVISION")
            print("        text variable in Project Settings, or pass --rev.")
        fields = schematic_field_names(project.all_sheets())
        if fields:
            print(f"    fields    : {', '.join(fields)}")
            pn = [f for f in PART_NUMBER_FIELDS
                  if f.lower() in {x.lower() for x in fields}]
            print(f"    part nos  : {', '.join(pn) if pn else '(none recognised)'}")
        else:
            print("    fields    : (no user fields found in the schematic)")
        for key in ("bom_fields", "bom_labels"):
            if (cfg.get(key) or "").strip():
                print(f"    {key}: {cfg[key]}   (from your config, overrides detection)")
        if project.vars:
            print(f"    text vars : {', '.join(sorted(project.vars))}")
        if project.pcb:
            present = board_layers(project.pcb)
            print(f"    copper    : {', '.join(copper_layers(present))}")
            print(f"    PDF pages : {', '.join(copper_layers(present) + silkscreen_layers(present))}")

    print("\n" + RULE)
    print(f"  {'No problems found.' if not problems else str(problems) + ' problem(s) above.'}")
    print(RULE)
    return 0 if not problems else 1


# --------------------------------------------------------------------------- #
#  dev workflow
# --------------------------------------------------------------------------- #

def find_installed_plugin_dirs() -> list[str]:
    out: list[str] = []
    for base in kicad_plugin_dirs():
        for entry in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            init = os.path.join(base, entry, "__init__.py")
            if not os.path.isfile(init):
                continue
            try:
                if "Board Release" in open(init, encoding="utf-8", errors="ignore").read(4000):
                    out.append(os.path.join(base, entry))
            except Exception:
                pass
    return out


def dev_link(target: str | None, unlink: bool = False) -> int:
    src = os.path.dirname(os.path.abspath(__file__))
    targets = [os.path.abspath(os.path.expanduser(target))] if target \
        else find_installed_plugin_dirs()
    if not targets:
        print(f"{CROSS} No installed Board Release plugin folder found. Searched:")
        for d in kicad_plugin_dirs():
            print(f"      {d}")
        print("   Pass the folder explicitly:  --dev-link <path>")
        return 1
    for t in targets:
        link = os.path.join(t, "dev_source.txt")
        if unlink:
            if os.path.isfile(link):
                os.remove(link)
                print(f"{TICK} Removed {link}")
            else:
                print(f"    nothing to remove in {t}")
            continue
        if not os.path.isdir(t):
            print(f"{CROSS} Not a folder: {t}")
            return 1
        try:
            with open(link, "w", encoding="utf-8") as fh:
                fh.write("# Board Release: the toolbar button loads its code from here.\n")
                fh.write("# Delete this file (or run --dev-unlink) to go back.\n")
                fh.write(src + "\n")
        except Exception as e:
            print(f"{CROSS} Could not write {link}: {e}")
            return 1
        print(f"{TICK} {t}\n      now loads its code from {src}")
    if not unlink:
        print("\n  Restart KiCad once. After that, edits are picked up on every click.")
    return 0


# --------------------------------------------------------------------------- #
#  interactive menu
# --------------------------------------------------------------------------- #

def _ask(prompt: str, default: str = "") -> str:
    try:
        return input(prompt).strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def interactive(project: Project, cfg: dict, rev: str):
    steps = dict(cfg["steps"])
    archive_root = resolve_archive_root(cfg.get("archive_root", ""), project.dir, project.stem)
    prefix = cfg.get("rev_prefix", "")

    while True:
        cfg["archive_root"] = archive_root
        cfg["rev_prefix"] = prefix
        preview = ReleaseRunner(project, cfg, rev, Log(quiet=True), dry_run=True)

        print("\n" + RULE)
        print(f" {APP_NAME} {__version__}")
        print(RULE)
        print(f" Project : {project.title}   [{project.stem}]")
        print(f" Revision: {rev or '(NOT SET)'}"
              + (f"   from {project.rev_source}" if project.rev_source and rev == project.rev else "")
              + (f"      prefix: '{prefix}'" if True else ""))
        print(f" Output  : {preview.out_root}")
        print(f" Files   : {preview.base_name()}_*")
        print(RULE)
        for i, (key, label) in enumerate(STEP_ORDER, 1):
            print(f"  {i:>2}  {TICK if steps.get(key) else BLANK}  {label}")
        print(RULE)
        print("  numbers  toggle (e.g. 1 3 or 2-4)       a  all         n  none")
        print("  r  revision      x  revision prefix     d  archive folder")
        print("  p  project       c  check tools         v  dry run     s  save defaults")
        print("  ENTER  run          q  quit")
        choice = _ask("\n  > ").lower()

        if choice in ("q", "quit", "exit"):
            return None
        if choice == "":
            selected = [k for k, _ in STEP_ORDER if steps.get(k)]
            if not selected:
                print("  Nothing selected.")
                continue
            if not rev:
                rev = _ask("  Revision is empty. Enter one (e.g. C, 1.2, A1): ")
                if not rev:
                    print("  A revision is required - it is the folder name.")
                    continue
            cfg["steps"] = steps
            return selected, rev, cfg
        if choice == "v":
            cfg["steps"] = steps
            ReleaseRunner(project, cfg, rev or "X", Log(), dry_run=True).run(
                [k for k, _ in STEP_ORDER if steps.get(k)])
            _ask("\n  Press Enter to go back...")
            continue
        if choice == "a":
            steps = {k: True for k, _ in STEP_ORDER}
            continue
        if choice == "n":
            steps = {k: False for k, _ in STEP_ORDER}
            continue
        if choice == "r":
            rev = _ask(f"  Revision [{rev}]: ", rev)
            continue
        if choice == "x":
            prefix = _ask(f"  Revision prefix (blank for none) [{prefix}]: ", "")
            continue
        if choice == "d":
            typed = _ask(f"  Archive root (blank = <project>/{ARCHIVE_DIRNAME}) "
                         f"[{archive_root}]: ", archive_root)
            archive_root = resolve_archive_root(typed, project.dir, project.stem)
            continue
        if choice == "p":
            newp = _ask("  Path to .kicad_pro (or its folder): ").strip('"')
            found = find_project(newp) if newp else None
            if found:
                # follow the new project's own archive folder, not the old one's
                if portable_archive_root(archive_root, project.dir) == "":
                    archive_root = default_archive_root(found.dir)
                project, rev = found, (found.rev or rev)
            else:
                print("  No .kicad_pro found there.")
            continue
        if choice == "c":
            doctor(cfg, project)
            _ask("\n  Press Enter to go back...")
            continue
        if choice == "s":
            cfg["steps"], cfg["rev_prefix"] = steps, prefix
            cfg["archive_root"] = portable_archive_root(archive_root, project.dir)
            where = _ask("  Save to (1) this project  (2) all projects [1]: ", "1")
            path = (os.path.join(project.dir, CONFIG_BASENAME) if where == "1"
                    else user_config_path())
            try:
                save_config(cfg, path)
                print(f"  Saved -> {path}")
            except Exception as e:
                print(f"  Could not save: {e}")
            _ask("  Press Enter to go back...")
            continue

        touched = False
        for tok in re.split(r"[,\s]+", choice):
            if not tok:
                continue
            m = re.fullmatch(r"(\d+)-(\d+)", tok)
            rng = range(int(m.group(1)), int(m.group(2)) + 1) if m else (
                [int(tok)] if tok.isdigit() else [])
            for n in rng:
                if 1 <= n <= len(STEP_ORDER):
                    k = STEP_ORDER[n - 1][0]
                    steps[k] = not steps.get(k)
                    touched = True
        if not touched:
            print("  Not sure what that means.")


# --------------------------------------------------------------------------- #
#  entry point
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="board_release",
        description=f"{APP_NAME} {__version__} - every KiCad board-release output in "
                    "one shot, filed into a versioned archive folder.")
    p.add_argument("project", nargs="?",
                   help="path to a .kicad_pro (or a folder containing one). "
                        "Default: search upwards from the current folder.")
    p.add_argument("-r", "--rev", help="revision label. Default: the title block.")
    p.add_argument("--rev-prefix", help="text before the revision in folder and file "
                                        "names. Default: none.")
    p.add_argument("-a", "--archive", help="archive root folder (overrides config).")
    p.add_argument("-s", "--steps", help="comma-separated step keys, or 'all'. Keys: "
                                         + ", ".join(k for k, _ in STEP_ORDER))
    p.add_argument("-y", "--yes", action="store_true",
                   help="run immediately using saved defaults, no menu, no prompts.")
    p.add_argument("--dry-run", action="store_true", help="show what would happen.")
    p.add_argument("--force", action="store_true", help="overwrite an existing folder.")
    p.add_argument("--stop-on-error", action="store_true")
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--doctor", action="store_true",
                   help="test kicad-cli, KiCad's Python and InteractiveHtmlBom, and "
                        "say exactly what is wrong.")
    p.add_argument("--paths", action="store_true", help="alias for --doctor.")
    p.add_argument("--write-config", action="store_true",
                   help="write a starter config file next to the project and exit.")
    p.add_argument("--dev-link", nargs="?", const="", metavar="PLUGIN_DIR",
                   help="point the installed toolbar plugin at THIS copy of the code.")
    p.add_argument("--dev-unlink", nargs="?", const="", metavar="PLUGIN_DIR",
                   help="undo --dev-link.")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.dev_unlink is not None:
        return dev_link(args.dev_unlink.strip() or None, unlink=True)
    if args.dev_link is not None:
        val = args.dev_link.strip()
        if val.lower() in ("off", "none", "remove", "unlink"):
            return dev_link(None, unlink=True)
        return dev_link(val or None)

    project = find_project(args.project)
    if project is None and not (args.doctor or args.paths):
        print(f"{CROSS} No .kicad_pro found"
              + (f" at {args.project}" if args.project else " here or in any parent folder."))
        print("   Pass one explicitly:  python kicad_release.py path/to/board.kicad_pro")
        return 2

    cfg = load_config(project.dir if project else None)
    if project and cfg.get("revision_from"):
        project.resolve_revision(cfg["revision_from"])
    if args.archive:
        # on the command line a relative path means relative to where you are
        cfg["archive_root"] = os.path.abspath(
            os.path.expanduser(os.path.expandvars(args.archive)))
    if args.rev_prefix is not None:
        cfg["rev_prefix"] = args.rev_prefix

    if args.doctor or args.paths:
        return doctor(cfg, project)

    if args.write_config:
        path = os.path.join(project.dir, CONFIG_BASENAME)
        if os.path.exists(path) and not args.force:
            print(f"{CROSS} {path} already exists. Use --force to overwrite.")
            return 1
        cfg["archive_root"] = portable_archive_root(cfg.get("archive_root", ""), project.dir)
        save_config(cfg, path)
        print(f"{TICK} Wrote {path}")
        return 0

    rev = args.rev or project.rev or ""

    if args.steps:
        if args.steps.strip().lower() == "all":
            selected = [k for k, _ in STEP_ORDER]
        else:
            keys = {k for k, _ in STEP_ORDER}
            selected = []
            for tok in re.split(r"[,\s]+", args.steps):
                if not tok.strip():
                    continue
                if tok not in keys:
                    print(f"{CROSS} Unknown step '{tok}'. Valid: {', '.join(sorted(keys))}")
                    return 2
                selected.append(tok)
    elif args.yes:
        selected = [k for k, _ in STEP_ORDER if cfg["steps"].get(k)]
    else:
        picked = interactive(project, cfg, rev)
        if picked is None:
            print("  Cancelled.")
            return 130
        selected, rev, cfg = picked

    if not rev:
        if args.yes:
            rev = _dt.datetime.now().strftime("%Y%m%d")
            print(f"  No revision in the title block - using {rev}."
                  f"  (Run --doctor to see where it looked.)")
        else:
            rev = _ask("  Revision (e.g. C, 1.2, A1): ") or "X"

    log = Log()
    runner = ReleaseRunner(project, cfg, rev, log, dry_run=args.dry_run,
                           stop_on_error=args.stop_on_error)

    if os.path.isdir(runner.out_root) and os.listdir(runner.out_root) \
            and not args.force and not args.dry_run:
        print(f"\n  {CROSS} {runner.out_root}\n      already exists and is not empty.")
        if args.yes:
            print("      Refusing to overwrite. Re-run with --force, or bump the revision.")
            return 1
        if _ask("      Overwrite its contents? [y/N]: ").lower() not in ("y", "yes"):
            print("  Cancelled.")
            return 1

    ok = runner.run(selected)
    if not args.dry_run and not args.no_open and cfg.get("open_folder_when_done", True):
        open_in_file_manager(runner.out_root)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(130)
