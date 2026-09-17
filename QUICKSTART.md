# HomeStew Quick Start Guide

## 🚀 One-Command Deployment (with Docker)

```powershell
# 1. From the repository root (where docker-compose.yml lives)
cd <repo-root>

# 2. (Optional) Configure LLM settings
copy .env.example .env
notepad .env  # Edit if needed — keep real API keys out of this file;
              # enter them in the Settings UI (stored encrypted)

# 3. Build and run (encryption is on out of the box — HomeStew generates a
#    master key in the data volume on first boot; see README "Secrets")
docker-compose up -d --build

# 4. Open browser
start http://localhost:8000
```

> **Upgrading from an older checkout?** Data used to live in `./data`; the
> compose file now uses a named volume. Move it once before/after the first
> start:
>
> ```powershell
> docker-compose up -d
> docker-compose cp ./data/. homestew:/data   # then remove .\data when satisfied
> ```
>
> Plaintext secrets in an old `settings.json` are encrypted automatically on
> the first boot (once the auto-generated master key exists).

## 📋 Step-by-Step Instructions

### Prerequisites Check

- [ ] Docker Desktop installed and running
- [ ] (Optional) Ollama or other LLM server running locally

### Installation Steps

**Step 1: Build the container**

```powershell
cd <repo-root>  # where docker-compose.yml lives
docker-compose build
```

**Step 2: Start the service**

```powershell
docker-compose up -d
```

**Step 3: Verify it's running**

```powershell
docker ps | Select-String homestew
curl http://localhost:8000/health
```

**Step 4: Access the web UI**
Open your browser to: http://localhost:8000

### First-Time Setup

1. **Add your first device** (e.g., Samsung TV):
   - Name: "Living Room TV"
   - Brand: "Samsung"
   - Model: "QN90A"
   - Click "Add Device"

2. **Download manuals**:
   - Click "Download Manuals" on the new device
   - Wait for download to complete (check toast notification)

3. **Try searching**:
   - Go to "Search" tab
   - Type: "HDMI port location"
   - See results from your PDFs

4. **Ask AI questions**:
   - Switch to "AI Chat" tab
   - Ask: "How do I connect my Samsung TV to WiFi?"
   - AI will search manuals and answer!

## 🔧 Troubleshooting

### Container won't start?

```powershell
# Check logs
docker-compose logs homestew

# Rebuild with verbose output
docker-compose build --no-cache --verbose
```

### LLM not responding?

- Make sure your LLM server is running: `ollama serve` (if using Ollama)
- Test connection: `curl http://localhost:11434/v1/models`
- Update `LLM_BASE_URL` in docker-compose.yml if needed

### No manuals found?

- Check internet connectivity from container
- Try different brand/model names
- Manually verify DuckDuckGo search works for your device

## 📊 View Logs & Monitor

```powershell
# Live logs
docker-compose logs -f homestew

# Resource usage
docker stats homestew

# Stop service
docker-compose down

# Stop and remove data (careful!)
docker-compose down -v
```

## 💡 Tips

- **Data persistence**: All PDFs and database are in `./data/` folder
- **Backup**: Just copy the `data/` directory
- **Multiple devices**: Add as many devices as you want
- **Manual uploads**: (Future feature) - for now, rely on auto-download

## 🎯 Next Steps

1. Add all your home devices
2. Download manuals for each
3. Build up your searchable knowledge base
4. Use AI chat to quickly find answers!

---

**Need help?** Check the full README.md or inspect container logs.
