"""TST2SK Entry Point."""
import asyncio
import os
import sys
from rich.console import Console
from rich.panel import Panel
from rich.align import Align
from rich.text import Text

from config import AppConfig
from engine.brain import Brain, _read_multiline_input

console = Console()

LOGO = r"""
  ████████╗ ███████╗    ████████╗ ██████╗     ███████╗ ██╗  ██╗
  ╚══██╔══╝ ██╔════╝    ╚══██╔══╝ ╚════██╗    ██╔════╝ ██║ ██╔╝
     ██║    ███████╗       ██║     █████╔╝    ███████╗ █████╔╝
     ██║    ╚════██║       ██║    ██╔═══╝     ╚════██║ ██╔═██╗
     ██║    ███████║       ██║    ███████╗    ███████║ ██║  ██╗
     ╚═╝    ╚══════╝       ╚═╝    ╚══════╝    ╚══════╝ ╚═╝  ╚═╝
"""

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tst2sk.lock")

def ensure_single_instance():
    """Prevent multiple instances using a PID lockfile."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                old_pid = int(f.read().strip())
            # Check if the old process is still running
            try:
                os.kill(old_pid, 0)  # Signal 0 = check if alive
                # Process exists — terminate it
                import signal
                os.kill(old_pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass  # Process already dead — stale lock
        except (ValueError, IOError):
            pass  # Corrupt lock file — just overwrite

    # Write our PID
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except IOError:
        pass  # Non-critical

def _cleanup_lock():
    """Remove lockfile on exit."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, "r") as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.remove(LOCK_FILE)
    except Exception:
        pass

async def main():
    ensure_single_instance()

    # Premium Header
    logo_text = Text(LOGO, style="bold cyan")
    console.print(Align.center(logo_text))

    console.print(Panel(
        "[bold white]Troubleshooting Tier 2 Sidekick[/bold white]\n[dim]Autonomous web page investigator and fixer[/dim]",
        subtitle="[bold magenta]Built by Jall Fiel[/bold magenta]",
        expand=False,
        border_style="cyan"
    ), justify="center")

    # 1. Configuration
    config = AppConfig.from_env()
    if not config.api_key:
        console.print("\n[bold red]Error: AI_API_KEY not found in .env file.[/bold red]")
        console.print("Please create a .env file based on .env.example")
        return
    if not config.base_url:
        console.print("\n[bold red]Error: AI_BASE_URL not found in .env file.[/bold red]")
        console.print("Set AI_BASE_URL to your API endpoint (e.g., https://generativelanguage.googleapis.com/v1beta/openai/)")
        return
    if not config.model:
        console.print("\n[bold red]Error: AI_MODEL not found in .env file.[/bold red]")
        console.print("Set AI_MODEL to your model name (e.g., gemini-2.0-flash)")
        return

    # Show provider and model info
    provider = os.getenv("AI_LLM_PROVIDER", "Unknown")
    model = config.model
    mode = "Multimodal (Vision)" if config.multimodal else "Text-Only"
    info_block = Text()
    info_block.append(f"Provider : ", style="dim")
    info_block.append(f"{provider}\n", style="bold white")
    info_block.append(f"Model    : ", style="dim")
    info_block.append(f"{model}\n", style="bold white")
    info_block.append(f"Mode     : ", style="dim")
    info_block.append(f"{mode}", style="bold white")
    console.print()
    console.print(Align.center(info_block))

    # 2. User Input
    url = input("\n📍 Target URL: ").strip()
    if not url:
        console.print("[red]URL is required.[/red]")
        return
    if not url.startswith("http"):
        url = "https://" + url

    query = _read_multiline_input("❓ Your concern: ")
    if not query:
        query = "Investigate the page for any issues."

    # 3. Start Brain
    brain = Brain(config)
    try:
        await brain.start(url, query)
    except KeyboardInterrupt:
        console.print("\n[yellow]Session interrupted by user.[/yellow]")
    except SystemExit:
        pass  # Clean exit from signal handler
    except Exception as e:
        console.print(f"\n[bold red]Fatal Error: {e}[/bold red]")
    finally:
        await brain.browser.close()

if __name__ == "__main__":
    import atexit
    atexit.register(_cleanup_lock)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
