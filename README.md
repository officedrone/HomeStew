# HomeStew — User Guide

**Your home appliance & device knowledge hub.** HomeStew keeps track of every device in your home, downloads their manuals, makes them fully searchable, and lets you ask an AI questions answered straight from your own documentation. Everything runs locally on your machine — your data never leaves it.

---

## What You Can Do With It

- **Track your devices** — TVs, appliances, electronics: brand, model, serial, purchase date and warranty (end date is calculated for you).
- **Fetch manuals automatically** — HomeStew searches the web for official PDF manuals and stores them with the device. You can also upload your own PDFs.
- **Search inside manuals** — instant full-text search across every manual, with highlighted snippets and direct links to the exact PDF page.
- **Ask the AI** (Requires integration with a self-hosted or cloud-based LLM) — chat in plain language ("How do I connect my TV to WiFi?") and get answers grounded in your actual manuals, with source links. The AI can also add devices or create reminders for you.
- **Maintenance calendar** — recurring reminders (replace the filter every 3 months…) with optional notifications sent to a webhook (e.g. Synology Chat).
- **Photos & vision** — with a vision-capable AI model, snap a nameplate or error screen and ask about it.
- **Backup & restore** — one click exports everything (devices, manuals, calendar) to a single `.zip`.

> **Note:** the AI chat feature needs an LLM — a local one like [Ollama](https://ollama.com), [LM Studio](https://lmstudio.ai/) llama.cpp , VLLM, or any hosted OpenAI-compatible API. While core functionality works without this integration, many of the product's enhanced features rely on the LLM integration.

---

## Requirements

- Docker installed and running (with Docker Compose for the from-source install)
- A modern web browser
- _(Optional,but recommended)_ An LLM server such as Ollama, vLLM, or an OpenAI-compatible API key

---

## Installation

Depending on where you are looking to install from, there are two ways to get HomeStew running:

### Option A: From Docker Hub (prebuilt image)

1. **Pull the image:**

   ```bash
   docker pull officedrone/homestew:latest
   ```

2. **Run it:**

   ```bash
   docker run -d --name homestew \
     -p 8000:8000 \
     -v homestew_data:/data \
     --restart unless-stopped \
     officedrone/homestew:latest
   ```

3. **Open your browser** at **http://localhost:8000**

### Option B: From GitHub (build from source)

1. **Clone the repository:**

   ```bash
   git clone https://github.com/officedrone/HomeStew.git
   cd HomeStew
   ```

2. **Start it:**

   ```bash
   docker-compose up -d --build
   ```

3. **Open your browser** at **http://localhost:8000**

That's it — no other configuration is required to get started.

---

## First Run

1. **Create a password.** The first page load asks you to create one account password. It protects the whole app and cannot be skipped.
2. **Setup wizard.** A short, skippable wizard offers three one-time steps:
   - Create an encryption key (protects your API keys and tokens at rest)
   - Connect your AI model (base URL + model name — or skip and do it later in Settings)
     > **Tip:** if your AI runs on the same machine (e.g. Ollama), use `http://host.docker.internal:11434/v1` as the base URL so the container can reach it.
   - Add your first device

---

## Using HomeStew

### Add a device

Click **+ New Device** and fill in a label, brand and model (e.g. _Living Room TV / Samsung / QN90A_). You can fetch manuals in PDF format from the internet, or upload manuals from your hard drive. You can also or add custom attributes not present in the default schema (e.g. colour, room, etc.).

If you have LLM integration configured you can also simply ask the LLM to add/modify devices for you.

### Get manuals

On any device, click **Fetch Manuals**: HomeStew searches several web engines for PDF manuals, shows you the candidates, and stores the ones you pick. Only real PDFs are kept. Prefer your own copy? Use **Upload Manual**.

### Search

Open the **Search** tab and type keywords like `HDMI port location` or `filter replacement`. Results show highlighted snippets with page numbers — click one to open the exact page in the PDF. Optionally narrow the search to a single device.

### AI Chat

Open the **AI Chat** tab and just ask:

- _"How do I clean my coffee machine?"_
- _"What's the power consumption of this TV?"_

The LLM will search the uploaded manuals and provide grounded answers.

Answers come with links to the manual pages they came from. You can also ask the AI to add devices or set reminders ("remind me to descale it every month").

You can attach photos if your model supports vision (enable **Model supports Vision** under Settings → AI). This makes it really easy to add/modify devices, or troubleshoot error messages.

### Calendar & notifications

Create one-time or recurring maintenance events, optionally tied to a device. Overdue and upcoming items appear in the sidebar; enable notifications in Settings → Notifications to have due reminders pushed to a webhook of your choice.

### Backup & restore

Under **Settings → Backups**, **Download Backup** exports everything (optionally including PDFs) as a `.zip`. **Restore** can _merge_ (add only what's missing) or _replace_ everything with the archive contents.

---

## Updating to a New Version

Your data lives in the `homestew_data` Docker volume and is not affected by updates.

**From Docker Hub:** pull the new image and recreate the container:

```bash
docker stop homestew
docker rm homestew
docker pull officedrone/homestew:latest
docker run -d --name homestew \
  -p 8000:8000 \
  -v homestew_data:/data \
  --restart unless-stopped \
  officedrone/homestew:latest
```

**From GitHub:**

```bash
cd HomeStew
git pull
docker-compose up -d --build
```

---

## Your Data & Privacy

- All devices, manuals and settings are stored in the `homestew_data` Docker volume on your machine — nothing is sent to any service except the manual search/downloads and (if configured) your chosen AI provider.
- Sensitive values (API key, webhook token, password) are never returned to the browser; API keys and tokens are encrypted at rest once you create the encryption key in the wizard.

---

## Password Reset & Login Lockout

HomeStew is protected by a single account password (for now), created on first run. Tick **"Remember me"** at
login for a 30-day session; otherwise sessions end when you close the browser.

**Changing your password:** go to **Settings → Advanced**, enter the current
password and the new one. This immediately signs out every _other_ browser;
your current one stays signed in.

**Brute-force lockout:** failed logins are throttled per client IP. After a
number of consecutive failures (default **8**), further attempts from that
address are refused for a cool-down period (default **5 minutes**), then the
streak resets. Both limits are tunable under **Settings → Advanced**. The
lockout is in-memory, so restarting the container also clears it.

> **Note:** Even though there is a lockout mechanism, **_DO NOT_** expose HomeStew directly to the internet. Configure and use VPN to reach it instead.

### Forgotten password? Reset it from the container

There is no email reset — you prove ownership by having access to the machine
running HomeStew:

```bash
docker exec -it homestew python -m homestew.auth_cli reset-password
```

You'll be prompted for the new password twice (echo hidden). All existing
sessions are invalidated, so sign in again in the browser. Non-interactive /
CI variant:

```bash
docker exec -e HOMESTEW_PASSWORD='new-secret' homestew python -m homestew.auth_cli reset-password
```

Other subcommands: `status` (is a password set?) and `clear-password` (remove
it and re-arm the create-account screen). If even this fails, confirm the
container name (`docker ps`) and that the data volume is mounted — the
password lives in `/data/settings.json`.

---

## Quick Troubleshooting

| Problem              | Fix                                                                                                                                                  |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Page won't load      | Check the container is running: `docker ps`, then view logs with `docker logs homestew` (or `docker compose logs homestew` if installed from GitHub) |
| AI doesn't answer    | Verify your LLM server is running and the base URL/model are correct in Settings → AI                                                                |
| No manuals found     | Try different brand/model spellings; check the container has internet access                                                                         |
| Forgot your password | Reset it: `docker exec -it homestew python -m homestew.auth_cli reset-password`                                                                      |

For deeper troubleshooting, configuration (environment variables), secrets management, and API details, see the [Technical Guide](GUIDE.md).

---

## License

Copyright 2026 HomeStew contributors.

Licensed under the Apache License, Version 2.0 (the "License"); you may not
use this file except in compliance with the License. You may obtain a copy of
the License at [http://www.apache.org/licenses/LICENSE-2.0](http://www.apache.org/licenses/LICENSE-2.0).

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.

See [LICENSE](LICENSE) for the full text and [NOTICE](NOTICE) for third-party
attribution notices. If you redistribute this project or a modified version of
it, please retain the copyright notice and give credit to the HomeStew project.

## Contributing

This is a personal homelab project, but PRs are welcome.
