# HomeStew

A **super lightweight** home device manual manager with AI-powered search. Store, index, and search through your device manuals using natural language queries.

## Features

- ✅ **Add devices** - Track your home devices (TVs, appliances, electronics, etc.)
- ✅ **Auto-download manuals** - Automatically search and download PDF manuals from the internet
- ✅ **Full-text search** - Lightning-fast keyword search across all manuals using SQLite FTS5
- ✅ **AI chat interface** - Ask questions in natural language, AI searches manuals for answers
- ✅ **Zero heavy dependencies** - No vector databases, no complex infrastructure
- ✅ **Single Docker container** - ~60MB image, easy homelab deployment
- ✅ **Persistent storage** - All data stored locally with volume mounts

## Architecture

```
┌─────────────────────────────────────┐
│          HomeStew Container          │
│  ┌──────────┐  ┌──────────┐        │
│  │ FastAPI  │  │ SQLite   │        │
│  │ Backend  │◄─┤ FTS5     │        │
│  └────┬─────┘  └──────────┘        │
│       │                            │
│  ┌────▼─────┐  ┌──────────┐       │
│  │ HTMX Web │  │ LLM Chat │       │
│  │ Frontend │  │ w/ Tools │       │
│  └──────────┘  └──────────┘       │
└─────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Docker & Docker Compose
- An LLM server (Ollama, vLLM, or OpenAI-compatible API)

### Installation

1. **Clone or download this repository**

2. **Configure your LLM connection** (optional - defaults to local Ollama):

   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

3. **Build and run**:

   ```bash
   docker-compose up -d --build
   ```

4. **Access the web UI**: http://localhost:8000

## Usage

### 1. Add a Device

Click "Add New Device" in the sidebar:

- **Device name**: Living Room TV (your label)
- **Brand**: Samsung
- **Model**: QN90A
- **Description** (optional): 65-inch Neo QLED TV

### 2. Download Manuals

Click "Download Manuals" on any device:

- HomeStew searches several web engines (Bing, Brave, DuckDuckGo, Mojeek...) for PDF manuals
- Downloads up to 5 relevant PDFs (validated: real `%PDF` files only, size-capped)
- Automatically indexes all text content

### 3. Search Manuals

Use the search tab:

- Type keywords: `"HDMI port location"` or `"WiFi setup"`
- Filter by specific device if needed
- View highlighted snippets from PDFs

### 4. AI Chat

Switch to the chat tab and ask questions:

- _"How do I connect my Samsung QN90A to WiFi?"_
- _"What's the power consumption of this TV?"_
- _"Where are the HDMI ports located?"_

The AI will automatically search your manuals and provide answers based on the actual documentation.

## Configuration

### Environment Variables

| Variable                | Default                            | Description                                             |
| ----------------------- | ---------------------------------- | ------------------------------------------------------- |
| `LLM_BASE_URL`          | `http://localhost:11434/v1`        | LLM API endpoint (Ollama, vLLM, etc.)                   |
| `LLM_API_KEY`           | _(empty)_                          | API key (ignored by local servers)                      |
| `LLM_MODEL`             | `llama3.2`                         | Model name to use                                       |
| `DATA_DIR`              | `/data`                            | Persistent data directory                               |
| `SECRETS_KEY_FILE`      | `/run/secrets/homestew_secret_key` | Master-key file for secret encryption (see Secrets)     |
| `EXTRA_ALLOWED_ORIGINS` | _(empty - same-origin only)_       | Comma-separated origins allowed cross-origin API access |

### Example: Using with Ollama

```bash
# Start Ollama first
docker run -d -v ollama_data:/root/.ollama -p 11434:11434 ollama/ollama

# Pull a model
docker exec -it <ollama_container> ollama pull llama3.2

# Then start HomeStew (uses Ollama by default)
docker-compose up -d
```

### Example: Using with OpenAI

Set the base URL / model in `docker-compose.yml`, but **do not put the API key
there** - enter it once in the Settings UI instead. It is encrypted at rest
(see below) and never written to any file you commit:

```yaml
environment:
  - LLM_BASE_URL=https://api.openai.com/v1
  - LLM_MODEL=gpt-4o-mini
```

## Secrets (API keys & tokens)

Credentials - the LLM API key, webhook URL and webhook bearer token - are
encrypted with **AES-256-GCM** before being written to `settings.json` in the
data volume. The Settings UI is _write-only_ for them: their values never
leave the server (the API reports only whether each is configured), so they
cannot be read back through the browser.

**One-time setup.** On first start HomeStew shows a short **setup wizard**:
create an encryption key, connect your AI model and add your first device.
The key step generates a random 32-byte master key at `/data/.secrets_key`
(mode `0600`) inside the `homestew_data` volume. Creating it turns encryption
on for good - the key survives rebuilds and container recreation, and the
step never appears again. _Every_ step can be skipped, and skipping (or
saving) is remembered in the data volume, so a resolved step is not re-shown
on later launches even after a restart. Steps whose condition no longer
applies - a key already exists, an LLM was configured via env/Settings, or
devices already exist - are skipped automatically. You can also manage the
key later under **Settings > Advanced**, which lets you **delete** the
managed key to troubleshoot or rotate it; AI settings live under
**Settings > AI**.

**Back it up.** The key lives next to the ciphertext: a backup of the data
volume covers both, but if you lose the volume (or delete `.secrets_key`)
stored secrets can no longer be decrypted - they're reported as unreadable
and treated as unset, and you re-enter them once in the Settings UI.

**What this protects against - and what it doesn't:** plaintext keys in git,
in `settings.json`, or in container metadata. With a HomeStew-managed key it
sits in the same volume as the ciphertext, so anyone who can read `/data`
(or restore a backup of it) can decrypt. To raise that bar, keep the master
key **outside** the volume by mounting your own key file (the mounted file
always wins over the managed one):

```bash
# Once, from the repository root
mkdir -p secrets
openssl rand -base64 32 > secrets/homestew_secret_key   # Linux/macOS/WSL
```

```powershell
# PowerShell
mkdir secrets -Force
[IO.File]::WriteAllBytes("secrets\homestew_secret_key",
    (1..32 | ForEach-Object { Get-Random -Maximum 256 }))
```

Then uncomment the `secrets:` blocks in `docker-compose.yml` - Compose mounts
the file read-only at `/run/secrets/homestew_secret_key`, so it never appears
in `docker-compose.yml`, `docker inspect` or the image. The `secrets/`
directory is git-ignored. Even then, the `file` secret driver is still a host
file: someone with root on the host can read both the key and the ciphertext.
For that threat use full-disk encryption or an external secrets manager.

- **No usable key at all?** (fresh install, skipped wizard, or `/data` not
  writable) HomeStew still runs but stores secrets as plaintext and shows a
  warning banner in Settings - useful for bare `uvicorn` dev runs on a
  read-only checkout.
- **Key rotated or lost?** Stored secrets can no longer be decrypted; they're
  reported as unreadable and treated as unset - re-enter them once in the
  Settings UI.
- **Rotating deliberately:** delete the managed key under Settings >
  Advanced (or replace the mounted file), create a new one, then re-enter
  the secrets; old ciphertext is overwritten with new on save. A _mounted_
  key file is never deleted by the app - remove the container's secret
  mount for that.

## Project Structure

```
HomeStew/
├── homestew/                  # Python backend
│   ├── api/                   # FastAPI routes
│   │   ├── devices.py         # Device CRUD endpoints
│   │   ├── downloads.py       # Manual download triggers
│   │   ├── search.py          # Search endpoints
│   │   └── chat.py            # Chat with LLM
│   ├── services/              # Business logic
│   │   ├── pdf_extractor.py   # PDF text extraction (pypdf)
│   │   ├── web_search.py         # ddgs wrapper (engine-agnostic web search)
│   │   ├── manual_finder.py      # query building + PDF candidate ranking
│   │   ├── manual_downloader.py  # PDF download + validation
│   │   ├── indexer.py         # SQLite FTS5 indexing
│   │   ├── search_engine.py   # Full-text search
│   │   └── llm_client.py      # OpenAI-compatible client
│   ├── models/                # Pydantic schemas
│   ├── config.py              # Configuration
│   ├── db.py                  # Database setup
│   └── main.py                # FastAPI app entry
├── frontend/                  # Static web UI
│   ├── index.html             # Single-page app
│   ├── styles.css             # Minimal CSS
│   └── app.js                 # HTMX + vanilla JS
├── data/                      # Persistent storage (mounted volume)
│   ├── homestew.db            # SQLite database
│   └── devices/               # PDF manuals by device ID
├── Dockerfile                 # Multi-stage build (~60MB)
├── docker-compose.yml         # Container orchestration
├── requirements.txt           # Python dependencies
└── README.md                  # This file
```

## Tech Stack

- **Backend**: FastAPI (Python 3.12)
- **Database**: SQLite with FTS5 (full-text search, zero deps)
- **PDF Processing**: pypdf (pure Python, ~200KB)
- **Web Search**: ddgs (metasearch over bing/brave/duckduckgo/...) + requests
- **LLM Integration**: openai library (works with any OpenAI-compatible API)
- **Frontend**: HTMX + vanilla JavaScript (no build process)
- **Deployment**: Docker multi-stage build (~60MB image)

## Why So Lightweight?

### No Vector Databases

We use SQLite FTS5 for keyword search - it's built into Python, supports highlighting and ranking (BM25), and requires zero extra dependencies.

### Tool-Based RAG

Instead of complex vector embeddings, the LLM decides when to search using a simple tool call. This is more flexible and requires no embedding models.

### Minimal Frontend

HTMX handles AJAX without React/Vue build complexity. Single HTML file, no npm, no webpack.

### Single Container

Everything runs in one ~60MB container with persistent volumes for data.

## API Endpoints

### Devices

- `POST /api/devices` - Add new device
- `GET /api/devices` - List all devices
- `DELETE /api/devices/{id}` - Delete device

### Downloads

- `POST /api/downloads/trigger` - Download manuals for device
- `GET /api/downloads/{device_id}/manuals` - List device manuals

### Search

- `GET /api/search?q=query&device_id=X` - Search manuals
- `GET /api/search/stats` - Get index statistics

### Chat

- `POST /api/chat` - Send message, get AI response with manual search
- `POST /api/chat/stream` - Stream chat response (SSE)

## Development

### Local Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run development server
uvicorn homestew.main:app --reload --host 0.0.0.0 --port 8000
```

### Testing

```bash
# Run tests (if you add them)
pytest tests/
```

## Troubleshooting

### Manuals not downloading?

- Check internet connectivity from container
- Verify web search works: `docker exec homestew python -c "from ddgs import DDGS; print(DDGS().text('nespresso manual filetype:pdf', max_results=3))"`
- **`Invalid impersonate: "..."` in the log?** That is a dependency mismatch inside the
  image (the search library and its HTTP client `primp` disagree on browser profiles),
  not a network problem. Rebuild with fresh layers:
  `docker-compose build --no-cache && docker-compose up -d`
- Tunables are env vars (see `homestew/config.py`): `MANUAL_SEARCH_BACKENDS`
  (comma-list of engines, empty = auto), `MANUAL_SEARCH_REGION`, `MANUAL_MAX_DOWNLOADS`,
  `MANUAL_MAX_PDF_MB`, `MANUAL_PROXY`
- Dependencies in `requirements.txt` are range-pinned on purpose: an unpinned
  transitive bump silently broke manual fetching once (primp 2.x dropping browser
  profiles the old search library hardcoded). Keep upper bounds when updating.

### LLM not responding?

- Verify your LLM server is running and accessible
- Check `LLM_BASE_URL` in docker-compose.yml
- Test connection: `curl http://localhost:11434/v1/chat/completions`

### Search returns no results?

- Ensure PDFs were successfully downloaded and indexed
- Check `/data/devices/{id}/manuals/` for PDF files
- Verify text extraction works on your PDFs (some scanned PDFs need OCR)

## Future Enhancements

Potential additions if needed:

- More notification channels (email, MQTT)
- Support for ZIP archives with multiple manuals
- Scheduled re-indexing
- Export/import device configurations
- Mobile-responsive improvements

## License

MIT License - feel free to use and modify!

## Contributing

This is a personal homelab project, but PRs are welcome if they align with the "super lightweight" philosophy!

---

**Built with ❤️ for homelab enthusiasts who hate running unnecessary databases.**
