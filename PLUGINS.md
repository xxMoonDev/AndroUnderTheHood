# Writing AndroUnderTheHood plugins

A plugin adds features (toolbar buttons, windows, file-menu items) without rebuilding AndroUnderTheHood.
You write it as a folder, and ship it as a **.7z package**, optionally password protected. Packages are
decrypted and read **into memory** and imported from there; their contents are never written to disk.

The folder has two files:

```
my_plugin/
  plugin.json
  plugin.py
```

**plugin.json**
```json
{
  "id": "my_plugin",
  "name": "My plugin",
  "version": "1.0",
  "author": "You",
  "description": "What it does",
  "api": 1,
  "main": "plugin.py",
  "requires": []
}
```
- `id`: letters, digits, `_` and `-`. It must be unique.
- `api`: the plugin API version it needs (currently **1**).
- `requires`: optional PyPI packages, e.g. `["pyusb", "qrcode"]`. They are installed into `my_plugin/_lib` automatically the first time the plugin loads.

**plugin.py**
```python
def register(api):
    api.add_tool("Hello", lambda: api.message("Hello from my plugin!"), icon="star")

def unload(api):          # optional: called on Reload / disable / exit
    pass
```

## Where plugins live
| Place | Used for |
|---|---|
| `%APPDATA%\AndroUnderTheHood\plugins\*.7z` | Packages you install (**Plugins → Install…**). Installing copies the .7z as it is; nothing is unpacked. |
| `<program folder>\plugins\*.7z` | Packages shipped with the program |
| Development folders (**Plugins → Development folders…**) | Plain plugin folders you are writing. **New plugin…** creates one in `Documents\AndroUnderTheHood\PluginDev`. |

## Workflow
1. **New plugin…** creates a folder from a template. Edit `plugin.py` and press **Reload**: no restart and no rebuild.
2. **Pack plugin…** turns the folder into `<id>-<version>.7z`. You can tick **Protect with a password**
   (AES-256) and **Also hide the file names**.
3. Share the .7z. Others use **Install…**. For a protected package they enter the password once. It is kept
   encrypted for their Windows account (DPAPI), so the plugin loads by itself afterwards. If the saved password
   stops working, the plugin shows as **Locked** until they **Unlock…** it.

Errors (with the full traceback, including lines inside packages) appear in the Plugins window. Messages from
`api.log` go to `%APPDATA%\AndroUnderTheHood\androunderthehood.log`.

### In-memory rules for packaged plugins
- Your modules are imported from memory: `from . import helpers` works as usual.
- Read your own files with `api.read_file("data/table.json")` (bytes). `api.icon("img/logo.png")` loads an image
  from the package. There is no folder on disk (`api.folder` is empty), so `open(__file__)` and paths relative
  to the plugin don't work.
- Packages are limited to 256 MB unpacked.
- PyPI packages listed in `requires` are ordinary public libraries. They are installed to
  `%APPDATA%\AndroUnderTheHood\plugin-data\<id>\_lib`. Only your plugin's own code stays memory-only.

## The API (`api`)

### Device / connection
| Call | What it does |
|---|---|
| `api.connected()` | True while a device is connected |
| `api.conn` | The current connection object (SSH or ADB), or `None` |
| `api.device_name` | Name of the connected profile |
| `api.is_root()` | True when Root is on and working |
| `api.run(cmd, root=True, timeout=60)` | Runs a shell command on the device and returns `(exit_code, stdout, stderr)` as text. `root=False` runs it as the session user. **It blocks**, so call it inside `api.task`. |
| `api.fs` | File access: `listdir(path)`, `stat(path)`, `download(remote, local, progress)`, `upload(local, remote, progress)`, `mkdir`, `rename`, `remove`, `read_range(path, offset, length)` |

### Background work
```python
def work():                     # runs on a worker thread – no Qt widgets here
    rc, out, err = api.run("getprop ro.product.model")
    return out.strip()

api.task(work, on_done=lambda model: api.message(model), on_error=lambda msg: api.message(msg, error=True))
```

### UI
| Call | What it does |
|---|---|
| `api.add_tool(title, callback, icon="star", tooltip="", shortcut=None, needs="connection", toolbar=True)` | Adds a toolbar button and an entry in **⋯ → Plugins**. **No two buttons share an icon:** if `icon` is already used by the app or another plugin, the button gets a letter badge made from its title instead (pick an unused icon or ship your own image to avoid that). `needs`: `None` (always usable), `"connection"`, `"root"` (hidden while Root is off), `"shell"` (root or the ADB/Shizuku shell user). |
| `api.add_file_action(title, callback, when="any", icon=None)` | Adds an item to the file browser's right-click menu. `callback(entries)` gets the selected entries (`.path`, `.name`, `.size`, `.is_dir`, `.mtime`, `.mode`). `when`: `"any"`, `"file"`, `"dir"` or `"single"`. |
| `api.message(text, ms=6000, error=False)` | Shows text in the status bar |
| `api.icon(name)` | One of the app's line icons, or an image file in your plugin folder. Toolbar buttons use: `connect`, `disconnect`, `hash`, `back`, `forward`, `up`, `refresh`, `grid`, `list`, `download`, `upload`, `folder-plus`, `rename`, `trash`, `copy`, `star`, `terminal`, `package`, `pie`, `chip`, `log`, `shield`, `antenna`, `tree`, `sliders`, `broom`, `flash`, `usb`, `phone`, `puzzle`, `gear`; the shipped plugins use `accel`, `history`, `battery`, `camera`, `pin`, `badge`, `mic`, `wifi`, `restore`, `speaker`, `pulse`. Free for new plugins: `lock`, `plus`, `save`, `power`, `bookmark`, `sdcard`, `down`, `root` |
| `api.theme` | Colours: `api.theme.ACCENT`, `TEXT`, `TEXT_DIM`, `TEXT_FAINT`, `DANGER`, `CARD`, `BG` |
| `api.window` | The main window, to use as the parent of your dialogs |
| `api.cwd`, `api.navigate(path)`, `api.refresh()`, `api.selected()` | The file browser |

Windows are ordinary PySide6 widgets: `from PySide6.QtWidgets import QDialog, ...`.

### Events
```python
api.on("connected", lambda conn: ...)
api.on("disconnected", lambda: ...)
api.on("folder_changed", lambda path: ...)
```

### Settings and files
| Call | What it does |
|---|---|
| `api.get_setting(key, default)`, `api.set_setting(key, value)` | Settings saved as JSON per plugin |
| `api.data_dir` | A folder for the plugin's own files (`%APPDATA%\AndroUnderTheHood\plugin-data\<id>`) |
| `api.read_file(name)` | Bytes of a file inside the plugin (from memory for packages) |
| `api.folder` | The plugin's folder (development plugins only; empty for packages) |
| `api.log` | A `logging` logger |

## What a plugin can import
- The whole Python standard library.
- PySide6 (QtCore, QtGui, QtWidgets, QtNetwork, QtSvg, QtCharts, QtPrintSupport, QtConcurrent).
- paramiko and Pillow.
- Anything listed in `requires`.
- Several files in one plugin: put `helpers.py` next to `plugin.py` and write `from . import helpers`.
- AndroUnderTheHood's own modules (`androunderthehood.*`) are importable too, but they are not a stable API and may change. Prefer `api`.

## Example
`plugins\battery_tools.7z` ships with the program (source: `plugins\battery_tools` in the project). It adds a live **Battery** window and a **SHA-256 checksum** item
to the file right-click menu. Copy it as a starting point.

## Safety
Plugins run with the same rights as AndroUnderTheHood: your PC's files and every connected phone.
Only install plugins you trust.
