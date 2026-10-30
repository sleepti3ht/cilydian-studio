"""Minimal Textual TUI: chat with the pipeline at interrupt points."""

import argparse

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Input, RichLog

from studio.session import StudioSession


class StudioApp(App):
    """Textual TUI for CiLydian human-in-the-loop sessions."""

    CSS = """
    RichLog {
        height: 1fr;
        border: solid green;
    }
    Input {
        dock: bottom;
    }
    """

    def __init__(self, idea: str, run_id: str | None = None,
                 config_path: str = "config.toml"):
        super().__init__()
        self.session = StudioSession(run_id=run_id, idea=idea,
                                     config_path=config_path)
        self.title = f"CiLydian Studio: {idea[:40]}"
        self._session_finished = False

    def compose(self) -> ComposeResult:
        yield Header()
        self.rich_log = RichLog(markup=True, max_lines=5000)
        yield self.rich_log
        yield Input(
            placeholder="continue | edit <spec> | retry <feedback> | accept | reject | abort"
        )
        yield Footer()

    def on_mount(self) -> None:
        """Start session and poll events."""
        try:
            self.session.start()
        except Exception as exc:  # noqa: BLE001 - TUI must not crash if worker fails to start
            self.rich_log.write(f"[red]Failed to start session:[/red] {exc}")
            self._session_finished = True
            return
        self.set_interval(0.2, self._poll)
        self.rich_log.write("[blue]Session started. Waiting for interrupt...[/blue]")

    def _poll(self) -> None:
        """Check for events from graph worker."""
        if self._session_finished:
            return

        while not self.session.events.empty():
            ev = self.session.events.get()
            if ev["type"] == "interrupt":
                payload = ev["payload"]
                stage = payload.get("stage", "?").upper()
                self.rich_log.write(f"[yellow]== {stage} awaits decision ==[/yellow]")

                # Show context (spec or verdict)
                if "spec" in payload:
                    self.rich_log.write(f"[dim]SPEC:[/dim]\n{payload['spec'][:1000]}")
                if "verdict" in payload:
                    self.rich_log.write(f"[dim]VERDICT:[/dim] {payload['verdict']}")

                actions = payload.get("actions", [])
                self.rich_log.write(f"[cyan]actions: {actions}[/cyan]")
                if stage == "ESCALATION":
                    self.rich_log.write(
                        "[yellow]accept = mark pass despite failing gates; "
                        "code stays broken as-is[/yellow]"
                    )
            elif ev["type"] == "done":
                self.rich_log.write(f"[green]DONE[/green] {ev['verdict']}")
                self.rich_log.write("[dim]Session finished. Press Ctrl+C to exit.[/dim]")
                self._session_finished = True
            elif ev["type"] == "error":
                self.rich_log.write(f"[red]ERROR[/red] {ev['message']}")
                self.rich_log.write(
                    "[dim]Session failed. Press Ctrl+C to exit, "
                    "then rerun with the same --run-id after fixing.[/dim]"
                )
                self._session_finished = True

        # Fallback: worker died without emitting an event
        if not self._session_finished and not self.session.is_alive():
            self.rich_log.write(
                "[red]Worker thread exited unexpectedly.[/red] "
                "[dim]Press Ctrl+C to exit.[/dim]"
            )
            self._session_finished = True

    def on_input_submitted(self, msg: Input.Submitted) -> None:
        self.rich_log.write(f"[blue]> {msg.value}[/blue]")
        if self._session_finished:
            self.rich_log.write("[yellow]session is over; command ignored[/yellow]")
        elif not self.session.awaiting_decision.is_set():
            self.rich_log.write("[yellow]no pending decision; command ignored[/yellow]")
        else:
            self.session.send(msg.value)
        msg.input.value = ""
    def on_exit(self) -> None:
        """Called when TUI closes."""
        self.session.stop()

def main_entry() -> None:
    """CLI entry point: cilydian-studio [--config PATH] [--run-id ID] "idea" """
    parser = argparse.ArgumentParser(
        description="Textual TUI for CiLydian human-in-the-loop sessions"
    )
    parser.add_argument("idea", nargs="+", help="one-line task description")
    parser.add_argument("--config", default="config.toml",
                        help="path to role/model config (default: config.toml in CWD)")
    parser.add_argument("--run-id", default=None,
                        help="artifact dir under runs/ (default: studio-<uuid>)")
    args = parser.parse_args()
    StudioApp(idea=" ".join(args.idea), config_path=args.config,
              run_id=args.run_id).run()


if __name__ == "__main__":
    main_entry()
