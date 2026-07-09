"""Command-line entry point.

Two subcommands are exposed:

* ``python main.py chat``  - interactive terminal chat (default action).
* ``python main.py serve`` - run the FastAPI application via uvicorn.

Both delegate to the same :class:`app.agent.AgentService` singleton so the
behaviour is identical across surfaces.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import uuid
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table

from app import __version__
from app.agent import get_agent_service
from app.config import get_settings
from app.utils import configure_logging, format_duration, get_logger

app = typer.Typer(
    name="ai-devops-assistant",
    help="AI DevOps Assistant powered by Google ADK + Gemini 2.5.",
    add_completion=False,
    no_args_is_help=False,
)
console = Console()
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Interactive chat
# ---------------------------------------------------------------------------


@app.command(help="Start an interactive terminal chat with the DevOps agent.")
def chat(
    session_id: Optional[str] = typer.Option(
        None,
        "--session-id",
        "-s",
        help="Reuse an existing session id (default: fresh session per run).",
    ),
    user_id: Optional[str] = typer.Option(
        None,
        "--user-id",
        "-u",
        help="Stable user identifier (default: 'cli-user').",
    ),
    single_question: Optional[str] = typer.Option(
        None,
        "--ask",
        "-a",
        help="Ask a single question and exit (non-interactive mode).",
    ),
) -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        json_logs=settings.log_json,
    )
    _install_signal_handlers()

    session_id = session_id or f"cli-{uuid.uuid4().hex[:12]}"
    user_id = user_id or "cli-user"

    _render_banner(settings, session_id=session_id, user_id=user_id)

    service = get_agent_service()

    if single_question:
        _ask_once(service, single_question, session_id=session_id, user_id=user_id)
        return

    _interactive_loop(service, session_id=session_id, user_id=user_id)


# ---------------------------------------------------------------------------
# FastAPI server
# ---------------------------------------------------------------------------


@app.command(help="Run the FastAPI service (uvicorn under the hood).")
def serve(
    host: Optional[str] = typer.Option(None, help="Host to bind. Defaults to API_HOST."),
    port: Optional[int] = typer.Option(None, help="Port to bind. Defaults to API_PORT."),
    reload: bool = typer.Option(False, help="Enable uvicorn auto-reload (dev only)."),
    workers: int = typer.Option(1, help="Number of uvicorn workers."),
) -> None:
    import uvicorn  # imported lazily so `python main.py chat` doesn't need it

    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        json_logs=settings.log_json,
    )

    bind_host = host or settings.api_host
    bind_port = port or settings.api_port
    console.print(
        Panel.fit(
            f"[bold]AI DevOps Assistant[/bold] v{__version__}\n"
            f"[cyan]http://{bind_host}:{bind_port}[/cyan]  "
            f"docs: [cyan]http://{bind_host}:{bind_port}/docs[/cyan]",
            title="Serving",
            border_style="green",
        )
    )
    uvicorn.run(
        "app.api.routes:app",
        host=bind_host,
        port=bind_port,
        reload=reload,
        workers=workers if not reload else 1,
        log_config=None,
    )


# ---------------------------------------------------------------------------
# Version / info
# ---------------------------------------------------------------------------


@app.command(help="Print the assistant version and exit.")
def version() -> None:
    console.print(f"ai-devops-assistant [bold]{__version__}[/bold]")


@app.callback(invoke_without_command=True)
def _default(ctx: typer.Context) -> None:
    """When invoked without arguments, drop straight into chat mode."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(chat)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _render_banner(settings, *, session_id: str, user_id: str) -> None:
    banner = Table.grid(padding=(0, 2))
    banner.add_row("[bold]AI DevOps Assistant[/bold]", f"v{__version__}")
    banner.add_row("Model", f"[cyan]{settings.model_name}[/cyan]")
    banner.add_row(
        "Target VM",
        f"[cyan]{settings.vm_user}@{settings.vm_host}:{settings.vm_port}[/cyan]",
    )
    banner.add_row("Read-only mode", "[green]ON[/green]" if settings.read_only_mode else "[yellow]OFF[/yellow]")
    banner.add_row("Session", session_id)
    banner.add_row("User", user_id)
    console.print(Panel(banner, title="devops-copilot", border_style="cyan"))
    console.print(
        "[dim]Type your question and press Enter. "
        "Use /help for commands, /quit to exit.[/dim]"
    )
    console.print(Rule(style="dim"))


def _interactive_loop(service, *, session_id: str, user_id: str) -> None:
    """Blocking REPL that shuttles user prompts through :class:`AgentService`."""
    while True:
        try:
            message = Prompt.ask("[bold cyan]you[/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return

        if not message:
            continue

        cmd = message.lower().strip()
        if cmd in {"/quit", "/exit", "/q"}:
            console.print("[dim]bye[/dim]")
            return
        if cmd in {"/help", "/?"}:
            _render_help()
            continue
        if cmd in {"/session"}:
            console.print(f"session_id = [cyan]{session_id}[/cyan]")
            continue
        if cmd in {"/reset"}:
            session_id = f"cli-{uuid.uuid4().hex[:12]}"
            console.print(f"[green]new session:[/green] {session_id}")
            continue

        _ask_once(service, message, session_id=session_id, user_id=user_id)


def _ask_once(service, message: str, *, session_id: str, user_id: str) -> None:
    """Send a single question and pretty-print the reply."""
    with console.status("[bold cyan]thinking...[/bold cyan]", spinner="dots"):
        try:
            reply = asyncio.run(
                service.chat(message, session_id=session_id, user_id=user_id)
            )
        except Exception as exc:  # noqa: BLE001 - CLI must survive one bad turn
            console.print(f"[bold red]error:[/bold red] {exc}")
            logger.opt(exception=True).error("chat call failed in CLI")
            return

    console.print("[bold green]assistant[/bold green]")
    console.print(Markdown(reply.answer or "(no response)"))
    footer_bits = [f"[dim]{format_duration(reply.duration_ms / 1000)}[/dim]"]
    if reply.tool_calls:
        footer_bits.append(f"[dim]tools: {', '.join(reply.tool_calls)}[/dim]")
    footer_bits.append(f"[dim]session: {reply.session_id}[/dim]")
    console.print("  ".join(footer_bits))
    console.print(Rule(style="dim"))


def _render_help() -> None:
    help_table = Table(show_header=True, header_style="bold cyan", box=None)
    help_table.add_column("Command", style="bold")
    help_table.add_column("Description")
    help_table.add_row("/help", "Show this help.")
    help_table.add_row("/session", "Print the current session id.")
    help_table.add_row("/reset", "Start a brand-new session.")
    help_table.add_row("/quit", "Exit the CLI.")
    help_table.add_row("Ctrl+C / Ctrl+D", "Exit the CLI.")
    console.print(help_table)


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001 - signal callback signature
        console.print("\n[dim]signal caught, bye[/dim]")
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Some environments (e.g. Windows main-thread only for SIGTERM,
            # or non-main thread) forbid installing signal handlers. Ignore.
            pass


if __name__ == "__main__":  # pragma: no cover - CLI entry
    app()
