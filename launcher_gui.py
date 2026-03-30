#!/usr/bin/env python3
"""Windows GUI launcher and repair surface for mtgacoach."""

from __future__ import annotations

import argparse
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import webbrowser

from windows_integration import (
    REPO_DIR,
    RuntimeState,
    detect_runtime_state,
    find_zombie_processes,
    install_bepinex,
    install_plugin,
    kill_zombie_processes,
    launch_mode,
    open_path,
    repair_bridge_stack,
    run_setup_wizard,
    set_saved_mtga_dir,
    tail_text,
)


APP_NAME = "mtgacoach Launcher"
GITHUB_RELEASES = "https://github.com/josharmour/mtgacoach/releases"


def _read_version() -> str:
    pyproject = REPO_DIR / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("version = "):
                return line.split('"')[1]
    except Exception:
        pass
    return "unknown"


class LauncherGUI:
    def __init__(self, root: tk.Tk, *, setup_tab: bool = False) -> None:
        self.root = root
        self.version = _read_version()
        self.state: RuntimeState | None = None

        self.dry_run_var = tk.BooleanVar(value=False)
        self.afk_var = tk.BooleanVar(value=False)
        self.mtga_path_var = tk.StringVar()
        self.summary_var = tk.StringVar()
        self.launch_maintenance_button: ttk.Button | None = None

        self.status_value_labels: dict[str, tk.Label] = {}
        self.status_keys = [
            "Runtime Root",
            "Python Runtime",
            "MTGA Install",
            "MTGA Process",
            "BepInEx",
            "Bridge Plugin",
            "BepInEx Bundle",
            "Player.log",
            "Bridge Readiness",
        ]

        self.root.title(f"{APP_NAME} v{self.version}")
        self.root.geometry("980x760")
        self.root.minsize(860, 680)
        self.root.configure(bg="#f3f1e6")

        self._build_ui()
        if setup_tab:
            self.notebook.select(self.repair_tab)
        self.refresh_status()
        self._check_first_run()

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Header.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("SubHeader.TLabel", font=("Segoe UI", 10))
        style.configure("Panel.TFrame", background="#f8f6ec")
        style.configure("Card.TLabelframe", background="#f8f6ec")
        style.configure("Card.TLabelframe.Label", font=("Segoe UI", 11, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))

        container = ttk.Frame(self.root, padding=18, style="Panel.TFrame")
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 12))
        ttk.Label(header, text="mtgacoach", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=(
                "Installer-first Windows launcher for mtgacoach. "
                "Install once, then use this surface to launch the coach or repair runtime and bridge integration."
            ),
            style="SubHeader.TLabel",
            wraplength=920,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        summary = tk.Label(
            container,
            textvariable=self.summary_var,
            anchor="w",
            justify="left",
            bg="#f3f1e6",
            fg="#2f4b3f",
            font=("Segoe UI", 10, "bold"),
        )
        summary.pack(fill="x", pady=(0, 10))

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)

        self.launch_tab = ttk.Frame(self.notebook, padding=14, style="Panel.TFrame")
        self.repair_tab = ttk.Frame(self.notebook, padding=14, style="Panel.TFrame")
        self.logs_tab = ttk.Frame(self.notebook, padding=14, style="Panel.TFrame")

        self.notebook.add(self.launch_tab, text="Launch")
        self.notebook.add(self.repair_tab, text="Repair")
        self.notebook.add(self.logs_tab, text="Logs")

        self._build_launch_tab()
        self._build_repair_tab()
        self._build_logs_tab()

    def _build_launch_tab(self) -> None:
        status_frame = ttk.LabelFrame(self.launch_tab, text="Runtime Status", style="Card.TLabelframe")
        status_frame.pack(fill="x", pady=(0, 12))

        for idx, key in enumerate(self.status_keys):
            row = ttk.Frame(status_frame, style="Panel.TFrame")
            row.grid(row=idx, column=0, sticky="ew", padx=10, pady=3)
            status_frame.grid_columnconfigure(0, weight=1)
            ttk.Label(row, text=f"{key}:", width=16).pack(side="left")
            value = tk.Label(
                row,
                text="Detecting...",
                anchor="w",
                justify="left",
                bg="#f8f6ec",
                fg="#334e68",
                font=("Segoe UI", 10),
            )
            value.pack(side="left", fill="x", expand=True)
            self.status_value_labels[key] = value

        options_frame = ttk.LabelFrame(self.launch_tab, text="Launch Options", style="Card.TLabelframe")
        options_frame.pack(fill="x", pady=(0, 12))

        checks = ttk.Frame(options_frame, style="Panel.TFrame")
        checks.pack(anchor="w", padx=10, pady=(8, 4))
        ttk.Checkbutton(
            checks,
            text="Autopilot dry-run (plan but do not click)",
            variable=self.dry_run_var,
        ).pack(anchor="w")
        ttk.Checkbutton(
            checks,
            text="Autopilot AFK mode",
            variable=self.afk_var,
        ).pack(anchor="w")

        button_row = ttk.Frame(options_frame, style="Panel.TFrame")
        button_row.pack(fill="x", padx=10, pady=(4, 10))
        ttk.Button(
            button_row,
            text="Launch Coach",
            style="Primary.TButton",
            command=lambda: self._launch(False),
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            button_row,
            text="Launch Autopilot",
            style="Primary.TButton",
            command=lambda: self._launch(True),
        ).pack(side="left", padx=(0, 8))
        self.launch_maintenance_button = ttk.Button(
            button_row,
            text="Repair / Tools",
            command=self._open_repair_tab,
        )
        self.launch_maintenance_button.pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Refresh", command=self.refresh_status).pack(side="left")

        note_frame = ttk.LabelFrame(self.launch_tab, text="Notes", style="Card.TLabelframe")
        note_frame.pack(fill="both", expand=True)
        note_text = (
            "The intended product flow is: download the installer from GitHub once, then use this launcher "
            "for normal launch and occasional repair actions. The underlying runtime still opens the current "
            "TUI until the full GUI coach replaces it."
        )
        tk.Label(
            note_frame,
            text=note_text,
            wraplength=860,
            justify="left",
            anchor="nw",
            bg="#f8f6ec",
            fg="#40362e",
            padx=10,
            pady=10,
        ).pack(fill="both", expand=True)

    def _build_repair_tab(self) -> None:
        path_frame = ttk.LabelFrame(self.repair_tab, text="MTGA Location", style="Card.TLabelframe")
        path_frame.pack(fill="x", pady=(0, 12))

        ttk.Label(
            path_frame,
            text="Saved MTGA install folder:",
        ).pack(anchor="w", padx=10, pady=(10, 4))

        entry_row = ttk.Frame(path_frame, style="Panel.TFrame")
        entry_row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Entry(entry_row, textvariable=self.mtga_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(entry_row, text="Browse", command=self._browse_mtga_dir).pack(side="left", padx=(8, 0))
        ttk.Button(entry_row, text="Save", command=self._save_mtga_dir).pack(side="left", padx=(8, 0))

        repair_frame = ttk.LabelFrame(self.repair_tab, text="Repair Actions", style="Card.TLabelframe")
        repair_frame.pack(fill="x", pady=(0, 12))

        button_row_1 = ttk.Frame(repair_frame, style="Panel.TFrame")
        button_row_1.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Button(button_row_1, text="Provision Runtime", command=self._run_setup_wizard).pack(side="left", padx=(0, 8))
        ttk.Button(button_row_1, text="Repair MTGA Bridge", command=self._repair_everything).pack(side="left", padx=(0, 8))
        ttk.Button(button_row_1, text="Kill Zombie Processes", command=self._kill_zombies).pack(side="left")

        button_row_2 = ttk.Frame(repair_frame, style="Panel.TFrame")
        button_row_2.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(button_row_2, text="Install / Repair BepInEx", command=self._install_bepinex).pack(side="left", padx=(0, 8))
        ttk.Button(button_row_2, text="Install / Update Bridge Plugin", command=self._install_plugin).pack(side="left", padx=(0, 8))
        ttk.Button(button_row_2, text="Open MTGA Folder", command=self._open_mtga_folder).pack(side="left", padx=(0, 8))
        ttk.Button(button_row_2, text="Open Player.log", command=self._open_player_log).pack(side="left", padx=(0, 8))
        ttk.Button(button_row_2, text="Open BepInEx Log", command=self._open_bepinex_log).pack(side="left", padx=(0, 8))
        ttk.Button(button_row_2, text="Open Releases", command=lambda: webbrowser.open(GITHUB_RELEASES)).pack(side="left")

        help_frame = ttk.LabelFrame(self.repair_tab, text="Installer Direction", style="Card.TLabelframe")
        help_frame.pack(fill="both", expand=True)
        help_text = (
            "Provision Runtime is the transitional repo/manual bootstrap path. In the installed product, "
            "the GitHub-downloaded installer should have already created the LocalAppData runtime and desktop "
            "entrypoint. This tab remains the place for repair actions such as fixing BepInEx or reinstalling "
            "the bridge plugin into MTGA."
        )
        tk.Label(
            help_frame,
            text=help_text,
            wraplength=860,
            justify="left",
            anchor="nw",
            bg="#f8f6ec",
            fg="#40362e",
            padx=10,
            pady=10,
        ).pack(fill="both", expand=True)

    def _build_logs_tab(self) -> None:
        self.logs_text = scrolledtext.ScrolledText(
            self.logs_tab,
            wrap="word",
            font=("Consolas", 10),
            height=30,
        )
        self.logs_text.pack(fill="both", expand=True)
        self.logs_text.configure(state="disabled")

        button_row = ttk.Frame(self.logs_tab, style="Panel.TFrame")
        button_row.pack(fill="x", pady=(10, 0))
        ttk.Button(button_row, text="Refresh Log Tails", command=self.refresh_status).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Open Runtime Folder", command=self._open_runtime_root).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Open .arenamcp Folder", command=self._open_settings_dir).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Open Repo Folder", command=lambda: self._safe_open(REPO_DIR)).pack(side="left")

    def refresh_status(self) -> None:
        self.state = detect_runtime_state()
        state = self.state

        if not self.mtga_path_var.get() and state.mtga_dir:
            self.mtga_path_var.set(str(state.mtga_dir))

        launchable = (
            state.python_exe is not None
            and state.mtga_dir is not None
            and state.bepinex_installed
            and state.plugin_installed
        )
        fully_provisioned = launchable and state.runtime_venv_exists

        if fully_provisioned:
            summary = "Bridge runtime looks ready."
        elif launchable:
            summary = (
                "Bridge launch is available now. "
                "Use Repair / Tools only for runtime provisioning or MTGA bridge fixes."
            )
        elif state.issues:
            summary = "Action needed: " + " | ".join(state.issues[:3])
        else:
            summary = "Launcher ready, but GRE bridge prerequisites are incomplete."
        self.summary_var.set(summary)
        if self.launch_maintenance_button is not None:
            button_text = "Finish Setup" if not state.runtime_venv_exists else "Repair / Tools"
            self.launch_maintenance_button.configure(text=button_text)

        if state.runtime_venv_exists:
            runtime_label = f"{state.runtime_root} (venv ready)"
            runtime_level = "ok"
        else:
            fallback_label = {
                "app_venv": "repo venv fallback",
                "current_process": "current Python fallback",
            }.get(state.python_source, state.python_source)
            runtime_label = (
                f"{state.runtime_root} (not provisioned; using {fallback_label})"
                if state.python_exe
                else f"{state.runtime_root} (setup required)"
            )
            runtime_level = "warn" if state.python_exe else "error"
        self._set_status(
            "Runtime Root",
            runtime_label,
            runtime_level,
        )
        self._set_status(
            "Python Runtime",
            f"{state.python_exe} [{state.python_source}]" if state.python_exe else "Missing",
            "ok" if state.python_exe else "error",
        )
        mtga_label = (
            f"{state.mtga_dir} ({state.mtga_dir_source})"
            if state.mtga_dir
            else "Not detected"
        )
        self._set_status("MTGA Install", mtga_label, "ok" if state.mtga_dir else "error")
        self._set_status(
            "MTGA Process",
            "Running" if state.mtga_running else "Not running",
            "warn" if state.mtga_running else "ok",
        )
        self._set_status(
            "BepInEx",
            str(state.bepinex_dir) if state.bepinex_installed else "Missing",
            "ok" if state.bepinex_installed else "error",
        )
        plugin_status = (
            str(state.plugin_install_path)
            if state.plugin_installed
            else (
                f"Built locally at {state.plugin_build_path}"
                if state.plugin_built
                else "Missing build output"
            )
        )
        self._set_status(
            "Bridge Plugin",
            plugin_status,
            "ok" if state.plugin_installed else ("warn" if state.plugin_built else "error"),
        )
        if state.bepinex_bundle:
            bundle_text = str(state.bepinex_bundle)
            bundle_level = "ok"
        elif state.bepinex_installed:
            bundle_text = "No bundled payload in this repo build; existing MTGA install already has BepInEx"
            bundle_level = "ok"
        else:
            bundle_text = "No bundled BepInEx payload found"
            bundle_level = "warn"
        self._set_status("BepInEx Bundle", bundle_text, bundle_level)
        self._set_status(
            "Player.log",
            str(state.player_log) if state.player_log.exists() else f"Missing ({state.player_log})",
            "ok" if state.player_log.exists() else "warn",
        )
        if fully_provisioned:
            readiness = "Ready for direct GRE bridge"
            readiness_level = "ok"
        elif launchable:
            readiness = "Ready for direct GRE bridge via current repo/runtime fallback"
            readiness_level = "ok"
        else:
            readiness = "Partial / repair needed"
            readiness_level = "warn"
        self._set_status("Bridge Readiness", readiness, readiness_level)

        self._refresh_log_tails(state)

    def _set_status(self, key: str, text: str, level: str) -> None:
        label = self.status_value_labels[key]
        colors = {
            "ok": "#245c3c",
            "warn": "#8a5a00",
            "error": "#8d1f1f",
        }
        label.configure(text=text, fg=colors.get(level, "#334e68"))

    def _refresh_log_tails(self, state: RuntimeState) -> None:
        sections = []
        sections.append("[Issues]")
        if state.issues:
            for item in state.issues:
                sections.append(f"- {item}")
        else:
            sections.append("- none")

        sections.append("\n[Player.log tail]")
        sections.append(tail_text(state.player_log, max_bytes=6000) or "<empty>")

        sections.append("\n[BepInEx LogOutput.log tail]")
        sections.append(tail_text(state.bepinex_log, max_bytes=6000) or "<empty>")

        self.logs_text.configure(state="normal")
        self.logs_text.delete("1.0", "end")
        self.logs_text.insert("1.0", "\n".join(sections))
        self.logs_text.configure(state="disabled")

    def _needs_setup(self) -> bool:
        """True when the runtime venv has not been provisioned yet."""
        if self.state is None:
            return True
        if self.state.python_exe is None:
            return True
        # A repo-local venv counts as provisioned (developer workflow).
        return not self.state.runtime_venv_exists and self.state.python_source != "app_venv"

    def _check_first_run(self) -> None:
        """On first launch, prompt the user to run the setup wizard."""
        if self.state is None:
            return
        if self.state.python_exe is None:
            messagebox.showwarning(
                APP_NAME,
                "Python 3.10+ is required but was not found.\n\n"
                "Install from python.org and check\n"
                '"Add Python to PATH", then reopen this launcher.',
            )
            return
        if self._needs_setup():
            if messagebox.askyesno(
                APP_NAME,
                "Welcome to mtgacoach!  First-time setup is needed to\n"
                "create the Python environment and install dependencies.\n\n"
                "Run the setup wizard now?",
            ):
                self._run_setup_wizard()

    def _launch(self, autopilot: bool) -> None:
        if self._needs_setup():
            if messagebox.askyesno(
                APP_NAME,
                "Setup has not been completed yet.\n\n"
                "Run the setup wizard first?",
            ):
                self._run_setup_wizard()
            return

        try:
            launch_mode(
                autopilot=autopilot,
                dry_run=self.dry_run_var.get(),
                afk=self.afk_var.get(),
            )
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Launch failed:\n\n{exc}")
            return
        label = "Autopilot" if autopilot else "Coach"
        messagebox.showinfo(APP_NAME, f"{label} launch requested in a new window.")

    def _open_repair_tab(self) -> None:
        self.refresh_status()
        self.notebook.select(self.repair_tab)

    def _run_setup_wizard(self) -> None:
        try:
            run_setup_wizard()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Failed to start setup wizard:\n\n{exc}")

    def _browse_mtga_dir(self) -> None:
        initial = self.mtga_path_var.get() or str(Path.home())
        selected = filedialog.askdirectory(
            title="Select MTGA install folder",
            initialdir=initial,
            mustexist=True,
        )
        if selected:
            self.mtga_path_var.set(selected)

    def _save_mtga_dir(self) -> None:
        raw = self.mtga_path_var.get().strip()
        if not raw:
            messagebox.showwarning(APP_NAME, "Choose an MTGA folder first.")
            return
        path = Path(raw)
        set_saved_mtga_dir(path)
        self.refresh_status()
        messagebox.showinfo(APP_NAME, f"Saved MTGA folder:\n{path}")

    def _selected_mtga_dir(self) -> Path:
        raw = self.mtga_path_var.get().strip()
        if raw:
            return Path(raw)
        if self.state and self.state.mtga_dir:
            return self.state.mtga_dir
        raise FileNotFoundError("MTGA install folder is not set")

    def _install_bepinex(self) -> None:
        try:
            target = install_bepinex(self._selected_mtga_dir())
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"BepInEx install failed:\n\n{exc}")
            return
        self.refresh_status()
        messagebox.showinfo(APP_NAME, f"BepInEx installed/repaired at:\n{target}")

    def _install_plugin(self) -> None:
        try:
            target = install_plugin(self._selected_mtga_dir())
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Plugin install failed:\n\n{exc}")
            return
        self.refresh_status()
        messagebox.showinfo(APP_NAME, f"Bridge plugin installed at:\n{target}")

    def _repair_everything(self) -> None:
        try:
            changed = repair_bridge_stack(self._selected_mtga_dir())
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Repair failed:\n\n{exc}")
            return
        self.refresh_status()
        detail = "\n".join(str(path) for path in changed) or "No changes were needed."
        messagebox.showinfo(APP_NAME, f"MTGA bridge repair completed:\n\n{detail}")

    def _kill_zombies(self) -> None:
        zombies = find_zombie_processes()
        if not zombies:
            messagebox.showinfo(APP_NAME, "No orphaned mtgacoach processes found.")
            return

        details = "\n".join(
            f"  PID {z['pid']} \u2014 {z['reason']}" for z in zombies
        )
        if not messagebox.askyesno(
            APP_NAME,
            f"Found {len(zombies)} orphaned process(es):\n\n{details}\n\nKill them all?",
        ):
            return

        killed, lock_cleaned = kill_zombie_processes()
        parts = [f"PID {k['pid']}: {k.get('status', 'killed')}" for k in killed]
        if lock_cleaned:
            parts.append("Stale lock file removed.")
        messagebox.showinfo(APP_NAME, "Cleanup complete:\n\n" + "\n".join(parts))
        self.refresh_status()

    def _open_mtga_folder(self) -> None:
        try:
            self._safe_open(self._selected_mtga_dir())
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _open_player_log(self) -> None:
        if not self.state:
            return
        self._safe_open(self.state.player_log)

    def _open_bepinex_log(self) -> None:
        if not self.state or not self.state.bepinex_log:
            messagebox.showwarning(APP_NAME, "BepInEx log path not available.")
            return
        self._safe_open(self.state.bepinex_log)

    def _open_settings_dir(self) -> None:
        self._safe_open(Path.home() / ".arenamcp")

    def _open_runtime_root(self) -> None:
        if not self.state:
            return
        self._safe_open(self.state.runtime_root)

    def _safe_open(self, path: Path) -> None:
        try:
            open_path(path)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Failed to open:\n{path}\n\n{exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Open on the repair/setup tab",
    )
    args = parser.parse_args()

    root = tk.Tk()
    LauncherGUI(root, setup_tab=args.setup)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
