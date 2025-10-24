# jira_worklog_gui_cloud.py
import os
import re
import json
import base64
import datetime as dt
import threading
import traceback
import tkinter as tk
from tkinter import ttk, messagebox
from typing import List, Tuple

# --- Optional safe password store ---
try:
    import keyring  # pip install keyring
except Exception:
    keyring = None

# --- HTTP client (requests with retries) ---
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================== CONFIG ==================
JIRA_CLOUD_BASE = "https://xxx.atlassian.net"
DEFAULT_EMAIL = "xxx"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".jira_logger_config.json")
LOG_PATH = os.path.join(os.path.expanduser("~"), ".jira_worklog_gui.log")

# Optional ping
TIME_TRACKING_URL = "https://time-tracking-dev-time-tracking.apps.dev.cp.cloud/"
TIME_TRACKING_TOKEN = "xxx"

# Slovak holidays 2025
SK_HOLIDAYS_2025 = {
    dt.date(2025, 1, 1), dt.date(2025, 1, 6), dt.date(2025, 4, 18), dt.date(2025, 4, 21),
    dt.date(2025, 5, 1), dt.date(2025, 5, 8), dt.date(2025, 7, 5), dt.date(2025, 8, 29),
    dt.date(2025, 9, 1), dt.date(2025, 9, 15), dt.date(2025, 11, 1), dt.date(2025, 11, 17),
    dt.date(2025, 12, 24), dt.date(2025, 12, 25), dt.date(2025, 12, 26),
}

# ================== LOGGING HELPERS ==================
def log_exc(prefix: str, exc: Exception):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{dt.datetime.now().isoformat()}] {prefix}: {repr(exc)}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            f.write("\n")
    except Exception:
        pass

def log_text(text: str):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now().isoformat()}] {text}\n")
    except Exception:
        pass

# ================== CONFIG & SECRET HELPERS ==================
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
        log_exc("save_config", e)

def get_saved_secret(email: str):
    if not email:
        return ""
    if keyring:
        try:
            val = keyring.get_password("jira_worklog_cloud", email)
            if val:
                return val
        except Exception as e:
            log_exc("get_saved_secret(keyring)", e)
    cfg = load_config()
    raw = cfg.get("saved_api_tokens", {}).get(email, "")
    if raw:
        try:
            return base64.b64decode(raw.encode("utf-8")).decode("utf-8")
        except Exception as e:
            log_exc("get_saved_secret(base64)", e)
            return ""
    return ""

def set_saved_secret(email: str, secret: str):
    if not email:
        return
    if keyring:
        try:
            keyring.set_password("jira_worklog_cloud", email, secret)
            return
        except Exception as e:
            log_exc("set_saved_secret(keyring)", e)
    cfg = load_config()
    sp = cfg.get("saved_api_tokens", {})
    if secret:
        sp[email] = base64.b64encode(secret.encode("utf-8")).decode("utf-8")
    else:
        sp.pop(email, None)
    cfg["saved_api_tokens"] = sp
    save_config(cfg)

def clear_saved_secret(email: str):
    if not email:
        return
    if keyring:
        try:
            keyring.delete_password("jira_worklog_cloud", email)
        except Exception as e:
            log_exc("clear_saved_secret(keyring)", e)
    cfg = load_config()
    sp = cfg.get("saved_api_tokens", {})
    if email in sp:
        del sp[email]
        cfg["saved_api_tokens"] = sp
        save_config(cfg)

# ================== TEXT / KEY HELPERS ==================
KEY_RE = re.compile(r"[A-Z][A-Z0-9_]+-\d+$")

def extract_issue_key(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"/browse/([A-Z][A-Z0-9_]+-\d+)", s, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    if KEY_RE.match(s.upper()):
        return s.upper()
    return s

# ================== DATE/TIME HELPERS ==================
def working_days(start: dt.date, end: dt.date, skip_weekends=True, skip_sk_holidays=True):
    d = start
    out = []
    while d <= end:
        if (not skip_weekends or d.weekday() < 5) and (not skip_sk_holidays or d not in SK_HOLIDAYS_2025):
            out.append(d)
        d += dt.timedelta(days=1)
    return out

def proportional_split(total_minutes: int, weights: List[int], round_to=15) -> List[int]:
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
    return start_of_week(d) + dt.timedelta(days=4)

def last_week_range(today=None) -> Tuple[dt.date, dt.date]:
    if today is None:
        today = dt.date.today()
    # last week's Mon..Fri relative to today
    this_monday = start_of_week(today)
    last_monday = this_monday - dt.timedelta(days=7)
    last_friday = last_monday + dt.timedelta(days=4)
    return last_monday, last_friday

def first_day_of_month(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)

def last_day_of_month(d: dt.date) -> dt.date:
    if d.month == 12:
        return dt.date(d.year, 12, 31)
    next_month = dt.date(d.year, d.month + 1, 1)
    return next_month - dt.timedelta(days=1)

def local_iso_with_tz(day: dt.date, hour=16, minute=0) -> str:
    local_naive = dt.datetime(day.year, day.month, day.day, hour, minute, 0, 0)
    local_aware = local_naive.astimezone()
    tz_offset = local_aware.strftime("%z")
    return local_aware.strftime("%Y-%m-%dT%H:%M:%S") + ".000" + tz_offset

# ================== JIRA CLOUD CLIENT ==================
def build_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=5, connect=3, read=3, backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["HEAD","GET","POST","PUT","DELETE","OPTIONS","TRACE","PATCH"])
    )
    adapter = HTTPAdapter(max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"Accept": "application/json"})
    return s

def jira_get_myself(session: requests.Session, base_url: str, email: str, api_token: str) -> Tuple[bool, str]:
    try:
        resp = session.get(f"{base_url}/rest/api/3/myself", auth=(email, api_token), timeout=15)
        if resp.status_code == 200:
            return True, ""
        return False, f"/myself status {resp.status_code}: {resp.text[:500]}"
    except Exception as e:
        log_exc("jira_get_myself", e)
        return False, repr(e)

def jira_resolve_issue(session: requests.Session, base_url: str, email: str, api_token: str, raw_input: str) -> Tuple[bool, str, str, str]:
    """Resolve input to (key, summary)."""
    candidate = extract_issue_key(raw_input)

    try:
        r = session.get(f"{base_url}/rest/api/3/issue/{candidate}?fields=key,summary",
                        auth=(email, api_token), timeout=15)
        if r.status_code == 200:
            data = r.json()
            key = data.get("key", candidate).upper()
            summary = (data.get("fields", {}) or {}).get("summary", "")
            return True, key, summary or "", ""
    except Exception as e:
        log_exc("jira_resolve_issue(GET)", e)
        return False, "", "", repr(e)

    if candidate.isdigit():
        try:
            jql = f"id={candidate}"
            sr = session.get(f"{base_url}/rest/api/3/search",
                             params={"jql": jql, "fields": "key,summary"},
                             auth=(email, api_token), timeout=20)
            if sr.status_code == 200:
                issues = sr.json().get("issues", [])
                if issues:
                    key = issues[0]["key"].upper()
                    summary = (issues[0].get("fields", {}) or {}).get("summary", "")
                    return True, key, summary or "", ""
            return False, "", "", f"{raw_input}: Nie je možné nájsť podľa numerického ID. Použi issue key (napr. SINT-1234)."
        except Exception as e:
            log_exc("jira_resolve_issue(JQL)", e)
            return False, "", "", repr(e)

    try:
        txt = r.text[:500]
    except Exception:
        txt = "neznáma odpoveď"
    return False, "", "", f"{raw_input}: status {r.status_code if 'r' in locals() else '?'}: {txt}"

def log_work_cloud(session: requests.Session, base_url: str, email: str, api_token: str,
                   issue_key: str, started_iso_tz: str, seconds: int, comment: str = None) -> Tuple[bool, str]:
    url = f"{base_url}/rest/api/3/issue/{issue_key}/worklog"
    payload = {"started": started_iso_tz, "timeSpentSeconds": int(seconds)}
    if comment:
        payload["comment"] = {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
        }
    try:
        resp = session.post(url, json=payload, auth=(email, api_token), timeout=20)
    except Exception as e:
        log_exc("log_work_cloud(request)", e)
        return False, f"request error: {repr(e)}"

    if resp.status_code == 201:
        return True, ""
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    return False, f"HTTP {resp.status_code}: {data}"

# ================== TKINTER GUI APP ==================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Jira Cloud Worklog – Multi-ticket Tracker")
        self.geometry("920x760")
        self.resizable(False, False)

        self.cfg = load_config()

        # State vars
        self.email_var = tk.StringVar(value=self.cfg.get("email", DEFAULT_EMAIL))
        self.api_token_var = tk.StringVar(value=get_saved_secret(self.email_var.get()))
        self.save_token_var = tk.BooleanVar(value=self.cfg.get("save_token", False))
        self.remember_settings_var = tk.BooleanVar(value=True)

        today = dt.date.today()
        default_start = self.cfg.get("start_date") or first_day_of_month(today).strftime("%d.%m.%Y")
        default_end = self.cfg.get("end_date") or last_day_of_month(today).strftime("%d.%m.%Y")
        self.start_var = tk.StringVar(value=default_start)
        self.end_var = tk.StringVar(value=default_end)

        self.skip_weekends_var = tk.BooleanVar(value=True)
        self.skip_holidays_var = tk.BooleanVar(value=True)

        # Table data
        saved = self.cfg.get("tickets", [{"issue": "SINT-1234", "weight": 1, "summary": "", "checked": 1}])
        for t in saved:
            t.setdefault("summary", "")
            t.setdefault("checked", 1)
        self.tickets = saved

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Try to auto-refresh summaries on startup (non-blocking)
        self.after(100, self.refresh_table_async)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        # --- Credentials ---
        fr_auth = ttk.LabelFrame(self, text="Jira Cloud – Prihlásenie (Email + API token)")
        fr_auth.place(x=10, y=10, width=900, height=150)

        ttk.Label(fr_auth, text="Email:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(fr_auth, textvariable=self.email_var, width=34).grid(row=0, column=1, **pad)

        ttk.Label(fr_auth, text="API token:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(fr_auth, textvariable=self.api_token_var, width=30, show="•").grid(row=0, column=3, **pad)

        ttk.Checkbutton(fr_auth, text="Uložiť API token", variable=self.save_token_var).grid(row=1, column=1, sticky="w", **pad)
        ttk.Checkbutton(fr_auth, text="Pamätať nastavenia", variable=self.remember_settings_var).grid(row=1, column=3, sticky="w", **pad)

        ttk.Button(fr_auth, text="Test prihlásenia", command=self.test_auth_clicked).grid(row=2, column=1, sticky="w", **pad)

        # --- Date range ---
        fr_dates = ttk.LabelFrame(self, text="Obdobie")
        fr_dates.place(x=10, y=170, width=900, height=140)

        ttk.Label(fr_dates, text="Od (dd.mm.rrrr):").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(fr_dates, textvariable=self.start_var, width=16).grid(row=0, column=1, **pad)

        ttk.Label(fr_dates, text="Do (dd.mm.rrrr):").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(fr_dates, textvariable=self.end_var, width=16).grid(row=0, column=3, **pad)

        ttk.Button(fr_dates, text="Tento týždeň", command=self.set_this_week).grid(row=1, column=0, **pad)
        ttk.Button(fr_dates, text="Minulý týždeň", command=self.set_last_week).grid(row=1, column=1, **pad)
        ttk.Button(fr_dates, text="Tento mesiac", command=self.set_this_month).grid(row=1, column=2, **pad)
        ttk.Button(fr_dates, text="Dnes", command=self.set_today).grid(row=1, column=3, **pad)

        ttk.Checkbutton(fr_dates, text="Preskočiť víkendy", variable=self.skip_weekends_var).grid(row=2, column=0, sticky="w", **pad)
        ttk.Checkbutton(fr_dates, text="Preskočiť SK sviatky", variable=self.skip_holidays_var).grid(row=2, column=1, sticky="w", **pad)

        # --- Tickets table ---
        fr_tickets = ttk.LabelFrame(self, text="Tikety (zaškrtni riadky, ktoré chceš logovať)")
        fr_tickets.place(x=10, y=320, width=900, height=340)

        # Order: [checkbox], ID, Summary, Váha
        self.columns = ("checked", "issue", "summary", "weight")
        self.tree = ttk.Treeview(fr_tickets, columns=self.columns, show="headings", height=11)
        self.tree.heading("checked", text="✓")
        self.tree.column("checked", width=36, anchor="center")
        self.tree.heading("issue", text="ID")
        self.tree.column("issue", width=180, anchor="w")
        self.tree.heading("summary", text="Summary")
        self.tree.column("summary", width=530, anchor="w")
        self.tree.heading("weight", text="Váha")
        self.tree.column("weight", width=70, anchor="center")
        self.tree.grid(row=0, column=0, columnspan=6, padx=8, pady=(8, 4), sticky="nsew")

        vsb = ttk.Scrollbar(fr_tickets, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=6, sticky="ns", pady=(8, 4))

        # Fill initial rows
        for t in self.tickets:
            chk = "☑" if t.get("checked", 1) else "☐"
            self.tree.insert("", "end", values=(chk, t["issue"], t.get("summary",""), t.get("weight",1)))

        # Add/remove & refresh
        self.new_issue_var = tk.StringVar()
        self.new_weight_var = tk.StringVar(value="1")
        ttk.Label(fr_tickets, text="Nový ID / URL:").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        ttk.Entry(fr_tickets, textvariable=self.new_issue_var, width=30).grid(row=1, column=0, padx=(110,8), pady=4, sticky="w")

        ttk.Label(fr_tickets, text="Váha:").grid(row=1, column=1, padx=8, pady=4, sticky="e")
        ttk.Entry(fr_tickets, textvariable=self.new_weight_var, width=8).grid(row=1, column=1, padx=(40,8), pady=4, sticky="w")

        ttk.Button(fr_tickets, text="Pridať", command=self.add_ticket).grid(row=1, column=2, padx=8, pady=4, sticky="w")
        ttk.Button(fr_tickets, text="Odstrániť vybrané", command=self.remove_selected).grid(row=1, column=3, padx=8, pady=4, sticky="w")
        ttk.Button(fr_tickets, text="Obnoviť tabuľku", command=self.refresh_table_async).grid(row=1, column=4, padx=8, pady=4, sticky="w")

        # Toggle checkbox on click + inline edit on double-click
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.on_tree_double_click)

        # --- Actions ---
        fr_actions = ttk.Frame(self)
        fr_actions.place(x=10, y=670, width=900, height=70)

        self.run_btn = ttk.Button(fr_actions, text="Spustiť logovanie (8h/deň podľa váh, len zaškrtnuté)", command=self.run_clicked)
        self.run_btn.grid(row=0, column=0, padx=8, pady=8)

        ttk.Button(fr_actions, text="Ukončiť", command=self.on_close).grid(row=0, column=1, padx=8, pady=8)

        self.status_var = tk.StringVar(value="Pripravené.")
        ttk.Label(fr_actions, textvariable=self.status_var).grid(row=0, column=2, padx=8, pady=8, sticky="w")

    # ---------- Tree events ----------
    def on_tree_click(self, event):
        # Toggle checkbox if first column clicked
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)  # '#1'..'#n'
        if not row_id:
            return
        if col_id == "#1":  # checkbox column
            vals = list(self.tree.item(row_id, "values"))
            vals[0] = "☐" if vals[0] == "☑" else "☑"
            self.tree.item(row_id, values=vals)

    def on_tree_double_click(self, event):
        # Inline edit for ID / Summary / Váha
        row_id = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not row_id or col == "#1":
            return  # ignore checkbox

        x, y, w, h = self.tree.bbox(row_id, col)
        value = self.tree.set(row_id, column=col)
        entry = tk.Entry(self.tree)
        entry.insert(0, value)
        entry.select_range(0, tk.END)
        entry.focus()
        entry.place(x=x, y=y, width=w, height=h)

        def save_edit(event=None):
            new_val = entry.get().strip()
            entry.destroy()
            colname = self.columns[int(col[1:])-1]  # map '#2' -> 'issue' etc.

            # Validate & normalize
            if colname == "weight":
                try:
                    iv = int(new_val)
                    if iv < 0:
                        raise ValueError
                    new_val = str(iv)
                except Exception:
                    messagebox.showerror("Zlá váha", "Váha musí byť celé nezáporné číslo.")
                    return
            elif colname == "issue":
                new_val = extract_issue_key(new_val).upper()
                # After ID edit, try refresh summary for this row (async)
                self.refresh_row_async(row_id, new_val)

            vals = list(self.tree.item(row_id, "values"))
            idx = self.columns.index(colname)
            vals[idx] = new_val
            self.tree.item(row_id, values=vals)

        entry.bind("<Return>", save_edit)
        entry.bind("<FocusOut>", save_edit)

    # ---------- Date helpers in UI ----------
    def set_this_week(self):
        today = dt.date.today()
        s = start_of_week(today)
        e = end_of_week(today)
        self.start_var.set(s.strftime("%d.%m.%Y"))
        self.end_var.set(e.strftime("%d.%m.%Y"))

    def set_last_week(self):
        s, e = last_week_range()
        self.start_var.set(s.strftime("%d.%m.%Y"))
        self.end_var.set(e.strftime("%d.%m.%Y"))

    def set_this_month(self):
        today = dt.date.today()
        s = first_day_of_month(today)
        e = last_day_of_month(today)
        self.start_var.set(s.strftime("%d.%m.%Y"))
        self.end_var.set(e.strftime("%d.%m.%Y"))

    def set_today(self):
        today = dt.date.today()
        self.start_var.set(today.strftime("%d.%m.%Y"))
        self.end_var.set(today.strftime("%d.%m.%Y"))

    # ---------- Table ops ----------
    def add_ticket(self):
        raw_issue = self.new_issue_var.get().strip()
        weight = self.new_weight_var.get().strip()
        if not raw_issue:
            messagebox.showwarning("Chýba issue", "Zadaj ID alebo URL (napr. SINT-1234).")
            return
        try:
            w = int(weight)
            if w < 0:
                raise ValueError()
        except Exception:
            messagebox.showwarning("Zlá váha", "Váha musí byť celé nezáporné číslo.")
            return

        issue = extract_issue_key(raw_issue).upper()
        self.tree.insert("", "end", values=("☑", issue, "", str(w)))
        self.new_issue_var.set("")
        self.new_weight_var.set("1")
        # Try refresh just this new row
        row_id = self.tree.get_children()[-1]
        self.refresh_row_async(row_id, issue)

    def remove_selected(self):
        sel = self.tree.selection()
        for s in sel:
            self.tree.delete(s)

    def read_checked_tickets(self):
        items = []
        for iid in self.tree.get_children():
            chk, issue, summary, weight = self.tree.item(iid, "values")
            if chk != "☑":
                continue
            issue_norm = extract_issue_key(str(issue)).upper()
            try:
                w = int(weight)
            except Exception:
                w = 1
            items.append({"issue": issue_norm, "weight": max(0, w), "summary": summary or ""})
        return items

    def read_all_tickets(self):
        items = []
        for iid in self.tree.get_children():
            chk, issue, summary, weight = self.tree.item(iid, "values")
            issue_norm = extract_issue_key(str(issue)).upper()
            try:
                w = int(weight)
            except Exception:
                w = 1
            items.append({"checked": 1 if chk == "☑" else 0,
                          "issue": issue_norm, "weight": max(0, w), "summary": summary or ""})
        return items

    # ---------- Refresh actions ----------
    def test_auth_clicked(self):
        email = self.email_var.get().strip()
        token = self.api_token_var.get().strip()
        if not email or not token:
            messagebox.showerror("Prihlásenie", "Zadaj Email aj API token.")
            return
        session = build_session()
        ok, info = jira_get_myself(session, JIRA_CLOUD_BASE, email, token)
        if ok:
            messagebox.showinfo("OK", "Prihlásenie úspešné (myself).")
        else:
            messagebox.showerror("Chyba prihlásenia", info)

    def refresh_table_async(self):
        """Fetch summaries for all rows (if credentials present)."""
        email = self.email_var.get().strip()
        token = self.api_token_var.get().strip()
        if not email or not token:
            return
        th = threading.Thread(target=self._refresh_all_summaries, args=(email, token), daemon=True)
        th.start()

    def _refresh_all_summaries(self, email, token):
        try:
            session = build_session()
            ok, info = jira_get_myself(session, JIRA_CLOUD_BASE, email, token)
            if not ok:
                self._append_status("Prihlásenie zlyhalo – obnova tabuľky preskočená.")
                return
            for iid in self.tree.get_children():
                _, issue, _, _ = self.tree.item(iid, "values")
                self._refresh_row(session, email, token, iid, issue)
            self._append_status("Tabuľka obnovená.")
        except Exception as e:
            log_exc("_refresh_all_summaries", e)

    def refresh_row_async(self, row_id, issue):
        email = self.email_var.get().strip()
        token = self.api_token_var.get().strip()
        if not email or not token:
            return
        th = threading.Thread(target=self._refresh_row_wrapper, args=(email, token, row_id, issue), daemon=True)
        th.start()

    def _refresh_row_wrapper(self, email, token, row_id, issue):
        try:
            session = build_session()
            ok, _ = jira_get_myself(session, JIRA_CLOUD_BASE, email, token)
            if not ok:
                return
            self._refresh_row(session, email, token, row_id, issue)
        except Exception as e:
            log_exc("_refresh_row_wrapper", e)

    def _refresh_row(self, session, email, token, row_id, issue):
        try:
            ok, key, summary, err = jira_resolve_issue(session, JIRA_CLOUD_BASE, email, token, issue)
            if ok:
                vals = list(self.tree.item(row_id, "values"))
                # Keep checkbox & weight, update ID + summary
                vals[1] = key
                vals[2] = summary or ""
                self.after(0, lambda: self.tree.item(row_id, values=vals))
        except Exception as e:
            log_exc("_refresh_row", e)

    # ---------- Run ----------
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

        email = self.email_var.get().strip()
        api_token = self.api_token_var.get().strip()
        if not email or not api_token:
            messagebox.showerror("Prihlásenie", "Zadaj Email aj API token.")
            return

        tickets = self.read_checked_tickets()
        if not tickets:
            messagebox.showerror("Tikety", "Zaškrtni aspoň jeden riadok.")
            return

        # Save config
        if self.remember_settings_var.get():
            cfg = load_config()
            cfg["email"] = email
            cfg["tickets"] = self.read_all_tickets()
            cfg["start_date"] = self.start_var.get().strip()
            cfg["end_date"] = self.end_var.get().strip()
            cfg["save_token"] = bool(self.save_token_var.get())
            save_config(cfg)
        if self.save_token_var.get():
            set_saved_secret(email, api_token)
        else:
            clear_saved_secret(email)

        self.run_btn.config(state="disabled")
        self.status_var.set("Prebieha logovanie…")
        th = threading.Thread(target=self._do_logging, args=(email, api_token, tickets, start, end), daemon=True)
        th.start()

    def _do_logging(self, email, api_token, tickets, start, end):
        try:
            session = build_session()

            ok, info = jira_get_myself(session, JIRA_CLOUD_BASE, email, api_token)
            if not ok:
                self._fail_with_popup(f"Prihlásenie zlyhalo: {info}")
                return

            # resolve keys + summaries (final check)
            resolved = []
            for t in tickets:
                ok_i, key, summary, err = jira_resolve_issue(session, JIRA_CLOUD_BASE, email, api_token, t["issue"])
                if not ok_i:
                    self._fail_with_popup(f"Issue {t['issue']} neexistuje alebo nemáš prístup: {err}")
                    return
                resolved.append({"issue": key, "weight": t["weight"], "summary": summary or ""})
            tickets = resolved

            days = working_days(start, end, self.skip_weekends_var.get(), self.skip_holidays_var.get())
            if not days:
                self._set_status("Žiadne pracovné dni v zadanom rozsahu.")
                self._reenable()
                return

            weights = [t["weight"] for t in tickets]
            minutes_per_day = proportional_split(8 * 60, weights, round_to=15)

            for day in days:
                started_iso = local_iso_with_tz(day, hour=16, minute=0)
                for idx, t in enumerate(tickets):
                    issue_key = t["issue"]
                    mins = minutes_per_day[idx] if idx < len(minutes_per_day) else 0
                    if mins <= 0:
                        continue
                    ok, err = log_work_cloud(
                        session=session, base_url=JIRA_CLOUD_BASE,
                        email=email, api_token=api_token,
                        issue_key=issue_key, started_iso_tz=started_iso,
                        seconds=int(mins * 60), comment=None,
                    )
                    day_str = day.strftime("%d.%m.%Y")
                    time_str = (f"{mins//60}h {mins%60}m") if mins >= 60 else f"{mins}m"
                    if ok:
                        self._append_status(f"✔ {day_str} – {issue_key}: {time_str}")
                    else:
                        self._append_status(f"✖ {day_str} – {issue_key}: {err}")
                        log_text(f"Worklog error {issue_key} {day_str}: {err}")
                        if "HTTP 400" in err or "HTTP 401" in err or "HTTP 403" in err:
                            self.after(0, lambda e=err, k=issue_key, d=day_str: messagebox.showerror(
                                "Jira odpoveď", f"Chyba pri logovaní do {k} ({d}):\n\n{e}"
                            ))

            # Optional ping
            try:
                q_user = email
                q_from = start.strftime("%Y-%m-%d")
                q_to = end.strftime("%Y-%m-%d")
                final_url = f"{TIME_TRACKING_URL}?user={q_user}&from={q_from}&to={q_to}&token={TIME_TRACKING_TOKEN}"
                session.get(final_url, timeout=10)
            except Exception as e:
                log_exc("time_tracking_ping", e)

            self._append_status("Hotovo. Zalogované do Jira Cloud.")
        except Exception as e:
            log_exc("_do_logging", e)
            self._fail_with_popup(f"Chyba: {e}")
        finally:
            self._reenable()

    # ---------- UI helpers ----------
    def _set_status(self, msg: str):
        self.status_var.set(msg)

    def _append_status(self, line: str):
        self.status_var.set(line)

    def _fail_with_popup(self, msg: str):
        self._set_status(msg)
        try:
            self.after(0, lambda: messagebox.showerror("Chyba", f"{msg}\n\nPozri log: {LOG_PATH}"))
        except Exception:
            pass

    def _reenable(self):
        try:
            self.run_btn.config(state="normal")
        except Exception:
            pass

    def on_close(self):
        try:
            cfg = load_config()
            cfg["email"] = self.email_var.get().strip()
            cfg["tickets"] = self.read_all_tickets()
            cfg["start_date"] = self.start_var.get().strip()
            cfg["end_date"] = self.end_var.get().strip()
            cfg["save_token"] = bool(self.save_token_var.get())
            save_config(cfg)

            if self.save_token_var.get():
                set_saved_secret(self.email_var.get().strip(), self.api_token_var.get().strip())
            else:
                clear_saved_secret(self.email_var.get().strip())
        finally:
            self.destroy()

# ===== Dup helpers (clarity) =====
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
