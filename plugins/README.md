# Board Release

One button (or one command) that produces every output you need when you cut a
board revision, and files it in `<project>/archive/<revision>/`.

```
<project>/archive/
└── C/
    ├── 01_Documentation/     WidgetBoard_C_Schematic.pdf   (all pages)
    │                         WidgetBoard_C_PCB.pdf         (copper + silkscreen)
    ├── 02_Manufacturing/     WidgetBoard_C_Gerbers.zip     (gerbers + drills)
    │                         WidgetBoard_C_BOM.csv
    │                         WidgetBoard_C_Positions.csv
    │                         WidgetBoard_C_Manufacturing.zip  <- send this to the fab
    ├── 03_Assembly_BOM/      WidgetBoard_C_AssemblyBOM.html
    ├── 04_Reports/           ERC.rpt, DRC.rpt              (optional)
    ├── 05_Source/            WidgetBoard_C_source.zip
    ├── 06_3D/                WidgetBoard_C.step            (optional)
    ├── release_manifest.txt  every file, its size and SHA-256
    ├── release_manifest.json same thing, machine readable
    └── release_log.txt       the full console output of the run
```

Two front ends, one engine:

* a **toolbar button** in the KiCad PCB editor that opens a checklist dialog
* a **standalone script** with the same checklist in the terminal, plus flags for
  scripting and CI

---

## What it runs

| Step | How | Default |
|---|---|---|
| Schematic PDF, all pages | `kicad-cli sch export pdf` | **on** |
| PCB PDF: copper, silkscreen + fab layers | `kicad-cli pcb export pdf --mode-multipage` | **on** |
| Manufacturing package | `kicad-cli` gerbers + drill + bom + pos | **on** |
| Interactive assembly BOM | built in — reads the board file directly | **on** |
| Interactive HTML BOM | InteractiveHtmlBom plugin | off |
| ERC report | `kicad-cli sch erc` | off |
| DRC report | `kicad-cli pcb drc` | off |
| STEP 3D model | `kicad-cli pcb export step` | off |
| Source snapshot zip | built in | **on** |
| Manifest + log | built in | **on** |

Every step is independent. If one fails the rest still run, and the failure is
recorded in the manifest with the underlying error.

**Steps are judged on the file, not the exit code.** `kicad-cli` returns non-zero
for plenty of things that are not failures — a missing symbol library, a
substituted font, ERC violations — while still writing a perfectly good PDF. If
the file is there and non-empty, the step passed; the warning is printed in the
log and noted in the manifest:

```
note: kicad-cli exited non-zero but the PDF was written. It said:
  Warning: symbol library 'CompanyParts' not found.
[OK] Schematic PDF - all pages   written, but kicad-cli reported a warning
```

The target file is deleted before each step runs, so a leftover from a previous
run can never make a failed step look successful.

If `kicad-cli` writes a file somewhere other than where it was asked, it is moved
into the right folder and the move is logged, so the schematic and board PDFs
always end up side by side in `01_Documentation`. That includes the case where
`--mode-multipage` treats `--output` as a *folder* to write into (KiCad 9.0.8
does this) and leaves you with `Name_PCB.pdf/Board.pdf` — the folder is unwrapped
back into a single file.

### The interactive assembly BOM

This is the built-in replacement for InteractiveHtmlBom. It reads the
`.kicad_pcb` file directly — no `pcbnew`, no plugin — and writes one standalone
HTML file: the board drawn as SVG next to a clickable BOM.

* Board outline, every pad, and the silkscreen, top and bottom (bottom mirrored,
  parts on the far side faded).
* Click a BOM line to light up its parts; click a part to jump to its line. The
  whole footprint body is clickable, not just the pads.
* Search by value, footprint or designator.
* Tick parts off as you place them, with a progress bar. <kbd>n</kbd>/<kbd>p</kbd>
  step, <kbd>space</kbd> ticks, <kbd>Esc</kbd> clears. Ticks are remembered per
  board and revision in that browser.
* Light and dark, and it works offline from a file:// URL.
* Honours `exclude_from_bom` and DNP (DNP is hidden by default —
  `"assembly_bom_include_dnp": true` shows it, marked).

**What it is not:** a pixel-for-pixel InteractiveHtmlBom clone. It draws pads and
silkscreen, not copper pours, tracks or 3D. For finding R47 at a bench that is
what matters, but if you rely on seeing the copper, keep using iBOM — set
`"ibom": true` and both will be produced side by side so you can compare.

### Part numbers

Board Release does not guess what you call your part-number field. It reads the
field names your schematic actually defines and carries whichever ones look like
part numbers into **all three** BOM outputs - the CSV, the interactive assembly
BOM, and InteractiveHtmlBom:

```
part number fields found: MPN, MfrPN
```

Recognised names are in `part_number_fields` (MPN, MfrPN, Manufacturer, LCSC,
Digikey, Mouser, Part Number and similar). Only the ones your schematic uses are
included. To take full control:

```jsonc
"bom_extra_fields": ["MPN", "MfrPN"],   // exactly these, skip detection
"bom_fields": "",                       // or override the CSV columns outright
"bom_labels": ""
```

Two details worth knowing. `kicad-cli` accepts a field name that does not exist
and emits a blank column for it, so a wrong guess fails silently - which is why
detection matters. And iBOM reads fields from footprint properties, which only
hold your schematic's fields if *Update PCB from Schematic* copied them across;
Board Release also exports an XML netlist and hands it over, so the part numbers
are there either way. Flags are checked against your installed iBOM's `--help`
first, so an older copy never gets an argument it does not understand.


**If you ever pressed "Save as defaults" on an older version**, your config may
still contain that version's hardcoded column list, which would override the
detection above. Board Release now checks a configured column list against the
schematic: a column naming a field the schematic does not define is dropped, and
any detected part-number field that is missing is appended. The log says what it
did:

```
configured BOM column(s) not in this schematic, dropped: LCSC
part number fields added: MPN, MfrPN
```

"Save as defaults" also now writes only the values that differ from the
built-in defaults, so a config saved today cannot freeze today's defaults and
block a later improvement.


### The manufacturing package

This replaces the JLCPCB Fabrication Toolkit with plain `kicad-cli`, so there is
no third-party plugin in the path:

* **Gerbers** for every copper layer in the stackup, both silkscreens, both
  masks, both pastes, `Edge.Cuts`, the fab layers and `User.1`–`User.4`.
* **Empty layers are dropped.** Each exported gerber is checked for actual draw
  or flash commands (`D01`/`D03`) or a region fill (`G36`); anything with nothing
  on it never reaches the fab. `Edge.Cuts` is always kept. The log names both
  lists, so you can see exactly what was sent and what was skipped:
  ```
  empty, not sent to the fab: B.Fab, B.Paste, F.Paste, In2.Cu
  layers included (9): B.Cu, B.Mask, B.Silkscreen, Edge.Cuts, F.Cu, ...
  ```
* **Drill files** in Excellon, plated and non-plated separated, with a drill map.
* **BOM.csv** grouped by value and footprint, with an `LCSC Part #` column when
  your symbols carry one. If that field doesn't exist the column is dropped
  automatically; if your KiCad is too old for `sch export bom`, a BOM is derived
  from the board instead so you always get one.
* **Positions.csv** with the headers assembly houses expect
  (`Designator, Comment, Footprint, Mid X, Mid Y, Rotation, Layer`).
* Gerbers and drills are zipped together, then the whole folder is zipped again
  as `<name>_Manufacturing.zip`, which sits in `02_Manufacturing/` alongside the
  files it contains — one attachment to send. It never nests a previous run's
  copy of itself.

Gerbers, drills and positions all use the same **drill/place file origin**, so
the fab's coordinates line up.

---

## If you have more than one KiCad installed

This matters more than it sounds. Board Release picks the kicad-cli from the
KiCad you are running inside; standalone, it picks the highest version number
(compared numerically — `10.0` beats `9.0`, which plain string sorting gets
backwards).

An older `kicad-cli` cannot read a project a newer KiCad saved. It reports
**`Failed to load schematic`** and nothing else, and the BOM export fails with
it. If your versions don't match you get a warning at the top of every run:

```
[!] WARNING: you are running KiCad 10.0.1 but kicad-cli is 9.0.4.
             C:\Program Files\KiCad\9.0\bin\kicad-cli.exe
```

`--doctor` lists every install it found and marks the one in use. To pin it, set
`"kicad_cli"` in the config to the right path.

---

## Requirements

* KiCad 8, 9 or 10. KiCad 9+ recommended: `sch export bom` and the PDF layer
  modes only exist there. Every flag is probed against `--help` before use, so
  older builds degrade quietly instead of erroring out.
* **No plugins at all** by default. Everything comes from `kicad-cli` (KiCad's
  own command-line tool, which ships with KiCad) or is built in.
* **InteractiveHtmlBom** is optional — only if you turn the `ibom` step back on.
* Nothing from pip. Standard library only.

---

## Install

**PCM zip.** KiCad main window → *Plugin and Content Manager* → *Install from
File...* → `BoardRelease-pcm.zip`. Restart KiCad, or *Tools → External Plugins →
Refresh Plugins*.

**Or copy the folder** into your KiCad plugin directory:

| OS | Path |
|---|---|
| Windows | `C:\Users\<you>\Documents\KiCad\9.0\3rdparty\plugins\` |
| macOS | `~/Documents/KiCad/9.0/3rdparty/plugins/` |
| Linux | `~/.local/share/kicad/9.0/3rdparty/plugins/` |

**Standalone.** Copy `kicad_release.py` anywhere and run it with Python 3.8+.
On Windows a one-line `.bat` is friendliest:

```bat
@echo off
"C:\Program Files\KiCad\9.0\bin\python.exe" "C:\tools\kicad_release.py" %*
pause
```

---

## The revision

The folder name **is** the revision, so it matters that this is right. It is
looked for in this order:

1. **the board's title block** (*PCB Editor → File → Page Settings → Revision*)
2. the root schematic's title block, then any other sheet
3. a `REVISION` / `REV` project text variable (*Project Settings → Text
   Variables*)

The board comes first because the board is the thing being released, and its
title block is what appears on the fabrication drawing. Change the order with
`"revision_from": ["schematic", "board", "project"]` in the config.

`${VARIABLES}` inside a title block are expanded, so a title block reading
`${REVISION}` resolves to whatever the project defines. Whatever it finds, the
dialog and the menu let you override it before running, and `--rev D` wins on the
command line.

`--doctor` prints where the revision came from — or where it looked and found
nothing.

### The archive folder follows the project

By default every release goes into the open project's own folder:
`<project>/archive/<revision>/`. Nothing needs to be set per project — open a
different board and the Archive box already points at *that* board's `archive`
folder. (A project that already has an `Archive` folder from an older version
keeps using it.)

Whenever you press **Preview** or **Generate**, the archive folder and revision
prefix in the dialog are written to that project's own `.board_release.json`.
The folder is stored **relative to the project** — blank for the default, or
e.g. `releases` for `<project>/releases` — so copying or moving a project never
drags an old absolute path along with it. Only a folder outside the project is
stored as a full path.

In the user-wide config, a fixed absolute `archive_root` is ignored (it would
send every board to the same folder). To keep a central archive, make it
per-project: `"archive_root": "D:/Engineering/Archive/{project}"`, or set
`folder_template` to `{project}/{revlabel}`.

It writes only those two keys and leaves the rest of the file alone, so a config
you have hand-tuned is not flattened, and defaults are not frozen into it.

### Prefix

By default the folder is just the revision: `archive/C/`. Set `rev_prefix` (in
the config, the dialog's **Prefix** box, or `--rev-prefix Rev`) to put text in
front of it: `archive/RevC/`. It applies to file names too — `WidgetBoard_RevC_PCB.pdf`.

---

## Using the toolbar button

1. Save the board (**Ctrl+S**) and update the PCB from the schematic (**F8**).
   The tool reads files from disk, so unsaved work will not appear.
2. Click the **Board Release** icon.
3. Confirm the revision and prefix, confirm the archive folder, tick the steps.
4. **Preview** shows exactly what would be written without writing anything.
   **Generate** does it.
5. **Check tools** runs the full diagnostic into the log pane.

If the destination folder already exists and has files in it, you are asked
before anything is overwritten.

---

## Using the command line

```
python kicad_release.py [project] [options]

  -r, --rev REV        revision label (default: the title block)
      --rev-prefix S   text before the revision in folder and file names
  -a, --archive DIR    archive root (overrides the config file; default <project>/archive)
  -s, --steps LIST     comma-separated step keys, or "all"
  -y, --yes            no menu, no prompts — run the saved defaults
      --dry-run        show what would happen, write nothing
      --force          overwrite an existing revision folder
      --stop-on-error  halt at the first failure
      --no-open        don't open the folder afterwards
      --doctor         test every external tool and say what is wrong
      --write-config   drop a starter config file next to the project
      --dev-link [DIR] point the installed toolbar plugin at this copy of the code
      --dev-unlink     undo --dev-link
```

Step keys: `sch_pdf pcb_pdf manufacturing assembly_bom ibom erc drc step source_zip manifest`

```bash
# full release, no questions asked
python kicad_release.py ~/pcb/WidgetBoard --rev D --yes --force

# just the fab package
python kicad_release.py --steps manufacturing

# what would this do?
python kicad_release.py --steps all --dry-run

# something failed - why?
python kicad_release.py --doctor
```

Exit code is `0` when every step succeeded, `1` if any failed.

### Interactive menu keys

```
numbers   toggle a step (1 3 5, or a range like 2-4)
a / n     tick all / none
r         set the revision          x   set the revision prefix
d         set the archive folder    p   switch project
c         check tools               v   dry run
s         save these ticks as defaults
ENTER     run                       q   quit
```

---

## When something fails

Run `--doctor` (or the dialog's **Check tools**). It tests each tool in turn and
tells you the actual error rather than "failed":

```
  kicad-cli
    [✓] C:\Program Files\KiCad\9.0\bin\kicad-cli.exe
        version 9.0.4
        has `sch export bom`

  Python that can import pcbnew   (needed by InteractiveHtmlBom)
    [✓] C:\Program Files\KiCad\9.0\bin\python.exe
        reports pcbnew 9.0.4

  InteractiveHtmlBom
    [✓] ...\3rdparty\plugins\org_openscopeproject_InteractiveHtmlBom\generate_interactive_bom.py
    [✓] runs correctly under python.exe
```

**Almost every "tool step failed" comes down to one thing:** InteractiveHtmlBom
needs an interpreter that can `import pcbnew`, and that is KiCad's own bundled
Python, not the system one. The tool now finds it by walking up from wherever
`pcbnew` actually lives, tries several candidates, and — when it is running
inside KiCad already — falls back to executing iBOM in-process, which needs no
separate interpreter at all. If it still can't, `--doctor` lists every
interpreter it tried and the exact import error from each.

If `--doctor` reports a missing module, install it into **KiCad's** Python:

```
"C:\Program Files\KiCad\9.0\bin\python.exe" -m pip install jsonschema
```

Other common ones:

* **kicad-cli not found** — set `kicad_cli` in the config to the full path.
* **Revision is empty** — `--doctor` shows where it looked. Set it in Page
  Settings, add a `REVISION` text variable, or pass `--rev`.
* **ERC/DRC "failed"** — they are marked ok whenever the report is written.
  `kicad-cli` exits non-zero when violations exist; read the `.rpt`.
* **Outputs look stale** — the tool reads from disk. Save the board, press F8.

---

## Tinkering: the edit loop

`__init__.py` is a 20-line stub — the only file KiCad caches. The engine
(`kicad_release.py`) and the dialog (`action_plugin.py`) are re-imported from
disk **every time you click the button**:

> save the file → click the button → your new code runs

No *Refresh Plugins*, no restart, no reinstall. The dialog also has a **Reload
code** button that re-imports and reopens, keeping your revision, prefix,
archive path and ticked boxes. It prints where it loaded from and when that file
was last saved, so you can confirm your edit was picked up.

### Editing outside the plugin folder

Keep the source where you like and point the installed plugin at it:

```bash
cd D:\tools\board_release
python kicad_release.py --dev-link
```

That writes a one-line `dev_source.txt` into the installed plugin folder.
Restart KiCad **once**; after that the button always runs your working copy.
`--dev-unlink` reverts. `BOARD_RELEASE_SRC=<folder>` does the same and takes
priority.

| You changed | What's needed |
|---|---|
| `kicad_release.py` (engine) | nothing — click the button |
| `action_plugin.py` (dialog) | nothing — click the button |
| the config JSON | nothing — read at launch |
| `__init__.py`, the icon, the plugin name | Refresh Plugins, or restart |

For the first round of tinkering, skip the GUI: `--steps manufacturing --dry-run`
prints the exact `kicad-cli` commands with nothing in the way. A syntax error in
your edit pops a traceback box and KiCad carries on — you can't wedge it.

---

## Two KiCad versions, or a redirected Documents folder

If Documents is redirected into OneDrive or Dropbox, Windows leaves the plain
`C:\Users\<you>\Documents` in place, and you can end up with two KiCad trees -
each with its own copy of installed plugins. An InteractiveHtmlBom installed
under KiCad 9 cannot be imported by KiCad 10's Python, so picking the wrong copy
guarantees failure.

Board Release now asks Windows where Documents actually is, searches both trees,
and picks the plugin copy whose KiCad version matches the one you are running.
`--doctor` (or **Check tools**) lists every copy it found and marks the one in
use:

```
  InteractiveHtmlBom
    2 copies installed - the first is the one used:
      -> ...\Dropbox\...\Documents\KiCad\10.0\3rdparty\...   (KiCad 10.0)
         C:\Users\you\Documents\KiCad\9.0\3rdparty\...        (KiCad 9.0)
```

If the first attempt fails, it tries the other combinations and prints the real
error from each rather than guessing.

---

## Why it cannot crash KiCad

The toolbar button does not run the release inside KiCad. It spawns
`kicad_release.py` as a separate process using KiCad's own Python and streams
the output into the log pane. The engine is standard-library only, so the child
needs no `pcbnew` and shares nothing with the editor.

Earlier versions had an "in-process" fallback that executed InteractiveHtmlBom
inside KiCad when the subprocess failed. iBOM touches wx and `pcbnew`, neither of
which is safe off the main thread, and it took KiCad down moments after the run
finished. That path has been removed and will not come back.

---

## Configuration

Read from two places, project first:

1. `.board_release.json` next to your `.kicad_pro`
2. a user-wide file — `%APPDATA%\BoardRelease\config.json` (Windows),
   `~/Library/Application Support/BoardRelease/config.json` (macOS),
   `~/.config/board_release/config.json` (Linux)

Write a starter one with `--write-config`.

```jsonc
{
  "archive_root": "",          // blank = <project>/archive; relative = inside the project;
                               // {project} / {project_dir} allowed, e.g. "D:/Archive/{project}"
  "rev_prefix": "",            // "" -> archive/C   "Rev" -> archive/RevC

  // tokens: {rev} {revlabel} {project} {title} {date} {year} {month} {day}
  //         {company} {user}
  "folder_template": "{revlabel}",
  "file_name_template": "{project}_{revlabel}",

  "subfolders": {
    "docs": "01_Documentation", "manufacturing": "02_Manufacturing",
    "ibom": "03_Interactive_BOM", "reports": "04_Reports",
    "source": "05_Source", "models": "06_3D"
  },

  "revision_from": ["board", "schematic", "project"],

  "steps": { "sch_pdf": true, "pcb_pdf": true, "manufacturing": true,
             "assembly_bom": true, "ibom": false, "erc": false, "drc": false,
             "step": false, "source_zip": true, "manifest": true },

  "assembly_bom_dark": true,
  "assembly_bom_include_dnp": false,

  // empty = every copper layer plus both silkscreens
  "pcb_pdf_layers": [],
  "pcb_pdf_extra_args": ["--mode-multipage", "--include-border-title"],
  "sch_pdf_extra_args": [],

  // empty = copper from the board + silks, masks, pastes, Edge.Cuts,
  //         fab layers and User.1-4, with empty ones pruned after export
  "gerber_layers": [],
  "gerber_extra_args": ["--use-drill-file-origin", "--no-x2"],
  "drill_extra_args": ["--format", "excellon", "--excellon-units", "mm",
                       "--excellon-separate-th", "--generate-map",
                       "--map-format", "gerberx2", "--drill-origin", "plot"],
  "pos_extra_args": ["--format", "csv", "--units", "mm", "--side", "both",
                     "--exclude-dnp", "--use-drill-file-origin"],
  "bom_fields": "Value,Reference,Footprint,${QUANTITY},LCSC",
  "bom_labels": "Comment,Designator,Footprint,Quantity,LCSC Part #",
  "bom_group_by": "Value,Footprint",
  "prune_empty_gerbers": true,
  "zip_manufacturing": true,

  "ibom_extra_args": ["--no-browser", "--dark-mode", "--layer-view", "FB"],
  "ibom_allow_in_process": true,
  "step_extra_args": ["--subst-models", "--no-dnp"],

  "open_folder_when_done": true,
  "kicad_cli": "", "kicad_python": "", "ibom_script": ""
}
```

Anything in `*_extra_args` is passed straight through, so you can retune the
outputs without touching code. Flags your KiCad doesn't recognise are dropped
with a note in the log rather than failing the step — and dropping a flag can
never swallow the file name, because the input file is passed separately from
the flags.

### Folder naming

`folder_template` is appended to `archive_root`:

| Template | Result |
|---|---|
| `{revlabel}` *(default)* | `archive/C/` |
| `{revlabel}` + prefix `Rev` | `archive/RevC/` |
| `{project}/{revlabel}` | `archive/WidgetBoard/C/` |
| `{year}/{revlabel}_{date}` | `archive/2026/C_2026-08-26/` |

---

## Notes

* Layer names come from the board file, so a 2-layer board doesn't get empty
  `In1.Cu` pages, and both the KiCad 6 (`F.SilkS`) and 7+ (`F.Silkscreen`)
  spellings are understood.
* The root schematic is checked for the revision **first**, so a sub-sheet that
  sorts earlier alphabetically can't shadow it.
* The source zip skips `-backups/`, `fp-info-cache`, `.git` and the archive
  folder itself.
* If your project is in git, the manifest records the commit, branch, and
  whether the tree was dirty when you cut the release.

MIT licence.
