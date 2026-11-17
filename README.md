# Board Release

A KiCad plugin that generates the full set of release files for a board revision and files them into a versioned archive folder.

## Features

- Schematic PDF, all pages
- Board PDF — copper, silkscreen and fab layers, one layer per page
- Manufacturing package — gerbers, Excellon drills, BOM and position files, zipped for the fab
- Interactive assembly BOM, built in
- InteractiveHtmlBom output, if that plugin is installed
- ERC and DRC reports
- STEP 3D model
- Source snapshot archive
- Release manifest listing every output file with its size and SHA-256
- Part-number fields read from the schematic and carried into every BOM output
- Revision read from the board title block, with configurable folder and file naming
- Toolbar button in the PCB editor, plus a command line interface for scripted use

## Requirements

- KiCad 8, 9 or 10
- InteractiveHtmlBom, only if that output is enabled

## Install

Download `BoardRelease-<version>-pcm.zip` from the [latest release](../../releases/latest).

In KiCad: **Plugin and Content Manager → Install from File**, then select the zip.

## Previous versions

Every release on the [releases page](../../releases) has its own zip attached. To go back to an earlier version, download that release's zip and install it the same way.

## License

MIT. See [LICENSE](LICENSE).
