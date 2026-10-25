# FH6 Livery Swap

Turns a livery you downloaded from another player in Forza Horizon 6 into your own.
Everything runs offline on your PC, and the original is backed up before anything changes.

> **Status:** working in game. Backups are still made every time, just in case.

## Use it

1. **Close Forza Horizon 6** completely.
2. Run **LiverySwap.exe** (download it from [Releases](https://github.com/DVS-code/fh6-livery-swap/releases)). Your save folder fills in automatically. If it doesn't,
   click **Browse…** and pick `C:\XboxGames\GameSave\pgs\u_<your id>_16D460`.
3. Pick a livery from the drop-down, or pick **All downloaded liveries**.
4. Click **Make it mine**.

The app reads your account ID from the save. It takes your gamertag from a livery you
already made. If it can't find one, it asks you once and remembers it in `settings.json`.

Originals are copied to the `backups\` folder next to the app before they're changed.
Click **Open backups** to see them.

## What changes

- The creator account ID (in the header and the body) is set to yours, and the gamertag to yours.
- The "downloaded" flag and share ID are cleared, so it looks like a livery you made.
- The folder name, livery ID and thumbnail stay the same, so cars keep their livery.
- Vinyl groups inside the livery keep their original author.

Liveries in `backups\` that are no longer in the save show as `[backup]`. Picking one
writes the converted copy back into the save.

## Run from source

Needs Python 3.9+ on Windows (Tkinter comes with Python).

```bat
pip install -r requirements.txt
python liveryswap.py
```

Or double-click `Livery Swap.bat`. Pillow is optional; it shows the thumbnail preview.

Command line:

```bat
python liveryswap.py <save folder> --list
python liveryswap.py <save folder> --all
python liveryswap.py <save folder> <Livery_folder_name>
```

## Build the .exe

```bat
build.bat
```

This installs the requirements and PyInstaller, then writes `dist\LiverySwap.exe`.
Ship the .exe on its own. It creates `backups\` and `settings.json` next to itself.

## Files

| File | What it is |
| --- | --- |
| `liveryswap.py` | The app: livery format, save handling, GUI and command line |
| `build.bat` | Builds the single-file .exe |
| `icon.ico` | App icon |

## License

Copyright (C) 2026 DVS-code

This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. See [LICENSE](LICENSE).

Not affiliated with or endorsed by Playground Games, Turn 10, Microsoft or Xbox.
