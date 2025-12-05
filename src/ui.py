import difflib
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Dict

from .core import Match, SearchEngine
from .utils import atomic_write, read_file_lines


class ReplacementWorker(threading.Thread):
    """Background thread to safely replace text in files."""

    def __init__(self, file_map, find_opts, repl_text, backup, progress_q):
        super().__init__(daemon=True)
        self.file_map = file_map
        self.find_opts = find_opts
        self.repl_text = repl_text
        self.backup = backup
        self.progress_q = progress_q

    def run(self):
        try:
            term = self.find_opts["text"]
            flags = 0 if self.find_opts["case"] else re.IGNORECASE
            rx = None
            if self.find_opts["regex"]:
                rx = re.compile(term, flags)
            elif self.find_opts["whole"]:
                rx = re.compile(rf"\b{re.escape(term)}\b", flags)

            count = 0

            # TODO: Optimize by reading file once and applying all replacements
            for i, (item_id, path) in enumerate(self.file_map.items()):
                try:
                    lines = read_file_lines(path)
                    old_text = "".join(lines)

                    if rx:
                        new_text = rx.sub(self.repl_text, old_text)
                    else:
                        if self.find_opts["case"]:
                            new_text = old_text.replace(term, self.repl_text)
                        else:
                            new_text = re.sub(
                                re.escape(term),
                                self.repl_text,
                                old_text,
                                flags=re.IGNORECASE,
                            )

                    if new_text != old_text:
                        atomic_write(path, new_text, make_backup=self.backup)
                        count += 1

                    self.progress_q.put(("step", (i + 1, path.name)))
                except Exception as e:
                    self.progress_q.put(("error", f"{path.name}: {e}"))

            self.progress_q.put(("done", count))
        except Exception as e:
            self.progress_q.put(("fatal", str(e)))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PySearch Tool")
        self.geometry("1100x750")
        self.minsize(900, 600)
        self._make_style()

        self.stop_event = threading.Event()
        self.out_q = queue.Queue()
        self._row_to_match: Dict[str, Match] = {}

        self._build_ui()
        self._poll_queue()

    def _make_style(self):
        style = ttk.Style(self)
        style.theme_use("vista")
        style.configure("TButton", padding=5)
        style.configure("Header.TLabel", font=("Segoe UI", 10, "bold"))

    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # --- Top Inputs ---
        frm = ttk.Frame(root)
        frm.pack(fill="x", pady=(0, 10))

        ttk.Label(frm, text="Directory:").grid(row=0, column=0, sticky="w")
        self.dir_var = tk.StringVar(value=str(Path.home()))
        ttk.Entry(frm, textvariable=self.dir_var).grid(
            row=0, column=1, sticky="ew", padx=5
        )
        ttk.Button(frm, text="Browse", command=self._choose_dir).grid(row=0, column=2)

        ttk.Label(frm, text="Search Text:").grid(row=1, column=0, sticky="w", pady=5)
        self.text_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.text_var).grid(
            row=1, column=1, sticky="ew", padx=5, pady=5
        )

        ttk.Label(frm, text="Includes:").grid(row=2, column=0, sticky="w")
        self.include_var = tk.StringVar(value="*.py;*.txt;*.md;*.json;*.zip;*.tar.gz")
        ttk.Entry(frm, textvariable=self.include_var).grid(
            row=2, column=1, sticky="ew", padx=5
        )

        ttk.Label(frm, text="Excludes:").grid(row=3, column=0, sticky="w", pady=5)
        self.exclude_var = tk.StringVar(value="*.min.js;*.log;*.bin;__pycache__")
        ttk.Entry(frm, textvariable=self.exclude_var).grid(
            row=3, column=1, sticky="ew", padx=5, pady=5
        )

        frm.columnconfigure(1, weight=1)

        # --- Options ---
        opts = ttk.Frame(root)
        opts.pack(fill="x", pady=(0, 10))
        self.case_var = tk.BooleanVar()
        self.regex_var = tk.BooleanVar()
        self.word_var = tk.BooleanVar()
        self.hidden_var = tk.BooleanVar()
        self.archives_var = tk.BooleanVar()

        ttk.Checkbutton(opts, text="Match Case", variable=self.case_var).pack(
            side="left"
        )
        ttk.Checkbutton(opts, text="Regex", variable=self.regex_var).pack(
            side="left", padx=10
        )
        ttk.Checkbutton(opts, text="Whole Word", variable=self.word_var).pack(
            side="left", padx=10
        )
        ttk.Checkbutton(opts, text="Search Hidden", variable=self.hidden_var).pack(
            side="left", padx=10
        )
        ttk.Checkbutton(opts, text="Search Archives", variable=self.archives_var).pack(
            side="left", padx=10
        )

        # --- Buttons ---
        btn_box = ttk.Frame(root)
        btn_box.pack(fill="x", pady=(0, 10))
        self.btn_search = ttk.Button(btn_box, text="Search", command=self._start_search)
        self.btn_search.pack(side="left")
        self.btn_stop = ttk.Button(
            btn_box, text="Stop", command=self._stop_search, state="disabled"
        )
        self.btn_stop.pack(side="left", padx=5)
        ttk.Button(btn_box, text="Clear Results", command=self._clear).pack(
            side="left", padx=5
        )
        ttk.Button(
            btn_box, text="Replace in Files...", command=self._replace_dialog
        ).pack(side="left", padx=5)

        # --- Tree & Preview ---
        paned = ttk.Panedwindow(root, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # Left: Tree
        self.tree = ttk.Treeview(
            paned, columns=("path", "line", "preview"), show="headings"
        )
        self.tree.heading("path", text="File")
        self.tree.heading("line", text="Line")
        self.tree.heading("preview", text="Match Preview")
        self.tree.column("path", width=450)
        self.tree.column("line", width=50, anchor="center")
        self.tree.column("preview", width=300)

        scroll_y = ttk.Scrollbar(self.tree, orient="vertical", command=self.tree.yview)
        scroll_x = ttk.Scrollbar(
            self.tree, orient="horizontal", command=self.tree.xview
        )
        self.tree.configure(yscroll=scroll_y.set, xscroll=scroll_x.set)
        scroll_y.pack(side="right", fill="y")
        scroll_x.pack(side="bottom", fill="x")

        paned.add(self.tree, weight=2)

        # Right: Preview
        prev_frm = ttk.Frame(paned)
        self.preview = tk.Text(prev_frm, width=40, state="disabled", wrap="none")
        prev_scroll = ttk.Scrollbar(prev_frm, command=self.preview.yview)
        self.preview.configure(yscrollcommand=prev_scroll.set)

        self.preview.pack(side="left", fill="both", expand=True)
        prev_scroll.pack(side="right", fill="y")

        paned.add(prev_frm, weight=1)

        # Status Bar
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status_var).pack(anchor="w", pady=(5, 0))

        # Bindings
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._open_file)
        self.tree.bind("<Button-3>", self._context_menu)

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)

    def _start_search(self):
        d = self.dir_var.get()
        if not os.path.isdir(d):
            messagebox.showerror("Error", "Invalid directory")
            return

        self._clear()
        self.btn_search["state"] = "disabled"
        self.btn_stop["state"] = "normal"
        self.status_var.set("Searching...")
        self.stop_event.clear()

        opts = {
            "text": self.text_var.get(),
            "case": self.case_var.get(),
            "regex": self.regex_var.get(),
            "whole_word": self.word_var.get(),
            "include_hidden": self.hidden_var.get(),
            "search_archives": self.archives_var.get(),
            "use_default_ignores": True,
        }

        # Start Thread
        t = threading.Thread(target=self._run_engine, args=(Path(d), opts), daemon=True)
        t.start()

    def _run_engine(self, root, opts):
        try:
            inc = [p.strip() for p in self.include_var.get().split(";") if p.strip()]
            exc = [p.strip() for p in self.exclude_var.get().split(";") if p.strip()]
            engine = SearchEngine(root, opts, inc, exc, self.stop_event)
            engine.run(self.out_q)
        except Exception as e:
            self.out_q.put(("error", str(e)))

    def _poll_queue(self):
        try:
            for _ in range(50):
                msg, data = self.out_q.get_nowait()
                if msg == "match":
                    row = self.tree.insert(
                        "", "end", values=(str(data.path), data.line_no, data.preview)
                    )
                    self._row_to_match[row] = data
                elif msg == "progress":
                    self.status_var.set(
                        f"Scanned: {data.scanned_files} | Found: {data.matches_found}"
                    )
                elif msg == "done":
                    self.status_var.set(f"Done. Found {data.matches_found} matches.")
                    self.btn_search["state"] = "normal"
                    self.btn_stop["state"] = "disabled"
                elif msg == "warn":
                    messagebox.showwarning("Warning", data)
                elif msg == "error":
                    messagebox.showerror("Error", data)
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    # TODO: Refactor _on_select to reduce complexity
    def _on_select(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        m = self._row_to_match.get(sel[0])
        if not m:
            return

        # Load Context
        lines = read_file_lines(m.path, m.is_archive, m.member)
        if not lines:
            return

        start = max(0, m.line_no - 5)
        end = min(len(lines), m.line_no + 5)

        self.preview["state"] = "normal"
        self.preview.delete("1.0", "end")
        for i in range(start, end):
            prefix = ">> " if (i + 1) == m.line_no else "   "
            self.preview.insert("end", f"{prefix}{i + 1}: {lines[i]}")
        self.preview["state"] = "disabled"

    def _context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)

        m = self._row_to_match[item]
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Open File", command=lambda: self._open_file(None))
        menu.add_command(
            label="Copy Path", command=lambda: self.clipboard_append(str(m.path))
        )
        menu.post(event.x_root, event.y_root)

    def _open_file(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        m = self._row_to_match.get(sel[0])
        if not m:
            return

        try:
            if sys.platform == "win32":
                os.startfile(m.path)
            # TODO: Test on macOS and Linux, for now we simply open the file with default app
            elif sys.platform == "linux":
                subprocess.Popen(["xdg-open", str(m.path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(m.path)])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open file: {e}")

    def _clear(self):
        self.tree.delete(*self.tree.get_children())
        self._row_to_match.clear()

    def _stop_search(self):
        self.stop_event.set()

    def _replace_dialog(self):
        # 1. Get unique files
        files_map = {}
        for m in self._row_to_match.values():
            if not m.is_archive:
                files_map[str(m.path)] = m.path
        unique_files = sorted(files_map.values())

        if not unique_files:
            messagebox.showinfo("Replace", "No text files found to edit.")
            return

        top = tk.Toplevel(self)
        top.title("Replace in Files")
        top.geometry("900x600")

        # Controls
        input_frame = ttk.Frame(top, padding=10)
        input_frame.pack(fill="x")

        ttk.Label(input_frame, text="Find:").grid(row=0, column=0, sticky="e")
        find_var = tk.StringVar(value=self.text_var.get())
        ttk.Entry(input_frame, textvariable=find_var, width=40).grid(
            row=0, column=1, padx=5
        )

        ttk.Label(input_frame, text="Replace:").grid(row=1, column=0, sticky="e")
        repl_var = tk.StringVar()
        ttk.Entry(input_frame, textvariable=repl_var, width=40).grid(
            row=1, column=1, padx=5
        )

        backup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            input_frame, text="Create .bak backups", variable=backup_var
        ).grid(row=2, column=1, sticky="w")

        # Split View
        paned = ttk.Panedwindow(top, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=5)

        # Left: File Check List
        left_frm = ttk.Frame(paned)
        paned.add(left_frm, weight=1)

        file_tree = ttk.Treeview(
            left_frm, columns=("checked", "path"), show="headings", selectmode="browse"
        )
        file_tree.heading("checked", text="✓")
        file_tree.heading("path", text="File")
        file_tree.column("checked", width=30)
        file_tree.pack(fill="both", expand=True)

        # Populate
        file_items = {}  # map item_id -> Path
        check_states = {}  # map item_id -> bool
        for p in unique_files:
            iid = file_tree.insert("", "end", values=("☑", str(p)))
            file_items[iid] = p
            check_states[iid] = True

        # Right: Diff
        right_frm = ttk.Frame(paned)
        paned.add(right_frm, weight=2)
        diff_text = tk.Text(right_frm, wrap="none")
        diff_text.pack(fill="both", expand=True)

        # Logic
        def toggle_check(event):
            region = file_tree.identify("region", event.x, event.y)
            if region == "cell":
                col = file_tree.identify_column(event.x)
                if col == "#1":
                    iid = file_tree.identify_row(event.y)
                    if iid:
                        check_states[iid] = not check_states[iid]
                        file_tree.set(iid, "checked", "☑" if check_states[iid] else "☐")
                        return "break"

        def show_diff(event):
            sel = file_tree.selection()
            if not sel:
                return
            p = file_items[sel[0]]
            try:
                old = p.read_text(encoding="utf-8", errors="ignore")
                term = find_var.get()
                repl = repl_var.get()

                # Simple replace preview logic (mirroring worker)
                if self.regex_var.get():
                    new = re.sub(
                        term,
                        repl,
                        old,
                        flags=0 if self.case_var.get() else re.IGNORECASE,
                    )
                else:
                    if self.case_var.get():
                        new = old.replace(term, repl)
                    else:
                        new = re.sub(re.escape(term), repl, old, flags=re.IGNORECASE)

                diff = difflib.unified_diff(
                    old.splitlines(), new.splitlines(), lineterm=""
                )
                diff_text.delete("1.0", "end")
                diff_text.insert("1.0", "\n".join(diff))
            except:
                pass

        def run_replace():
            selected = {i: file_items[i] for i in check_states if check_states[i]}
            if not selected:
                return

            opts = {
                "text": find_var.get(),
                "regex": self.regex_var.get(),
                "case": self.case_var.get(),
                "whole": self.word_var.get(),
            }

            # Progress Dialog
            prog_win = tk.Toplevel(top)
            prog_win.geometry("300x150")
            prog_lbl = ttk.Label(prog_win, text="Working...")
            prog_lbl.pack(pady=20)
            prog_bar = ttk.Progressbar(
                prog_win, mode="determinate", maximum=len(selected)
            )
            prog_bar.pack(fill="x", padx=20)

            q = queue.Queue()
            w = ReplacementWorker(selected, opts, repl_var.get(), backup_var.get(), q)
            w.start()

            def check():
                try:
                    while True:
                        msg, data = q.get_nowait()
                        if msg == "step":
                            prog_bar.step()
                            prog_lbl["text"] = f"Processed {data[1]}"
                        elif msg == "done":
                            prog_win.destroy()
                            top.destroy()
                            messagebox.showinfo("Success", f"Updated {data} files.")
                            self._start_search()  # Refresh
                            return
                        elif msg == "error":
                            print(data)
                except:
                    pass
                prog_win.after(50, check)

            check()

        file_tree.bind("<Button-1>", toggle_check)
        file_tree.bind("<<TreeviewSelect>>", show_diff)
        ttk.Button(input_frame, text="Apply Changes", command=run_replace).grid(
            row=3, column=1, sticky="e", pady=10
        )
