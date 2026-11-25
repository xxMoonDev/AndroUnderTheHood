"""Plugin system: add features to AndroUnderTheHood without rebuilding it.

A plugin is a .7z package (optionally password protected, AES-256) – or, while you develop it, a folder –
with:
    plugin.json   {"id": "battery_tools", "name": "Battery tools", "version": "1.0", "author": "…",
                   "description": "…", "api": 1, "main": "plugin.py", "requires": ["some-pypi-package"]}
    plugin.py     def register(api): …          (and optionally  def unload(api): …)

`register` receives a PluginAPI – the stable surface plugins should use (see PLUGINS.md).
Packages are read into memory and imported from there; their contents are never written to disk.
Loaded from:
    %APPDATA%\\AndroUnderTheHood\\plugins\\*.7z   – installed by the user
    <program folder>\\plugins\\*.7z               – shipped with the program
    development folders (plain folders, edit + Reload – no restart) added in the Plugins window
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import traceback
from dataclasses import dataclass, field

from . import backend as B
from . import pluginpack as PP

log = logging.getLogger("androunderthehood")
API_VERSION = 1
EVENTS = ("connected", "disconnected", "folder_changed")


def user_dir() -> str:
    d = os.path.join(B.app_dir(), "plugins")
    os.makedirs(d, exist_ok=True)
    return d


def dev_default_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "PluginDev")


def bundled_dir() -> str:
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else \
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "plugins")


def _cfg_path() -> str:
    return os.path.join(B.app_dir(), "plugins.json")


def load_cfg() -> dict:
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cfg(cfg: dict):
    with open(_cfg_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


@dataclass
class Plugin:
    id: str
    folder: str
    meta: dict
    origin: str                     # "user", "bundled" or "dev"
    module: object = None
    api: "PluginAPI | None" = None
    error: str = ""
    status: str = "not loaded"      # loaded / disabled / error / locked / installing packages / not loaded
    archive: str = ""               # path of the .7z package ("" for a development folder)
    files: dict | None = None       # the package's files, in memory
    encrypted: bool = False
    importer: object = None

    @property
    def lib_dir(self) -> str:
        """Where its PyPI "requires" are installed (third-party packages, not the plugin's own code)."""
        if self.archive:
            return os.path.join(B.app_dir(), "plugin-data", self.id, "_lib")
        return os.path.join(self.folder, "_lib")

    @property
    def name(self) -> str:
        return self.meta.get("name") or self.id

    @property
    def version(self) -> str:
        return str(self.meta.get("version", ""))


# ---------------------------------------------------------------------------
# The API handed to plugins
# ---------------------------------------------------------------------------
class PluginAPI:
    """Everything a plugin needs. Keep plugins on this surface: it stays compatible between versions.
    `api.window` is the escape hatch to the whole main window (may change between versions)."""

    API_VERSION = API_VERSION

    def __init__(self, mgr: "PluginManager", plugin: Plugin):
        self._mgr = mgr
        self._plugin = plugin
        self._actions: list = []        # QActions added by this plugin
        self._file_actions: list = []   # (label, callback, when, icon)
        self._handlers: list = []       # (event, fn)
        self.id = plugin.id
        self.name = plugin.name
        self.folder = "" if plugin.archive else plugin.folder   # packaged plugins have no folder on disk
        self.archive = plugin.archive
        self.log = logging.getLogger(f"androunderthehood.plugin.{plugin.id}")
        self.data_dir = os.path.join(B.app_dir(), "plugin-data", plugin.id)
        os.makedirs(self.data_dir, exist_ok=True)

    # ---- app & connection -------------------------------------------------
    @property
    def window(self):
        return self._mgr.win

    @property
    def conn(self) -> "B.Connection | None":
        """The current connection (SSH or ADB), or None."""
        return self._mgr.win.conn

    @property
    def fs(self):
        """The file system of the current connection (listdir, stat, download, upload…), or None."""
        return self._mgr.win.fs

    def connected(self) -> bool:
        return self._mgr.win.conn is not None

    def is_root(self) -> bool:
        c = self._mgr.win.conn
        return bool(c and (c.is_root or c.native_root) and self._mgr.win.a_root.isChecked())

    @property
    def device_name(self) -> str:
        c = self._mgr.win.conn
        return c.profile.name if c else ""

    def run(self, cmd: str, root: bool = True, timeout: float = 60) -> tuple[int, str, str]:
        """Run a shell command on the device. root=True uses root when Root is on.
        Returns (exit code, stdout, stderr) as text. Blocks – call it from api.task() for slow commands."""
        c = self._mgr.win.conn
        if not c:
            raise RuntimeError("Not connected")
        rc, out, err = c.exec(cmd, timeout=timeout, as_root=root)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def task(self, fn, on_done=None, on_error=None):
        """Run fn() in the background; on_done(result) / on_error(message) are called on the GUI thread."""
        from .ui import Task
        return Task.start(fn, on_done or (lambda _r: None),
                          on_error or (lambda m: self.message(f"{self.name}: {m}", error=True)))

    # ---- file browser -----------------------------------------------------------
    @property
    def cwd(self) -> str:
        return self._mgr.win.cwd

    def navigate(self, path: str):
        self._mgr.win.navigate(path)

    def refresh(self):
        self._mgr.win.navigate(self._mgr.win.cwd, record=False)

    def selected(self) -> list:
        """Selected entries in the file browser (objects with .path, .name, .size, .is_dir …)."""
        return self._mgr.win.selected_entries()

    # ---- UI ---------------------------------------------------------------------
    def message(self, text: str, ms: int = 6000, error: bool = False):
        """Show a message in the status bar (error=True also logs it as an error)."""
        (self.log.error if error else self.log.info)(text)
        self._mgr.win.statusBar().showMessage(text, ms)

    def read_file(self, name: str) -> bytes:
        """A file from the plugin's own package (or folder), e.g. api.read_file("data/table.json")."""
        pl = self._plugin
        if pl.files is not None:
            return pl.files[name.replace("\\", "/").lstrip("/")]
        with open(os.path.join(pl.folder, name), "rb") as f:
            return f.read()

    def icon(self, name: str):
        """One of the app's line icons by name ("chip", "log", "star", "terminal", "package", …) or an image
        file inside your plugin (loaded from memory for packaged plugins)."""
        from PySide6.QtGui import QIcon, QPixmap
        from . import theme
        if "." in name:
            try:
                pm = QPixmap()
                if pm.loadFromData(self.read_file(name)):
                    return QIcon(pm)
            except (KeyError, OSError):
                pass
        return theme.line_icon(name)

    def _unique_icon(self, name: str, title: str):
        """No two buttons share a picture: if another button already uses this line icon, this one gets a
        letter badge made from its title instead."""
        from . import theme
        if "." in (name or ""):
            return self.icon(name)
        owners = self._mgr.icon_owners
        me = f"plugin:{self.id}"
        if name and owners.get(name, me) == me:
            owners[name] = me
            return theme.line_icon(name)
        words = [w for w in "".join(c if c.isalnum() else " " for c in title).split() if w]
        cands = []
        if len(words) >= 2:
            cands.append((words[0][0] + words[1][0]).upper())
        if words:
            cands += [words[0][:2].title(), words[0][0].upper()]
        cands += [f"{(words[0][0] if words else '?').upper()}{k}" for k in range(2, 10)]
        for c in cands:
            if owners.get("mono:" + c, me) == me:
                owners["mono:" + c] = me
                self.log.info("icon %r is already used by another button; using the letters %r", name, c)
                return theme.monogram_icon(c)
        return theme.monogram_icon("?")

    @property
    def theme(self):
        """The app's colours (theme.ACCENT, theme.TEXT_DIM, theme.DANGER …) and theme.line_icon()."""
        from . import theme
        return theme

    def add_tool(self, title: str, callback, icon: str = "star", tooltip: str = "", shortcut: str | None = None,
                 needs: str | None = "connection", toolbar: bool = True):
        """Add a button to the toolbar (toolbar=True) and the Plugins menu.
        needs: None (always usable), "connection", "root" (only shown while Root is on and working),
        "shell" (root or the ADB/Shizuku shell user)."""
        from PySide6.QtGui import QAction, QKeySequence
        a = QAction(self._unique_icon(icon, title), title, self._mgr.win)
        a.setObjectName(f"plugin:{self.id}:{title}")    # key for the saved toolbar order
        a.setToolTip(tooltip or title)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(lambda _c=False: self._call(callback))
        a.setProperty("aoh_needs", needs or "")
        a.setProperty("aoh_toolbar", toolbar)
        self._actions.append(a)
        self._mgr.attach_action(a, toolbar)
        return a

    def add_file_action(self, title: str, callback, when: str = "any", icon: str | None = None):
        """Add an item to the file browser's right-click menu. callback(entries) gets the selected entries.
        when: "any", "file" (only files selected), "dir" (only folders), "single" (exactly one item)."""
        self._file_actions.append((title, callback, when, icon))

    def on(self, event: str, fn):
        """Listen for an app event: "connected" fn(conn), "disconnected" fn(), "folder_changed" fn(path)."""
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}; use one of {EVENTS}")
        self._handlers.append((event, fn))

    # ---- settings ---------------------------------------------------------------
    def _settings_path(self):
        return os.path.join(self.data_dir, "settings.json")

    def get_setting(self, key: str, default=None):
        try:
            with open(self._settings_path(), encoding="utf-8") as f:
                return json.load(f).get(key, default)
        except (OSError, ValueError):
            return default

    def set_setting(self, key: str, value):
        try:
            with open(self._settings_path(), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        d[key] = value
        with open(self._settings_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)

    # ---- internal ---------------------------------------------------------------
    def _call(self, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self.log.error("plugin callback failed: %s", traceback.format_exc())
            self.message(f"{self.name}: {e}", 10000, error=True)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
def read_meta(folder: str) -> dict:
    with open(os.path.join(folder, "plugin.json"), encoding="utf-8") as f:
        meta = json.load(f)
    if not isinstance(meta, dict) or not meta.get("id"):
        raise ValueError("plugin.json needs at least an \"id\"")
    return meta


class PluginManager:
    def __init__(self, win):
        self.win = win
        self.plugins: dict[str, Plugin] = {}
        self.menu = None          # Plugins menu (set by the main window)
        self.toolbar = None
        self.toolbar_before = None
        self.icon_owners: dict[str, str] = {}   # line icon name (or "mono:XY") -> "app" / "plugin:<id>"

    # ---- discovery --------------------------------------------------------------
    def dirs(self) -> list[tuple[str, str]]:
        cfg = load_cfg()
        # running from source: the project's plugins\ folder holds plain folders = development plugins
        out = [(user_dir(), "user"), (bundled_dir(), "bundled" if getattr(sys, "frozen", False) else "dev")]
        out += [(d, "dev") for d in cfg.get("dev_dirs", [])]
        return out

    @staticmethod
    def _password(archive: str) -> str | None:
        tok = load_cfg().get("passwords", {}).get(os.path.basename(archive).lower())
        return PP.unprotect(tok) if tok else None

    @staticmethod
    def _store_password(archive: str, password: str | None):
        cfg = load_cfg()
        pw = cfg.setdefault("passwords", {})
        key = os.path.basename(archive).lower()
        if password:
            pw[key] = PP.protect(password)
        else:
            pw.pop(key, None)
        save_cfg(cfg)

    def open_archive(self, path: str, origin: str, password: str | None = None) -> Plugin:
        """Read a .7z package into memory → Plugin (status "locked" if it needs a password we don't have)."""
        stem = os.path.splitext(os.path.basename(path))[0]
        pw = password if password is not None else self._password(path)
        try:
            files = PP.read_archive(path, pw)
        except (PP.PasswordNeeded, PP.WrongPassword) as e:
            return Plugin(stem, path, {"name": stem}, origin, archive=path, encrypted=True, status="locked",
                          error=("Enter its password to unlock it." if isinstance(e, PP.PasswordNeeded)
                                 else "The saved password no longer works – enter it again."))
        except Exception as e:  # noqa: BLE001
            return Plugin(stem, path, {"name": stem}, origin, archive=path, status="error",
                          error=f"{os.path.basename(path)}: {e}")
        try:
            meta = json.loads(files["plugin.json"].decode("utf-8-sig"))
            if not isinstance(meta, dict) or not meta.get("id"):
                raise ValueError("plugin.json needs at least an \"id\"")
        except Exception as e:  # noqa: BLE001
            return Plugin(stem, path, {"name": stem}, origin, archive=path, status="error", error=f"plugin.json: {e}")
        return Plugin(meta["id"], path, meta, origin, archive=path, files=files, encrypted=bool(pw))

    def discover(self) -> list[Plugin]:
        found: dict[str, Plugin] = {}

        def add(pl: Plugin):
            if pl.id not in found or pl.origin == "dev":   # a development copy overrides a package
                found[pl.id] = pl

        for base, origin in self.dirs():
            if not os.path.isdir(base):
                continue
            if origin == "dev" and os.path.isfile(os.path.join(base, "plugin.json")):
                entries = [base]
            else:
                entries = [os.path.join(base, d) for d in sorted(os.listdir(base))]
            for e in entries:
                if os.path.isfile(e) and e.lower().endswith(".7z"):
                    add(self.open_archive(e, origin))
                elif origin == "dev" and os.path.isfile(os.path.join(e, "plugin.json")):
                    try:
                        add(Plugin(read_meta(e)["id"], e, read_meta(e), origin))
                    except Exception as ex:  # noqa: BLE001
                        add(Plugin(os.path.basename(e), e, {"name": os.path.basename(e)}, origin, status="error",
                                   error=f"plugin.json: {ex}"))
        return list(found.values())

    # ---- load / unload ----------------------------------------------------------
    def load_all(self):
        for p in list(self.plugins.values()):
            self.unload(p)
        self.plugins = {p.id: p for p in self.discover()}
        disabled = set(load_cfg().get("disabled", []))
        for p in self.plugins.values():
            if p.status in ("error", "locked"):
                continue
            if p.id in disabled:
                p.status = "disabled"
                continue
            self.load(p)

    def _deps_ok(self, p: Plugin) -> bool:
        req = p.meta.get("requires") or []
        if not req:
            return True
        marker = os.path.join(p.lib_dir, ".ok")
        want = hashlib.sha1(json.dumps(req).encode()).hexdigest()
        try:
            return open(marker, encoding="utf-8").read().strip() == want
        except OSError:
            return False

    def install_deps(self, p: Plugin, say=lambda s: None):
        """pip-install the plugin's "requires" into <plugin>/_lib with the private Python (same 3.12)."""
        from . import flashtools as F
        req = p.meta.get("requires") or []
        F.ensure_python(say)
        lib = p.lib_dir
        shutil.rmtree(lib, ignore_errors=True)
        say(f"Installing {', '.join(req)} for {p.name}…")
        F._run([F.python_exe(), "-m", "pip", "install", "--no-warn-script-location", "--prefer-binary",
                "--no-build-isolation", "--target", lib, *req], f"{p.name} packages")
        with open(os.path.join(lib, ".ok"), "w", encoding="utf-8") as f:
            f.write(hashlib.sha1(json.dumps(req).encode()).hexdigest())

    def load(self, p: Plugin):
        p.error = ""
        api_need = int(p.meta.get("api", 1))
        if api_need > API_VERSION:
            p.status, p.error = "error", f"needs plugin API {api_need}; this AndroUnderTheHood has {API_VERSION}"
            return
        if not self._deps_ok(p):
            p.status = "installing packages"

            def done(_r):
                self.load(p)
                self.win.statusBar().showMessage(f"Plugin {p.name} ready", 5000)

            def fail(m):
                p.status, p.error = "error", f"installing its packages failed: {m}"
            from .ui import Task
            Task.start(lambda: self.install_deps(p), done, fail)
            return
        if p.status == "locked":
            return
        lib = p.lib_dir
        if os.path.isdir(lib) and lib not in sys.path:
            sys.path.insert(0, lib)
        modname = f"aoh_plugin_{p.id}"
        for k in [k for k in sys.modules if k == modname or k.startswith(modname + ".")]:
            del sys.modules[k]
        main_rel = p.meta.get("main", "plugin.py")
        try:
            if p.files is not None:          # packaged: imported from memory, nothing written to disk
                if main_rel not in p.files:
                    raise FileNotFoundError(f"{main_rel} is not in the package")
                p.importer = PP.MemoryImporter(modname, p.files, main_rel, f"7z:{os.path.basename(p.archive)}")
                p.importer.install()
                spec = p.importer.find_spec(modname)
            else:
                spec = importlib.util.spec_from_file_location(modname, os.path.join(p.folder, main_rel),
                                                              submodule_search_locations=[p.folder])
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            p.api = PluginAPI(self, p)
            if not hasattr(mod, "register"):
                raise AttributeError("the plugin has no register(api) function")
            mod.register(p.api)
            p.module = mod
            p.status = "loaded"
            log.info("plugin %s %s loaded from %s%s", p.id, p.version, p.folder,
                     " (in memory)" if p.files is not None else "")
        except Exception as e:  # noqa: BLE001
            p.status, p.error = "error", f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
            log.error("plugin %s failed to load: %s", p.id, traceback.format_exc())
            if p.api:
                self._detach(p.api)
            p.api = None
        self.win.update_actions()

    def unload(self, p: Plugin):
        if p.module is not None and hasattr(p.module, "unload") and p.api:
            p.api._call(p.module.unload, p.api)
        if p.api:
            self._detach(p.api)
        p.module, p.api = None, None
        if p.status == "loaded":
            p.status = "not loaded"
        modname = f"aoh_plugin_{p.id}"
        for k in [k for k in sys.modules if k == modname or k.startswith(modname + ".")]:
            del sys.modules[k]
        if p.importer is not None:
            p.importer.uninstall()
            p.importer = None

    def reload(self, pid: str):
        p = self.plugins.get(pid)
        if not p:
            return
        self.unload(p)
        if p.archive:
            fresh = self.open_archive(p.archive, p.origin)
            if fresh.id != pid:
                self.plugins.pop(pid, None)
            self.plugins[fresh.id] = fresh
            if fresh.status not in ("error", "locked"):
                self.load(fresh)
            return
        try:
            p.meta = read_meta(p.folder)
        except Exception as e:  # noqa: BLE001
            p.status, p.error = "error", f"plugin.json: {e}"
            return
        self.load(p)

    def unlock(self, pid: str, password: str, fresh: Plugin | None = None) -> Plugin:
        """Try a password on a locked package; on success keep it (DPAPI-encrypted) and load the plugin.
        `fresh` = the result of open_archive() already done in a worker thread."""
        p = self.plugins[pid]
        if fresh is None:
            fresh = self.open_archive(p.archive, p.origin, password)
        if fresh.status == "locked":
            raise PP.WrongPassword()
        if fresh.status == "error":
            raise ValueError(fresh.error)
        self._store_password(p.archive, password)
        self.plugins.pop(pid, None)
        self.plugins[fresh.id] = fresh
        if fresh.status != "error" and fresh.id not in set(load_cfg().get("disabled", [])):
            self.load(fresh)
        return fresh

    def set_enabled(self, pid: str, on: bool):
        cfg = load_cfg()
        dis = set(cfg.get("disabled", []))
        (dis.discard if on else dis.add)(pid)
        cfg["disabled"] = sorted(dis)
        save_cfg(cfg)
        p = self.plugins.get(pid)
        if p:
            if on:
                self.load(p)
            else:
                self.unload(p)
                p.status = "disabled"

    # ---- UI glue ----------------------------------------------------------------
    def attach_action(self, a, toolbar: bool):
        if self.menu is not None:
            self.menu.insertAction(self.menu.actions()[0] if self.menu.actions() else None, a)
        if toolbar and self.toolbar is not None:
            self.toolbar.insertAction(self.toolbar_before, a)
            self.win.toolbar_changed()

    def _detach(self, api: PluginAPI):
        for a in api._actions:
            if self.menu is not None:
                self.menu.removeAction(a)
            if self.toolbar is not None:
                self.toolbar.removeAction(a)
                self.win.toolbar_changed()
            a.deleteLater()
        mine = f"plugin:{api.id}"
        for k in [k for k, v in self.icon_owners.items() if v == mine]:
            del self.icon_owners[k]
        api._actions.clear()
        api._file_actions.clear()
        api._handlers.clear()

    def actions(self):
        for p in self.plugins.values():
            if p.api:
                yield from p.api._actions

    def update_actions(self, connected: bool, root: bool, shell: bool):
        for a in self.actions():
            need = a.property("aoh_needs") or ""
            a.setEnabled(connected or not need)
            a.setVisible(need not in ("root", "shell") or not connected or root or (need == "shell" and shell))

    def file_actions(self, entries: list):
        """(label, fn, icon) for the file browser's context menu."""
        out = []
        for p in self.plugins.values():
            if not p.api:
                continue
            for title, cb, when, icon in p.api._file_actions:
                if when == "file" and (not entries or any(e.is_dir for e in entries)):
                    continue
                if when == "dir" and (not entries or not all(e.is_dir for e in entries)):
                    continue
                if when == "single" and len(entries) != 1:
                    continue
                out.append((title, (lambda cb=cb, api=p.api: api._call(cb, list(entries))), icon, p.api))
        return out

    def emit(self, event: str, *args):
        for p in self.plugins.values():
            if p.api:
                for ev, fn in list(p.api._handlers):
                    if ev == event:
                        p.api._call(fn, *args)

    # ---- install / create / remove / pack ----------------------------------------
    @staticmethod
    def check_package(src: str, password: str | None = None) -> dict:
        """Read and validate a package (in memory) – safe to call from a worker thread.
        Returns its files; raises PP.PasswordNeeded / PP.WrongPassword / PP.TooLarge / ValueError."""
        files = PP.read_archive(src, password)
        meta = json.loads(files["plugin.json"].decode("utf-8-sig"))
        if not isinstance(meta, dict) or not meta.get("id"):
            raise ValueError("plugin.json needs at least an \"id\"")
        return files

    def install(self, src: str, password: str | None = None, files: dict | None = None) -> str:
        """Install a .7z package (copied as it is – never unpacked) or pack a plugin folder into one.
        Raises PP.PasswordNeeded / PP.WrongPassword for protected packages. Returns the plugin id."""
        if os.path.isdir(src):
            meta = read_meta(src)
            pid = meta["id"]
            self._check_id(pid)
            dest = os.path.join(user_dir(), f"{pid}.7z")
            self._unload_id(pid)
            PP.pack(src, dest)
            self._store_password(dest, None)
        elif src.lower().endswith(".7z"):
            if files is None:
                files = self.check_package(src, password)   # validates the package (and the password) in memory
            meta = json.loads(files["plugin.json"].decode("utf-8-sig"))
            pid = meta["id"]
            self._check_id(pid)
            dest = os.path.join(user_dir(), f"{pid}.7z")
            self._unload_id(pid)
            if os.path.abspath(src) != os.path.abspath(dest):
                shutil.copyfile(src, dest + ".part")
                os.replace(dest + ".part", dest)
            self._store_password(dest, password)
        else:
            raise ValueError("Choose a plugin package (.7z) or a plugin folder")
        self.load_all()
        return pid

    @staticmethod
    def _check_id(pid: str):
        if not pid.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"bad plugin id {pid!r} (use letters, digits, _ and -)")

    def _unload_id(self, pid: str):
        old = self.plugins.get(pid)
        if old:
            self.unload(old)

    def remove(self, pid: str):
        p = self.plugins.get(pid)
        if not p:
            return
        if p.origin != "user":
            raise ValueError("Only installed packages can be removed here "
                             f"(this one is {'shipped with the program' if p.origin == 'bundled' else 'a development folder'}: "
                             f"{p.folder})")
        self.unload(p)
        if p.archive:
            os.remove(p.archive)
            self._store_password(p.archive, None)
        self.plugins.pop(pid, None)

    def create(self, name: str) -> str:
        """A new plugin from the template, in the development folder (Documents\\AndroUnderTheHood\\PluginDev)."""
        pid = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_") or "my_plugin"
        base = dev_default_dir()
        dest = os.path.join(base, pid)
        if os.path.exists(dest):
            raise ValueError(f"A plugin folder named {pid} already exists in {base}")
        os.makedirs(dest)
        with open(os.path.join(dest, "plugin.json"), "w", encoding="utf-8") as f:
            json.dump({"id": pid, "name": name, "version": "0.1", "author": "", "api": API_VERSION,
                       "description": "What this plugin does", "main": "plugin.py", "requires": []}, f, indent=2)
        with open(os.path.join(dest, "plugin.py"), "w", encoding="utf-8") as f:
            f.write(TEMPLATE.replace("{NAME}", name))
        cfg = load_cfg()
        dirs = cfg.setdefault("dev_dirs", [])
        if base not in dirs:
            dirs.append(base)
            save_cfg(cfg)
        self.load_all()
        return dest


TEMPLATE = '''"""{NAME} – an AndroUnderTheHood plugin. See PLUGINS.md for the full API.
Edit this file, then press Reload in the Plugins window – no restart needed. When it's ready, use
"Pack plugin…" to turn the folder into a .7z package (optionally password protected)."""


def register(api):
    # a toolbar button (only enabled while connected)
    api.add_tool("{NAME}", lambda: show(api), icon="star", tooltip="Run {NAME}", needs="connection")
    # an item in the file browser's right-click menu
    api.add_file_action("{NAME}: show size", lambda entries: api.message(
        ", ".join(f"{e.name}: {e.size} bytes" for e in entries)), when="file")
    api.on("connected", lambda conn: api.log.info("connected to %s", api.device_name))


def show(api):
    from PySide6.QtWidgets import QMessageBox

    def work():                       # runs in the background
        rc, out, err = api.run("getprop ro.product.model; uname -r")
        return out.strip()

    api.task(work, lambda text: QMessageBox.information(api.window, "{NAME}", text))
'''
