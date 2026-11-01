"""Visible plan reminder, launched in the signed-in user's desktop session."""

from __future__ import annotations

import argparse
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from daily import now_local, read_plans
import plan_store


def show(report: Path | None, ready_file: Path) -> None:
    root = tk.Tk()
    root.title("每日计划提醒")
    root.geometry("590x440")
    root.minsize(450, 300)
    root.attributes("-topmost", True)

    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="今日计划提醒", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
    count_label = ttk.Label(frame, font=("Microsoft YaHei UI", 10))
    count_label.pack(anchor="w", pady=(4, 14))

    list_area = ttk.Frame(frame)
    list_area.pack(fill="both", expand=True)
    canvas = tk.Canvas(list_area, borderwidth=0, highlightthickness=0, background="#ffffff")
    scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    rows = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=rows, anchor="nw")
    rows.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
    canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))

    def complete_plan(item_id: str) -> None:
        try:
            plan_store.update_plan(item_id, "complete")
        except Exception as exc:
            messagebox.showerror("完成计划失败", str(exc), parent=root)
            return
        refresh_plans(schedule=False)

    def refresh_plans(schedule: bool = True) -> None:
        plans = read_plans(now_local().date())
        if not plans:
            root.after(0, root.destroy)
            return
        count_label.config(text=f"{now_local().date().isoformat()} · {len(plans)} 项待办")
        for child in rows.winfo_children():
            child.destroy()
        for item in plans:
            row = ttk.Frame(rows, padding=(8, 10))
            row.pack(fill="x")
            row.columnconfigure(0, weight=1)
            ttk.Label(row, text=item["title"], font=("Microsoft YaHei UI", 11, "bold"), wraplength=390).grid(row=0, column=0, sticky="w")
            ttk.Button(row, text="完成", command=lambda item_id=item["id"]: complete_plan(item_id)).grid(row=0, column=1, rowspan=2, padx=(12, 2))
            due = f"（截止 {item['due']}）" if item["due"] else ""
            progress = f" · {item['progress']}%" if item["progress"] else ""
            ttk.Label(row, text=f"{item['status']}{progress}{due}", foreground="#62758a", font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w")
            ttk.Separator(rows, orient="horizontal").pack(fill="x")
        if schedule:
            root.after(3000, refresh_plans)

    refresh_plans()

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(12, 0))
    if report and report.exists():
        ttk.Button(buttons, text="查看今日日报", command=lambda: os.startfile(report)).pack(side="left")
    ttk.Button(buttons, text="关闭", command=root.destroy).pack(side="right")

    root.update_idletasks()
    root.deiconify()
    root.lift()
    root.update()
    ready_file.write_text("visible", encoding="utf-8")
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        show(args.report, args.ready_file)
    except Exception as exc:
        args.ready_file.with_suffix(".error").write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
