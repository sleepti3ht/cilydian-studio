# cilydian-studio

Textual TUI client for [CiLydian](https://github.com/sleepti3ht/CiLydian): human-in-the-loop agentic CI pipeline.

## Installation

```bash
pip install cilydian-studio
```

## Usage

```bash
cilydian-studio "Sliding-window rate limiter: max 5 requests per 10s window"
```

The TUI will pause at interrupt points:
- **After SPEC generation**: review spec, type `continue`, `edit <new spec>`, or `reject`
- **At escalation** (when bounded loop exhausted): type `accept`, `retry <feedback>`, or `abort`

## Commands

| Command | Action |
|---|---|
| `continue` | Proceed to next stage |
| `edit <text>` | Replace spec with `<text>` |
| `reject` | Discard spec, regenerate |
| `accept` | Accept failing gates (escalation only) |
| `retry <feedback>` | Send feedback to coder, retry (escalation only) |
| `abort` | Stop execution (escalation only) |

## Architecture

Studio is a thin client over CiLydian core:
- **cilydian** (PyPI package): graph, gates, memory
- **cilydian-studio**: Textual TUI + interrupt/resume loop

Interrupt points use LangGraph checkpointing: state persists in SQLite, any client can resume.

## Requirements

- Python >= 3.12
- cilydian >= 0.2.0 (installed automatically)
- Local Ollama or cloud API keys (same as CiLydian)

## License

MIT
