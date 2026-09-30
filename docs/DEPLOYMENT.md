# Running and deploying ExportScout

- [Run locally](#run-locally)
- [Deploy to Streamlit Community Cloud](#deploy-to-streamlit-community-cloud)
- [Why Streamlit Community Cloud and not Vercel](#why-streamlit-community-cloud-and-not-vercel)
- [Live mode and keys](#live-mode-and-keys)
- [Credit safety](#credit-safety)
- [Troubleshooting](#troubleshooting)

## Run locally

You need Python 3.11+. Demo Mode needs no API keys and spends no credits.

**Windows (PowerShell)**

```powershell
git clone https://github.com/bhuvanesh15/ExportScout.git
cd ExportScout
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m streamlit run app.py
```

**macOS / Linux**

```bash
git clone https://github.com/bhuvanesh15/ExportScout.git
cd ExportScout
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

The app opens at <http://localhost:8501>. Using it:
1. In the sidebar, leave **Demo Mode** and **Compare other markets** on.
2. Pick a demo product.
3. Press **Scout the UK market**.
4. Go through the **Brief**, **Markets**, **Buyers**, **Pitch** and **Evidence** tabs.

Run the tests (offline, zero credits):

```bash
python -m pytest -q
```

## Deploy to Streamlit Community Cloud

The public deployment runs **Demo Mode only**. No API keys go on the server, so visitors can't spend SerpApi credits or incur LLM costs. With no key present, the app switches Demo Mode on automatically and locks it.

1. Go to **<https://share.streamlit.io>** and sign in with the GitHub account that owns the repo.
2. Click **Create app** → **Deploy a public app from GitHub**.
3. Fill in the app details:
   - **Repository:** `bhuvanesh15/ExportScout`
   - **Branch:** `main`
   - **Main file path:** `app.py`
   - **App URL:** choose a subdomain, e.g. `exportscout` → `https://exportscout.streamlit.app`
4. Open **Advanced settings**:
   - **Python version:** 3.12
   - **Secrets:** leave empty
5. Click **Deploy**. The first build installs `requirements.txt` and takes a few minutes.
6. Open the app URL in a **private/incognito window**. The hackathon requires links to open without asking for access. Then:
   - pick each demo product
   - run it
   - check all five tabs

**Notes**
- Community Cloud apps **sleep after a period without traffic**. Open the URL shortly before judging or recording so it's awake.
- Every push to `main` redeploys the app automatically.
- The app writes its local cache to `.cache/` inside the container. This storage is temporary and that's fine: Demo Mode reads the committed `demo_cache/` files.

## Why Streamlit Community Cloud and not Vercel

| | Streamlit Community Cloud | Vercel |
|---|---|---|
| Runtime model | A long-running `streamlit run` server per app | Serverless Functions that start per request and stop |
| What Streamlit needs | A persistent WebSocket per browser session: supported natively | A WebSocket lasts only up to the function's maximum duration, and later connections may reach a different instance ([Vercel KB](https://vercel.com/kb/guide/do-vercel-serverless-functions-support-websocket-connections)) |
| Effort | Connect the repo and deploy; no config files | A rewrite of the UI (e.g. Next.js + a Python API) |
| Cost | Free for public repos | Free tier, but it doesn't fit this app |

Vercel is excellent for static sites and Next.js front ends. ExportScout's UI is Streamlit, so Streamlit Community Cloud is the right host.

## Live mode and keys

Live mode runs **locally** with your own keys.

| Variable | Purpose |
|---|---|
| `SERPAPI_API_KEY` (or `SERPAPI_KEY`) | SerpApi searches |
| `ANTHROPIC_API_KEY` | The LLM steps: keywords, the German search phrase for Amazon.de, review themes, buyer tags, brief and pitch text. Without it, the deterministic fallbacks are used |

You can set them in either place:
- **A `.env` file:** copy `.env.example` to `.env`. It is gitignored.
- **Environment variables**, e.g. set at the system level on Windows.

Then turn **Demo Mode** off in the sidebar, upload a product photo, and enter your cost and MOQ.

**Recording new Demo Mode data**

```bash
python scripts/record_demo.py              # dry run: what would be recorded
python scripts/record_demo.py --live --only lantern
python scripts/record_demo.py --verify     # replay with keys hidden: must show 0 credits, 0 misses
```

## Credit safety

The free SerpApi plan has 250 searches a month. ExportScout protects them in six ways:

| Safeguard | Where |
|---|---|
| Demo Mode is on by default, even when a key is present | `app.py` sidebar |
| Live mode reuses any recorded response before spending a credit (`prefer_fixtures`) | `exportscout/serp/client.py`, `make_clients` in `exportscout/agent/orchestrator.py` |
| A local SQLite cache with a time-to-live per engine; identical searches are free | `exportscout/serp/client.py` (`TTL_HOURS`) |
| A per-run budget (default 45, adjustable 15–60); the agent stops searching and builds the brief from what it has | the sidebar and the orchestrator |
| **Compare other markets** can be switched off. It adds about 9 credits to a live run (4 Amazon searches, 1 Trends request, 4 exchange rates, which are cached and shared), and enrichment keeps them back. It is free in Demo Mode | the sidebar; `MARKETS_RESERVE` in the orchestrator |
| Recording refuses to start if it could leave fewer than 20 searches | `scripts/record_demo.py` (`RESERVE`) |

The Account API used for the "searches left" meter is free.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "This input isn't in the Demo Mode recordings" | Use a demo product, or run locally in live mode |
| The Demo Mode toggle is greyed out | No SerpApi key was found, so Demo Mode is locked on. This is expected on the public deployment |
| `streamlit` not found | Run it through the venv: `.venv\Scripts\python -m streamlit run app.py` |
| Live run shows "Credit budget reached" | Raise the budget in the sidebar, untick **Compare other markets**, or re-run: cached searches are free |
| The Markets tab says "No market comparison in this brief" | **Compare other markets** was off for that run. Tick it in the sidebar and scout again |
| PowerShell blocks `activate` | Skip activation and call `.venv\Scripts\python` directly, as shown above |
