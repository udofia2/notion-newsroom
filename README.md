# Notion MCP Newsroom OS

An invisible AI operations layer on top of Notion databases.

This project turns Notion into an autonomous newsroom operating system with MCP tools, scheduled workflows, historical retrieval, and publishing bridge automation.

## Why this project exists

News teams already run planning and production in Notion, but repetitive work still slows them down:

- **digging through 5+ years of old coverage** for context (30 min per story)
- **drafting second-angle pitches** from traffic data (20 min per angle)
- **checking narrative quality** against brand rules (15 min per draft)
- **packaging content for agencies and downstream systems** (manual exports, formatting)

Notion MCP Newsroom OS automates these steps **without forcing a new UI**. Editors stay in Notion while AI workflows run in the background.

## Core Capabilities

- **MCP Tools**: 5 newsroom operations exposed via Model Context Protocol
- **Background Scheduler**: Polls Notion every 120 seconds, auto-triggers workflows
- **Vector Memory**: Chroma + Ollama embeddings for semantic search across archives
- **LLM-Powered Generation**: Context queries, angle ideation, narrative analysis
- **Analytics Bridge**: Real-time audience signals from GA4 or Plausible
- **Structured Observability**: Every workflow execution is logged with context


## How the 4 Workflows Work

### 1. **Context Hunter** – Historical Retrieval

Automatically find and surface related past coverage for any story.

```
Page Status: "Researching" → Context Hunter Triggered
    ↓
Extract draft title + content
    ↓
Generate semantic search query (using Ollama)
    ↓
Search Chroma vector store for similar articles
    ↓
Score results (0.55 threshold) by relevance
    ↓
Append "Historical Context" toggle block to page
    │ - 3-8 related stories with snippets, URLs, dates
    │ - Ranked by semantic similarity
    │ - Clickable links to archived Notion pages
    ↓
Researcher can explore context, reuse language, find contradictions
```

**Example usage:**

> Editor writes draft titled "Tech CEO Files for Bankruptcy"
> Context Hunter searches Chroma and finds:
>
> - "2019: Which Startups Survived the Downturn?" (0.82 match)
> - "The Silicon Valley Bankruptcy Timeline" (0.79 match)
> - "Comparing 2008 vs 2024 Founder Impacts" (0.76 match)
>
> Toggle block appears at top of draft with these links + snippets.

---

### 2. **Traffic Strategist** – Angle Discovery

Generate follow-up story angles based on real-time audience momentum.

```
Page Status: "Trending" OR runs on schedule
    ↓
Fetch real-time story views from GA4
    ↓
Detect stories hitting traffic thresholds (50+ views/30min)
    ↓
For each trending story, generate 3 "Angle 2" ideas:
    │ - "What changed since the original story?"
    │ - "Who are the hidden operators behind this?"
    │ - "What are the downstream impacts?"
    ↓
Create pitch page in Notion with:
    │ - Angle title + hypothesis + rationale
    │ - Priority (high/medium/low)
    │ - Supporting signals (view count, trend line)
    │ - Links to original story + related context
    ↓
Editorial team reviews pitches in real-time
```

**Example usage:**

> Story "New AI Regulation Announced" hits 340 views in 2 hours
> Traffic Strategist generates:
>
> 1. **Angle 2: Industry Reaction** – What are competitors saying?
> 2. **Angle 2: Loophole Analysis** – What regulatory gaps remain?
> 3. **Angle 2: Timeline Impact** – When does this law take effect & affect margins?
>
> Each angle is a full pitch page with hypothesis, sources, and editorial notes.

---

### 3. **Narrative Auditor** – Quality Assurance

Evaluate drafts against brand guidelines and post constructive feedback.

```
Page Status: "Needs Audit" → Narrative Auditor Triggered
    ↓
Extract full draft text + read brand guide page
    ↓
Heuristic evaluation (no async LLM call):
    │ - Minimum word count (250+)
    │ - Long sentence count (>34 words = clarity risk)
    │ - Hype language detection (disruptive, revolutionary, etc.)
    │ - Evidence-first approach validation
    │ - Citation & sourcing patterns
    ↓
Score draft (0-100) and assign status:
    │ - "pass": ready for publication (85+)
    │ - "needs_revision": has potential (60-84)
    │ - "fail": critical issues (<60)
    ↓
Post detailed comment to page:
    │ - Issues grouped by category (tone, clarity, citations, bias)
    │ - Severity levels (low/medium/high)
    │ - Actionable suggestions for each finding
    ↓
Editor sees comment in Notion, makes revisions
```

**Example output comment:**

```
🔍 Narrative Audit Summary: needs_revision (score: 71/100)

**Tone Issues**
- ⚠️ HIGH: "disruptive technology that changes everything"
  → Suggestion: Replace with specific metrics/impacts

**Structure**
- 🔸 MEDIUM: 3 sentences exceed 35 words (readability risk)
  → Suggestion: Break into 2-3 clearer statements

**Evidence**
- ✅ Good: "According to [source]" appears 4 times
- 🔸 MEDIUM: Lead paragraph lacks sourcing
  → Suggestion: Add attribution for main claim

👉 Next Steps: Revise tone language, add lead source, split long sentences
```

---

### 4. **Agency Bridge** – Publication Pipeline

Prepare page for external systems and downstream publishing.

```
Page Status: "Approved for Publication" → Agency Bridge Triggered
    ↓
Clean page blocks:
    │ - Remove toggles (Context Hunter blocks, workflow artifacts)
    │ - Remove child databases (synced blocks)
    │ - Remove property lines (status: X, date: Y, etc.)
    ↓
Extract clean text → markdown conversion
    ↓
Generate publication-ready payload:
    │ {
    │   "title": "...",
    │   "body_markdown": "...",
    │   "body_html": "...",
    │   "metadata": {author, date, tags},
    │   "notion_url": "...",
    │   "published_at": "..."
    │ }
    ↓
POST to external webhook or publishing platform
    ↓
On success: Mark page as "Published" in Notion
    │ - Add publication timestamp
    │ - Log external URL (if returned)
    │ - Archive page (optional)
    ↓
Content flows downstream: CDN, email, RSS, social scheduling tools
```

---

## MCP Tools Reference

All tools are exposed via the FastMCP server and can be called by compatible MCP clients (Cline, Continue, etc.).

### `search_historical_context(page_id, query, limit=8)`

Search Chroma vector store for related past coverage.

- **Returns**: List of contexts with snippet, URL, publish date, relevance score
- **Errors**: Handles missing pages gracefully

### `append_historical_block(page_id, query, limit=8)`

Automatically add a Historical Context toggle block to a page.

- **Returns**: Confirmation + count of appended contexts
- **Example**: Called by Context Hunter workflow

### `generate_followup_angles(page_id, query, top_n=3)`

Generate 3 follow-up story angle ideas with hypotheses.

- **Returns**: PitchIdea objects with title, hypothesis, rationale, priority
- **Example**: Called by Traffic Strategist when detecting trending stories

### `audit_narrative(page_id, brand_guide_page_id=None, post_comment=True)`

Evaluate draft quality and optionally post findings as a Notion comment.

- **Returns**: AuditResult with status (pass/needs_revision/fail), score, issues, recommendations
- **Example**: Called by Narrative Auditor workflow

### `prepare_for_publication(page_id)`

Clean formatting and produce publication-ready markdown.

- **Returns**: Cleanup statistics, markdown preview, character count
- **Example**: Called by Agency Bridge workflow

---

## Status-Based Workflow Dispatch

The scheduler monitors page status and automatically triggers workflows:

| Page Status                | Triggered Workflow | Action                                                 |
| -------------------------- | ------------------ | ------------------------------------------------------ |
| `Researching`              | Context Hunter     | Search historical context + append toggle block        |
| `Trending`                 | Traffic Strategist | Detect high-traffic stories + generate Angle 2 pitches |
| `Needs Audit`              | Narrative Auditor  | Evaluate draft + post feedback comment                 |
| `Approved for Publication` | Agency Bridge      | Clean + format markdown + POST webhook                 |

---


## Prerequisites

- **Python 3.12+**
- **uv** (recommended) or pip
- **Ollama** running locally or remotely (default: `http://localhost:11434`)
- **Notion** integration token with database/page access
- **Optional**: GA4 service account for real-time audience signals

## Quick Start

### 1. Clone and set up environment

```bash
git clone <repo>
cd notion-newsroom
cp .env.example .env
```

### 2. Install dependencies

```bash
uv sync
# or
pip install -e ".[dev]"
```

### 3. Start Ollama (in another terminal)

```bash
ollama pull llama3.2:3b
ollama pull nomic-embed-text:v1.5
ollama serve
```

### 4. Run the MCP server

```bash
uv run uvicorn newsroom.main:app --host 0.0.0.0 --port 8000 --reload
```

Server will start at `http://localhost:8000`

### 5. Test a workflow

```bash
# In VS Code or Cline, create a Notion page, set status to "Researching"
# Server will poll and auto-run Context Hunter within 120 seconds
# Check the page for the appended "Historical Context" toggle block
```

---

## Development Commands

```bash
# Lint and format
uv run ruff check src/
uv run black src/

# Type checking
uv run mypy src/

# Run tests
uv run pytest tests/ -v --asyncio-mode=auto

# Watch mode (rebuild on changes)
uv run uvicorn newsroom.main:app --reload

# Check dependencies
uv sync --dry-run
```

---

## Screenshots & Demo

### Context Hunter in Action

**[Screenshot placeholder: Notion page with "Historical Context" toggle block expanded]**

- Shows 3-5 related articles with snippets
- Ranked by semantic similarity (scores 0.55–0.95)
- Clickable links to archived pages

### Narrative Audit Comment

**[Screenshot placeholder: Notion page with detailed audit comment]**

- Issues grouped by category (tone, clarity, citations)
- Severity levels (LOW / MEDIUM / HIGH)
- Actionable suggestions for each finding
