"""
lm-bridge — Anthropic API ↔ LM Studio Proxy
Bridges Claude Code to any local LLM with proper tool/function calling support.
"""

import uuid
import json
import logging
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from config import settings

# ── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lm-bridge")

app = FastAPI(title="lm-bridge", version="1.0.0")


# ══════════════════════════════════════════════════════════
#  TRANSLATION LAYER
# ══════════════════════════════════════════════════════════

def anthropic_tools_to_openai(tools: list) -> list:
    """Anthropic tools → OpenAI function calling format"""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name":        t.get("name", ""),
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


def anthropic_messages_to_openai(messages: list) -> list:
    """Convert Anthropic messages array to OpenAI format"""
    result = []
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")

        # ── String content ──
        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue

        # ── Content blocks ──
        text_parts   = []
        tool_calls   = []
        tool_results = []

        for block in content:
            t = block.get("type", "")

            if t == "text":
                text_parts.append(block.get("text", ""))

            elif t == "tool_use":
                # Assistant is calling a tool
                tool_calls.append({
                    "id":   block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name":      block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })

            elif t == "tool_result":
                # Result coming back from tool execution
                rc = block.get("content", "")
                if isinstance(rc, list):
                    rc = "\n".join(b.get("text", "") for b in rc)
                tool_results.append({
                    "role":         "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content":      rc,
                })

        # Assemble assistant message
        if role == "assistant":
            assistant_msg: dict = {"role": "assistant"}
            if text_parts:
                assistant_msg["content"] = "\n".join(text_parts)
            else:
                assistant_msg["content"] = None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            result.append(assistant_msg)

        # Tool results become separate messages
        elif tool_results:
            result.extend(tool_results)

        elif text_parts:
            result.append({"role": role, "content": "\n".join(text_parts)})

    return result


def build_openai_request(body: dict) -> dict:
    """Build the full OpenAI-compatible request for LM Studio"""
    messages = []

    # System prompt
    system = body.get("system", "")
    if isinstance(system, list):
        system = "\n".join(b.get("text", "") for b in system if b.get("type") == "text")
    if system:
        messages.append({"role": "system", "content": system})

    messages.extend(anthropic_messages_to_openai(body.get("messages", [])))

    req: dict = {
        "model":       settings.LM_MODEL,
        "messages":    messages,
        "stream":      body.get("stream", False),
        "temperature": body.get("temperature", settings.DEFAULT_TEMPERATURE),
        "max_tokens":  body.get("max_tokens", settings.DEFAULT_MAX_TOKENS),
    }

    tools = body.get("tools", [])
    if tools:
        req["tools"]       = anthropic_tools_to_openai(tools)
        req["tool_choice"] = "auto"

    return req


# ──────────────────────────────────────────────────────────

def openai_tool_calls_to_anthropic(tool_calls: list) -> list:
    """Convert OpenAI tool_calls → Anthropic tool_use content blocks"""
    blocks = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            arguments = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            arguments = {"raw": fn.get("arguments", "")}

        blocks.append({
            "type":  "tool_use",
            "id":    tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name":  fn.get("name", ""),
            "input": arguments,
        })
    return blocks


def openai_response_to_anthropic(oai: dict, original_model: str) -> dict:
    """Convert full OpenAI response → Anthropic Messages API response"""
    choice  = oai.get("choices", [{}])[0]
    message = choice.get("message", {})
    usage   = oai.get("usage", {})

    content_blocks = []
    stop_reason    = "end_turn"

    # Text content
    text = message.get("content") or ""
    if text:
        content_blocks.append({"type": "text", "text": text})

    # Tool calls
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        content_blocks.extend(openai_tool_calls_to_anthropic(tool_calls))
        stop_reason = "tool_use"

    return {
        "id":            f"msg_{uuid.uuid4().hex[:24]}",
        "type":          "message",
        "role":          "assistant",
        "content":       content_blocks,
        "model":         original_model,
        "stop_reason":   stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens":  usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ══════════════════════════════════════════════════════════
#  STREAMING
# ══════════════════════════════════════════════════════════

async def stream_anthropic(oai_stream: httpx.Response, model: str):
    """
    Collect the full OpenAI SSE stream, then emit proper Anthropic SSE events.
    We buffer because tool_calls only appear in the final chunk.
    """
    msg_id     = f"msg_{uuid.uuid4().hex[:24]}"
    full_text  = ""
    tool_calls: dict[int, dict] = {}  # index → accumulated tool call

    async for line in oai_stream.aiter_lines():
        if not line or not line.startswith("data: "):
            continue
        raw = line[6:]
        if raw == "[DONE]":
            break
        try:
            chunk  = json.loads(raw)
            choice = chunk.get("choices", [{}])[0]
            delta  = choice.get("delta", {})

            # Text delta
            full_text += delta.get("content") or ""

            # Tool call deltas (streamed in pieces)
            for tc_delta in delta.get("tool_calls") or []:
                idx = tc_delta.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        "id":       tc_delta.get("id", ""),
                        "type":     "function",
                        "function": {"name": "", "arguments": ""},
                    }
                fn = tc_delta.get("function", {})
                tool_calls[idx]["function"]["name"]      += fn.get("name", "")
                tool_calls[idx]["function"]["arguments"] += fn.get("arguments", "")
        except json.JSONDecodeError:
            continue

    # ── Build final content blocks ──
    content_blocks = []
    stop_reason    = "end_turn"

    if full_text:
        content_blocks.append({"type": "text", "text": full_text})

    if tool_calls:
        tc_list = [tool_calls[i] for i in sorted(tool_calls)]
        content_blocks.extend(openai_tool_calls_to_anthropic(tc_list))
        stop_reason = "tool_use"

    # ── Emit Anthropic SSE ──
    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    for i, block in enumerate(content_blocks):
        yield sse("content_block_start", {
            "type": "content_block_start", "index": i, "content_block": block,
        })
        if block["type"] == "text":
            chunk_size = 30
            text = block["text"]
            for j in range(0, len(text), chunk_size):
                yield sse("content_block_delta", {
                    "type": "content_block_delta", "index": i,
                    "delta": {"type": "text_delta", "text": text[j:j+chunk_size]},
                })
        yield sse("content_block_stop", {"type": "content_block_stop", "index": i})

    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": 0},
    })
    yield sse("message_stop", {"type": "message_stop"})


# ══════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════

@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    model     = body.get("model", settings.LM_MODEL)
    is_stream = body.get("stream", False)

    oai_req = build_openai_request(body)

    log.info(f"📨 model={model} stream={is_stream} tools={len(body.get('tools',[]))}")

    async with httpx.AsyncClient(timeout=300) as client:
        try:
            if is_stream:
                async with client.stream(
                    "POST",
                    f"{settings.LM_BASE_URL}/v1/chat/completions",
                    json=oai_req,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    resp.raise_for_status()
                    return StreamingResponse(
                        stream_anthropic(resp, model),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )
            else:
                resp = await client.post(
                    f"{settings.LM_BASE_URL}/v1/chat/completions",
                    json=oai_req,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                result = openai_response_to_anthropic(resp.json(), model)
                log.info(f"✅ stop_reason={result['stop_reason']} blocks={len(result['content'])}")
                return JSONResponse(result)

        except httpx.ConnectError:
            log.error(f"❌ LM Studio غير متاح على {settings.LM_BASE_URL}")
            raise HTTPException(503, "LM Studio is not running")
        except Exception as e:
            log.error(f"❌ {e}")
            raise HTTPException(500, str(e))


@app.get("/v1/models")
async def list_models():
    return {"data": [{"id": settings.LM_MODEL, "object": "model", "owned_by": "local"}]}


@app.get("/health")
async def health():
    return {"status": "ok", "model": settings.LM_MODEL, "lm_url": settings.LM_BASE_URL}
