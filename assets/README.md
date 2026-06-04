# Kestrel assets

Drop the app artwork here. The GUI, the window/taskbar icon, and the project README pick
these up automatically — **no code changes needed**:

- **`logo.png`** — shown in the window header, the README banner, and used as the
  window/taskbar icon.
- **`icon.ico`** — preferred Windows window/taskbar icon if present (takes priority over
  `logo.png`).

## Recommended

- A **square PNG**, ~256×256 or larger, with a **transparent background**.
- For the crispest taskbar/title-bar icon on Windows, also export an **`icon.ico`**
  containing 16 / 32 / 48 / 256 px sizes. (Tip: `py -m pip install pillow`, then
  `python -c "from PIL import Image; Image.open('assets/logo.png').save('assets/icon.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])"`)
