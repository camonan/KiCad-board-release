# KiCad board release plugin

This KiCad plugin is an automation to standardize board releases. It is a wrapper over some installed and built-in tools in KiCad. I go through the board release process often for professional and hobby projects and the repetitive nature can be tedious. This tool helps to keep project board release file structure consistent across many projects and version releases. 

**Requires KiCad 8 or newer.**

## Outputs generated
Similar to the JLCPCB or PCBWay plugins, it automates multiple file generation tools into a single interface. The benefit here is further automation for other aspects of a board release in one place including 
1. PDF capture of the schematic and board layout
2. PCB Manufacturer gerber, BOM, and position file generation as well as a single zip with all of these items packaged together for easy transfer
3. Generation of Interactive Html Bom output (which is another KiCad plugin, needs to be installed) or option to use internally built assembly BOM where no other installs needed (separate "Interactive Html Bom" installation recommended)
   - The Interactive HTML BOM option is unchecked by default. Check it once you've installed that plugin.
4.  Generation of ERC/DRC report to capture board state at time of board release generation
5. Zip file to capture native KiCad source at time of board release generation
6. Generation of 3D STEP file for the board and populated components

<img width="632" height="360" alt="image" src="https://github.com/user-attachments/assets/50e9f884-1411-4530-89c0-b2b395b47a79" />

## How to install

Installation of this KiCad plugin is easy. 
#### Quick install
Click this link to download the most up to date release package zip file. Skip to **Installing plugin in KiCad** below.

[**Current plugin version**](https://github.com/camonan/KiCAD-board-release/releases/latest/download/BoardRelease-pcm.zip)

#### Version specific install
Navigate to **Releases** on the right hand side.
Pick a version, and under Assets download BoardRelease-x.y.z-pcm.zip

#### Installing plugin in KiCad
1. Open KiCad and select Plugin and Content Manager
2. Select Install from File...
3. Select the downloaded zip file
4. Restart PCB Editor if open during install
5. The plugin will now show in PCB Editor

## How to use
We'll use a demo project to demonstrate the functionality. This board lights an LED with a coin cell battery and small resistor. 

### Setup Page Settings

<img width="675" height="373" alt="image" src="https://github.com/user-attachments/assets/81e30d2d-4de1-446e-9c43-3e554e5961a7" />

Add date, revision, and title as desired. Using this format allows you to reference this text with a variable rather than having to update text in multiple locations when the revision or date changes as shown below.

<img width="319" height="144" alt="image" src="https://github.com/user-attachments/assets/bf750072-9264-42f8-ac5a-53be46311186" />

### Select the "Board Release..." plugin from the tool bar

<img width="333" height="160" alt="image" src="https://github.com/user-attachments/assets/1e88c2aa-dd1b-414a-a21b-5eaa2708f357" />

### Release package generation

Interface of all options for package generation

<img width="583" height="410" alt="image" src="https://github.com/user-attachments/assets/9ce13ccd-f70a-426a-8bf1-8afe4c023e27" />

1. Board revision is automatically pulled from the Page Settings once the board file is saved.
2. Prefix comes before board revision and combines to become the title of the folder holding all release items. For prefix "v" and revision "1.0.0", the output folder is named "v1.0.0".
3. "archive" folder is created in the project directory if one does not exist. This allows all releases to be held in the same location.
4. "What to generate" allows individual file outputs to be selected as desired. I generally prefer to generate all items and ignore the unneeded ones as they will be saved in separate subfolders anyways.
5. Finally, "Generate" at the bottom of the window performs all actions at once. This takes a few seconds and the files can be viewed once completed. A named board release snapshot has now been generated with all selected files.

**License:** MIT. See [LICENSE](LICENSE). Free to use, modify and share, with no warranty.

**Disclaimer:** This plugin was written by an LLM. Test the outputs before sending files to a manufacturer.
