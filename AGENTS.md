# AGENTS.md - TST2SK Guidance

## Tech Stack & Setup
- **Python 3.11+**, **Playwright**, **OpenAI SDK**, **Rich**.
- **Setup**:
  - `pip install -r requirements.txt`
  - `playwright install`
  - Create `.env` from `.env.example`.

## Commands
- **Run**: `python main.py` or run `run.bat`


## Architecture
- `browser/`: Manages browser lifecycle and CDP access (bypasses CSP for JS injection).
- `ai/`: Handles AI communication and the "Brain" system prompt.
- `engine/`: Implements the agent loop (`brain.py`), diagnostic engine, and knowledge base.
- `scratch/`: Runtime folder for DOM snapshots, logs, and screenshots (auto-created).
- `kb/` & `playbooks/`: Long-term fix memory and recipes.

## Key Conventions & Quirks
- **Browser Access**: Use CDP via `browser.cdp` for low-level actions (e.g., `Runtime.evaluate`) to bypass Content Security Policy.
- **AI Protocol**:
  - **Silent until solved**: AI should not message user until a fix is verified or 3 attempts fail.
  - **Visual First**: Every thought must describe what the screenshot shows.
  - **Escalation**: `inject_css` $\rightarrow$ `inject_js` $\rightarrow$ DOM reconstruction.
- **Observation Flow**: `capture_observation` $\rightarrow$ write to `scratch/` $\rightarrow$ AI analysis.

## Verification
- Fixes must be verified by both visual check (screenshot) and `run_test` (visual computed styles).
