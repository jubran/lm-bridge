# lm-bridge 🌉

> Run Claude Code against any local LLM via LM Studio — with **real** tool/function calling support.

## Why?

Claude Code sends requests in Anthropic's API format with structured tool calls.  
Local LLMs speak OpenAI format. This bridge translates between them — properly.

```
Claude Code  →  lm-bridge  →  LM Studio (qwen / mistral / any model)
```

## Features

- ✅ Full Anthropic ↔ OpenAI message translation
- ✅ Proper **function calling** (not just text prompting)
- ✅ Streaming support
- ✅ Works with any OpenAI-compatible server (LM Studio, Ollama, vLLM…)
- ✅ Zero cloud calls — 100% local

## Quick Start

### 1. Install

```bash
pip install fastapi uvicorn httpx pydantic-settings
# or with uv:
uv sync
```

### 2. Configure (optional)

Copy `.env.example` to `.env` and edit:

```env
LM_BASE_URL=http://localhost:1234
LM_MODEL=qwen3.5-9b
```

### 3. Start LM Studio

- Load your model (e.g. `Qwen3.5 9B`)
- Enable **Local Server** → Start Server

### 4. Run lm-bridge

```bash
uvicorn server:app --host 0.0.0.0 --port 8082
# or:
uv run uvicorn server:app --host 0.0.0.0 --port 8082
```

### 5. Run Claude Code

```powershell
# Windows PowerShell
$env:ANTHROPIC_BASE_URL = "http://localhost:8082"
$env:ANTHROPIC_API_KEY  = "local"
claude
```

```bash
# macOS / Linux
ANTHROPIC_BASE_URL=http://localhost:8082 ANTHROPIC_API_KEY=local claude
```

## Health Check

```bash
curl http://localhost:8082/health
```

## Supported Servers

| Server     | Default URL             | Notes                    |
|------------|-------------------------|--------------------------|
| LM Studio  | `http://localhost:1234` | Recommended, GPU support |
| Ollama     | `http://localhost:11434`| Change in `.env`         |
| vLLM       | `http://localhost:8000` | Change in `.env`         |

## Recommended Models

Models with strong tool/function calling support:

- `Qwen3.5 9B` ⭐ (recommended)
- `Qwen2.5-Coder 7B`
- `Mistral Small 3.2`

## License

MIT
