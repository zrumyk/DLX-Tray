English text comes from the source code. Ukrainian text is maintained in `uk.ts`.
`qt_uk.ts` supplements and corrects widget strings in the Ukrainian catalog shipped by Qt.
After editing UI strings, refresh the catalog from the repository root:

```powershell
uv run pyside6-lupdate -extensions py -no-obsolete -locations none -source-language en -target-language uk src/dlx_tray -ts src/dlx_tray/i18n/uk.ts
```

Fill in new translations and preserve placeholders, file filter patterns, and keyboard mnemonics.
For a packaged build, compile the catalog and ship it under `dlx_tray/i18n/`:

```powershell
uv run pyside6-lrelease src/dlx_tray/i18n/uk.ts src/dlx_tray/i18n/qt_uk.ts -qm src/dlx_tray/i18n/uk.qm
```

Compiled catalogs are git-ignored. A source or wheel installation without `uk.qm`
compiles both TS files in a temporary directory at Ukrainian startup using the Qt tool
bundled in PySide6 Essentials. Remove a local `uk.qm` or rebuild it after updating a TS file.
The app loads PySide6's `qtbase_uk.qm` for standard Qt controls.
