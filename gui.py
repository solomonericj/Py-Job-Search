#!/usr/bin/env python3
"""
Job Match Finder – GUI
Run with: python gui.py
"""
from __future__ import annotations

import io
import logging
import queue
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox

import job_match_finder as jmf

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

WIN_W, WIN_H = 1280, 820
SIDEBAR_W = 190
STATUS_OPTIONS = ["new", "saved", "applied", "rejected", "ignored"]


# ─────────────────────────────────────────────────────────────────────────────
# Log capture utilities
# ─────────────────────────────────────────────────────────────────────────────

class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(self.format(record) + "\n")


class _QueueWriter:
    """Redirect print() / sys.stdout to queue, buffering until newline."""
    encoding = "utf-8"

    def __init__(self, q: queue.Queue):
        self.q = q
        self._buf = ""

    def write(self, msg: str) -> int:
        self._buf += msg
        if "\n" in self._buf:
            parts = self._buf.split("\n")
            for line in parts[:-1]:
                if line.strip():
                    self.q.put(line + "\n")
            self._buf = parts[-1]
        return len(msg)

    def flush(self) -> None:
        if self._buf.strip():
            self.q.put(self._buf + "\n")
            self._buf = ""

    def isatty(self) -> bool:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Search Page
# ─────────────────────────────────────────────────────────────────────────────

class SearchPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, app: "JobMatchApp"):
        super().__init__(parent, corner_radius=0, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build()

    def _build(self) -> None:
        ctk.CTkLabel(self, text="Run Job Search",
                     font=ctk.CTkFont(size=20, weight="bold")
                     ).grid(row=0, column=0, padx=24, pady=(20, 4), sticky="w")

        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=1, column=0, padx=24, pady=(0, 8), sticky="ew")

        self._no_cache_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="Bypass cache",
                        variable=self._no_cache_var).pack(side="left", padx=(0, 16))

        self._verbose_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="Verbose",
                        variable=self._verbose_var).pack(side="left", padx=(0, 20))

        ctk.CTkLabel(opts, text="Limit:").pack(side="left", padx=(0, 4))
        self._limit_var = tk.StringVar(value="")
        ctk.CTkEntry(opts, textvariable=self._limit_var, width=64,
                     placeholder_text="none").pack(side="left", padx=(0, 24))

        self._run_btn = ctk.CTkButton(opts, text="▶  Run Search", width=140,
                                      command=self._on_run)
        self._run_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(opts, text="Clear Cache", width=110,
                      fg_color=("gray70", "gray40"), hover_color=("gray60", "gray30"),
                      command=self._on_clear_cache).pack(side="left")

        self._status_lbl = ctk.CTkLabel(opts, text="", text_color="gray50")
        self._status_lbl.pack(side="left", padx=12)

        self._log = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Courier", size=12),
                                   state="disabled", wrap="none")
        self._log.grid(row=2, column=0, padx=24, pady=(0, 20), sticky="nsew")

    def _on_run(self) -> None:
        raw = self._limit_var.get().strip()
        limit = int(raw) if raw.isdigit() else None
        self.app.run_search({
            "no_cache": self._no_cache_var.get(),
            "verbose":  self._verbose_var.get(),
            "limit":    limit,
            "no_notify": True,
            "no_db":    False,
        })

    def _on_clear_cache(self) -> None:
        try:
            cfg = jmf.load_config()
            path = cfg.get("cache", {}).get("path", str(jmf.DEFAULT_CACHE_PATH))
            jmf.JobCache(path).clear()
            messagebox.showinfo("Cache Cleared", "Job cache has been cleared.")
        except Exception as exc:
            messagebox.showerror("Error", str(exc))

    def append_log(self, msg: str) -> None:
        self._log.configure(state="normal")
        self._log.insert("end", msg)
        self._log.see("end")
        self._log.configure(state="disabled")

    def clear_log(self) -> None:
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def set_running(self, running: bool) -> None:
        if running:
            self._run_btn.configure(state="disabled", text="⏳  Running…")
            self._status_lbl.configure(text="Search in progress…")
        else:
            self._run_btn.configure(state="normal", text="▶  Run Search")
            self._status_lbl.configure(text="Done.")


# ─────────────────────────────────────────────────────────────────────────────
# Detail Panel
# ─────────────────────────────────────────────────────────────────────────────

class DetailPanel(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, results_page: "ResultsPage"):
        super().__init__(parent, corner_radius=8, border_width=1,
                         border_color=("gray80", "gray30"))
        self.results_page = results_page
        self._url: str | None = None
        self._build()
        self._show_empty()

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)

        # Left: job info
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, padx=16, pady=12, sticky="nw")

        self._title_lbl = ctk.CTkLabel(
            left, text="", font=ctk.CTkFont(size=14, weight="bold"),
            wraplength=340, justify="left", anchor="w")
        self._title_lbl.grid(row=0, column=0, columnspan=2, sticky="w")

        self._co_lbl = ctk.CTkLabel(left, text="", text_color="gray50")
        self._co_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))

        ctk.CTkLabel(left, text="Keywords:", text_color="gray50",
                     font=ctk.CTkFont(size=11), width=80, anchor="e"
                     ).grid(row=2, column=0, sticky="ne", padx=(0, 6))
        self._kw_lbl = ctk.CTkLabel(
            left, text="", wraplength=360, justify="left",
            font=ctk.CTkFont(size=11), anchor="w")
        self._kw_lbl.grid(row=2, column=1, sticky="w")

        self._url_btn = ctk.CTkButton(
            left, text="Open in Browser →", width=160, anchor="w",
            fg_color="transparent", text_color="#2563eb",
            hover_color=("gray90", "gray20"), command=self._open_url)
        self._url_btn.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # Right: status + notes
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, padx=16, pady=12, sticky="ne")

        ctk.CTkLabel(right, text="Status").grid(row=0, column=0, sticky="w")
        self._status_var = tk.StringVar(value="new")
        self._status_menu = ctk.CTkOptionMenu(
            right, values=STATUS_OPTIONS, variable=self._status_var, width=140)
        self._status_menu.grid(row=1, column=0, pady=(2, 10), sticky="w")

        ctk.CTkLabel(right, text="Notes").grid(row=2, column=0, sticky="w")
        self._notes = ctk.CTkTextbox(right, height=68, width=250)
        self._notes.grid(row=3, column=0, pady=(2, 8), sticky="ew")

        ctk.CTkButton(right, text="Save", width=100,
                      command=self._save).grid(row=4, column=0, sticky="e")

    def _show_empty(self) -> None:
        self._title_lbl.configure(text="Select a job to see details")
        self._co_lbl.configure(text="")
        self._kw_lbl.configure(text="")
        self._url_btn.configure(state="disabled")
        self._status_menu.configure(state="disabled")
        self._notes.configure(state="disabled")

    def load(self, row: dict) -> None:
        self._url = row.get("job_url")
        self._title_lbl.configure(text=row.get("title") or "Untitled")
        co   = row.get("company") or ""
        site = row.get("site") or ""
        self._co_lbl.configure(text=f"{co}  •  {site}".strip(" •"))
        self._kw_lbl.configure(text=row.get("matched_keywords") or "—")
        self._url_btn.configure(state="normal")
        self._status_var.set(row.get("status") or "new")
        self._status_menu.configure(state="normal")
        self._notes.configure(state="normal")
        self._notes.delete("1.0", "end")
        self._notes.insert("1.0", row.get("notes") or "")

    def _open_url(self) -> None:
        if self._url:
            webbrowser.open(self._url)

    def _save(self) -> None:
        if not self._url:
            return
        status = self._status_var.get()
        notes  = self._notes.get("1.0", "end").strip()
        try:
            cfg = jmf.load_config()
            db_path = cfg.get("database", {}).get("path", str(jmf.DEFAULT_DB_PATH))
            conn = sqlite3.connect(db_path)
            conn.execute("UPDATE jobs SET status=?, notes=? WHERE job_url=?",
                         (status, notes, self._url))
            conn.commit()
            conn.close()
            self.results_page.update_row_status(self._url, status)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Results Page
# ─────────────────────────────────────────────────────────────────────────────

_TV_COLS = [
    # (id,         label,      width, stretch)
    ("score",    "Score %",    65,   True),
    ("title",    "Title",     220,   False),
    ("company",  "Company",   155,   False),
    ("location", "Location",  110,   False),
    ("salary",   "Salary",    110,   False),
    ("site",     "Site",       80,   False),
    ("status",   "Status",     80,   False),
    ("posted",   "Posted",     92,   False),
]


class ResultsPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, app: "JobMatchApp"):
        super().__init__(parent, corner_radius=0, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._sort_col = "score"
        self._sort_asc = False
        self._all_rows: list[dict] = []
        self._build()

    def _build(self) -> None:
        ctk.CTkLabel(self, text="Job Results",
                     font=ctk.CTkFont(size=20, weight="bold")
                     ).grid(row=0, column=0, padx=24, pady=(20, 0), sticky="w")

        # Filter bar
        fbar = ctk.CTkFrame(self, fg_color="transparent")
        fbar.grid(row=1, column=0, padx=24, pady=(8, 4), sticky="ew")

        ctk.CTkLabel(fbar, text="Filter:").pack(side="left", padx=(0, 4))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filters())
        ctk.CTkEntry(fbar, textvariable=self._search_var, width=220,
                     placeholder_text="title / company…").pack(side="left", padx=(0, 14))

        ctk.CTkLabel(fbar, text="Status:").pack(side="left", padx=(0, 4))
        self._status_filter = ctk.CTkOptionMenu(
            fbar, values=["All"] + STATUS_OPTIONS, width=110,
            command=lambda _: self._apply_filters())
        self._status_filter.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(fbar, text="Min score:").pack(side="left", padx=(0, 4))
        self._min_score_var = tk.IntVar(value=0)
        ctk.CTkSlider(fbar, from_=0, to=100, variable=self._min_score_var,
                      width=110, command=lambda _: self._apply_filters()
                      ).pack(side="left", padx=(0, 4))
        self._score_lbl = ctk.CTkLabel(fbar, text="0%", width=36)
        self._score_lbl.pack(side="left", padx=(0, 14))

        ctk.CTkButton(fbar, text="↻ Refresh", width=88,
                      command=self.refresh).pack(side="left", padx=(0, 6))
        ctk.CTkButton(fbar, text="Export CSV", width=96,
                      fg_color=("gray70", "gray40"), hover_color=("gray60", "gray30"),
                      command=self._export_csv).pack(side="left")

        # Treeview
        tv_frame = tk.Frame(self, bg="#ebebeb")
        tv_frame.grid(row=2, column=0, padx=24, pady=4, sticky="nsew")
        tv_frame.grid_columnconfigure(0, weight=1)
        tv_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Jobs.Treeview",
                        background="white", fieldbackground="white",
                        rowheight=26, font=("Helvetica", 11))
        style.configure("Jobs.Treeview.Heading",
                        font=("Helvetica", 11, "bold"), background="#e5e7eb",
                        relief="flat")
        style.map("Jobs.Treeview",
                  background=[("selected", "#bfdbfe")],
                  foreground=[("selected", "#1e3a5f")])

        cols = [c[0] for c in _TV_COLS]
        self._tv = ttk.Treeview(tv_frame, columns=cols, show="headings",
                                style="Jobs.Treeview", selectmode="browse")
        for col_id, label, width, stretch in _TV_COLS:
            self._tv.heading(col_id, text=label,
                             command=lambda c=col_id: self._sort_by(c))
            self._tv.column(col_id, width=width, minwidth=40,
                            stretch=tk.YES if stretch else tk.NO)

        vsb = ttk.Scrollbar(tv_frame, orient="vertical",   command=self._tv.yview)
        hsb = ttk.Scrollbar(tv_frame, orient="horizontal", command=self._tv.xview)
        self._tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        self._tv.bind("<<TreeviewSelect>>", self._on_select)

        self._count_lbl = ctk.CTkLabel(self, text="0 jobs", text_color="gray50")
        self._count_lbl.grid(row=3, column=0, padx=24, pady=(2, 4), sticky="w")

        self._detail = DetailPanel(self, self)
        self._detail.grid(row=4, column=0, padx=24, pady=(0, 16), sticky="ew")

    # ── Data ──────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        cfg = jmf.load_config()
        db_path = Path(cfg.get("database", {}).get("path", str(jmf.DEFAULT_DB_PATH)))
        if not db_path.exists():
            self._all_rows = []
            self._populate([])
            return
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY match_score_pct DESC").fetchall()
        conn.close()
        self._all_rows = [dict(r) for r in rows]
        self._apply_filters()

    def _apply_filters(self) -> None:
        q     = self._search_var.get().lower()
        stat  = self._status_filter.get()
        min_s = self._min_score_var.get()
        self._score_lbl.configure(text=f"{min_s}%")

        filtered = []
        for r in self._all_rows:
            if min_s and (r.get("match_score_pct") or 0) < min_s:
                continue
            if stat != "All" and r.get("status") != stat:
                continue
            if q and q not in (r.get("title") or "").lower() \
                  and q not in (r.get("company") or "").lower():
                continue
            filtered.append(r)

        key_map: dict[str, Any] = {
            "score":    lambda r: r.get("match_score_pct") or 0,
            "title":    lambda r: (r.get("title") or "").lower(),
            "company":  lambda r: (r.get("company") or "").lower(),
            "location": lambda r: (r.get("city") or "").lower(),
            "status":   lambda r: r.get("status") or "",
            "posted":   lambda r: r.get("date_posted") or "",
            "site":     lambda r: r.get("site") or "",
            "salary":   lambda r: r.get("max_amount") or 0,
        }
        fn = key_map.get(self._sort_col, lambda r: 0)
        filtered.sort(key=fn, reverse=not self._sort_asc)
        self._populate(filtered)

    def _populate(self, rows: list[dict]) -> None:
        self._tv.delete(*self._tv.get_children())
        for r in rows:
            score = r.get("match_score_pct")
            score_str = f"{score:.1f}%" if score is not None else "—"

            city  = r.get("city") or ""
            state = r.get("state") or ""
            loc   = ", ".join(p for p in (city, state) if p) or "—"

            mn, mx = r.get("min_amount"), r.get("max_amount")
            if mx and mn:
                sal = f"${int(mn):,}–${int(mx):,}"
            elif mx:
                sal = f"${int(mx):,}"
            elif mn:
                sal = f"${int(mn):,}+"
            else:
                sal = "—"

            status = r.get("status") or "new"
            iid = str(r.get("id") or "") or (r.get("job_url") or "")
            self._tv.insert("", "end", iid=iid, tags=(status,), values=(
                score_str,
                r.get("title") or "—",
                r.get("company") or "—",
                loc,
                sal,
                r.get("site") or "—",
                status,
                r.get("date_posted") or "—",
            ))

        self._tv.tag_configure("new",      foreground="#1d4ed8")
        self._tv.tag_configure("saved",    foreground="#6d28d9")
        self._tv.tag_configure("applied",  foreground="#065f46")
        self._tv.tag_configure("rejected", foreground="#b91c1c")
        self._tv.tag_configure("ignored",  foreground="#6b7280")

        n = len(rows)
        self._count_lbl.configure(text=f"{n} job{'s' if n != 1 else ''}")

    def _sort_by(self, col: str) -> None:
        self._sort_asc = not self._sort_asc if self._sort_col == col else (col != "score")
        self._sort_col = col
        self._apply_filters()

    def _on_select(self, _=None) -> None:
        sel = self._tv.selection()
        if not sel:
            return
        iid = sel[0]
        row = next(
            (r for r in self._all_rows
             if str(r.get("id") or "") == iid or r.get("job_url") == iid),
            None,
        )
        if row:
            self._detail.load(row)

    def update_row_status(self, url: str, new_status: str) -> None:
        for r in self._all_rows:
            if r.get("job_url") == url:
                r["status"] = new_status
        self._apply_filters()

    def _export_csv(self) -> None:
        try:
            import pandas as pd
            if not self._all_rows:
                messagebox.showinfo("Export", "No data to export.")
                return
            pd.DataFrame(self._all_rows).to_csv("job_matches.csv", index=False)
            messagebox.showinfo("Exported", "Saved to job_matches.csv")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Config Page
# ─────────────────────────────────────────────────────────────────────────────

class ConfigPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, app: "JobMatchApp"):
        super().__init__(parent, corner_radius=0, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=0, column=0, sticky="nsew")
        self._scroll.grid_columnconfigure(0, weight=1)

        self._build()
        self._load()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _section(self, title: str, row: int) -> ctk.CTkFrame:
        ctk.CTkLabel(self._scroll, text=title,
                     font=ctk.CTkFont(size=14, weight="bold")
                     ).grid(row=row, column=0, padx=24, pady=(16, 4), sticky="w")
        frame = ctk.CTkFrame(self._scroll, fg_color=("gray95", "gray15"),
                             corner_radius=8)
        frame.grid(row=row + 1, column=0, padx=24, pady=(0, 4), sticky="ew")
        frame.grid_columnconfigure(1, weight=1)
        return frame

    def _row(self, frame: ctk.CTkFrame, label: str, row: int,
             multiline: bool = False, height: int = 80) -> ctk.CTkEntry | ctk.CTkTextbox:
        ctk.CTkLabel(frame, text=label, width=180, anchor="e"
                     ).grid(row=row, column=0, padx=(12, 8), pady=6, sticky="ne" if multiline else "e")
        if multiline:
            w = ctk.CTkTextbox(frame, height=height, wrap="word")
        else:
            w = ctk.CTkEntry(frame)
        w.grid(row=row, column=1, padx=(0, 12), pady=6, sticky="ew")
        return w

    def _cb_row(self, frame: ctk.CTkFrame, label: str, row: int) -> tk.BooleanVar:
        ctk.CTkLabel(frame, text=label, width=180, anchor="e"
                     ).grid(row=row, column=0, padx=(12, 8), pady=6, sticky="e")
        v = tk.BooleanVar()
        ctk.CTkCheckBox(frame, text="", variable=v
                        ).grid(row=row, column=1, padx=(0, 12), pady=6, sticky="w")
        return v

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        s = self._scroll
        ctk.CTkLabel(s, text="Configuration",
                     font=ctk.CTkFont(size=20, weight="bold")
                     ).grid(row=0, column=0, padx=24, pady=(20, 0), sticky="w")

        # Profile
        pf = self._section("Profile", 1)
        self._f_name      = self._row(pf, "Profile Name",      0)
        self._f_location  = self._row(pf, "Location",          1)
        self._f_distance  = self._row(pf, "Distance (miles)",  2)
        self._f_job_type  = self._row(pf, "Job Type",          3)
        self._f_hours_old = self._row(pf, "Hours Old",         4)
        self._f_results   = self._row(pf, "Results Wanted",    5)

        ctk.CTkLabel(pf, text="Sites", width=180, anchor="e"
                     ).grid(row=6, column=0, padx=(12, 8), pady=6, sticky="e")
        sf = ctk.CTkFrame(pf, fg_color="transparent")
        sf.grid(row=6, column=1, sticky="w", padx=(0, 12), pady=6)
        self._site_vars: dict[str, tk.BooleanVar] = {}
        for s_name in ["linkedin", "indeed", "google", "glassdoor", "zip_recruiter"]:
            v = tk.BooleanVar(value=False)
            self._site_vars[s_name] = v
            ctk.CTkCheckBox(sf, text=s_name, variable=v).pack(side="left", padx=6)

        # Search Terms
        st = self._section("Search Terms", 3)
        ctk.CTkLabel(st, text="One per line", width=180, anchor="e"
                     ).grid(row=0, column=0, padx=(12, 8), pady=6, sticky="ne")
        self._f_terms = ctk.CTkTextbox(st, height=90, wrap="word")
        self._f_terms.grid(row=0, column=1, padx=(0, 12), pady=6, sticky="ew")

        # Skills
        sk = self._section("Skills  (one keyword per line)", 5)
        ctk.CTkLabel(sk, text="Tier 1  (3 pts each)", width=180,
                     anchor="e").grid(row=0, column=0, padx=(12, 8), pady=6, sticky="ne")
        self._f_t1 = ctk.CTkTextbox(sk, height=90, wrap="word")
        self._f_t1.grid(row=0, column=1, padx=(0, 12), pady=6, sticky="ew")

        ctk.CTkLabel(sk, text="Tier 2  (2 pts each)", width=180,
                     anchor="e").grid(row=1, column=0, padx=(12, 8), pady=6, sticky="ne")
        self._f_t2 = ctk.CTkTextbox(sk, height=90, wrap="word")
        self._f_t2.grid(row=1, column=1, padx=(0, 12), pady=6, sticky="ew")

        ctk.CTkLabel(sk, text="Tier 3  (1 pt each)", width=180,
                     anchor="e").grid(row=2, column=0, padx=(12, 8), pady=6, sticky="ne")
        self._f_t3 = ctk.CTkTextbox(sk, height=90, wrap="word")
        self._f_t3.grid(row=2, column=1, padx=(0, 12), pady=6, sticky="ew")

        # Scoring
        sc = self._section("Scoring", 7)
        self._f_title_bonus  = self._row(sc, "Title Bonus (%/match)",    0)
        self._f_sal_thresh   = self._row(sc, "Salary Threshold ($)",     1)
        self._f_sal_boost    = self._row(sc, "Salary Boost (%)",         2)
        self._f_remote_boost = self._row(sc, "Remote Boost (%)",         3)
        self._fuzzy_var      = self._cb_row(sc, "Fuzzy Matching",        4)
        self._f_fuzzy_thresh = self._row(sc, "Fuzzy Threshold (0–100)",  5)

        # Notifications
        nt = self._section("Email Notifications", 9)
        self._email_en_var  = self._cb_row(nt, "Enabled",           0)
        self._f_smtp_server = self._row(nt, "SMTP Server",           1)
        self._f_smtp_port   = self._row(nt, "SMTP Port",             2)
        self._f_sender      = self._row(nt, "Sender Email",          3)
        self._f_recipient   = self._row(nt, "Recipient Email",       4)
        self._f_min_score   = self._row(nt, "Min Score (%) to notify", 5)

        # Cache & DB
        cd = self._section("Cache & Database", 11)
        self._cache_en_var = self._cb_row(cd, "Cache Enabled",       0)
        self._f_cache_ttl  = self._row(cd,  "Cache TTL (minutes)",   1)
        self._db_en_var    = self._cb_row(cd, "Database Enabled",    2)

        ctk.CTkButton(s, text="💾  Save Config", width=160,
                      command=self._save
                      ).grid(row=13, column=0, padx=24, pady=(12, 28), sticky="e")

    # ── Load / Save ───────────────────────────────────────────────────────────

    @staticmethod
    def _set_entry(w: ctk.CTkEntry, val: Any) -> None:
        w.delete(0, "end")
        w.insert(0, str(val))

    @staticmethod
    def _set_tb(w: ctk.CTkTextbox, lines: list[str]) -> None:
        w.delete("1.0", "end")
        w.insert("1.0", "\n".join(lines))

    @staticmethod
    def _get_tb(w: ctk.CTkTextbox) -> list[str]:
        return [ln.strip() for ln in w.get("1.0", "end").splitlines() if ln.strip()]

    def _load(self) -> None:
        try:
            cfg = jmf.load_config()
        except Exception:
            return

        p = cfg.get("profile", {})
        self._set_entry(self._f_name,      p.get("name", ""))
        self._set_entry(self._f_location,  p.get("location", ""))
        self._set_entry(self._f_distance,  p.get("distance_miles", 50))
        self._set_entry(self._f_job_type,  p.get("job_type", "fulltime"))
        self._set_entry(self._f_hours_old, p.get("hours_old", 168))
        self._set_entry(self._f_results,   p.get("results_wanted", 25))

        for s, v in self._site_vars.items():
            v.set(s in p.get("sites", []))
        self._set_tb(self._f_terms, p.get("search_terms", []))

        sk = p.get("skills", {})
        self._set_tb(self._f_t1, sk.get("tier1", []))
        self._set_tb(self._f_t2, sk.get("tier2", []))
        self._set_tb(self._f_t3, sk.get("tier3", []))

        sc = cfg.get("scoring", {})
        self._set_entry(self._f_title_bonus,  sc.get("title_bonus_per_match", 3))
        sal = sc.get("salary_boost", {})
        self._set_entry(self._f_sal_thresh,   sal.get("annual_threshold", 80000))
        self._set_entry(self._f_sal_boost,    sal.get("boost_pct", 10))
        self._set_entry(self._f_remote_boost, sc.get("remote_boost_pct", 5))
        fz = sc.get("fuzzy", {})
        self._fuzzy_var.set(fz.get("enabled", True))
        self._set_entry(self._f_fuzzy_thresh, fz.get("threshold", 80))

        em = cfg.get("notifications", {}).get("email", {})
        self._email_en_var.set(em.get("enabled", False))
        self._set_entry(self._f_smtp_server, em.get("smtp_server", "smtp.gmail.com"))
        self._set_entry(self._f_smtp_port,   em.get("smtp_port", 587))
        self._set_entry(self._f_sender,      em.get("sender_email", ""))
        self._set_entry(self._f_recipient,   em.get("recipient_email", ""))
        self._set_entry(self._f_min_score,   em.get("min_score", 70))

        ca = cfg.get("cache", {})
        self._cache_en_var.set(ca.get("enabled", True))
        self._set_entry(self._f_cache_ttl, ca.get("ttl_minutes", 30))
        self._db_en_var.set(cfg.get("database", {}).get("enabled", True))

    def _save(self) -> None:
        try:
            import yaml as _yaml
        except ImportError:
            messagebox.showerror("Missing dependency",
                                 "Install PyYAML first:  pip install pyyaml")
            return

        def _int(val: Any, default: int) -> int:
            try:
                return int(str(val).strip())
            except (ValueError, TypeError):
                return default

        cfg = {
            "profile": {
                "name":           self._f_name.get(),
                "location":       self._f_location.get(),
                "distance_miles": _int(self._f_distance.get(), 50),
                "job_type":       self._f_job_type.get(),
                "hours_old":      _int(self._f_hours_old.get(), 168),
                "results_wanted": _int(self._f_results.get(), 25),
                "sites":          [s for s, v in self._site_vars.items() if v.get()],
                "search_terms":   self._get_tb(self._f_terms),
                "skills": {
                    "tier1": self._get_tb(self._f_t1),
                    "tier2": self._get_tb(self._f_t2),
                    "tier3": self._get_tb(self._f_t3),
                },
            },
            "scoring": {
                "tier_weights": {"tier1": 3, "tier2": 2, "tier3": 1},
                "title_bonus_per_match": _int(self._f_title_bonus.get(), 3),
                "salary_boost": {
                    "enabled":          True,
                    "annual_threshold": _int(self._f_sal_thresh.get(), 80000),
                    "boost_pct":        _int(self._f_sal_boost.get(), 10),
                },
                "remote_boost_pct": _int(self._f_remote_boost.get(), 5),
                "fuzzy": {
                    "enabled":   self._fuzzy_var.get(),
                    "threshold": _int(self._f_fuzzy_thresh.get(), 80),
                },
            },
            "notifications": {
                "email": {
                    "enabled":         self._email_en_var.get(),
                    "smtp_server":     self._f_smtp_server.get(),
                    "smtp_port":       _int(self._f_smtp_port.get(), 587),
                    "sender_email":    self._f_sender.get(),
                    "recipient_email": self._f_recipient.get(),
                    "min_score":       _int(self._f_min_score.get(), 70),
                },
            },
            "cache": {
                "enabled":     self._cache_en_var.get(),
                "ttl_minutes": _int(self._f_cache_ttl.get(), 30),
            },
            "database": {"enabled": self._db_en_var.get()},
        }

        with open(jmf.DEFAULT_CONFIG_PATH, "w") as fh:
            _yaml.dump(cfg, fh, default_flow_style=False,
                       allow_unicode=True, sort_keys=False)
        messagebox.showinfo("Saved", f"Config saved to {jmf.DEFAULT_CONFIG_PATH}")


# ─────────────────────────────────────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────────────────────────────────────

class JobMatchApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Job Match Finder")
        self.geometry(f"{WIN_W}x{WIN_H}")
        self.minsize(900, 620)

        self._log_q: queue.Queue = queue.Queue()
        self._search_thread: threading.Thread | None = None

        self._build_ui()
        self._show_page("search")
        self._poll_log_queue()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        sidebar = ctk.CTkFrame(self, width=SIDEBAR_W, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_rowconfigure(10, weight=1)
        sidebar.grid_propagate(False)

        ctk.CTkLabel(sidebar, text="Job Match\nFinder",
                     font=ctk.CTkFont(size=15, weight="bold"), justify="center"
                     ).grid(row=0, column=0, padx=16, pady=(28, 24), sticky="ew")

        self._nav: dict[str, ctk.CTkButton] = {}
        for i, (key, label) in enumerate([
            ("search",  "🔍   Search"),
            ("results", "📋   Results"),
            ("config",  "⚙️   Config"),
        ], start=1):
            btn = ctk.CTkButton(
                sidebar, text=label, anchor="w", height=42, corner_radius=8,
                fg_color="transparent", text_color=("gray10", "gray90"),
                hover_color=("gray85", "gray25"),
                command=lambda k=key: self._show_page(k),
            )
            btn.grid(row=i, column=0, padx=10, pady=4, sticky="ew")
            self._nav[key] = btn

        # Pages — all stacked in same cell; raise the active one
        self._pages: dict[str, ctk.CTkFrame] = {
            "search":  SearchPage(self, self),
            "results": ResultsPage(self, self),
            "config":  ConfigPage(self, self),
        }
        for page in self._pages.values():
            page.grid(row=0, column=1, sticky="nsew")

    def _show_page(self, key: str) -> None:
        self._pages[key].tkraise()
        for k, btn in self._nav.items():
            btn.configure(fg_color=("gray75", "gray35") if k == key else "transparent")
        if key == "results":
            self._pages["results"].refresh()

    def _poll_log_queue(self) -> None:
        sp: SearchPage = self._pages["search"]
        try:
            while True:
                sp.append_log(self._log_q.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_log_queue)

    def run_search(self, options: dict) -> None:
        if self._search_thread and self._search_thread.is_alive():
            messagebox.showwarning("In Progress", "A search is already running.")
            return
        self._pages["search"].clear_log()
        self._pages["search"].set_running(True)
        self._search_thread = threading.Thread(
            target=self._search_worker, args=(options,), daemon=True)
        self._search_thread.start()

    def _search_worker(self, options: dict) -> None:
        # Route all logger output to our queue
        for h in list(jmf.logger.handlers):
            jmf.logger.removeHandler(h)
        qh = _QueueHandler(self._log_q)
        jmf.logger.addHandler(qh)
        jmf.logger.setLevel(logging.DEBUG if options.get("verbose") else logging.INFO)

        old_stdout = sys.stdout
        sys.stdout = _QueueWriter(self._log_q)  # type: ignore[assignment]
        try:
            config = jmf.load_config(options.get("config_path"))
            jmf.run(
                config,
                limit=options.get("limit"),
                no_cache=options.get("no_cache", False),
                no_notify=options.get("no_notify", True),
                no_db=options.get("no_db", False),
            )
            self._log_q.put("\n✅  Search complete — switching to Results…\n")
        except Exception as exc:
            self._log_q.put(f"\n❌  Error: {exc}\n")
        finally:
            sys.stdout = old_stdout
            jmf.logger.removeHandler(qh)
            self.after(0, self._on_search_done)

    def _on_search_done(self) -> None:
        self._pages["search"].set_running(False)
        self._show_page("results")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    app = JobMatchApp()
    app.mainloop()


if __name__ == "__main__":
    main()
