# JARVIS

> *"Not a chatbot. A digital chief of staff."*

A real-world AI assistant that remembers you, plans with you, and acts for you.
Modeled on FRIDAY from the MCU. Built to the JARVIS Master Spec (April 2026).

---

## What it does

- **Remembers you** — every conversation extracts facts, events, and patterns into long-term memory. Retrieved automatically before every response.
- **Executes tasks** — creates tasks, tracks them, marks them done.
- **Searches the web** — Tavily or Serper integration for real-time information.
- **Three modes** — Work (execution-focused), Personal (coach-style), Strategic (co-founder lens).
- **Streams responses** — token-by-token, no waiting for the full answer.
- **Voice-ready** — MCP tool server + LiveKit scaffold ready for Phase 2.

---

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env — set at minimum:
#   ANTHROPIC_API_KEY (or OPENAI_API_KEY)
#   JARVIS_USER_NAME = your name
#   JARVIS_TIMEZONE = your timezone (e.g. Asia/Kolkata)
```

### 2. Run with Docker Compose

```bash
docker compose up --build
```

- **Frontend** → http://localhost:3000
- **API** → http://localhost:8000
- **API docs** → http://localhost:8000/docs
- **MCP server** → http://localhost:8001

### 3. Run locally (development)

**Backend:**
```bash
cd backend
python -m venv .venv
.venv/Scripts/activate       # Windows
# source .venv/bin/activate  # Mac/Linux
pip install -r requirements.txt

# Requires Postgres running (or use docker compose up postgres redis -d)
uvicorn main:app --reload
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

---

## Project Structure

```
jarvis/
├── docker-compose.yml
├── .env.example
├── scripts/
│   └── db_init.sql              # Postgres schema + seed
│
├── backend/                     # FastAPI Python backend
│   ├── core/                    # Config, DB, Redis
│   ├── models/                  # SQLAlchemy ORM models
│   ├── providers/               # LLM abstraction (Anthropic / OpenAI / Ollama)
│   ├── memory/                  # MemoryManager + ChromaDB retriever + extractor
│   ├── tools/                   # Memory, task, search, datetime tools
│   ├── agents/                  # JarvisAgent (LangGraph ReAct)
│   ├── prompts/                 # System prompt builder + mode configs
│   └── routers/                 # FastAPI routes: /chat, /memory, /tasks
│
├── mcp_server/                  # FastMCP tool server (for voice agent)
│   └── main.py
│
├── voice_agent/                 # LiveKit voice pipeline (Phase 2)
│   └── main.py
│
└── frontend/                    # Next.js 15 chat UI
    ├── app/
    ├── components/
    │   ├── ChatInterface.tsx
    │   ├── MessageBubble.tsx
    │   └── ModeSelector.tsx
    └── lib/api.ts
```

---

## Architecture

```
[Browser / Voice]
       │ SSE streaming / WebSocket
       ▼
[FastAPI Backend :8000]
  ├─ Auth (X-API-Key)
  ├─ Chat router (SSE streaming)
  ├─ Memory router
  └─ Tasks router
       │
       ▼
[JarvisAgent — LangGraph ReAct]
  1. Retrieve memories (ChromaDB semantic search)
  2. Build system prompt (mode + user profile + memories)
  3. Run tool-calling loop
  4. Stream response tokens
  5. After conversation: extract + store new memories
       │
  ┌────┼──────────────────────────┐
  ▼    ▼                          ▼
[LLM Layer]    [Memory/Data]    [Tools]
 Anthropic      ChromaDB         save_memory
 OpenAI         Postgres         search_memory
 Ollama         Redis cache      create_task / list_tasks
                                 search_web
                                 get_datetime
```

---

## API

All endpoints require `X-API-Key: local-dev-key` header (or set `JARVIS_API_KEY`).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/chat/start` | Start a conversation |
| `POST` | `/chat/{id}/message` | Send message → SSE stream |
| `POST` | `/chat/{id}/end` | Close + extract memories |
| `GET`  | `/chat/history` | List past conversations |
| `POST` | `/memory/search` | Semantic memory search |
| `POST` | `/memory/store` | Store a memory |
| `GET`  | `/memory/list` | List memories |
| `GET`  | `/memory/profile` | Get user profile facts |
| `PUT`  | `/memory/profile` | Update profile fact |
| `GET`  | `/tasks/` | List tasks |
| `POST` | `/tasks/` | Create task |
| `PATCH`| `/tasks/{id}` | Update task |

---

## Configuration

Key variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `openai` \| `ollama` |
| `ANTHROPIC_API_KEY` | — | Required if using Anthropic |
| `OPENAI_API_KEY` | — | Required if using OpenAI |
| `TAVILY_API_KEY` | — | Web search (get free at tavily.com) |
| `JARVIS_USER_NAME` | `Boss` | Your name |
| `JARVIS_TIMEZONE` | `Asia/Kolkata` | Your timezone |
| `JARVIS_API_KEY` | `local-dev-key` | Auth key for all API calls |

---

## Modes

| Mode | Character | Use for |
|------|-----------|---------|
| **Work** | Direct, efficient, time-aware | Daily execution, tasks, deadlines |
| **Personal** | Warm, coach-style | Wellbeing, habits, life balance |
| **Strategic** | Co-founder, probing | Big decisions, planning, analysis |

Switch modes by saying *"Switch to strategic mode"* or using the UI selector.

---

## Roadmap

- [x] **Phase 0** — 48hr MVP: voice agent skeleton + personality
- [x] **Phase 1** — Text + Memory: full backend, streaming, task system, chat UI
- [ ] **Phase 2** — Voice + Automation: LiveKit voice, Google Calendar, Gmail drafting
- [ ] **Phase 3** — Multi-Agent: Research, Planning, Execution agents
- [ ] **Phase 4** — Autonomous: goal-based planning, proactive nudges, OS control

---

## Personality

Jarvis is not a chatbot with a cute name. It:

- Leads with the answer. No preamble.
- Never says *"Certainly!"*, *"Great question!"*, or *"As an AI..."*
- Has dry wit that surfaces occasionally, not constantly.
- Offers the next best action after every completed task.
- Asks one clarifying question, not five.
- Keeps voice responses to ≤ 3 sentences.

---

*Version 1.0 · Built to spec · April 2026*
