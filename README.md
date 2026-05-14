<center>
<p align="center">
<pre>
  ████████╗ ███████╗    ████████╗ ██████╗     ███████╗ ██╗  ██╗
  ╚══██╔══╝ ██╔════╝    ╚══██╔══╝ ╚════██╗    ██╔════╝ ██║ ██╔╝
     ██║    ███████╗       ██║     █████╔╝    ███████╗ █████╔╝
     ██║    ╚════██║       ██║    ██╔═══╝     ╚════██║ ██╔═██╗
     ██║    ███████║       ██║    ███████╗    ███████║ ██║  ██╗
     ╚═╝    ╚══════╝       ╚═╝    ╚══════╝    ╚══════╝ ╚═╝  ╚═╝
</pre>
</p>
</center>

<h3 align="center">Troubleshooting Tier 2 Sidekick</h3>
<p align="center"><b>Autonomous web page investigator and fixer</b></p>

<p align="center">
  <a href="#features">Features</a> •
  <a href="#how-it-works">How It Works</a> •
  <a href="#installation">Installation</a> •
  <a href="#configuration">Configuration</a> •
  <a href="#usage">Usage</a> •
  <a href="#actions">Actions</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#license">License</a>
</p>

---

## What is TST2SK?

TST2SK is an autonomous AI-powered agent that investigates and fixes web page issues in real time. Point it at any URL, describe the problem, and it launches a browser, captures the full page state (DOM, console logs, network activity, screenshots), diagnoses the issue, applies fixes, verifies they work, and delivers the solution — all without human intervention.

Built for Tier 2 support teams who deal with front-end issues on live sites — broken layouts, hidden elements, disabled buttons, CSS conflicts, JavaScript errors, failed API calls. TST2SK handles the investigation loop that would normally take a human 15-30 minutes of DevTools work.

## Features

- **Fully autonomous agent loop** — observe, think, act, verify, repeat. No hand-holding required.
- **Multi-model support** — works with any OpenAI-compatible API: Google AI Studio, OpenRouter, OpenAI, or local models.
- **Vision + Text-only modes** — multimodal models get screenshots for visual analysis; text-only models use DOM annotations, `inspect_element`, and `run_test` to investigate without seeing the page.
- **26 actions** — from clicking buttons and injecting CSS/JS to deep DOM searches, network body inspection, and automated test assertions.
- **Fix-and-verify loop** — every fix is verified with computed style checks and visual confirmation before being delivered.
- **Escalation protocol** — CSS first, then targeted JS, then DOM reconstruction, then user notification. One fix per injection, verified separately.
- **Knowledge base** — logs verified fixes to `kb/fixes.log` and auto-searches past solutions for similar issues.
- **Session memory** — full conversation transcripts saved to `convo/` with auto-generated tags. The AI searches past sessions for relevant context.
- **Playbooks** — drop fix recipes into `playbooks/PLAYBOOKS.md` and the AI will reference them during investigations.
- **Smart retry logic** — incremental backoff (3s, 6s, 9s...) with live countdown for API rate limits and server errors. Free-tier friendly.
- **Stuck-loop detection** — if the AI repeats the same action with the same payload 3 times, it gets nudged to communicate with the user.
- **Robust JSON parsing** — 5-strategy parser handles malformed AI responses, XML-wrapped JSON, garbled text. Extracts intent even from broken output.
- **Clean CLI experience** — thought panels, action icons, compact results, animated thinking spinner, markdown-rendered code blocks.
- **Copy to clipboard** — type `copy` after any fix to copy the code to your clipboard.

## How It Works

```
You → describe the problem → TST2SK launches browser → captures page state
                                        ↓
                              AI analyzes DOM + console + network + screenshot
                                        ↓
                              Searches playbooks + knowledge base + past sessions
                                        ↓
                              Applies fix (CSS first, then JS if needed)
                                        ↓
                              Runs verification test (computed styles + visual check)
                                        ↓
                              Fix verified? → Delivers solution with copyable code
                              Not fixed?   → Tries different approach (up to 3 attempts)
                              Still broken? → Reports findings and asks for user input
```

## Installation

### Prerequisites

- **Python 3.10+**
- **Git** (to clone the repo)

### Step 1: Clone the repository

```bash
git clone https://github.com/your-username/tst2sk.git
cd tst2sk
```

### Step 2: Install dependencies

```bash
pip install -r requirements.txt
```

### Step 3: Install Playwright browsers

```bash
playwright install chromium
```

### Step 4: Get an API key

TST2SK works with any OpenAI-compatible API. Here are setup instructions for the two most popular free/cheap options:

#### Option A: Google AI Studio (Free tier available)

1. Go to [Google AI Studio](https://aistudio.google.com/)
2. Sign in with your Google account
3. Click **"Get API Key"** in the left sidebar
4. Click **"Create API key"** and select a project (or create one)
5. Copy the API key

Configure your `.env`:
```env
AI_API_KEY=your-google-api-key
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
AI_MODEL=gemma-4-27b-it
AI_LLM_PROVIDER=Google API
AI_MULTIMODAL_MODEL=true
```

Recommended Google models:
| Model | Vision | Notes |
|-------|--------|-------|
| `gemini-2.0-flash` | Yes | Fast, good for most tasks |
| `gemini-2.5-flash-preview-05-20` | Yes | Latest, best reasoning |
| `gemma-4-27b-it` | Yes | Smaller, works on free tier |

#### Option B: OpenRouter (Many models, pay-per-token)

1. Go to [OpenRouter](https://openrouter.ai/)
2. Sign up or sign in
3. Go to **"Keys"** in the top navigation
4. Click **"Create Key"**
5. Name it (e.g., "TST2SK") and copy the key

Configure your `.env`:
```env
AI_API_KEY=your-openrouter-api-key
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=google/gemini-2.0-flash-001
AI_LLM_PROVIDER=OpenRouter
AI_MULTIMODAL_MODEL=true
```

Recommended OpenRouter models:
| Model | Vision | Notes |
|-------|--------|-------|
| `google/gemini-2.0-flash-001` | Yes | Fast and affordable |
| `anthropic/claude-sonnet-4` | Yes | Strong reasoning |
| `meta-llama/llama-4-maverick` | Yes | Open-source, capable |

### Step 5: Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` with your API key and model choice.

### Step 6: Run

```bash
python main.py
```

Or on Windows, double-click `Run TST2SK.bat`.

## Configuration

All configuration is done through the `.env` file:

| Variable | Required | Description |
|----------|----------|-------------|
| `AI_API_KEY` | Yes | Your API key |
| `AI_BASE_URL` | Yes | API endpoint URL |
| `AI_MODEL` | Yes | Model name/ID |
| `AI_LLM_PROVIDER` | No | Display name shown in CLI (e.g., "Google API") |
| `AI_MULTIMODAL_MODEL` | No | `true` for vision models, `false` for text-only (default: `true`) |
| `AI_EXTRA_HEADERS` | No | JSON string of extra HTTP headers for custom providers |

### Vision vs Text-Only Mode

**Vision mode** (`AI_MULTIMODAL_MODEL=true`): The AI receives a screenshot every turn. It uses visual evidence to drive investigation — identifying missing content, broken layouts, invisible elements. Best with multimodal models like Gemini, GPT-4o, or Claude.

**Text-only mode** (`AI_MULTIMODAL_MODEL=false`): The AI works without screenshots, relying entirely on DOM annotations (`[HIDDEN:display]`, `[HIDDEN:opacity]`, `★` for interactive elements), `inspect_element` for computed styles, and `run_test` for verification. Use this with text-only models or to reduce API costs.

## Usage

```
python main.py
```

1. Enter the target URL
2. Describe the issue (or press Enter for a general investigation)
3. Watch the AI work — you'll see thought panels, actions, and results in real time
4. When the AI delivers a fix, type `copy` to copy the code to your clipboard
5. Confirm with `yes`, `done`, or `close` to end the session — or give follow-up instructions

### Example Prompts

**Fix a specific issue:**
> The Add to Cart button is not responding when clicked. Fix it.

**Styling changes:**
> The product title font is too small. Make it 32px bold. Also make the Add to Cart button bright red with white text.

**Investigation only:**
> Check if the page loads correctly and report what you see. Do not fix anything.

**Debug errors:**
> Something is broken on this page. Find all JavaScript errors, check which network requests failed, and read the response body of any failed API calls.

**Complex diagnosis:**
> Customers report that the variant selector doesn't update the price when they pick a different size. Investigate and fix.

## Actions

TST2SK has 26 actions split into two categories:

### Browser Actions
These interact with the page and trigger a fresh observation after execution.

| Action | Description |
|--------|-------------|
| `click` | Click an element by CSS selector |
| `click_at_position` | Click at specific x,y coordinates |
| `type` | Type text into an input field |
| `scroll` | Scroll the page by x,y pixels |
| `hover` | Hover over an element |
| `navigate` | Go to a URL |
| `inject_css` | Inject CSS styles (Level 1 fix — always tried first) |
| `inject_js` | Execute JavaScript via CDP, bypasses CSP (Level 2 fix) |
| `inspect_element` | Get computed styles, bounding rect, and attributes for an element |
| `run_test` | Run assertions in a try/catch — the primary verification tool |
| `observe` | Get a fresh observation without doing anything |
| `clear_site_data` | Clear cookies, localStorage, sessionStorage for the current page |
| `capture_element` | Crop screenshot to a specific element |
| `post_message` | Speak to the user (only after verified fix or 3 failures) |
| `answer_user` | Alias for post_message |

### Search Actions
These are instant lookups — no browser roundtrip needed.

| Action | Description |
|--------|-------------|
| `diagnose` | Full cross-reference of DOM, console, and network data with scenario detection |
| `search_dom` | Regex search of the full DOM snapshot |
| `search_console` | Search console logs for errors, warnings, messages |
| `search_network` | Search network request log for failures, specific endpoints |
| `read_network_body` | Read saved response bodies from API calls |
| `search_playbook` | Search playbook recipes for known fix patterns |
| `search_fixes` | Search the knowledge base for past verified fixes |
| `search_conversations` | Search past session transcripts by tags |
| `get_conversation_detail` | Load a full past session transcript |
| `log_fix` | Log a verified fix to the knowledge base |

## Architecture

```
tst2sk/
├── main.py                 # Entry point — CLI interface, startup display
├── config.py               # Configuration from .env
├── ai/
│   ├── client.py           # OpenAI-compatible API client with retry logic
│   └── prompts.py          # System prompts (multimodal + text-only variants)
├── browser/
│   ├── controller.py       # Playwright browser management
│   ├── observer.py         # Page state capture (DOM, console, network, screenshots)
│   └── actions.py          # All 26 action handlers
├── engine/
│   ├── brain.py            # The autonomous agent loop — observe, think, act, repeat
│   ├── diagnostics.py      # Cross-reference diagnostics + scenario detection
│   ├── search.py           # DOM, console, network search implementations
│   ├── kb.py               # Knowledge base — log fixes, search fixes, playbooks
│   └── convo_logger.py     # Session conversation logger with tag index
├── playbooks/              # Drop fix recipes here (PLAYBOOKS.md)
├── kb/                     # Knowledge base (auto-created)
│   └── fixes.log           # Verified fixes log (auto-created)
├── convo/                  # Session transcripts (auto-created)
├── scratch/                # Temp observation files (auto-created, overwritten each turn)
├── .env                    # Your configuration (not tracked in git)
├── .env.example            # Configuration template
├── requirements.txt        # Python dependencies
└── Run TST2SK.bat          # Windows launcher
```

### The Agent Loop

```
┌─────────────────────────────────────────────────────────────┐
│                     AGENT LOOP (brain.py)                    │
│                                                             │
│  1. Capture observation (DOM + console + network + screenshot)
│  2. Build slim context for AI (summaries + relevant KB fixes)
│  3. Send to AI model with system prompt                      │
│  4. Parse AI response → extract action                       │
│  5. Execute action (browser or local search)                │
│  6. Loop back to step 1 with fresh observation              │
│                                                             │
│  Exit conditions:                                           │
│  - User confirms fix ("yes", "done", "close")              │
│  - AI escalates to user after 3 failed fix attempts         │
│  - User interrupts (Ctrl+C)                                │
└─────────────────────────────────────────────────────────────┘
```

### Escalation Order

The AI follows a strict escalation protocol for fixes:

1. **Level 1: CSS** — `inject_css` first. Most visibility/layout issues resolve here.
2. **Level 2: Targeted JS** — `inject_js` if CSS failed. One fix per injection.
3. **Level 3: DOM reconstruction** — Only if targeted fixes keep failing.
4. **Level 4: User notification** — After 3 failed attempts, report findings and ask for help.

JS is allowed as a first attempt only for: disabled buttons, event handlers, form logic, variant IDs, fetch/API calls, script re-initialization.

### Knowledge Base

Every verified fix can be logged to `kb/fixes.log`. The AI auto-searches this file at the start of each session, matching by URL domain, detected scenario, and diagnosis hints. Over time, the KB becomes a library of proven solutions that accelerates future investigations.

### Playbooks

Drop fix recipes into `playbooks/PLAYBOOKS.md` using markdown headers (`# Recipe Name`). The AI searches playbooks by symptom keywords and uses matching recipes as guidance — not as blind orders. If its investigation reveals a different root cause than what the playbook describes, it trusts its investigation and pivots.

### Session Transcripts

Every session is saved to `convo/` as a JSON file with auto-generated tags (site domain, issue type, fix method, resolution status). A lightweight tag index (`convo/tag_index.json`) enables fast search across hundreds of sessions without reading every file.

## Troubleshooting

**"AI_API_KEY not found"** — Create a `.env` file from `.env.example` and add your API key.

**Empty AI responses or crashes on Turn 1** — Your model may be too small for the observation payload. Try a larger model or switch to text-only mode (`AI_MULTIMODAL_MODEL=false`) to reduce payload size.

**Retries keep firing (429/500 errors)** — You're hitting rate limits. The app retries automatically with incremental backoff. If using free-tier APIs, this is normal during peak hours.

**"Playwright browsers not installed"** — Run `playwright install chromium`.

## Contributing

Contributions are welcome! Some areas that could use help:

- **Playbook recipes** — Add proven fix patterns for common platforms (Shopify, WordPress, WooCommerce)
- **Scenario detection** — Improve the diagnostics engine to better identify page types
- **New actions** — Add browser automation capabilities
- **Model tuning** — Test with different AI models and report what works best

## License

MIT License

Copyright (c) 2025 Jall Fiel

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

<p align="center">
  <b>Built by Jall Fiel</b>
</p>
