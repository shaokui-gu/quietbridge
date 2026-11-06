# WorkBuddy menu bar assets

Exported through the official Figma MCP from [WorkBuddy CUA](https://www.figma.com/design/Tr6hAfLgUAulbXkagRyX3S?node-id=7-18).

`nodes.json` maps each image to its original design node. PNG files are complete transparent node exports; SVG files retain the original vector artwork. No icon paths are hand drawn. `MenuBarAssets.swift` embeds the PNG bytes so the existing single executable installation retains its assets. AppKit treats them as template images and supplies the menu bar or panel tint.

The panel follows the 360 × 654 pt handoff, system fonts, semantic Light / Dark colors, 42 pt capability rows and 34 × 20 pt switch visuals. The whole row is an accessible switch and keyboard target. The separately installed authorization plug-in is detected through the existing receipt and signature validator. Its install button opens the existing `cua-auth-plugin/install.command`; a deployed standalone helper can ask the user to locate that installer in the original distribution.
