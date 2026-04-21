Optional. Drop `icon16.png`, `icon48.png`, `icon128.png` here and add an `icons` block back into `../manifest.json` if you want a real toolbar icon. Without them, Chrome uses the puzzle-piece default.

Earlier versions of this README claimed Chrome would silently fall back if the `icons` block referenced missing files. That is wrong. If the `icons` block lists a file that is not present, Chrome refuses to load the manifest and the extension never runs. Keep the `icons` block out of the manifest unless the files actually exist.
