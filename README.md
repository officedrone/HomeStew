# HomeBrain 🧠

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
│         HomeBrain Container          │
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

- HomeBrain searches DuckDuckGo for PDF manuals
- Downloads up to 5 relevant PDFs
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

| Variable       | Default                     | Description                           |
| -------------- | --------------------------- | ------------------------------------- |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | LLM API endpoint (Ollama, vLLM, etc.) |
| `LLM_API_KEY`  | `changeme`                  | API key (ignored by local servers)    |
| `LLM_MODEL`    | `llama3.2`                  | Model name to use                     |
| `DATA_DIR`     | `/data`                     | Persistent data directory             |

### Example: Using with Ollama

```bash
# Start Ollama first
docker run -d -v ollama_data:/root/.ollama -p 11434:11434 ollama/ollama

# Pull a model
docker exec -it <ollama_container> ollama pull llama3.2

# Then start HomeBrain (uses Ollama by default)
docker-compose up -d
```

### Example: Using with OpenAI

Edit `docker-compose.yml`:

```yaml
environment:
  - LLM_BASE_URL=https://api.openai.com/v1
  - LLM_API_KEY=sk-your-api-key-here
  - LLM_MODEL=gpt-4o-mini
```

## Project Structure

```
HomeBrain - Simple/
├── homebrain/                 # Python backend
│   ├── api/                   # FastAPI routes
│   │   ├── devices.py         # Device CRUD endpoints
│   │   ├── downloads.py       # Manual download triggers
│   │   ├── search.py          # Search endpoints
│   │   └── chat.py            # Chat with LLM
│   ├── services/              # Business logic
│   │   ├── pdf_extractor.py   # PDF text extraction (pypdf)
│   │   ├── manual_downloader.py  # DuckDuckGo scraper
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
│   ├── homebrain.db           # SQLite database
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
- **Web Scraping**: duckduckgo-search + requests
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
uvicorn homebrain.main:app --reload --host 0.0.0.0 --port 8000
```

### Testing

```bash
# Run tests (if you add them)
pytest tests/
```

## Troubleshooting

### Manuals not downloading?

- Check internet connectivity from container
- Verify DuckDuckGo search works: `docker exec homebrain python -c "from duckduckgo_search import DDGS; print(list(DDGS().files('test pdf', max_results=3)))"`

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

- Manual PDF upload (if auto-download fails)
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
