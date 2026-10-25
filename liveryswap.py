#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
FH6 Livery Swap - turn a livery downloaded from another player into your own.

Pick your profile save folder, pick a downloaded livery from the drop-down,
click "Make it mine". Everything else is automatic:

  * your account ID is read from the save (User_<HEX> folder)
  * your gamertag comes from any livery stamped with your ID (in the save or in
    backups\), else from settings.json, else you are asked once
  * the original folder is backed up to backups\ before it is changed
  * the folder name and livery GUID are kept, so cars keep their livery

What changes in the livery (no encryption on these files):
  header : creator XUID -> yours, gamertag -> yours, "downloaded" block
           (u32 1 + 8 zero + share GUID) -> zeros, like a livery you made
  C_livery (zlib): downloaded flag @0x08 -> 0, creator XUID @0x22 -> yours
  Vinyl groups embedded inside the livery keep their own author.

Liveries in backups\ that are no longer in the save are listed too, marked
[backup]; picking one writes the converted copy into the save.

CLI: python liveryswap.py <save folder> --list | --all | <Livery_folder_name>
Build: build.bat  (PyInstaller -> dist\LiverySwap.exe)
"""
import json
import re
import shutil
import struct
import subprocess
import sys
import zlib
from datetime import datetime
from pathlib import Path

APP_NAME = "FH6 Livery Swap"
__version__ = "1.0.0"

# HERE holds user data (backups, settings) - next to the .exe when packaged.
# RES holds bundled files (icon) - PyInstaller's unpack dir when packaged.
if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent
    RES = Path(getattr(sys, "_MEIPASS", HERE))
else:
    HERE = RES = Path(__file__).resolve().parent
BACKUPS = HERE / "backups"
SETTINGS = HERE / "settings.json"
PGS = Path(r"C:\XboxGames\GameSave\pgs")

LIVERY_RE = re.compile(r"^Livery_\d{4}_\d{14}$")
BODY_FLAG_OFF = 0x08
BODY_XUID_OFF = 0x22
DL_BLOCK_LEN = 4 + 8 + 16          # u32 downloaded + 8 zero + share GUID


# ---------------------------------------------------------------- format

def _read_wstr(b, o):
    n = struct.unpack_from("<I", b, o)[0]
    if n > 4096:
        raise ValueError(f"bad string length {n} at 0x{o:X}")
    return b[o + 4:o + 4 + 2 * n].decode("utf-16le"), o + 4 + 2 * n


def _wstr(s):
    return struct.pack("<I", len(s)) + s.encode("utf-16le")


class Livery:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.name = self.folder.name
        self.header = (self.folder / "header").read_bytes()
        c = (self.folder / "C_livery").read_bytes()

        h = self.header
        if struct.unpack_from("<I", h, 0)[0] != 7:
            raise ValueError("unsupported header version")
        self.title, o = _read_wstr(h, 4)
        self.desc, o = _read_wstr(h, o)
        o += 16 + 4                                   # SYSTEMTIME + type
        self.hdr_xuid_off = o
        self.hdr_xuid = struct.unpack_from("<Q", h, o)[0]
        self.gt_off = o + 8
        self.gamertag, self.gt_end = _read_wstr(h, self.gt_off)

        comp, raw = struct.unpack_from("<II", c, 0)
        self.raw = zlib.decompress(c[8:8 + comp])
        if len(self.raw) != raw or self.raw[:4] != b"vlrc" or self.raw[0x1A:0x1E] != b"yrvl":
            raise ValueError("unexpected C_livery layout")
        self.downloaded = struct.unpack_from("<I", self.raw, BODY_FLAG_OFF)[0] == 1
        self.body_xuid = struct.unpack_from("<Q", self.raw, BODY_XUID_OFF)[0]
        self.car_id = int(self.name.split("_")[1])

    @property
    def creator(self):
        return self.body_xuid or self.hdr_xuid

    def as_own(self, xuid, gamertag):
        """(header, C_livery) bytes for this livery owned by xuid/gamertag."""
        h = bytearray(self.header)
        struct.pack_into("<Q", h, self.hdr_xuid_off, xuid)
        dl = self.gt_end                               # downloaded block follows the gamertag
        if struct.unpack_from("<I", h, dl)[0] == 1:
            h[dl:dl + DL_BLOCK_LEN] = bytes(DL_BLOCK_LEN)
        h[self.gt_off:self.gt_end] = _wstr(gamertag)

        raw = bytearray(self.raw)
        struct.pack_into("<I", raw, BODY_FLAG_OFF, 0)
        struct.pack_into("<Q", raw, BODY_XUID_OFF, xuid)
        comp = zlib.compress(bytes(raw), 6)
        return bytes(h), struct.pack("<II", len(comp), len(raw)) + comp


def scan(folder: Path):
    out = []
    if not folder.is_dir():
        return out
    for d in sorted(folder.iterdir()):
        if d.is_dir() and LIVERY_RE.match(d.name):
            try:
                out.append(Livery(d))
            except Exception:                          # noqa: BLE001
                pass
    return out


# ---------------------------------------------------------------- save folder

class Save:
    """Resolve any folder inside an FH6 PGS save to its ContainersRoot + account."""

    def __init__(self, folder):
        f = Path(folder)
        if (f / "current").exists():                   # u_<dec>_16D460
            f = (f / "current").resolve()
        if (f / "ContainersRoot").is_dir():            # <version>
            f = f / "ContainersRoot"
        if f.name.startswith("User_") and f.parent.name == "ContainersRoot":
            f = f.parent
        if f.name != "ContainersRoot":
            raise ValueError(f"Not an FH6 save folder:\n{folder}")
        self.root = f
        users = [d for d in f.iterdir() if re.match(r"^User_[0-9A-Fa-f]+$", d.name)]
        if not users:
            raise ValueError("No User_<ID> folder in this save.")
        self.user_dir = users[0]
        self.xuid = int(self.user_dir.name[5:], 16)
        self.gamertag = self._find_gamertag()

    def _find_gamertag(self):
        places = [self.root] + sorted(BACKUPS.glob("*"), reverse=True)
        for place in places:
            for lv in scan(place):
                if lv.creator == self.xuid and not lv.downloaded and lv.gamertag:
                    return lv.gamertag
        return load_settings().get(str(self.xuid), "")

    def candidates(self):
        """Downloaded liveries: in the save, plus ones only left in backups."""
        here = [lv for lv in scan(self.root) if lv.downloaded]
        names = {lv.name for lv in scan(self.root)}
        seen, extra = set(), []
        for b in sorted(BACKUPS.glob("*"), reverse=True):
            for lv in scan(b):
                if lv.downloaded and lv.name not in names and lv.name not in seen:
                    seen.add(lv.name)
                    extra.append(lv)
        return here, extra

    def make_mine(self, lv: Livery, log=print):
        if not self.gamertag:
            raise ValueError("No gamertag set.")
        dest = self.root / lv.name
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if dest.exists():
            bdir = BACKUPS / f"before_make_mine_{stamp}"
            bdir.mkdir(parents=True, exist_ok=True)
            if not (bdir / lv.name).exists():
                shutil.copytree(dest, bdir / lv.name)
        h, c = lv.as_own(self.xuid, self.gamertag)
        tmp = self.root / (lv.name + ".liveryswap_tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(lv.folder, tmp)
        (tmp / "header").write_bytes(h)
        (tmp / "C_livery").write_bytes(c)
        check = Livery(tmp)                            # verify before swapping in
        assert check.creator == self.xuid and not check.downloaded
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
        log(f"'{lv.title}' ({lv.name}) by {lv.gamertag} is now yours as {self.gamertag}.")


def resource(name):
    return RES / name


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except Exception:                                  # noqa: BLE001
        return {}


def save_gamertag(xuid, gt):
    s = load_settings()
    s[str(xuid)] = gt
    try:
        SETTINGS.write_text(json.dumps(s, indent=1))
    except OSError:                                    # read-only install dir: just don't remember
        pass


def default_save_folder():
    """Most recently used FH6 save on this PC."""
    try:
        roots = [d for d in PGS.iterdir() if d.name.endswith("_16D460") and (d / "current").exists()]
        return str(max(roots, key=lambda d: d.stat().st_mtime)) if roots else ""
    except Exception:                                  # noqa: BLE001
        return ""


def game_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq forzahorizon6.exe"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return "forzahorizon6.exe" in out.lower()
    except Exception:                                  # noqa: BLE001
        return False


# ---------------------------------------------------------------- GUI

# Black / grey palette. Green and red only mark lines in the activity log.
C = {
    "bg": "#0b0b0b",
    "surface": "#141414",
    "field": "#1c1c1c",
    "hover": "#262626",
    "select": "#333333",
    "border": "#2a2a2a",
    "border_hi": "#555555",
    "text": "#e8e8e8",
    "muted": "#8c8c8c",
    "faint": "#5a5a5a",
    "primary": "#e8e8e8",
    "primary_hover": "#ffffff",
    "primary_press": "#bdbdbd",
    "primary_text": "#0b0b0b",
    "ok": "#8fd18f",
    "err": "#ef7d7d",
}


def _dpi_aware():
    """Sharp text on high-DPI screens (Windows)."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                  # noqa: BLE001
        pass


def _dark_titlebar(w):
    """Black native title bar on Windows 10/11; ignored elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        w.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(w.winfo_id())
        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute

        def setattr_(attr, value):
            v = ctypes.c_int(value)
            return dwm(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        if setattr_(20, 1) != 0:                       # DWMWA_USE_IMMERSIVE_DARK_MODE
            setattr_(19, 1)                            # older Windows 10 builds

        def bgr(hex_):
            r, g, b = int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)
            return (b << 16) | (g << 8) | r

        setattr_(35, bgr(C["bg"]))                     # caption colour (Windows 11)
        setattr_(36, bgr(C["text"]))                   # caption text
        setattr_(34, bgr(C["border"]))                 # window border
    except Exception:                                  # noqa: BLE001
        pass


def run_gui():
    import os
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, ttk
    from tkinter import font as tkfont

    _dpi_aware()
    win = tk.Tk()
    win.withdraw()
    win.title(APP_NAME)
    win.configure(bg=C["bg"])
    icon = resource("icon.ico")
    if icon.exists():
        try:
            win.iconbitmap(default=str(icon))
        except Exception:                              # noqa: BLE001
            pass

    families = set(tkfont.families(win))
    UI = "Segoe UI" if "Segoe UI" in families else "TkDefaultFont"
    MONO = next((f for f in ("Cascadia Mono", "Consolas") if f in families), "TkFixedFont")
    F_BODY, F_SMALL = (UI, 10), (UI, 9)
    F_LABEL = (UI, 8, "bold")
    F_TITLE = (UI, 13, "bold")

    # ------------------------------------------------------------ theme
    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure(".", background=C["bg"], foreground=C["text"], font=F_BODY,
                    bordercolor=C["border"], lightcolor=C["border"], darkcolor=C["border"],
                    troughcolor=C["field"], focuscolor=C["border_hi"],
                    selectbackground=C["select"], selectforeground=C["text"],
                    insertcolor=C["text"])

    style.configure("TEntry", fieldbackground=C["field"], foreground=C["text"],
                    insertcolor=C["text"], padding=(10, 7))
    style.map("TEntry",
              bordercolor=[("focus", C["border_hi"]), ("hover", C["faint"])],
              lightcolor=[("focus", C["border_hi"])],
              darkcolor=[("focus", C["border_hi"])],
              fieldbackground=[("disabled", C["surface"])],
              foreground=[("disabled", C["faint"])])

    style.configure("TCombobox", fieldbackground=C["field"], background=C["field"],
                    foreground=C["text"], arrowcolor=C["muted"], arrowsize=14,
                    padding=(10, 7))
    style.map("TCombobox",
              fieldbackground=[("disabled", C["surface"]), ("readonly", C["field"])],
              background=[("disabled", C["surface"]), ("active", C["hover"]),
                          ("readonly", C["field"])],
              foreground=[("disabled", C["faint"]), ("readonly", C["text"])],
              arrowcolor=[("disabled", C["faint"]), ("active", C["text"])],
              bordercolor=[("focus", C["border_hi"]), ("hover", C["faint"])],
              lightcolor=[("focus", C["border_hi"])],
              darkcolor=[("focus", C["border_hi"])],
              selectbackground=[("readonly", C["field"])],
              selectforeground=[("readonly", C["text"])])
    style.configure("ComboboxPopdownFrame", background=C["border_hi"], relief="solid",
                    borderwidth=1)
    # The drop-down list is a plain Tk listbox, styled through the option database.
    win.option_add("*TCombobox*Listbox.background", C["field"])
    win.option_add("*TCombobox*Listbox.foreground", C["text"])
    win.option_add("*TCombobox*Listbox.selectBackground", C["select"])
    win.option_add("*TCombobox*Listbox.selectForeground", C["text"])
    win.option_add("*TCombobox*Listbox.font", F_BODY)
    win.option_add("*TCombobox*Listbox.relief", "flat")
    win.option_add("*TCombobox*Listbox.borderWidth", 6)
    win.option_add("*TCombobox*Listbox.highlightThickness", 0)
    win.option_add("*TCombobox*Listbox.activeStyle", "none")

    # Drop clam's dotted focus box; keyboard focus shows as a brighter border instead.
    style.layout("TButton", [("Button.border", {"sticky": "nswe", "border": "1", "children": [
        ("Button.padding", {"sticky": "nswe", "children": [
            ("Button.label", {"sticky": "nswe"})]})]})])
    style.configure("TButton", background=C["field"], foreground=C["text"],
                    padding=(14, 7), borderwidth=1, font=F_BODY)
    style.map("TButton",
              background=[("disabled", C["surface"]), ("pressed", C["select"]),
                          ("active", C["hover"])],
              foreground=[("disabled", C["faint"])],
              bordercolor=[("focus", C["border_hi"]), ("active", C["faint"])],
              lightcolor=[("pressed", C["select"]), ("active", C["hover"])],
              darkcolor=[("pressed", C["select"]), ("active", C["hover"])])

    style.configure("Primary.TButton", background=C["primary"], foreground=C["primary_text"],
                    bordercolor=C["primary"], lightcolor=C["primary"], darkcolor=C["primary"],
                    padding=(22, 9), font=(UI, 10, "bold"))
    style.map("Primary.TButton",
              background=[("disabled", C["hover"]), ("pressed", C["primary_press"]),
                          ("active", C["primary_hover"])],
              foreground=[("disabled", C["faint"])],
              bordercolor=[("disabled", C["hover"]), ("pressed", C["primary_press"]),
                           ("active", C["primary_hover"]), ("focus", C["muted"])],
              lightcolor=[("disabled", C["hover"]), ("pressed", C["primary_press"]),
                          ("active", C["primary_hover"])],
              darkcolor=[("disabled", C["hover"]), ("pressed", C["primary_press"]),
                         ("active", C["primary_hover"])])

    style.configure("Vertical.TScrollbar", background=C["border"], troughcolor=C["field"],
                    bordercolor=C["field"], lightcolor=C["border"], darkcolor=C["border"],
                    arrowcolor=C["muted"], gripcount=0, arrowsize=11)
    style.map("Vertical.TScrollbar", background=[("active", C["faint"])])

    def label(parent, text="", var=None, font=F_SMALL, fg=C["muted"], bg=C["bg"], **kw):
        return tk.Label(parent, text=text, textvariable=var, font=font, fg=fg, bg=bg,
                        anchor="w", justify="left", **kw)

    def section(parent, text):
        return label(parent, text.upper(), font=F_LABEL, fg=C["faint"])

    def center(w, over=None, size=None):
        w.update_idletasks()
        ww, wh = size or (w.winfo_reqwidth(), w.winfo_reqheight())
        if over is not None and over.winfo_viewable():
            x = over.winfo_rootx() + (over.winfo_width() - ww) // 2
            y = over.winfo_rooty() + (over.winfo_height() - wh) // 3
        else:
            x = (w.winfo_screenwidth() - ww) // 2
            y = (w.winfo_screenheight() - wh) // 3
        w.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def dialog(title, message, ask=False, initial="", confirm=None):
        """Dark modal. ask -> returns text or None; confirm -> True/False; else None."""
        d = tk.Toplevel(win)
        d.withdraw()
        d.title(APP_NAME)
        d.configure(bg=C["bg"])
        d.resizable(False, False)
        d.transient(win)
        body = tk.Frame(d, bg=C["bg"], padx=24, pady=20)
        body.pack(fill="both", expand=True)
        label(body, title, font=(UI, 11, "bold"), fg=C["text"]).pack(anchor="w")
        label(body, message, font=F_BODY, wraplength=400).pack(anchor="w", pady=(6, 16))
        var = tk.StringVar(value=initial)
        result = {"v": None if (ask or confirm is None) else False}
        if ask:
            e = ttk.Entry(body, textvariable=var, width=42)
            e.pack(fill="x", pady=(0, 18))
            e.focus_set()
            e.icursor("end")

        def ok(_=None):
            if ask:
                if not var.get().strip():
                    return
                result["v"] = var.get().strip()
            elif confirm is not None:
                result["v"] = True
            d.destroy()

        btns = tk.Frame(body, bg=C["bg"])
        btns.pack(fill="x")
        ttk.Button(btns, text=confirm or "OK", style="Primary.TButton",
                   command=ok).pack(side="right")
        if ask or confirm is not None:
            ttk.Button(btns, text="Cancel", command=d.destroy).pack(side="right", padx=(0, 8))
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda _: d.destroy())
        center(d, win)
        _dark_titlebar(d)
        d.deiconify()
        d.grab_set()
        win.wait_window(d)
        return result["v"]

    # ------------------------------------------------------------ layout
    st = {"save": None, "items": [], "busy": False, "thumb": None}
    folder_var, pick_var = tk.StringVar(), tk.StringVar()
    account_var, detail_var = tk.StringVar(), tk.StringVar()
    status_var = tk.StringVar(value="Ready")

    header = tk.Frame(win, bg=C["surface"])
    header.pack(fill="x")
    label(header, APP_NAME, font=F_TITLE, fg=C["text"], bg=C["surface"]).pack(
        side="left", padx=(22, 10), pady=14)
    label(header, "Make downloaded liveries your own", bg=C["surface"]).pack(
        side="left", pady=(4, 0))
    label(header, f"v{__version__}", fg=C["faint"], bg=C["surface"]).pack(
        side="right", padx=22)
    tk.Frame(win, bg=C["border"], height=1).pack(fill="x")

    footer = tk.Frame(win, bg=C["surface"])
    footer.pack(fill="x", side="bottom")
    tk.Frame(win, bg=C["border"], height=1).pack(fill="x", side="bottom")
    label(footer, "Close Forza Horizon 6 before applying. Originals are backed up first.",
          bg=C["surface"], fg=C["faint"]).pack(side="left", padx=22, pady=8)
    label(footer, var=status_var, bg=C["surface"], fg=C["muted"]).pack(side="right", padx=22)

    body = tk.Frame(win, bg=C["bg"], padx=22, pady=18)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(6, weight=1)

    # Save folder
    section(body, "Profile save folder").grid(row=0, column=0, columnspan=2, sticky="w")
    row = tk.Frame(body, bg=C["bg"])
    row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 4))
    row.columnconfigure(0, weight=1)
    entry = ttk.Entry(row, textvariable=folder_var)
    entry.grid(row=0, column=0, sticky="ew")
    browse_btn = ttk.Button(row, text="Browse…")
    browse_btn.grid(row=0, column=1, padx=(8, 0))
    reload_btn = ttk.Button(row, text="Reload")
    reload_btn.grid(row=0, column=2, padx=(8, 0))
    account_lbl = label(body, var=account_var)
    account_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 16))

    # Livery picker + preview
    section(body, "Downloaded livery").grid(row=3, column=0, columnspan=2, sticky="w")
    pick = tk.Frame(body, bg=C["bg"])
    pick.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 16))
    pick.columnconfigure(0, weight=1)

    left = tk.Frame(pick, bg=C["bg"])
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
    left.columnconfigure(0, weight=1)
    left.rowconfigure(1, weight=1)
    combo = ttk.Combobox(left, textvariable=pick_var, state="readonly", height=14)
    combo.grid(row=0, column=0, sticky="ew")
    label(left, var=detail_var, wraplength=360).grid(row=1, column=0, sticky="nw", pady=(10, 0))
    actions = tk.Frame(left, bg=C["bg"])
    actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
    go_btn = ttk.Button(actions, text="Make it mine", style="Primary.TButton")
    go_btn.pack(side="right")
    backups_btn = ttk.Button(actions, text="Open backups")
    backups_btn.pack(side="left")

    TW, TH = 320, 180
    preview = tk.Frame(pick, bg=C["border"], padx=1, pady=1)
    preview.grid(row=0, column=1, sticky="ne")
    blank = tk.PhotoImage(width=TW, height=TH)        # fixes the label size in pixels
    thumb = tk.Label(preview, image=blank, text="No preview", compound="center",
                     bg=C["field"], fg=C["faint"], font=F_SMALL, bd=0)
    thumb.image = blank
    thumb.pack()

    # Activity log
    section(body, "Activity").grid(row=5, column=0, columnspan=2, sticky="w")
    logf = tk.Frame(body, bg=C["border"], padx=1, pady=1)
    logf.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
    logf.columnconfigure(0, weight=1)
    logf.rowconfigure(0, weight=1)
    log_txt = tk.Text(logf, bg=C["field"], fg=C["text"], font=(MONO, 9), relief="flat",
                      bd=0, padx=12, pady=10, height=7, wrap="word", highlightthickness=0,
                      insertbackground=C["text"], selectbackground=C["select"],
                      cursor="arrow")
    log_txt.grid(row=0, column=0, sticky="nsew")
    sb = ttk.Scrollbar(logf, orient="vertical", command=log_txt.yview)
    sb.grid(row=0, column=1, sticky="ns")
    log_txt.configure(yscrollcommand=sb.set, state="disabled")
    log_txt.tag_configure("time", foreground=C["faint"])
    log_txt.tag_configure("info", foreground=C["muted"])
    log_txt.tag_configure("ok", foreground=C["ok"])
    log_txt.tag_configure("err", foreground=C["err"])

    def log(msg, kind="info"):
        log_txt.configure(state="normal")
        log_txt.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        log_txt.insert("end", msg + "\n", kind)
        log_txt.configure(state="disabled")
        log_txt.see("end")

    win.report_callback_exception = lambda _t, e, _tb: log(f"Error: {e}", "err")

    # ------------------------------------------------------------ behaviour
    def set_preview(lv):
        img = None
        path = lv.folder / "bigThumb.webp" if lv else None
        if path and path.exists():
            try:
                from PIL import Image, ImageTk
                im = Image.open(path).convert("RGBA")
                im.thumbnail((TW, TH))
                canvas = Image.new("RGBA", (TW, TH), C["field"])
                canvas.alpha_composite(im, ((TW - im.width) // 2, (TH - im.height) // 2))
                img = ImageTk.PhotoImage(canvas)
            except Exception:                          # noqa: BLE001 - Pillow missing / bad file
                img = None
        thumb.configure(image=img or blank, text="" if img else "No preview")
        thumb.image = img or blank

    def on_select(_=None):
        i = combo.current()
        items = st["items"]
        if i < 0 or not items:
            detail_var.set("")
            set_preview(None)
            return
        if i == 0:
            detail_var.set(f"Converts all {len(items)} downloaded liveries at once.")
            set_preview(items[0] if len(items) == 1 else None)
            return
        lv = items[i - 1]
        where = ("In your save" if lv.folder.parent == st["save"].root
                 else "From backups. The converted copy is written into your save")
        detail_var.set(f"{lv.title or '(untitled)'}\n"
                       f"By {lv.gamertag or 'unknown'}  ·  Car {lv.car_id}\n"
                       f"{where}")
        set_preview(lv)

    def set_busy(busy):
        st["busy"] = busy
        s = "disabled" if busy else "!disabled"
        for w in (entry, browse_btn, reload_btn):
            w.state([s])
        combo.state(["disabled"] if busy or not st["items"] else ["!disabled", "readonly"])
        go_btn.state(["disabled"] if busy or not st["items"] else ["!disabled"])
        status_var.set("Working…" if busy else "Ready")
        win.configure(cursor="watch" if busy else "")

    def load(_=None):
        if st["busy"]:
            return
        path = folder_var.get().strip()
        st["save"], st["items"] = None, []
        combo["values"] = []
        pick_var.set("")
        if not path:
            account_var.set("Choose your FH6 save folder with Browse…")
            account_lbl.configure(fg=C["muted"])
            set_busy(False)
            on_select()
            return
        try:
            s = Save(path)
        except Exception as e:                         # noqa: BLE001
            account_var.set(str(e).replace("\n", " "))
            account_lbl.configure(fg=C["err"])
            set_busy(False)
            on_select()
            return
        st["save"] = s
        account_lbl.configure(fg=C["muted"])
        if not s.gamertag:
            gt = dialog("Enter your gamertag",
                        "Your gamertag couldn't be found in this save. Type it once and "
                        "it will be remembered for this account.", ask=True)
            if gt:
                s.gamertag = gt
                save_gamertag(s.xuid, gt)
        here, extra = s.candidates()
        st["items"] = here + extra
        labels = [f"{lv.title or '(untitled)'}   ·   {lv.gamertag or 'unknown'}   ·   "
                  f"car {lv.car_id}" for lv in here]
        labels += [f"[backup]  {lv.title or '(untitled)'}   ·   {lv.gamertag or 'unknown'}"
                   f"   ·   car {lv.car_id}" for lv in extra]
        if st["items"]:
            labels.insert(0, f"All downloaded liveries ({len(st['items'])})")
            combo["values"] = labels
            combo.current(1)
        else:
            pick_var.set("No downloaded liveries found")
        account_var.set(f"Account 0x{s.xuid:X}   ·   Gamertag {s.gamertag or 'not set'}   ·   "
                        f"{len(here)} downloaded in save, {len(extra)} more in backups")
        set_busy(False)
        on_select()

    def browse():
        start = folder_var.get().strip() or (str(PGS) if PGS.exists() else None)
        p = filedialog.askdirectory(parent=win, title="Select your FH6 save folder",
                                    initialdir=start if start and Path(start).exists() else None)
        if p:
            folder_var.set(os.path.normpath(p))
            load()

    def open_backups():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(BACKUPS))                 # noqa: S606 - Windows only
        except Exception as e:                         # noqa: BLE001
            log(f"Couldn't open {BACKUPS}: {e}", "err")

    q = queue.Queue()

    def work(save, chosen):
        n_ok = 0
        for lv in chosen:
            try:
                save.make_mine(lv, lambda m: q.put(("ok", m)))
                n_ok += 1
            except Exception as e:                     # noqa: BLE001
                q.put(("err", f"Failed {lv.name}: {e}"))
        q.put(("done", (n_ok, len(chosen))))

    def poll():
        try:
            while True:
                kind, msg = q.get_nowait()
                if kind == "done":
                    n_ok, n = msg
                    st["busy"] = False
                    load()
                    status_var.set(f"Done: {n_ok} of {n} converted")
                    return
                log(msg, kind)
        except queue.Empty:
            pass
        win.after(80, poll)

    def go():
        s = st["save"]
        if st["busy"] or not s or not st["items"]:
            return
        i = combo.current()
        if i < 0:
            return
        chosen = st["items"] if i == 0 else [st["items"][i - 1]]
        if game_running():
            dialog("Forza Horizon 6 is running",
                   "Close the game completely, then try again. Changing the save while "
                   "the game is open could get overwritten or corrupt it.")
            return
        if not s.gamertag:
            gt = dialog("Enter your gamertag", "A gamertag is needed to sign the livery.",
                        ask=True)
            if not gt:
                return
            s.gamertag = gt
            save_gamertag(s.xuid, gt)
        if len(chosen) > 1 and not dialog(
                f"Convert {len(chosen)} liveries?",
                f"Every downloaded livery will be signed as {s.gamertag}. "
                "Originals are copied to the backups folder first.", confirm="Convert all"):
            return
        set_busy(True)
        log(f"Converting {len(chosen)} {'livery' if len(chosen) == 1 else 'liveries'}…")
        threading.Thread(target=work, args=(s, chosen), daemon=True).start()
        poll()

    browse_btn.configure(command=browse)
    reload_btn.configure(command=load)
    backups_btn.configure(command=open_backups)
    go_btn.configure(command=go)
    entry.bind("<Return>", load)
    combo.bind("<<ComboboxSelected>>", lambda _: (combo.selection_clear(), on_select()))

    # ------------------------------------------------------------ start
    folder_var.set(default_save_folder())
    log("Pick a downloaded livery and click Make it mine.")
    win.update_idletasks()
    scale = win.winfo_fpixels("1i") / 96
    win.minsize(win.winfo_reqwidth(), win.winfo_reqheight())
    size = (max(win.winfo_reqwidth(), int(860 * scale)),
            max(win.winfo_reqheight(), int(640 * scale)))
    win.geometry(f"{size[0]}x{size[1]}")
    center(win, size=size)
    _dark_titlebar(win)
    win.deiconify()
    win.after(30, load)
    win.mainloop()


# ---------------------------------------------------------------- CLI

def run_cli(argv):
    s = Save(argv[0])
    here, extra = s.candidates()
    items = here + extra
    print(f"account 0x{s.xuid:X}, gamertag '{s.gamertag}'")
    if len(argv) < 2 or argv[1] == "--list":
        for lv in here:
            print(f"  {lv.name}  '{lv.title}' by {lv.gamertag}")
        for lv in extra:
            print(f"  [backup] {lv.name}  '{lv.title}' by {lv.gamertag}")
        return
    if game_running():
        sys.exit("Close FH6 first.")
    chosen = items if argv[1] == "--all" else [lv for lv in items if lv.name == argv[1]]
    if not chosen:
        sys.exit("No such downloaded livery.")
    for lv in chosen:
        s.make_mine(lv)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_gui()
