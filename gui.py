#!/usr/bin/env python3
"""
Job Match Finder – GUI
Run with: python gui.py
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox

import job_match_finder as jmf

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

WIN_W, WIN_H = 1380, 900
SIDEBAR_W = 210
STATUS_OPTIONS = ["new", "saved", "applied", "rejected", "ignored"]
_MAX_DB_ROWS = 500
_MAX_CARD_ROWS = 200

STATUS_COLORS = {
    "new":      "#3b82f6",
    "saved":    "#8b5cf6",
    "applied":  "#10b981",
    "rejected": "#ef4444",
    "ignored":  "#64748b",
}


def _score_color(score: float) -> str:
    if score >= 75:
        return "#10b981"
    if score >= 50:
        return "#3b82f6"
    if score >= 25:
        return "#f59e0b"
    return "#64748b"


# ─────────────────────────────────────────────────────────────────────────────
# Log capture
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
# DB helpers (lightweight, for GUI-only reads/writes)
# ─────────────────────────────────────────────────────────────────────────────

def _db_path() -> str:
    cfg = jmf.load_config()
    return cfg.get("database", {}).get("path", str(jmf.DEFAULT_DB_PATH))


def _fetch_status_history(job_url: str) -> list[dict]:
    try:
        conn = sqlite3.connect(_db_path())
        rows = conn.execute(
            "SELECT status, changed_at FROM status_history "
            "WHERE job_url=? ORDER BY changed_at",
            (job_url,),
        ).fetchall()
        conn.close()
        return [{"status": r[0], "changed_at": r[1]} for r in rows]
    except Exception:
        return []


def _get_latest_run_at() -> str | None:
    try:
        path = _db_path()
        if not Path(path).exists():
            return None
        conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT started_at FROM run_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_dashboard_stats() -> dict | None:
    try:
        path = _db_path()
        if not Path(path).exists():
            return None
        conn = sqlite3.connect(path)

        score_dist: dict[str, int] = {}
        for label, lo, hi in [("<25", 0, 25), ("25-50", 25, 50), ("50-75", 50, 75), ("75+", 75, 101)]:
            count = conn.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE match_score_pct >= ? AND match_score_pct < ?",
                (lo, hi),
            ).fetchone()[0]
            score_dist[label] = count

        activity = conn.execute(
            """SELECT substr(first_seen,1,10) AS day, COUNT(*) AS cnt
               FROM jobs WHERE first_seen >= datetime('now','-30 days')
               GROUP BY day ORDER BY day"""
        ).fetchall()

        applications = conn.execute(
            """SELECT substr(changed_at,1,10) AS day, COUNT(*) AS cnt
               FROM status_history WHERE status='applied'
               AND changed_at >= datetime('now','-30 days')
               GROUP BY day ORDER BY day"""
        ).fetchall()

        top_cos = conn.execute(
            """SELECT company, COUNT(*) AS cnt FROM jobs
               WHERE company IS NOT NULL AND company!=''
               GROUP BY company ORDER BY cnt DESC LIMIT 10"""
        ).fetchall()

        st_counts = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
        ).fetchall()

        conn.close()
        return {
            "score_dist":    score_dist,
            "activity":      [(r[0], r[1]) for r in activity],
            "applications":  [(r[0], r[1]) for r in applications],
            "top_companies": [(r[0], r[1]) for r in top_cos],
            "status_counts": {r[0]: r[1] for r in st_counts},
        }
    except Exception:
        return None


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    return "\n".join(
        f"{h.get('changed_at','')[:10]}  →  {h.get('status','')}"
        for h in history
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tooltip
# ─────────────────────────────────────────────────────────────────────────────

class Tooltip:
    def __init__(self, widget: tk.Widget, text_func):
        self._widget = widget
        self._text_func = text_func
        self._win: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _=None) -> None:
        text = self._text_func()
        if not text:
            return
        x = self._widget.winfo_rootx() + 8
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._win = tk.Toplevel(self._widget)
        self._win.wm_overrideredirect(True)
        self._win.wm_geometry(f"+{x}+{y}")
        self._win.configure(bg="#1e293b")
        tk.Label(
            self._win, text=text, justify="left",
            bg="#1e293b", fg="#e2e8f0",
            font=("Helvetica", 10),
            padx=10, pady=7,
        ).pack()

    def _hide(self, _=None) -> None:
        if self._win:
            self._win.destroy()
            self._win = None


# ─────────────────────────────────────────────────────────────────────────────
# Canvas charts
# ─────────────────────────────────────────────────────────────────────────────

class BarChart(tk.Canvas):
    def __init__(self, parent, title: str, data: list[tuple[str, int]],
                 bar_colors: list[str] | str = "#3b82f6", **kwargs):
        super().__init__(parent, bg="#0f172a", highlightthickness=0, **kwargs)
        self._title = title
        self._data = data
        self._bar_colors = bar_colors
        self.bind("<Configure>", lambda _: self._draw())

    def update_data(self, data: list[tuple[str, int]]) -> None:
        self._data = data
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10 or not self._data:
            return
        PL, PR, PT, PB = 14, 14, 30, 46
        self.create_text(w // 2, 14, text=self._title,
                         fill="#94a3b8", font=("Helvetica", 10, "bold"))
        max_val = max(v for _, v in self._data) or 1
        n = len(self._data)
        cw = w - PL - PR
        ch = h - PT - PB
        slot = cw / n
        bw = slot * 0.58
        for i, (label, val) in enumerate(self._data):
            x0 = PL + i * slot + (slot - bw) / 2
            x1 = x0 + bw
            bar_h = max(2, (val / max_val) * ch)
            y0 = PT + ch - bar_h
            y1 = PT + ch
            color = (self._bar_colors[i]
                     if isinstance(self._bar_colors, list)
                     else self._bar_colors)
            self.create_rectangle(x0, y0, x1, y1, fill=color, outline="")
            if val > 0:
                self.create_text((x0 + x1) / 2, y0 - 5, text=str(val),
                                 fill="#cbd5e1", font=("Helvetica", 9))
            self.create_text((x0 + x1) / 2, h - PB + 16,
                             text=label, fill="#64748b", font=("Helvetica", 9))


class HBarChart(tk.Canvas):
    def __init__(self, parent, title: str, data: list[tuple[str, int]], **kwargs):
        super().__init__(parent, bg="#0f172a", highlightthickness=0, **kwargs)
        self._title = title
        self._data = data
        self.bind("<Configure>", lambda _: self._draw())

    def update_data(self, data: list[tuple[str, int]]) -> None:
        self._data = data
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        self.create_text(w // 2, 14, text=self._title,
                         fill="#94a3b8", font=("Helvetica", 10, "bold"))
        PL, PR, PT = 148, 54, 30
        data = self._data[:10]
        if not data:
            self.create_text(w // 2, h // 2, text="No data yet",
                             fill="#334155", font=("Helvetica", 11))
            return
        n = len(data)
        ch = h - PT - 10
        slot = ch / n
        bh = slot * 0.58
        max_val = max(v for _, v in data) or 1
        bw = w - PL - PR
        for i, (label, val) in enumerate(data):
            y0 = PT + i * slot + (slot - bh) / 2
            y1 = y0 + bh
            x1 = PL + (val / max_val) * bw
            self.create_rectangle(PL, y0, x1, y1, fill="#3b82f6", outline="")
            lbl = (label[:17] + "…") if len(label) > 17 else label
            self.create_text(PL - 6, (y0 + y1) / 2, text=lbl,
                             fill="#94a3b8", font=("Helvetica", 9), anchor="e")
            self.create_text(x1 + 5, (y0 + y1) / 2, text=str(val),
                             fill="#64748b", font=("Helvetica", 9), anchor="w")


class DualLineChart(tk.Canvas):
    def __init__(self, parent, title: str, **kwargs):
        super().__init__(parent, bg="#0f172a", highlightthickness=0, **kwargs)
        self._title = title
        self._series: list[tuple[str, list[tuple[str, int]], str]] = []
        self.bind("<Configure>", lambda _: self._draw())

    def update_series(self, series: list[tuple[str, list[tuple[str, int]], str]]) -> None:
        self._series = series
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        self.create_text(w // 2, 14, text=self._title,
                         fill="#94a3b8", font=("Helvetica", 10, "bold"))
        PL, PR, PT, PB = 44, 20, 30, 44
        all_dates = sorted({d for _, pts, _ in self._series for d, _ in pts})
        if not all_dates:
            self.create_text(w // 2, h // 2, text="No data yet",
                             fill="#334155", font=("Helvetica", 11))
            return
        all_vals = [v for _, pts, _ in self._series for _, v in pts]
        max_val = max(all_vals) if all_vals else 1
        cw = w - PL - PR
        ch = h - PT - PB
        n = len(all_dates)
        di = {d: i for i, d in enumerate(all_dates)}
        lx = PL
        for name, _, color in self._series:
            self.create_rectangle(lx, h - PB + 22, lx + 10, h - PB + 30,
                                   fill=color, outline="")
            self.create_text(lx + 14, h - PB + 26, text=name, fill="#64748b",
                             font=("Helvetica", 8), anchor="w")
            lx += len(name) * 6 + 26
        for _, pts, color in self._series:
            if not pts:
                continue
            pts_d = dict(pts)
            coords: list[float] = []
            for date in all_dates:
                val = pts_d.get(date, 0)
                x = PL + (di[date] / max(n - 1, 1)) * cw
                y = PT + ch - (val / max_val) * ch
                coords.extend([x, y])
            if len(coords) >= 4:
                self.create_line(coords, fill=color, width=2, smooth=True)
            for i in range(0, len(coords), 2):
                x2, y2 = coords[i], coords[i + 1]
                self.create_oval(x2 - 3, y2 - 3, x2 + 3, y2 + 3,
                                  fill=color, outline="")
        step = max(1, n // 7)
        for i in range(0, n, step):
            x = PL + (i / max(n - 1, 1)) * cw
            self.create_text(x, h - PB + 10, text=all_dates[i][5:],
                             fill="#475569", font=("Helvetica", 8))


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

        self._status_lbl = ctk.CTkLabel(opts, text="", text_color=("gray50", "gray60"))
        self._status_lbl.pack(side="left", padx=12)

        self._log = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Courier", size=12),
                                   state="disabled", wrap="none")
        self._log.grid(row=2, column=0, padx=24, pady=(0, 20), sticky="nsew")

    def _on_run(self) -> None:
        raw = self._limit_var.get().strip()
        limit = int(raw) if raw.isdigit() else None
        self.app.run_search({
            "no_cache":  self._no_cache_var.get(),
            "verbose":   self._verbose_var.get(),
            "limit":     limit,
            "no_notify": True,
            "no_db":     False,
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

    def set_refresh_status(self, text: str) -> None:
        self._status_lbl.configure(text=text)


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

        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, padx=16, pady=12, sticky="nw")

        self._title_lbl = ctk.CTkLabel(
            left, text="", font=ctk.CTkFont(size=14, weight="bold"),
            wraplength=340, justify="left", anchor="w")
        self._title_lbl.grid(row=0, column=0, columnspan=2, sticky="w")

        self._co_lbl = ctk.CTkLabel(left, text="", text_color=("gray50", "gray60"))
        self._co_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))

        ctk.CTkLabel(left, text="Keywords:", text_color=("gray50", "gray60"),
                     font=ctk.CTkFont(size=11), width=80, anchor="e"
                     ).grid(row=2, column=0, sticky="ne", padx=(0, 6))
        self._kw_lbl = ctk.CTkLabel(
            left, text="", wraplength=360, justify="left",
            font=ctk.CTkFont(size=11), anchor="w")
        self._kw_lbl.grid(row=2, column=1, sticky="w")

        self._url_btn = ctk.CTkButton(
            left, text="Open in Browser →", width=160, anchor="w",
            fg_color="transparent", text_color="#3b82f6",
            hover_color=("gray90", "gray20"), command=self._open_url)
        self._url_btn.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

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

        self._history_lbl = ctk.CTkLabel(
            right, text="",
            font=ctk.CTkFont(size=10),
            text_color=("gray50", "#64748b"),
            justify="left", anchor="w")
        self._history_lbl.grid(row=5, column=0, pady=(6, 0), sticky="w")

    def _show_empty(self) -> None:
        self._title_lbl.configure(text="Select a job to see details")
        self._co_lbl.configure(text="")
        self._kw_lbl.configure(text="")
        self._url_btn.configure(state="disabled")
        self._status_menu.configure(state="disabled")
        self._notes.configure(state="disabled")
        self._history_lbl.configure(text="")

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
        self._reload_history()

    def _reload_history(self) -> None:
        if not self._url:
            self._history_lbl.configure(text="")
            return
        history = _fetch_status_history(self._url)
        if not history:
            self._history_lbl.configure(text="")
            return
        lines = [
            f"{h.get('changed_at','')[:10]}  →  {h.get('status','')}"
            for h in history[-6:]
        ]
        self._history_lbl.configure(text="\n".join(lines))

    def _open_url(self) -> None:
        if self._url:
            webbrowser.open(self._url)

    def _save(self) -> None:
        if not self._url:
            return
        status = self._status_var.get()
        notes  = self._notes.get("1.0", "end").strip()
        now    = datetime.now(timezone.utc).isoformat()
        try:
            conn = sqlite3.connect(_db_path())
            conn.execute(
                "UPDATE jobs SET status=?, notes=? WHERE job_url=?",
                (status, notes, self._url),
            )
            conn.execute(
                "INSERT INTO status_history (job_url, status, changed_at) "
                "VALUES (?, ?, ?)",
                (self._url, status, now),
            )
            conn.commit()
            conn.close()
            self._reload_history()
            self.results_page.update_row_status(self._url, status)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Job Card
# ─────────────────────────────────────────────────────────────────────────────

class JobCard(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, row: dict, is_new: bool,
                 on_click, **kwargs):
        super().__init__(
            parent,
            corner_radius=8,
            border_width=1,
            border_color="#334155",
            fg_color="#1e293b",
            cursor="hand2",
            **kwargs,
        )
        self._row = row
        self._on_click = on_click
        self._selected = False
        self._build(row, is_new)
        self._bind_all(self)

    def _bind_all(self, w: tk.Widget) -> None:
        w.bind("<Button-1>", lambda _: self._on_click(self._row))
        for child in w.winfo_children():
            self._bind_all(child)

    def _build(self, row: dict, is_new: bool) -> None:
        self.grid_columnconfigure(1, weight=1)

        score = row.get("match_score_pct") or 0
        color = _score_color(score)

        badge_outer = ctk.CTkFrame(self, width=56, height=56,
                                    corner_radius=28, fg_color=color)
        badge_outer.grid(row=0, column=0, padx=(12, 10), pady=10)
        badge_outer.grid_propagate(False)
        ctk.CTkLabel(
            badge_outer,
            text=f"{score:.0f}%",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="white",
        ).place(relx=0.5, rely=0.5, anchor="center")

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.grid(row=0, column=1, sticky="ew", pady=8, padx=(0, 8))
        mid.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(mid, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew")

        title_text = (row.get("title") or "Untitled")[:72]
        ctk.CTkLabel(
            title_row,
            text=title_text,
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).pack(side="left")

        if is_new:
            ctk.CTkLabel(
                title_row,
                text="  NEW",
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color="#60a5fa",
            ).pack(side="left")

        ctk.CTkLabel(
            mid,
            text=row.get("company") or "",
            font=ctk.CTkFont(size=11),
            text_color="#64748b",
            anchor="w",
        ).grid(row=1, column=0, sticky="w")

        status = row.get("status") or "new"
        sc = STATUS_COLORS.get(status, "#64748b")
        job_url = row.get("job_url", "")

        status_lbl = ctk.CTkLabel(
            self,
            text=f"  {status}  ",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color="white",
            fg_color=sc,
            corner_radius=4,
            width=76,
            height=22,
        )
        status_lbl.grid(row=0, column=2, padx=(0, 12), pady=10)

        Tooltip(
            status_lbl,
            lambda u=job_url: _format_history(_fetch_status_history(u)),
        )

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.configure(
            border_color="#3b82f6" if selected else "#334155",
            border_width=2 if selected else 1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Results Page
# ─────────────────────────────────────────────────────────────────────────────

_TV_COLS = [
    ("score",    "Score %",    68,   True),
    ("title",    "Title",     220,   False),
    ("company",  "Company",   155,   False),
    ("location", "Location",  110,   False),
    ("salary",   "Salary",    110,   False),
    ("site",     "Site",       80,   False),
    ("status",   "Status",     84,   False),
    ("posted",   "Posted",     94,   False),
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
        self._filtered_rows: list[dict] = []
        self._total_db_count: int = 0
        self._latest_run_at: str | None = None
        self._view_mode = tk.StringVar(value="Table")
        self._selected_card: JobCard | None = None
        self._cards: list[JobCard] = []
        self._build()

    def _build(self) -> None:
        ctk.CTkLabel(self, text="Job Results",
                     font=ctk.CTkFont(size=20, weight="bold")
                     ).grid(row=0, column=0, padx=24, pady=(20, 0), sticky="w")

        fbar = ctk.CTkFrame(self, fg_color="transparent")
        fbar.grid(row=1, column=0, padx=24, pady=(8, 4), sticky="ew")

        ctk.CTkLabel(fbar, text="Filter:").pack(side="left", padx=(0, 4))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filters())
        ctk.CTkEntry(fbar, textvariable=self._search_var, width=200,
                     placeholder_text="title / company…").pack(side="left", padx=(0, 12))

        ctk.CTkLabel(fbar, text="Status:").pack(side="left", padx=(0, 4))
        self._status_filter = ctk.CTkOptionMenu(
            fbar, values=["All"] + STATUS_OPTIONS, width=110,
            command=lambda _: self._apply_filters())
        self._status_filter.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(fbar, text="Min:").pack(side="left", padx=(0, 4))
        self._min_score_var = tk.IntVar(value=0)
        ctk.CTkSlider(fbar, from_=0, to=100, variable=self._min_score_var,
                      width=100, command=lambda _: self._apply_filters()
                      ).pack(side="left", padx=(0, 4))
        self._score_lbl = ctk.CTkLabel(fbar, text="0%", width=32)
        self._score_lbl.pack(side="left", padx=(0, 12))

        ctk.CTkButton(fbar, text="↻ Refresh", width=88,
                      command=self.refresh).pack(side="left", padx=(0, 6))
        ctk.CTkButton(fbar, text="Export CSV", width=96,
                      fg_color=("gray70", "gray40"), hover_color=("gray60", "gray30"),
                      command=self._export_csv).pack(side="left", padx=(0, 14))

        ctk.CTkSegmentedButton(
            fbar,
            values=["Table", "Cards"],
            variable=self._view_mode,
            command=self._on_view_toggle,
            width=130,
        ).pack(side="left")

        # Content area — table and card container overlap in the same cell
        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.grid(row=2, column=0, padx=24, pady=4, sticky="nsew")
        self._content.grid_columnconfigure(0, weight=1)
        self._content.grid_rowconfigure(0, weight=1)

        self._build_table()
        self._build_card_container()
        self._tv_frame.grid(row=0, column=0, sticky="nsew")

        self._count_lbl = ctk.CTkLabel(
            self, text="0 jobs", text_color=("gray50", "gray60"))
        self._count_lbl.grid(row=3, column=0, padx=24, pady=(2, 4), sticky="w")

        self._detail = DetailPanel(self, self)
        self._detail.grid(row=4, column=0, padx=24, pady=(0, 16), sticky="ew")

    def _build_table(self) -> None:
        self._tv_frame = tk.Frame(self._content, bg="#0f172a")
        self._tv_frame.grid_columnconfigure(0, weight=1)
        self._tv_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Jobs.Treeview",
                        background="#1e293b", fieldbackground="#1e293b",
                        foreground="#e2e8f0", rowheight=28,
                        font=("Helvetica", 11))
        style.configure("Jobs.Treeview.Heading",
                        font=("Helvetica", 11, "bold"),
                        background="#0f172a", foreground="#94a3b8",
                        relief="flat")
        style.map("Jobs.Treeview",
                  background=[("selected", "#1d4ed8")],
                  foreground=[("selected", "white")])

        cols = [c[0] for c in _TV_COLS]
        self._tv = ttk.Treeview(self._tv_frame, columns=cols, show="headings",
                                style="Jobs.Treeview", selectmode="browse")
        for col_id, label, width, stretch in _TV_COLS:
            self._tv.heading(col_id, text=label,
                             command=lambda c=col_id: self._sort_by(c))
            self._tv.column(col_id, width=width, minwidth=40,
                            stretch=tk.YES if stretch else tk.NO)

        vsb = ttk.Scrollbar(self._tv_frame, orient="vertical",   command=self._tv.yview)
        hsb = ttk.Scrollbar(self._tv_frame, orient="horizontal", command=self._tv.xview)
        self._tv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self._tv.bind("<<TreeviewSelect>>", self._on_tv_select)

    def _build_card_container(self) -> None:
        self._card_outer = ctk.CTkFrame(self._content, fg_color="transparent")
        self._card_outer.grid_columnconfigure(0, weight=1)
        self._card_outer.grid_rowconfigure(0, weight=1)
        self._card_scroll = ctk.CTkScrollableFrame(
            self._card_outer, fg_color="transparent")
        self._card_scroll.grid(row=0, column=0, sticky="nsew")
        self._card_scroll.grid_columnconfigure(0, weight=1)

    def _on_view_toggle(self, value: str) -> None:
        if value == "Table":
            self._card_outer.grid_remove()
            self._tv_frame.grid(row=0, column=0, sticky="nsew")
        else:
            self._tv_frame.grid_remove()
            self._card_outer.grid(row=0, column=0, sticky="nsew")

    # ── Data ──────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        self._latest_run_at = _get_latest_run_at()
        cfg = jmf.load_config()
        db_path = Path(cfg.get("database", {}).get("path", str(jmf.DEFAULT_DB_PATH)))
        if not db_path.exists():
            self._all_rows = []
            self._total_db_count = 0
            self._filtered_rows = []
            self._populate([])
            return
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        self._total_db_count = conn.execute(
            "SELECT COUNT(*) FROM jobs").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY match_score_pct DESC LIMIT ?",
            (_MAX_DB_ROWS,),
        ).fetchall()
        conn.close()
        self._all_rows = [dict(r) for r in rows]
        self._apply_filters()

    def _is_new_job(self, row: dict) -> bool:
        if not self._latest_run_at:
            return False
        first_seen = row.get("first_seen") or ""
        return bool(first_seen and first_seen >= self._latest_run_at)

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
        self._filtered_rows = filtered
        self._populate(filtered)

    def _populate(self, rows: list[dict]) -> None:
        self._populate_table(rows)
        self._populate_cards(rows[:_MAX_CARD_ROWS])
        n = len(rows)
        label = f"{n} job{'s' if n != 1 else ''}"
        if self._total_db_count > _MAX_DB_ROWS:
            label += f"  (showing top {_MAX_DB_ROWS} of {self._total_db_count})"
        self._count_lbl.configure(text=label)

    def _populate_table(self, rows: list[dict]) -> None:
        self._tv.delete(*self._tv.get_children())
        for r in rows:
            score = r.get("match_score_pct")
            is_new = self._is_new_job(r)
            score_str = ("★ " if is_new else "") + (
                f"{score:.1f}%" if score is not None else "—"
            )
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
                loc, sal,
                r.get("site") or "—",
                status,
                r.get("date_posted") or "—",
            ))
        self._tv.tag_configure("new",      foreground="#60a5fa")
        self._tv.tag_configure("saved",    foreground="#a78bfa")
        self._tv.tag_configure("applied",  foreground="#34d399")
        self._tv.tag_configure("rejected", foreground="#f87171")
        self._tv.tag_configure("ignored",  foreground="#64748b")

    def _populate_cards(self, rows: list[dict]) -> None:
        for card in self._cards:
            card.destroy()
        self._cards.clear()
        self._selected_card = None
        for i, r in enumerate(rows):
            card = JobCard(
                self._card_scroll, r, self._is_new_job(r),
                on_click=self._on_card_click,
            )
            card.grid(row=i, column=0, padx=4, pady=3, sticky="ew")
            self._cards.append(card)

    def _on_card_click(self, row: dict) -> None:
        for card in self._cards:
            is_this = card._row is row
            if is_this:
                if self._selected_card and self._selected_card is not card:
                    self._selected_card.set_selected(False)
                card.set_selected(True)
                self._selected_card = card
        self._detail.load(row)

    def _sort_by(self, col: str) -> None:
        self._sort_asc = (not self._sort_asc) if self._sort_col == col else (col != "score")
        self._sort_col = col
        self._apply_filters()

    def _on_tv_select(self, _=None) -> None:
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
            rows = self._filtered_rows or self._all_rows
            if not rows:
                messagebox.showinfo("Export", "No data to export.")
                return
            pd.DataFrame(rows).to_csv("job_matches.csv", index=False)
            messagebox.showinfo("Exported",
                                f"Saved {len(rows)} job(s) to job_matches.csv")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard Page
# ─────────────────────────────────────────────────────────────────────────────

class DashboardPage(ctk.CTkFrame):
    def __init__(self, parent: tk.Widget, app: "JobMatchApp"):
        super().__init__(parent, corner_radius=0, fg_color="transparent")
        self.app = app
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build()

    def _build(self) -> None:
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=24, pady=(20, 0), sticky="ew")
        ctk.CTkLabel(hdr, text="Dashboard",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="↻ Refresh", width=88,
                      command=self.refresh).pack(side="right")

        # Status funnel
        funnel = ctk.CTkFrame(self, fg_color=("#f1f5f9", "#0f172a"),
                               corner_radius=8)
        funnel.grid(row=1, column=0, padx=24, pady=(12, 8), sticky="ew")
        for i in range(len(STATUS_OPTIONS)):
            funnel.grid_columnconfigure(i, weight=1)

        self._funnel_lbls: dict[str, ctk.CTkLabel] = {}
        for i, status in enumerate(STATUS_OPTIONS):
            col = ctk.CTkFrame(funnel, fg_color="transparent")
            col.grid(row=0, column=i, padx=12, pady=14)
            color = STATUS_COLORS.get(status, "#64748b")
            ctk.CTkLabel(col, text=status.upper(),
                         font=ctk.CTkFont(size=10, weight="bold"),
                         text_color=color).pack()
            lbl = ctk.CTkLabel(col, text="0",
                               font=ctk.CTkFont(size=24, weight="bold"))
            lbl.pack()
            self._funnel_lbls[status] = lbl

        # Charts (scrollable so they all fit)
        charts = ctk.CTkScrollableFrame(self, fg_color="transparent")
        charts.grid(row=2, column=0, padx=24, pady=4, sticky="nsew")
        charts.grid_columnconfigure((0, 1), weight=1)

        sf = ctk.CTkFrame(charts, fg_color=("#f1f5f9", "#0f172a"), corner_radius=8)
        sf.grid(row=0, column=0, padx=(0, 6), pady=6, sticky="nsew")
        ctk.CTkLabel(sf, text="Score Distribution",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#475569", "#94a3b8")
                     ).pack(anchor="w", padx=10, pady=(8, 0))
        self._score_chart = BarChart(
            sf, "",
            [("<25", 0), ("25-50", 0), ("50-75", 0), ("75+", 0)],
            bar_colors=["#64748b", "#f59e0b", "#3b82f6", "#10b981"],
            height=200,
        )
        self._score_chart.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        cf = ctk.CTkFrame(charts, fg_color=("#f1f5f9", "#0f172a"), corner_radius=8)
        cf.grid(row=0, column=1, padx=(6, 0), pady=6, sticky="nsew")
        ctk.CTkLabel(cf, text="Top Companies",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#475569", "#94a3b8")
                     ).pack(anchor="w", padx=10, pady=(8, 0))
        self._co_chart = HBarChart(cf, "", [], height=200)
        self._co_chart.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        af = ctk.CTkFrame(charts, fg_color=("#f1f5f9", "#0f172a"), corner_radius=8)
        af.grid(row=1, column=0, columnspan=2, pady=6, sticky="nsew")
        ctk.CTkLabel(af, text="Activity — Last 30 Days",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=("#475569", "#94a3b8")
                     ).pack(anchor="w", padx=10, pady=(8, 0))
        self._activity_chart = DualLineChart(af, "", height=200)
        self._activity_chart.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def refresh(self) -> None:
        stats = _get_dashboard_stats()
        if not stats:
            return
        sc = stats.get("status_counts", {})
        for status, lbl in self._funnel_lbls.items():
            lbl.configure(text=str(sc.get(status, 0)))

        sd = stats.get("score_dist", {})
        self._score_chart.update_data([
            ("<25",    sd.get("<25",    0)),
            ("25-50",  sd.get("25-50",  0)),
            ("50-75",  sd.get("50-75",  0)),
            ("75+",    sd.get("75+",    0)),
        ])
        self._co_chart.update_data(stats.get("top_companies", []))
        self._activity_chart.update_series([
            ("New jobs",     stats.get("activity",     []), "#3b82f6"),
            ("Applications", stats.get("applications", []), "#10b981"),
        ])


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
             multiline: bool = False, height: int = 80):
        ctk.CTkLabel(frame, text=label, width=180, anchor="e"
                     ).grid(row=row, column=0, padx=(12, 8), pady=6,
                            sticky="ne" if multiline else "e")
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

    def _build(self) -> None:
        s = self._scroll
        ctk.CTkLabel(s, text="Configuration",
                     font=ctk.CTkFont(size=20, weight="bold")
                     ).grid(row=0, column=0, padx=24, pady=(20, 0), sticky="w")

        pf = self._section("Profile", 1)
        self._f_name      = self._row(pf, "Profile Name",     0)
        self._f_location  = self._row(pf, "Location",         1)
        self._f_distance  = self._row(pf, "Distance (miles)", 2)
        self._f_job_type  = self._row(pf, "Job Type",         3)
        self._f_hours_old = self._row(pf, "Hours Old",        4)
        self._f_results   = self._row(pf, "Results Wanted",   5)

        ctk.CTkLabel(pf, text="Sites", width=180, anchor="e"
                     ).grid(row=6, column=0, padx=(12, 8), pady=6, sticky="e")
        sf = ctk.CTkFrame(pf, fg_color="transparent")
        sf.grid(row=6, column=1, sticky="w", padx=(0, 12), pady=6)
        self._site_vars: dict[str, tk.BooleanVar] = {}
        for s_name in ["linkedin", "indeed", "google", "glassdoor", "zip_recruiter"]:
            v = tk.BooleanVar(value=False)
            self._site_vars[s_name] = v
            ctk.CTkCheckBox(sf, text=s_name, variable=v).pack(side="left", padx=6)

        st = self._section("Search Terms", 3)
        ctk.CTkLabel(st, text="One per line", width=180, anchor="e"
                     ).grid(row=0, column=0, padx=(12, 8), pady=6, sticky="ne")
        self._f_terms = ctk.CTkTextbox(st, height=90, wrap="word")
        self._f_terms.grid(row=0, column=1, padx=(0, 12), pady=6, sticky="ew")

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

        sc = self._section("Scoring", 7)
        self._f_title_bonus  = self._row(sc, "Title Bonus (%/match)",   0)
        self._f_sal_thresh   = self._row(sc, "Salary Threshold ($)",    1)
        self._f_sal_boost    = self._row(sc, "Salary Boost (%)",        2)
        self._f_remote_boost = self._row(sc, "Remote Boost (%)",        3)
        self._fuzzy_var      = self._cb_row(sc, "Fuzzy Matching",       4)
        self._f_fuzzy_thresh = self._row(sc, "Fuzzy Threshold (0–100)", 5)

        nt = self._section("Email Notifications", 9)
        self._email_en_var  = self._cb_row(nt, "Enabled",              0)
        self._f_smtp_server = self._row(nt,  "SMTP Server",            1)
        self._f_smtp_port   = self._row(nt,  "SMTP Port",              2)
        self._f_sender      = self._row(nt,  "Sender Email",           3)
        self._f_recipient   = self._row(nt,  "Recipient Email",        4)
        self._f_min_score   = self._row(nt,  "Min Score (%) to notify", 5)

        cd = self._section("Cache & Database", 11)
        self._cache_en_var = self._cb_row(cd, "Cache Enabled",    0)
        self._f_cache_ttl  = self._row(cd,   "Cache TTL (minutes)", 1)
        self._db_en_var    = self._cb_row(cd, "Database Enabled", 2)

        ctk.CTkButton(s, text="💾  Save Config", width=160,
                      command=self._save
                      ).grid(row=13, column=0, padx=24, pady=(12, 28), sticky="e")

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
        for sn, v in self._site_vars.items():
            v.set(sn in p.get("sites", []))
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
        self.minsize(960, 660)

        self._log_q: queue.Queue = queue.Queue()
        self._search_thread: threading.Thread | None = None

        self._build_ui()
        self._show_page("search")
        self._poll_log_queue()
        self._start_background_refresh()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=SIDEBAR_W, corner_radius=0,
                               fg_color=("#1e293b", "#0f172a"))
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_rowconfigure(10, weight=1)
        sidebar.grid_propagate(False)

        ctk.CTkLabel(
            sidebar,
            text="Job Match\nFinder",
            font=ctk.CTkFont(size=15, weight="bold"),
            justify="center",
        ).grid(row=0, column=0, padx=16, pady=(28, 24), sticky="ew")

        self._nav: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("search",    "🔍   Search"),
            ("results",   "📋   Results"),
            ("dashboard", "📊   Dashboard"),
            ("config",    "⚙️   Config"),
        ]
        for i, (key, label) in enumerate(nav_items, start=1):
            btn = ctk.CTkButton(
                sidebar, text=label, anchor="w", height=42, corner_radius=8,
                fg_color="transparent",
                text_color=("gray10", "gray90"),
                hover_color=("gray85", "gray25"),
                command=lambda k=key: self._show_page(k),
            )
            btn.grid(row=i, column=0, padx=10, pady=4, sticky="ew")
            self._nav[key] = btn

        self._pages: dict[str, ctk.CTkFrame] = {
            "search":    SearchPage(self, self),
            "results":   ResultsPage(self, self),
            "dashboard": DashboardPage(self, self),
            "config":    ConfigPage(self, self),
        }
        for page in self._pages.values():
            page.grid(row=0, column=1, sticky="nsew")

    def _show_page(self, key: str) -> None:
        self._pages[key].tkraise()
        for k, btn in self._nav.items():
            btn.configure(
                fg_color=("#3b82f6", "#1d4ed8") if k == key else "transparent"
            )
        if key == "results":
            self._pages["results"].refresh()
        elif key == "dashboard":
            self._pages["dashboard"].refresh()

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

    # ── Background cache refresh ──────────────────────────────────────────────

    def _start_background_refresh(self) -> None:
        try:
            cfg = jmf.load_config()
            if not cfg.get("cache", {}).get("enabled", True):
                return
            if not cfg.get("database", {}).get("enabled", True):
                return
        except Exception:
            return
        sp: SearchPage = self._pages["search"]
        sp.set_refresh_status("⟳ Refreshing cache…")
        t = threading.Thread(target=self._bg_refresh_worker, daemon=True)
        t.start()

    def _bg_refresh_worker(self) -> None:
        try:
            cfg = jmf.load_config()
            cache_cfg = cfg.get("cache", {})
            cache = jmf.JobCache(
                cache_cfg.get("path", str(jmf.DEFAULT_CACHE_PATH)),
                cache_cfg.get("ttl_minutes", 30),
            )
            jmf.fetch_jobs(cfg, cache)
            self.after(0, lambda: self._pages["search"].set_refresh_status(
                "Cache ready"))
        except Exception:
            self.after(0, lambda: self._pages["search"].set_refresh_status(""))


# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    app = JobMatchApp()
    app.mainloop()


if __name__ == "__main__":
    main()
