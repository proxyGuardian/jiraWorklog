# jira_worklog_gui.py
# GUI na logovanie času do Jira s viacerými tiketmi a váhami, výberom rozsahu,
# ukladaním hesla, výberom ktoré tikety trackovať (checkboxy), dvojklikové editovanie,
# master checkbox, náhodný výber podmnožiny tiketov na každý deň (počet/deň),
# stĺpec "Názov" (expanduje na zvyšok šírky) a po dokončení sa prehliadač zavrie + status.
# Oprava: presné dorovnanie na 8h/deň (480 min) aj pri náhodnom výbere a zaokrúhľovaní.

import os
import json
import base64
import random
import datetime as dt
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from collections import defaultdict


# --- Optional: bezpečné uloženie hesla ---
try:
    import keyring  # pip install keyring
except Exception:
    keyring = None

# --- Optional: kalendár pre výber rozsahu ---
try:
    from tkcalendar import Calendar  # pip install tkcalendar
except Exception:
    Calendar = None

# --- Selenium ---
from selenium import webdriver  # pip install selenium
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ================== KONFIGURÁCIA ==================
JIRA_URL = "https://jira.cargo-partner.com"
DEFAULT_USERNAME = ""
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".jira_logger_config.json")

TIME_TRACKING_URL = ""
TIME_TRACKING_TOKEN = ""  # token na kontrolnej stránke

# Slovenské sviatky 2025
SK_HOLIDAYS_2025 = {
    dt.date(2025, 1, 1), dt.date(2025, 1, 6),
    dt.date(2025, 4, 18), dt.date(2025, 4, 21),
    dt.date(2025, 5, 1),  dt.date(2025, 5, 8),
    dt.date(2025, 7, 5),  dt.date(2025, 8, 29),
    dt.date(2025, 9, 1),  dt.date(2025, 9, 15),
    dt.date(2025, 11, 1), dt.date(2025, 11, 17),
    dt.date(2025, 12, 24),dt.date(2025, 12, 25), dt.date(2025, 12, 26),
}


# ===== Pomocné funkcie – config & heslá =====
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        messagebox.showwarning("Uloženie zlyhalo", f"Nepodarilo sa uložiť konfiguráciu:\n{e}")


def get_saved_password(username: str):
    if keyring:
        try:
            val = keyring.get_password("jira_worklog", username)
            if val:
                return val
        except Exception:
            pass
    cfg = load_config()
    raw = cfg.get("saved_passwords", {}).get(username, "")
    if raw:
        try:
            return base64.b64decode(raw.encode("utf-8")).decode("utf-8")
        except Exception:
            return ""
    return ""


def set_saved_password(username: str, password: str):
    if not username:
        return
    if keyring:
        try:
            keyring.set_password("jira_worklog", username, password)
            return
        except Exception:
            pass
    cfg = load_config()
    sp = cfg.get("saved_passwords", {})
    if password:
        sp[username] = base64.b64encode(password.encode("utf-8")).decode("utf-8")
    else:
        sp.pop(username, None)
    cfg["saved_passwords"] = sp
    save_config(cfg)


def clear_saved_password(username: str):
    if not username:
        return
    if keyring:
        try:
            keyring.delete_password("jira_worklog", username)
        except Exception:
            pass
    cfg = load_config()
    sp = cfg.get("saved_passwords", {})
    if username in sp:
        del sp[username]
        cfg["saved_passwords"] = sp
        save_config(cfg)


# ===== Pomocné funkcie – dátumy, rozdelenie času =====
def format_jira_date(date_obj: dt.date) -> str:
    return date_obj.strftime("%d/%b/%y")  # napr. 19/Aug/25


def working_days(start: dt.date, end: dt.date, skip_weekends=True, skip_sk_holidays=True):
    d = start
    out = []
    while d <= end:
        if (not skip_weekends or d.weekday() < 5) and (not skip_sk_holidays or d not in SK_HOLIDAYS_2025):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def minutes_to_jira_time(m: int) -> str:
    h = m // 60
    rem = m % 60
    if h and rem:
        return f"{h}h {rem}m"
    if h:
        return f"{h}h"
    return f"{rem}m"


def proportional_split(total_minutes, weights, round_to=15):
    """Rozdelí total_minutes podľa váh v krokoch round_to minút a doladí súčet k cieľu."""
    if not weights or sum(weights) == 0:
        n = len(weights)
        if n == 0:
            return []
        base = total_minutes // n
        res = [base] * n
        for i in range(total_minutes - base * n):
            res[i % n] += 1
    else:
        s = sum(weights)
        raw = [total_minutes * w / s for w in weights]
        res = [int(round(x / round_to) * round_to) for x in raw]
        diff = total_minutes - sum(res)
        step = round_to if diff > 0 else -round_to
        i = 0
        while diff != 0 and len(res) > 0:
            new_val = res[i] + step
            if new_val >= 0:
                res[i] = new_val
                diff -= step
            i = (i + 1) % len(res)
    return res


def start_of_week(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def end_of_week(d: dt.date) -> dt.date:
    return start_of_week(d) + dt.timedelta(days=4)  # Po-Pia


def first_day_of_month(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def last_day_of_month(d: dt.date) -> dt.date:
    if d.month == 12:
        return dt.date(d.year, 12, 31)
    next_month = dt.date(d.year, d.month + 1, 1)
    return next_month - dt.timedelta(days=1)


# ===== Hlavná aplikácia (Tkinter GUI) =====
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Jira Worklog – Multi-ticket Tracker")
        self.geometry("980x880")
        self.resizable(False, False)

        self.cfg = load_config()

        # Stavové premenné
        self.username_var = tk.StringVar(value=self.cfg.get("username", DEFAULT_USERNAME))
        self.password_var = tk.StringVar(value=get_saved_password(self.username_var.get()))
        self.save_password_var = tk.BooleanVar(value=self.cfg.get("save_password", False))
        self.remember_settings_var = tk.BooleanVar(value=True)
        self.open_tracking_var = tk.BooleanVar(value=False)

        today = dt.date.today()
        default_start = self.cfg.get("start_date") or first_day_of_month(today).strftime("%d.%m.%Y")
        default_end = self.cfg.get("end_date") or last_day_of_month(today).strftime("%d.%m.%Y")
        self.start_var = tk.StringVar(value=default_start)
        self.end_var = tk.StringVar(value=default_end)

        self.skip_weekends_var = tk.BooleanVar(value=True)
        self.skip_holidays_var = tk.BooleanVar(value=True)

        # Tikety a váhy (track default True, name voliteľný)
        self.tickets = self.cfg.get("tickets", [{"issue": "147331", "name": "Môj task", "weight": 1, "track": True}])
        for t in self.tickets:
            if "track" not in t:
                t["track"] = True
            if "name" not in t:
                t["name"] = ""

        # Náhodný výber tiketov / deň (default ON, počet=2)
        self.randomize_var = tk.BooleanVar(value=self.cfg.get("randomize_enabled", True))
        self.randomize_k_var = tk.IntVar(value=self.cfg.get("randomize_k", 2))

        # Editor overlay pre dvojklik
        self._edit_entry = None
        self._edit_item = None
        self._edit_col = None

        self._build_ui()

        # Reakcie na zmeny používateľa/hesla/checkboxu
        self.username_var.trace_add("write", self._on_username_change)
        self.password_var.trace_add("write", self._on_password_change)
        self.save_password_var.trace_add("write", self._on_save_password_toggle)

        # Hook na zavretie okna – uloženie konfigurácie a (ak je zaškrtnuté) hesla
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # --- Prihlásenie ---
        fr_auth = ttk.LabelFrame(self, text="Prihlásenie do Jira")
        fr_auth.place(x=10, y=10, width=960, height=120)

        ttk.Label(fr_auth, text="Používateľ:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(fr_auth, textvariable=self.username_var, width=24).grid(row=0, column=1, **pad)

        ttk.Label(fr_auth, text="Heslo:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(fr_auth, textvariable=self.password_var, width=24, show="•").grid(row=0, column=3, **pad)

        ttk.Checkbutton(fr_auth, text="Uložiť heslo", variable=self.save_password_var).grid(row=1, column=1, sticky="w", **pad)
        ttk.Checkbutton(fr_auth, text="Pamätať nastavenia", variable=self.remember_settings_var).grid(row=1, column=3, sticky="w", **pad)

        # --- Obdobie ---
        fr_dates = ttk.LabelFrame(self, text="Obdobie")
        fr_dates.place(x=10, y=140, width=960, height=140)

        ttk.Label(fr_dates, text="Od (dd.mm.rrrr):").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(fr_dates, textvariable=self.start_var, width=16).grid(row=0, column=1, **pad)

        ttk.Label(fr_dates, text="Do (dd.mm.rrrr):").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(fr_dates, textvariable=self.end_var, width=16).grid(row=0, column=3, **pad)

        ttk.Button(fr_dates, text="Dnes", command=self.set_today).grid(row=1, column=0, **pad)
        ttk.Button(fr_dates, text="Tento týždeň", command=self.set_this_week).grid(row=1, column=1, **pad)
        ttk.Button(fr_dates, text="Tento mesiac", command=self.set_this_month).grid(row=1, column=2, **pad)
        ttk.Button(fr_dates, text="Kalendár…", command=self.open_calendar_dialog).grid(row=1, column=3, **pad)

        ttk.Checkbutton(fr_dates, text="Preskočiť víkendy", variable=self.skip_weekends_var).grid(row=2, column=0, sticky="w", **pad)
        ttk.Checkbutton(fr_dates, text="Preskočiť SK sviatky", variable=self.skip_holidays_var).grid(row=2, column=1, sticky="w", **pad)

        # --- Tikety a váhy ---
        fr_tickets = ttk.LabelFrame(self, text="Tikety a váhy (8h/deň sa rozdelí podľa váh; trackuje sa len označené)")
        fr_tickets.place(x=10, y=290, width=960, height=520)

        # Horná lišta: master checkbox + náhodný výber
        topbar = ttk.Frame(fr_tickets)
        topbar.grid(row=0, column=0, columnspan=4, padx=8, pady=(8, 0), sticky="w")

        self.master_track_var = tk.BooleanVar(value=all(t.get("track", True) for t in self.tickets))
        ttk.Checkbutton(topbar, text="Označiť všetky (track)", variable=self.master_track_var,
                        command=lambda: self._set_all_track(self.master_track_var.get())).grid(row=0, column=0, padx=(0, 16))

        ttk.Checkbutton(topbar, text="Náhodne vyberať tikety každý deň", variable=self.randomize_var)\
            .grid(row=0, column=1, padx=(0, 8))
        ttk.Label(topbar, text="Počet tiketov / deň:").grid(row=0, column=2, padx=(8, 4))
        self.spin_k = tk.Spinbox(topbar, from_=1, to=50, width=5, textvariable=self.randomize_k_var)
        self.spin_k.grid(row=0, column=3, padx=(0, 8))

        # Treeview so stĺpcom "Názov"
        columns = ("track", "issue", "name", "weight")
        self.tree = ttk.Treeview(fr_tickets, columns=columns, show="headings", height=12)

        self.tree.heading("track", text="Trackovať", command=lambda: self._tree_sort("track"))
        self.tree.heading("issue", text="Issue ID", command=lambda: self._tree_sort("issue"))
        self.tree.heading("name",  text="Názov", command=lambda: self._tree_sort("name"))
        self.tree.heading("weight",text="Váha", command=lambda: self._tree_sort("weight"))


        # fixné stĺpce: len podľa textu
        self.tree.column("track", width=90, anchor="center", stretch=False)
        self.tree.column("issue", width=120, anchor="center", stretch=False)
        self.tree.column("weight", width=70, anchor="center", stretch=False)
        # názov vyplní zvyšok
        self.tree.column("name", anchor="w", stretch=True)

        self.tree.grid(row=1, column=0, columnspan=4, padx=8, pady=(4, 4), sticky="nsew")

        # aby sa názov mohol roztiahnuť
        fr_tickets.grid_columnconfigure(0, weight=1)
        fr_tickets.grid_rowconfigure(1, weight=1)

        vsb = ttk.Scrollbar(fr_tickets, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=1, column=4, sticky="ns", pady=(4, 4))

        for t in self.tickets:
            self.tree.insert(
                "", "end",
                values=("☑" if t.get("track", True) else "☐", t.get("issue", ""), t.get("name", ""), t.get("weight", 1))
            )

        # spodná pridávacia časť (vnútorný rámik -> zarovnanie naľavo)
        self.new_issue_var = tk.StringVar()
        self.new_name_var = tk.StringVar()
        self.new_weight_var = tk.StringVar(value="1")

        fr_add = ttk.Frame(fr_tickets)
        fr_add.grid(row=2, column=0, columnspan=4, padx=8, pady=6, sticky="w")

        ttk.Label(fr_add, text="Nový Issue ID:").grid(row=0, column=0, padx=(0, 8), pady=0, sticky="e")
        ent_issue = ttk.Entry(fr_add, textvariable=self.new_issue_var, width=16)
        ent_issue.grid(row=0, column=1, padx=(0, 16), pady=0, sticky="w")

        ttk.Label(fr_add, text="Názov:").grid(row=0, column=2, padx=(0, 8), pady=0, sticky="e")
        ent_name = ttk.Entry(fr_add, textvariable=self.new_name_var, width=40)
        ent_name.grid(row=0, column=3, padx=(0, 16), pady=0, sticky="w")

        ttk.Label(fr_add, text="Váha:").grid(row=0, column=4, padx=(0, 8), pady=0, sticky="e")
        ent_weight = ttk.Entry(fr_add, textvariable=self.new_weight_var, width=8)
        ent_weight.grid(row=0, column=5, padx=(0, 16), pady=0, sticky="w")

        ttk.Button(fr_add, text="Pridať", command=self.add_ticket)\
            .grid(row=0, column=6, padx=(0, 8), pady=0, sticky="w")
        ttk.Button(fr_add, text="Odstrániť vybrané", command=self.remove_selected)\
            .grid(row=0, column=7, padx=(0, 0), pady=0, sticky="w")

        ent_weight.bind("<Return>", lambda e: self.add_ticket())

        # Interakcie v tabuľke
        self.tree.bind("<Double-1>", self._on_double_click)     # dvojklik: toggle/inline edit
        self.tree.bind("<Button-1>", self._on_single_click)     # klik: toggle checkbox v stĺpci track

        # --- Akcie ---
        fr_actions = ttk.Frame(self)
        fr_actions.place(x=10, y=820, width=960, height=50)

        self.run_btn = ttk.Button(fr_actions, text="Spustiť logovanie (8h/deň podľa váh)", command=self.run_clicked)
        self.run_btn.grid(row=0, column=0, padx=8, pady=8, sticky="w")

        ttk.Button(fr_actions, text="Ukončiť", command=self.on_close).grid(row=0, column=1, padx=8, pady=8, sticky="w")

        ttk.Checkbutton(fr_actions, text="Otvoriť time-tracking po dokončení (vyplniť token)",
                        variable=self.open_tracking_var).grid(row=1, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="w")

        self.status_var = tk.StringVar(value="Pripravené.")
        ttk.Label(fr_actions, textvariable=self.status_var).grid(row=0, column=2, padx=8, pady=8, sticky="w")

    # ---------- Tree helpers ----------
    def _tree_sort(self, col, reverse=False):
        # mapovanie na index stĺpca
        idx_map = {"track": 0, "issue": 1, "name": 2, "weight": 3}
        cidx = idx_map[col]

        rows = []
        for iid in self.tree.get_children(""):
            vals = self.tree.item(iid, "values")
            key = vals[cidx]
            if col == "weight":
                try:
                    key = int(key)
                except Exception:
                    key = 0
            elif col == "track":
                # "☑" pred "☐"
                key = 1 if key == "☑" else 0
            else:
                key = str(key).lower()
            rows.append((key, iid))

        rows.sort(reverse=reverse)
        for i, (_, iid) in enumerate(rows):
            self.tree.move(iid, "", i)

    # prepnúť smer pri ďalšom kliku
    self.tree.heading(col, command=lambda: self._tree_sort(col, not reverse))

    def _toggle_track_item(self, iid):
        trk, issue, name, weight = self.tree.item(iid, "values")
        new_trk = "☐" if trk == "☑" else "☑"
        self.tree.item(iid, values=(new_trk, issue, name, weight))
        # aktualizuj master checkbox podľa stavu
        all_vals = [self.tree.item(x, "values")[0] for x in self.tree.get_children()]
        self.master_track_var.set(all(v == "☑" for v in all_vals))

    def _set_all_track(self, track_bool: bool):
        new_val = "☑" if track_bool else "☐"
        for iid in self.tree.get_children():
            trk, issue, name, weight = self.tree.item(iid, "values")
            self.tree.item(iid, values=(new_val, issue, name, weight))

    def _on_single_click(self, event):
        # ak klik v stĺpci #1 (track), prepni checkbox
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self._toggle_track_item(row)

    def _on_double_click(self, event):
        # dvojklik: track stĺpec -> toggle; inak otvor inline editor
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if col == "#1":
            self._toggle_track_item(row)
            return
        # inline edit
        self._begin_edit(row, col)

    def _begin_edit(self, item, col):
        # zruš predchádzajúci editor
        self._destroy_editor()

        x, y, w, h = self._cell_bbox(item, col)
        if x is None:
            return
        old_vals = self.tree.item(item, "values")
        col_idx = int(col[1:]) - 1  # "#2" -> 1
        old_text = old_vals[col_idx]

        # editujeme len issue (#2), name (#3), weight (#4)
        if col_idx not in (1, 2, 3):
            return

        self._edit_item = item
        self._edit_col = col
        self._edit_entry = tk.Entry(self.tree, borderwidth=1)
        self._edit_entry.insert(0, str(old_text))
        self._edit_entry.select_range(0, tk.END)
        self._edit_entry.focus()
        self._edit_entry.place(x=x, y=y, width=w, height=h)

        self._edit_entry.bind("<Return>", self._save_edit)
        self._edit_entry.bind("<Escape>", lambda e: self._destroy_editor())
        self._edit_entry.bind("<FocusOut>", self._save_edit)

    def _cell_bbox(self, item, col):
        try:
            bbox = self.tree.bbox(item, col)
            if not bbox:
                self.tree.see(item)
                bbox = self.tree.bbox(item, col)
            if not bbox:
                return (None, None, None, None)
            return bbox  # x, y, w, h
        except Exception:
            return (None, None, None, None)

    def _save_edit(self, event=None):
        if not self._edit_entry or not self._edit_item or not self._edit_col:
            self._destroy_editor()
            return
        new_val = self._edit_entry.get().strip()
        vals = list(self.tree.item(self._edit_item, "values"))
        col_idx = int(self._edit_col[1:]) - 1  # 0-based

        # #4 -> Váha
        if col_idx == 3:
            try:
                w = int(new_val)
                if w < 0:
                    raise ValueError()
                new_val = str(w)
            except Exception:
                messagebox.showwarning("Zlá váha", "Váha musí byť celé nezáporné číslo.")
                self._destroy_editor()
                return

        # #2 -> Issue (nesmie byť prázdny)
        if col_idx == 1 and not new_val:
            messagebox.showwarning("Chýba issue", "Issue ID nemôže byť prázdne.")
            self._destroy_editor()
            return

        # #3 -> Názov (môže byť prázdny)

        vals[col_idx] = new_val
        self.tree.item(self._edit_item, values=tuple(vals))
        self._destroy_editor()

    def _destroy_editor(self):
        if self._edit_entry is not None:
            try:
                self._edit_entry.destroy()
            except Exception:
                pass
        self._edit_entry = None
        self._edit_item = None
        self._edit_col = None

    def read_tickets(self, only_tracked=False):
        items = []
        for iid in self.tree.get_children():
            trk, issue, name, weight = self.tree.item(iid, "values")
            try:
                w = int(weight)
            except Exception:
                w = 1
            item = {
                "issue": str(issue).strip(),
                "name": str(name).strip(),
                "weight": max(0, w),
                "track": (trk == "☑"),
            }
            items.append(item)
        items = [t for t in items if t["issue"]]
        if only_tracked:
            items = [t for t in items if t["track"]]
        return items

    # ---------- Reakcie na zmeny (heslo/užívateľ/checkbox) ----------
    def _on_username_change(self, *args):
        u = self.username_var.get().strip()
        pw = get_saved_password(u)
        if pw:
            self.password_var.set(pw)

    def _on_password_change(self, *args):
        if self.save_password_var.get():
            set_saved_password(self.username_var.get().strip(), self.password_var.get())

    def _on_save_password_toggle(self, *args):
        if self.save_password_var.get():
            set_saved_password(self.username_var.get().strip(), self.password_var.get())
        else:
            clear_saved_password(self.username_var.get().strip())

    # ---------- Date helpers ----------
    def set_today(self):
        today = dt.date.today().strftime("%d.%m.%Y")
        self.start_var.set(today)
        self.end_var.set(today)

    def set_this_week(self):
        today = dt.date.today()
        s = start_of_week(today)
        e = end_of_week(today)
        self.start_var.set(s.strftime("%d.%m.%Y"))
        self.end_var.set(e.strftime("%d.%m.%Y"))

    def set_this_month(self):
        today = dt.date.today()
        s = first_day_of_month(today)
        e = last_day_of_month(today)
        self.start_var.set(s.strftime("%d.%m.%Y"))
        self.end_var.set(e.strftime("%d.%m.%Y"))

    def open_calendar_dialog(self):
        if Calendar is None:
            messagebox.showinfo("Kalendár nie je dostupný", "Nainštaluj modul 'tkcalendar':\n\npip install tkcalendar")
            return

        top = tk.Toplevel(self)
        top.title("Vyber rozsah dátumov")
        top.geometry("600x320")
        ttk.Label(top, text="Začiatočný dátum").grid(row=0, column=0, padx=8, pady=4)
        ttk.Label(top, text="Koncový dátum").grid(row=0, column=1, padx=8, pady=4)

        cal_from = Calendar(top, selectmode="day", date_pattern="dd.mm.yyyy")
        cal_from.grid(row=1, column=0, padx=8, pady=8)

        cal_to = Calendar(top, selectmode="day", date_pattern="dd.mm.yyyy")
        cal_to.grid(row=1, column=1, padx=8, pady=8)

        def apply_dates():
            self.start_var.set(cal_from.get_date())
            self.end_var.set(cal_to.get_date())
            top.destroy()

        ttk.Button(top, text="Použiť", command=apply_dates).grid(row=2, column=0, padx=8, pady=8, sticky="e")
        ttk.Button(top, text="Zrušiť", command=top.destroy).grid(row=2, column=1, padx=8, pady=8, sticky="w")

    # ---------- Tickets ops ----------
    def add_ticket(self):
        issue = self.new_issue_var.get().strip()
        name = self.new_name_var.get().strip()
        weight = self.new_weight_var.get().strip()
        if not issue:
            messagebox.showwarning("Chýba issue", "Zadaj Issue ID.")
            return
        try:
            w = int(weight)
            if w < 0:
                raise ValueError()
        except Exception:
            messagebox.showwarning("Zlá váha", "Váha musí byť celé nezáporné číslo.")
            return

        self.tree.insert("", "end", values=("☑", issue, name, w))
        self.new_issue_var.set("")
        self.new_name_var.set("")
        self.new_weight_var.set("1")
        # uprav master checkbox
        self.master_track_var.set(all(self.tree.item(i, "values")[0] == "☑" for i in self.tree.get_children()))

    def remove_selected(self):
        sel = self.tree.selection()
        for s in sel:
            self.tree.delete(s)
        self.master_track_var.set(all(self.tree.item(i, "values")[0] == "☑" for i in self.tree.get_children()))

    # ---------- Spustenie ----------
    def run_clicked(self):
        try:
            start = dt.datetime.strptime(self.start_var.get().strip(), "%d.%m.%Y").date()
            end = dt.datetime.strptime(self.end_var.get().strip(), "%d.%m.%Y").date()
        except Exception:
            messagebox.showerror("Nesprávny dátum", "Použi formát dd.mm.rrrr.")
            return

        if end < start:
            messagebox.showerror("Chyba rozsahu", "Dátum 'Do' musí byť >= 'Od'.")
            return

        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username or not password:
            messagebox.showerror("Prihlásenie", "Zadaj používateľa aj heslo.")
            return

        all_tickets = self.read_tickets(only_tracked=False)
        tickets = self.read_tickets(only_tracked=True)

        if not all_tickets:
            messagebox.showerror("Tikety", "Pridaj aspoň jeden tiket.")
            return

        if not tickets:
            messagebox.showerror("Tikety", "Nie je označený žiadny tiket na trackovanie.")
            return

        # Uloženie konfigurácie – uložíme všetko vrátane random nastavení a názvov
        if self.remember_settings_var.get():
            cfg = load_config()
            cfg["username"] = username
            cfg["tickets"] = all_tickets
            cfg["start_date"] = self.start_var.get().strip()
            cfg["end_date"] = self.end_var.get().strip()
            cfg["save_password"] = bool(self.save_password_var.get())
            cfg["randomize_enabled"] = bool(self.randomize_var.get())
            cfg["randomize_k"] = int(self.randomize_k_var.get() or 1)
            save_config(cfg)

        if self.save_password_var.get():
            set_saved_password(username, password)
        else:
            clear_saved_password(username)

        # Spustiť v thready (neblokovať GUI)
        self.run_btn.config(state="disabled")
        self.status_var.set("Prebieha logovanie…")
        th = threading.Thread(
            target=self._do_logging,
            args=(
                username, password, tickets,
                start, end,
                self.open_tracking_var.get(),
                bool(self.randomize_var.get()),
                int(self.randomize_k_var.get() or 1),
            ),
            daemon=True,
        )
        th.start()

    def _do_logging(self, username, password, tickets, start, end, open_tracking, randomize_enabled, randomize_k):
        driver = None
        try:
            days = working_days(start, end, self.skip_weekends_var.get(), self.skip_holidays_var.get())
            if not days:
                self._set_status("Žiadne pracovné dni v zadanom rozsahu.")
                self._reenable()
                return

            driver = webdriver.Chrome()
            wait = WebDriverWait(driver, 15)

            ok_logs = 0
            fail_logs = 0
            planned_logs = 0  # podľa skutočne plánovaných zápisov v daný deň

            try:
                # Login
                driver.get(JIRA_URL)
                wait.until(EC.presence_of_element_located((By.ID, "login-form-username"))).send_keys(username)
                driver.find_element(By.ID, "login-form-password").send_keys(password)
                driver.find_element(By.ID, "login").click()

                # Skontroluj prípadnú chybovú hlášku
                try:
                    err = WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.ID, "login-error-message"))
                    )
                    if err.is_displayed():
                        raise RuntimeError("Nesprávne meno alebo heslo do Jira.")
                except Exception:
                    pass

                # Logovanie – s presným dorovnaním na 8h/deň
                for day in days:
                    day_str = format_jira_date(day)
                    day_time = "04:00 PM"
                    dt_str = f"{day_str} {day_time}"

                    # 1) Podmnožina tiketov pre tento deň
                    todays_tickets = list(tickets)
                    if randomize_enabled and len(todays_tickets) > 1:
                        k = max(1, min(int(randomize_k or 1), len(todays_tickets)))
                        todays_tickets = random.sample(todays_tickets, k)

                    # 2) Rozdelenie 8h len medzi vybranú podmnožinu podľa ich váh
                    weights = [max(0, int(t.get("weight", 1))) for t in todays_tickets]
                    if sum(weights) == 0:
                        weights = [1] * len(todays_tickets)

                    minutes_for_subset = proportional_split(8 * 60, weights, round_to=15)

                    # 3) Dorovnanie na presne 480 min (kvôli zaokrúhľovaniu)
                    total_min = sum(minutes_for_subset)
                    if total_min != 8 * 60 and len(minutes_for_subset) > 0:
                        diff = (8 * 60) - total_min
                        step = 15 if diff > 0 else -15
                        order = sorted(range(len(weights)), key=lambda i: weights[i], reverse=True)
                        i = 0
                        while diff != 0:
                            idx = order[i % len(order)]
                            if minutes_for_subset[idx] + step >= 0:
                                minutes_for_subset[idx] += step
                                diff -= step
                            i += 1

                    # 4) Trackni len tie, ktoré majú > 0 minút
                    for idx, t in enumerate(todays_tickets):
                        mins = minutes_for_subset[idx] if idx < len(minutes_for_subset) else 0
                        if mins <= 0:
                            continue
                        planned_logs += 1

                        issue = t["issue"]
                        time_str = minutes_to_jira_time(mins)

                        log_url = f"{JIRA_URL}/secure/CreateWorklog!default.jspa?id={issue}"
                        driver.get(log_url)

                        try:
                            time_spent_input = wait.until(
                                EC.presence_of_element_located((By.ID, "log-work-time-logged"))
                            )
                            time_spent_input.clear()
                            time_spent_input.send_keys(time_str)

                            date_picker = wait.until(
                                EC.presence_of_element_located((By.ID, "log-work-date-logged-date-picker"))
                            )
                            date_picker.clear()
                            date_picker.send_keys(dt_str)

                            submit_button = driver.find_element(By.ID, "log-work-submit")
                            submit_button.click()

                            ok_logs += 1
                            self._append_status(f"✔ {day_str} – {issue}: {time_str}")
                        except Exception as e:
                            fail_logs += 1
                            self._append_status(f"✖ {day_str} – {issue}: {e}")

                # Otvoriť time-tracking len ak je checkbox zapnutý
                if open_tracking:
                    q_user = username or DEFAULT_USERNAME
                    q_from = start.strftime("%Y-%m-%d")
                    q_to = end.strftime("%Y-%m-%d")
                    final_url = f"{TIME_TRACKING_URL}?user={q_user}&from={q_from}&to={q_to}"
                    driver.get(final_url)

                    # Vyplniť token
                    self._fill_token_on_page(driver, TIME_TRACKING_TOKEN)
                    self._append_status("Token vyplnený do time-tracking stránky.")
                else:
                    self._append_status("Dokončené. Stránka na kontrolu sa neotvárala (checkbox vypnutý).")

            finally:
                # Po dokončení pre istotu zavri prehliadač
                try:
                    if driver is not None:
                        driver.quit()
                except Exception:
                    pass

                if planned_logs > 0 and fail_logs == 0:
                    self._set_status("✅ Všetko úspešne natrackované.")
                elif planned_logs == 0:
                    self._set_status("ℹ Nebolo čo trackovať (0 minút na rozdelenie).")
                else:
                    self._set_status(f"⚠ Čiastočne dokončené: úspešne {ok_logs}/{planned_logs}, neúspešné {fail_logs}.")

        except Exception as e:
            self._set_status(f"Chyba: {e}")
        finally:
            self._reenable()

    # --- Token vyplnenie (robustné) ---
    def _fill_token_on_page(self, driver, token: str):
        def try_fill_in_context():
            selectors = [
                "input[type='password'][name='password']",
                "input[autocomplete='current-password']",
                "input.MuiInputBase-input.MuiOutlinedInput-input.MuiInputBase-inputAdornedEnd",
                "input[type='password']",
            ]
            for sel in selectors:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    if el:
                        try:
                            el.clear()
                        except Exception:
                            pass
                        try:
                            el.send_keys(token)
                        except Exception:
                            pass
                        try:
                            driver.execute_script(
                                """
                                const el = arguments[0], val = arguments[1];
                                el.value = val;
                                el.setAttribute('value', val);
                                el.dispatchEvent(new Event('input', {bubbles:true}));
                                el.dispatchEvent(new Event('change', {bubbles:true}));
                                """,
                                el, token
                            )
                        except Exception:
                            pass
                        return True
                except Exception:
                    continue
            try:
                el = driver.execute_script(
                    "return document.querySelector(\"input[autocomplete='current-password']\") || "
                    "document.querySelector(\"input[type='password'][name='password']\") || "
                    "document.querySelector(\"input[type='password']\");"
                )
                if el:
                    driver.execute_script(
                        """
                        const el = arguments[0], val = arguments[1];
                        el.value = val;
                        el.setAttribute('value', val);
                        el.dispatchEvent(new Event('input', {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        """,
                        el, token
                    )
                    return True
            except Exception:
                pass
            return False

        try:
            if try_fill_in_context():
                return
        except Exception:
            pass

        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            for fr in frames:
                try:
                    driver.switch_to.frame(fr)
                    if try_fill_in_context():
                        driver.switch_to.default_content()
                        return
                except Exception:
                    pass
                finally:
                    driver.switch_to.default_content()
        except Exception:
            pass

    # --- UI pomocníci ---
    def _set_status(self, msg: str):
        self.status_var.set(msg)

    def _append_status(self, line: str):
        self.status_var.set(line)

    def _reenable(self):
        self.run_btn.config(state="normal")

    def on_close(self):
        """Uloží nastavenia a (ak je zaškrtnuté) heslo, potom ukončí aplikáciu."""
        try:
            cfg = load_config()
            cfg["username"] = self.username_var.get().strip()
            cfg["tickets"] = self.read_tickets(only_tracked=False)  # uloží aj track flagy, názvy a upravené hodnoty
            cfg["start_date"] = self.start_var.get().strip()
            cfg["end_date"] = self.end_var.get().strip()
            cfg["save_password"] = bool(self.save_password_var.get())
            cfg["randomize_enabled"] = bool(self.randomize_var.get())
            cfg["randomize_k"] = int(self.randomize_k_var.get() or 1)
            save_config(cfg)

            if self.save_password_var.get():
                set_saved_password(self.username_var.get().strip(), self.password_var.get())
            else:
                clear_saved_password(self.username_var.get().strip())
        finally:
            self.destroy()


# ===== Date helpers used above (duplicity kvôli čitateľnosti) =====
def start_of_week(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def end_of_week(d: dt.date) -> dt.date:
    return start_of_week(d) + dt.timedelta(days=4)


def first_day_of_month(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def last_day_of_month(d: dt.date) -> dt.date:
    if d.month == 12:
        return dt.date(d.year, 12, 31)
    next_month = dt.date(d.year, d.month + 1, 1)
    return next_month - dt.timedelta(days=1)


if __name__ == "__main__":
    app = App()
    app.mainloop()
