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
- **Viewport & Layout Resizing Constraints (CRITICAL)**:
  - **NEVER** simulate mobile viewports by modifying the DOM body dimensions via CSS (e.g., `body { width: 375px !important; }`) or JS (e.g., `document.body.style.width = '375px'`).
  - *Why*: Forcing the DOM width to 375px while the browser viewport remains at 1280px causes the browser to evaluate desktop CSS media queries within a cramped body container. This results in layout collapse, vertical text-wrapping, and click target displacements, which will cause the verification subagent to hang indefinitely.
- **Resilient Clicking**:
  - If a selector-based click is intercepted by overlays or sticky headers, do not write programmatic click injections. Use `cdp_query_selector_all` to retrieve coordinates and click via `click_at_position(x, y)`.

## Verification
- **Verification Loop**: Fixes must be verified by both visual check (screenshot) and `run_test` (visual computed styles).
- **Incremental Verification**: Call `verify_fix` early and incrementally for every 1-2 fixes applied. Do not pile up 20+ injections before calling verification, as a single regression (like a collapsed body layout) will break the subagent replay runner.
- **Selector Validation**: Always check that selectors exist in the DOM (e.g., via `cdp_query_selector_all`) before creating CSS/JS fixes. Visual assumptions can lead to invalid selectors (e.g., using `.volume-discount-tier` instead of `.amp-bundles__volume-discount-bundles__tier-option`) which fail programmatic verification.
