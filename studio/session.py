"""Interrupt/resume session bridge: worker thread runs the graph, TUI talks to human."""

import queue
import sqlite3
import threading
import uuid
from typing import Any

import cilydian.pipeline as core
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command


class StudioSession:
    """Wraps CiLydian graph with interrupt/resume for human-in-the-loop."""

    def __init__(
        self, idea: str, run_id: str | None = None, config_path: str = "config.toml"
    ):
        self.run_id = run_id or f"studio-{uuid.uuid4().hex[:8]}"
        self.idea = idea
        self.events: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.awaiting_decision = threading.Event()
        self._worker: threading.Thread | None = None
        self._stop_requested = threading.Event()

        # Initialize core pipeline
        core.init_from_config(config_path, hitl=True)
        core.preflight()
        core.begin_run(self.run_id)

        # Checkpointer for interrupt/resume
        self._db_conn = sqlite3.connect(
            f"studio-{self.run_id}.db", check_same_thread=False
        )
        self.checkpointer = SqliteSaver(self._db_conn)
        self.graph = core.build_app(checkpointer=self.checkpointer)
        self.thread_config = {"configurable": {"thread_id": self.run_id}}

    def _log(self, msg: str) -> None:
        """Write directly to a file, bypassing Textual's stdout capture."""
        with open("force_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")

    def start(self) -> None:
        """Start graph execution in worker thread."""
        self._log("--- START CALLED ---")
        self._spawn(None)

    def _spawn(self, resume: Any) -> None:
        self._worker = threading.Thread(target=self._run, args=(resume,), daemon=True)
        self._worker.start()

    def is_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def stop(self) -> None:
        """Signal the graph to stop and close the DB connection."""
        self._log("--- STOP CALLED ---")
        self._stop_requested.set()
        self.commands.put(None)  # Unblock get()
        if hasattr(self, "_db_conn"):
            try:
                self._db_conn.close()
            except Exception:
                pass

    def _initial_state(self) -> dict:
        return {
            "idea": self.idea,
            "spec": "",
            "spec_path": "",
            "code": "",
            "verdict": {},
            "iteration": 0,
            "human_action": "",
            "code_hash": "",
            "prev_code_hash": "",
        }

    def _run(self, resume: Any) -> None:
        """Run graph; catch interrupts, wait for human decision, resume."""
        self._log(f"--- RUN STARTED, resume={resume} ---")
        try:
            if resume is not None:
                payload = Command(resume=resume)
            else:
                payload = self._initial_state()

            self._log("Calling graph.invoke()...")
            result = self.graph.invoke(payload, config=self.thread_config)
            self._log("graph.invoke() returned.")

            state = self.graph.get_state(self.thread_config)
            if state.next:  # paused at an interrupt point
                self._log("Graph paused at interrupt.")
                value = {}
                if state.tasks and state.tasks[0].interrupts:
                    value = state.tasks[0].interrupts[0].value

                self.events.put({"type": "interrupt", "payload": value})
                self.awaiting_decision.set()

                # Wait for command without timing out the user
                while not self._stop_requested.is_set():
                    try:
                        answer = self.commands.get(timeout=0.5)
                        if answer is None:  # Stop signal
                            self._log("Received None command (stop). Exiting.")
                            return
                        self._log(f"Received command: {answer}")
                        self.awaiting_decision.clear()
                        self._spawn(answer)
                        return
                    except queue.Empty:
                        continue

                self._log("Stop requested while waiting. Exiting.")
                return
            else:
                self._log("Graph finished.")
                verdict = (
                    (result or {}).get("verdict", {})
                    if isinstance(result, dict)
                    else {}
                )
                self.events.put({"type": "done", "verdict": verdict})

        except Exception as exc:  # noqa: BLE001
            self._log(f"EXCEPTION in _run: {type(exc).__name__}: {exc}")
            self.events.put(
                {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
            )

    def send(self, raw: str) -> None:
        """Parse human input and send to graph."""
        if not self.awaiting_decision.is_set():
            return

        raw = raw.strip()
        if raw.startswith("edit "):
            value = {"action": "edit", "spec": raw[5:]}
        elif raw.startswith("retry"):
            value = {"action": "retry", "feedback": raw[5:].strip()}
        elif raw in ("reject", "abort", "accept", "continue"):
            value = {"action": raw}
        else:
            value = {"action": "continue"}
        self.commands.put(value)
