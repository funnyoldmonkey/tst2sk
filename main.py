"""TST2SK Entry Point."""
import asyncio
import os
import sys
import psutil
from rich.console import Console
from rich.panel import Panel
from rich.align import Align
from rich.text import Text

from config import AppConfig
from engine.brain import Brain

console = Console()

LOGO = r"""
  ████████╗ ███████╗    ████████╗ ██████╗     ███████╗ ██╗  ██╗
  ╚══██╔══╝ ██╔════╝    ╚══██╔══╝ ╚════██╗    ██╔════╝ ██║ ██╔╝
     ██║    ███████╗       ██║     █████╔╝    ███████╗ █████╔╝
     ██║    ╚════██║       ██║    ██╔═══╝     ╚════██║ ██╔═██╗
     ██║    ███████║       ██║    ███████╗    ███████║ ██║  ██╗
     ╚═╝    ╚══════╝       ╚═╝    ╚══════╝    ╚══════╝ ╚═╝  ╚═╝
"""

def ensure_single_instance():
    """Close any other running instances of this application."""
    current_pid = os.getpid()
    current_cwd = os.getcwd().lower()
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
        try:
            # Check if it's a python process
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                cmdline = proc.info['cmdline']
                if cmdline and any('main.py' in arg for arg in cmdline):
                    # Check if it's running from the same directory
                    proc_cwd = proc.info['cwd']
                    if proc_cwd and proc_cwd.lower() == current_cwd:
                        if proc.info['pid'] != current_pid:
                            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

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

    # Show provider and model info
    provider = os.getenv("AI_LLM_PROVIDER", "Unknown")
    model = config.model or "Unknown"
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

    query = input("❓ Your concern: ").strip()
    if not query:
        query = "Investigate the page for any issues."

    # 3. Start Brain
    brain = Brain(config)
    try:
        await brain.start(url, query)
    except KeyboardInterrupt:
        console.print("\n[yellow]Session interrupted by user.[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]Fatal Error: {e}[/bold red]")
    finally:
        await brain.browser.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

