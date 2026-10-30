"""Interrupt/resume session bridge: worker thread runs the graph, TUI talks to human."""

import queue
import sqlite3
import threading
import uuid
from typing import Any

import cilydian.pipeline as core
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command


class StudioSession:
    """Wraps CiLydian graph with interrupt/resume for human-in-the-loop."""

    def __init__(self, idea: str, run_id: str | None = None, config_path: str = "config.toml"):
        self.run_id = run_id or f"studio-{uuid.uuid4().hex[:8]}"
        self.idea = idea
        self.events: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.awaiting_decision = threading.Event()  # true only while graph is paused
        self._worker: threading.Thread | None = None

        # Initialize core pipeline
        core.init_from_config(config_path, hitl=True)
        core.preflight()
        core.begin_run(self.run_id)

        # Checkpointer for interrupt/resume
        self._db_conn = sqlite3.connect(f"studio-{self.run_id}.db", check_same_thread=False)
        self.checkpointer = SqliteSaver(self._db_conn)
        self.graph = core.build_app(checkpointer=self.checkpointer)
        self.thread_config = {"configurable": {"thread_id": self.run_id}}

    def start(self) -> None:
        """Start graph execution in worker thread."""
        self._spawn(None)

    def _spawn(self, resume: Any) -> None:
        self._worker = threading.Thread(target=self._run, args=(resume,), daemon=True)
        self._worker.start()

    def is_alive(self) -> bool:
        """True if worker thread is currently running."""
        return self._worker is not None and self._worker.is_alive()

    def stop(self) -> None:
        """Signal the graph to stop and close the DB connection."""
        # Note: LangGraph doesn't have a hard kill; we just close the DB.
        # The daemon thread will die when the TUI process exits.
        if hasattr(self, "_db_conn"):
            self._db_conn.close()

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
        try:
            if resume is None:
                result = self.graph.invoke(self._initial_state(), config=self.thread_config)
            else:
                result = self.graph.invoke(
                    Command(resume=resume), config=self.thread_config
                )

            state = self.graph.get_state(self.thread_config)
            if state.next:  # paused at an interrupt point (LangGraph >=1.0 contract)
                value = {}
                if state.tasks and state.tasks[0].interrupts:
                    value = state.tasks[0].interrupts[0].value
                self.events.put({"type": "interrupt", "payload": value})
                self.awaiting_decision.set()
                answer = self.commands.get()
                self.awaiting_decision.clear()
                self._spawn(answer)
            else:
                verdict = (result or {}).get("verdict", {}) if isinstance(result, dict) else {}
                self.events.put({"type": "done", "verdict": verdict})

        except GraphInterrupt as gi:  # fallback: subgraph-style propagation
            intr = getattr(gi, "interrupts", None)
            value = intr[0].value if intr else (gi.args[0] if gi.args else {})
            self.events.put({"type": "interrupt", "payload": value})
            self.awaiting_decision.set()  # FIX: was missing here
            answer = self.commands.get()
            self.awaiting_decision.clear()
            self._spawn(answer)

        except Exception as exc:  # noqa: BLE001
            self.events.put(
                {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
            )

    def send(self, raw: str) -> None:
        """Parse human input and send to graph."""
        if not self.awaiting_decision.is_set():
            return  # FIX: ignore if graph is not waiting for a decision

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
