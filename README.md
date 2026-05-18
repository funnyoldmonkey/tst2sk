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

TST2SK is an autonomous AI-powered agent that investigates and fixes web page issues in real time. Point it at any URL, describe the problem, and it launches a browser, captures the full page state (DOM, console logs, network activity, screenshots), diagnoses the issue, applies fixes, verifies the work, and delivers the solution — all without human intervention.

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
- **API key round-robin** — supply multiple Google API keys (comma-separated in `.env`) and TST2SK rotates between them every N requests. On 429 rate limits, it immediately switches to the next key. Keeps free-tier sessions alive longer.
- **Smart retry logic** — incremental backoff (3s, 6s, 9s...) with live countdown for API rate limits and server errors. Free-tier friendly.
- **Stuck-loop detection** — if the AI repeats the same action with the same payload 3 times, it gets nudged to communicate with the user.
- **Multi-line paste** — paste multi-line code or logs into the CLI prompt. Buffered input is drained automatically; press Enter on a blank line to submit.
- **Robust JSON parsing** — 5-strategy parser handles malformed AI responses, XML-wrapped JSON, and garbled text. Extracts intent even from broken output.
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
                              Fix verified? → Delivers a solution with copyable code
                              Not fixed?   → Tries different approach (up to 3 attempts)
                              Still broken? → Reports findings and asks for user input
```

## Installation

### Prerequisites

- **Python 3.10+**
- **Git** (to clone the repo)

### Step 1: Clone the repository

```bash
git clone https://github.com/funnyoldmonkey/tst2sk.git
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
AI_API_KEY_GOOGLE=your-key-1,your-key-2,your-key-3
AI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
AI_MODEL=gemma-4-31b-it
AI_LLM_PROVIDER=Google_API
AI_MULTIMODAL_MODEL=true
AI_ROUND_ROBIN_SWITCH=3
```
Multiple comma-separated keys enable round-robin rotation (see [Configuration](#configuration)).

Recommended Google models:
| Model | Vision | Notes |
|-------|--------|-------|
| `gemini-3.1-flash-lite` | Yes | Fast, good for most tasks, works on free tier |
| `gemma-4-31b-it` | Yes | Latest, best reasoning, works on free tier |
| `gemma-4-29b-a4b-it` | Yes | Smaller, works on free tier |

#### Option B: OpenRouter (Many models, pay-per-token)

1. Go to [OpenRouter](https://openrouter.ai/)
2. Sign up or sign in
3. Go to **"Keys"** in the top navigation
4. Click **"Create Key"**
5. Name it (e.g., "TST2SK") and copy the key

Configure your `.env`:
```env
AI_API_KEY_OPENROUTER=your-openrouter-api-key
AI_BASE_URL=https://openrouter.ai/api/v1
AI_MODEL=openrouter/free
AI_LLM_PROVIDER=OpenRouter
AI_MULTIMODAL_MODEL=false
```

Recommended OpenRouter models:
| Model | Vision | Notes |
|-------|--------|-------|
| `openrouter/free` | Yes | Fast and affordable |
| `poolside/laguna-m.1:free` | Yes | Strong reasoning |
| `openrouter/owl-alpha` | Yes | Open-source, capable |

### Step 5: Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and add your API key(s) under the correct provider variable (`AI_API_KEY_GOOGLE`, `AI_API_KEY_OPENROUTER`, or `AI_API_KEY_LMSTUDIO`).

### Step 6: Run the setup wizard

```bash
python setup.py
```

Or on Windows, double-click `Setup TST2SK.bat`.

The setup wizard lets you pick a provider, model, and mode interactively. It tests your API key(s) before saving. You can re-run it any time to switch providers or models. It does not touch your API keys — those are always edited directly in `.env`.

### Step 7: Run

```bash
python main.py
```

Or on Windows, double-click `Run TST2SK.bat`.

## Configuration

All configuration is done through the `.env` file. Use `python setup.py` (or `Setup TST2SK.bat`) to switch providers and models interactively — API keys are always edited directly in `.env`.

| Variable | Required | Description |
|----------|----------|-------------|
| `AI_API_KEY_GOOGLE` | Per provider | Google API key(s), comma-separated for round-robin |
| `AI_API_KEY_OPENROUTER` | Per provider | OpenRouter API key |
| `AI_API_KEY_LMSTUDIO` | Per provider | LMStudio API key (default: `lm-studio`) |
| `AI_LLM_PROVIDER` | Yes | Active provider: `Google_API`, `OpenRouter`, or `Local_(LMStudio)` |
| `AI_BASE_URL` | Yes | API endpoint URL (set by setup wizard) |
| `AI_MODEL` | Yes | Model name/ID (set by setup wizard) |
| `AI_MULTIMODAL_MODEL` | No | `true` for vision models, `false` for text-only (default: `true`) |
| `AI_ROUND_ROBIN_SWITCH` | No | Rotate API key every N requests — Google only (default: `3`) |
| `AI_EXTRA_HEADERS` | No | JSON string of extra HTTP headers for custom providers |

### API Key Round-Robin (Google)

Google's free tier has per-key rate limits (15 RPM). If you have multiple API keys, add them comma-separated:

```env
AI_API_KEY_GOOGLE=key-aaa,key-bbb,key-ccc
AI_ROUND_ROBIN_SWITCH=3
```

TST2SK rotates to the next key every 3 requests (configurable). If any key hits a 429 rate limit, it immediately rotates to the next key before retrying. The CLI shows rotation events:

```
🔄 Key rotated (scheduled): key 1 → 2/3
🔄 Key rotated (429 rate limit): key 2 → 3/3
```

Round-robin only applies to Google with multiple keys. OpenRouter and LMStudio use a single key and skip rotation entirely.

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
5. Paste multi-line content (code snippets, error logs) directly — the CLI handles buffered input and waits for a blank line to submit
6. Type `end` to close the session and auto-save fixes to the knowledge base — or give follow-up instructions

### Example Prompts

**Fix a specific issue:**
> The Add to Cart button is not responding when clicked. Fix it.

**Styling changes:**
> The product title font is too small. Make it 32px bold. Also, make the Add to Cart button bright red with white text.

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
These are instant lookups — no browser round-trip needed.

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
├── config.py               # Configuration from .env (provider keys, round-robin)
├── setup.py                # Interactive setup wizard — switch provider, model, mode
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
├── Run TST2SK.bat          # Windows launcher
└── Setup TST2SK.bat        # Windows launcher for setup wizard
```

### The Agent Loop

```
┌─────────────────────────────────────────────────────────────┐
│                     AGENT LOOP (brain.py)                    │
│                                                             │
│  1. Capture observation (DOM + console + network + screenshot)
│  2. Build slim context for AI (summaries + relevant KB fixes)
│  3. Send to the AI model with the system prompt                      │
│  4. Parse AI response → extract action                       │
│  5. Execute action (browser or local search)                │
│  6. Loop back to step 1 with fresh observation              │
│                                                             │
│  Exit conditions:                                           │
│  - User types "end" (auto-logs fixes to KB)                │
│  - AI escalates to the user after 3 failed fix attempts    │
│  - 3 consecutive API errors (circuit breaker)              │
│  - User interrupts (Ctrl+C)                                │
└─────────────────────────────────────────────────────────────┘
```

### Escalation Order

The AI follows a strict escalation protocol for fixes:

1. **Level 1: CSS** — `inject_css` first. Most visibility/layout issues resolve here.
2. **Level 2: Targeted JS** — `inject_js` if CSS failed. One fix per injection.
3. **Level 3: DOM reconstruction** — Only if targeted fixes keep failing.
4. **Level 4: User notification** — After 3 failed attempts, report findings and ask for help.

JS is allowed as a first attempt only for: disabled buttons, event handlers, form logic, variant IDs, fetch/API calls, and script re-initialization.

### Knowledge Base

When you type `end` to close a session, any fixes applied during that session are automatically logged to `kb/fixes.log`. The AI auto-searches this file at the start of each session, matching by URL domain, detected scenario, and diagnosis hints. Over time, the KB becomes a library of proven solutions that accelerates future investigations. Sessions closed via Ctrl+C or the circuit breaker save the transcript to `convo/` but do not write to the KB.

### Playbooks

Drop fix recipes into `playbooks/PLAYBOOKS.md`. The AI searches playbooks by symptom keywords and uses matching recipes as guidance — not as blind orders. If its investigation reveals a different root cause than what the playbook describes, it trusts its investigation and pivots.

#### How playbook search works

The search function (`search_playbook` in `engine/kb.py`) splits `PLAYBOOKS.md` into sections using the `# ` delimiter (top-level markdown header at the start of a line). Each `# Header` starts a new section. When the AI calls `search_playbook("opacity")`, it does a case-insensitive text search across all sections and returns up to 3 matching sections.

#### Playbook format

Each playbook entry must start with a top-level `# ` header. This is the section separator — everything from one `# ` header to the next is treated as one playbook entry. Use keywords in both the header and body that the AI would search for (symptom names, CSS properties, error messages, platform names).

```markdown
# Element Hidden by Opacity
- Symptom: Element exists in DOM but is invisible on page.
- Check: `getComputedStyle(el).opacity` returns `0` or near-zero.
- Fix: `inject_css` with `opacity: 1 !important` on the target selector.
- Verify: `run_test` checking `parseFloat(getComputedStyle(el).opacity) === 1`.

# Shopify Add to Cart Button Disabled
- Symptom: Add to Cart button is present but does not respond to clicks.
- Check: Look for `disabled` attribute, `pointer-events: none`, or overlay elements blocking clicks.
- Fix: Remove `disabled` attribute via `inject_js`, or remove overlay via `inject_css`/`inject_js`.
- Verify: `run_test` checking `!el.disabled && getComputedStyle(el).pointerEvents !== 'none'`.

# Popup or Modal Blocking Page
- Symptom: A modal, popup, or overlay is covering the page and intercepting clicks.
- Check: Search DOM for high z-index overlays, `position: fixed` elements, or known popup frameworks (Alia, Privy, Klaviyo, OptinMonster).
- Fix: Remove the popup container via `inject_js` or hide it via `inject_css` with `display: none !important`.
- Verify: Screenshot shows popup gone; `run_test` confirms target elements are now clickable.
```

**Rules:**
- `# ` (hash + space) at the start of a line is the only separator. Do NOT use `---` or blank lines as separators between entries.
- Subheadings (`##`, `###`) within a section are fine — they won't split the entry.
- Include searchable keywords: symptom descriptions, CSS property names, platform names, error text.
- Keep each entry self-contained — the AI receives only matching sections, not the whole file.
- The AI gets a max of 3 matching sections per search to keep context lean.

#### Adding a new playbook

Open `playbooks/PLAYBOOKS.md` and append a new entry at the bottom. Copy this template:

```markdown
# [Short Descriptive Title with Keywords]
- Platform: [Shopify / WordPress / WooCommerce / Any / etc.]
- Symptom: [What the user sees or reports. Use words they would use.]
- Check: [What to inspect — DOM selectors, computed styles, console errors, network calls.]
- Root Cause: [Why this happens — the underlying technical reason.]
- Fix: [Which action to use — inject_css, inject_js, or both. Include the actual code.]
- Code:
  ```js
  // paste the fix code here
  ```
- Verify: [How to confirm the fix worked — run_test assertion, inspect_element check, or visual confirmation.]
- Notes: [Optional. Edge cases, variations, or things to watch out for.]

**Real example** — paste this directly into your `PLAYBOOKS.md` to try it:

```markdown
# Shopify Product Price Hidden by App Conflict
- Platform: Shopify
- Symptom: Product price is missing or invisible on the product page.
- Check: Search DOM for `.price` or `[class*=price]`. If element exists but has `[HIDDEN:opacity]` or `[HIDDEN:display]`, it's a visibility issue. Check console for errors from third-party apps (e.g., Bold, ReCharge, Discount Ninja).
- Root Cause: A third-party pricing app injects CSS that hides the default price element, usually via `opacity: 0`, `display: none`, or `visibility: hidden`. When the app fails to load its replacement, the price disappears entirely.
- Fix: Use `inject_css` to restore visibility on the price element.
- Code:
  ```css
  .price, .price-item, [class*="price"] {
    opacity: 1 !important;
    display: block !important;
    visibility: visible !important;
  }
  ```
- Verify: `run_test` with `parseFloat(getComputedStyle(document.querySelector('.price')).opacity) === 1` and confirm price text is not empty.
- Notes: If the price shows as $0.00 or wrong value after making it visible, the issue is data-level (variant JSON or Liquid), not CSS. Escalate to Level 2 (inject_js) to read the variant data from `window.ShopifyAnalytics.meta` or the product JSON.


### Session Transcripts

Every session is saved to `convo/` as a JSON file with auto-generated tags (site domain, issue type, fix method, resolution status). A lightweight tag index (`convo/tag_index.json`) enables fast search across hundreds of sessions without reading every file.

## Troubleshooting

**"AI_API_KEY not found"** — Create a `.env` file from `.env.example` and add your API key.

**Empty AI responses or crashes on Turn 1** — Your model may be too small for the observation payload. Try a larger model or switch to text-only mode (`AI_MULTIMODAL_MODEL=false`) to reduce payload size.

**Retries keep firing (429/500 errors)** — You're hitting rate limits. The app retries automatically with incremental backoff. If using Google's free tier, add multiple API keys to `.env` (comma-separated under `AI_API_KEY_GOOGLE`) to enable round-robin rotation and spread the load across keys.

**"Playwright browsers not installed"** — Run `playwright install chromium`.

## Contributing

Contributions are welcome! Some areas that could use help:

- **Playbook recipes** — Add proven fix patterns for common platforms (Shopify, WordPress, WooCommerce)
- **Scenario detection** — Improve the diagnostics engine to better identify page types
- **New actions** — Add browser automation capabilities
- **Model tuning** — Test with different AI models and report what works best

## License

MIT License

Copyright (c) 2026 Jall Fiel

---

<p align="center">
  <b>Built by Jall Fiel</b>
</p>
