"""mtgacoach.com proxy server — routes AI requests and manages subscriptions."""

from collections import OrderedDict
import json
import logging
import logging.handlers
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db
from providers import ProviderRouter

# Logging: console + file
LOG_DIR = Path("./data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "proxy.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        ),
    ],
)
logger = logging.getLogger(__name__)

# Load config
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)

# Resolve env vars in admin password
admin_password = config.get("admin", {}).get("password", "")
if admin_password.startswith("${") and admin_password.endswith("}"):
    admin_password = os.environ.get(admin_password[2:-1], "changeme")

# Initialize
db.init_db()
router = ProviderRouter()
router.load_from_config(config.get("providers", []))

app = FastAPI(title="mtgacoach.com API", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log every request with timing and subscriber info."""
    start = time.time()
    # Extract license key for identifying subscriber (truncated for privacy)
    key = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        key = auth[7:19] + "..."  # First 12 chars only

    response = await call_next(request)
    elapsed_ms = (time.time() - start) * 1000

    # Skip noisy static/health requests from detailed logging
    path = request.url.path
    if path in ("/health", "/favicon.ico") or path.startswith("/static"):
        return response

    logger.info(
        f"{request.method} {path} → {response.status_code} "
        f"({elapsed_ms:.0f}ms) "
        f"key={key or 'none'} "
        f"ip={request.client.host if request.client else 'unknown'}"
    )
    return response


# Shared httpx client
http_client: Optional[httpx.AsyncClient] = None
_RESPONSE_STORE_MAX = 512
_response_store: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient(timeout=120.0)
    logger.info(f"Proxy server started with {len(router.providers)} providers")


@app.on_event("shutdown")
async def shutdown():
    if http_client:
        await http_client.aclose()


# --- Auth helpers ---

def _extract_license_key(request: Request) -> str:
    """Extract license key from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


def _extract_client_metadata(request: Request) -> dict[str, str]:
    """Extract optional client telemetry headers from a request."""
    install_id = (request.headers.get("X-MTGACoach-Install-ID") or "").strip()[:128]
    version = (request.headers.get("X-MTGACoach-Version") or "").strip()[:64]
    frontend = (request.headers.get("X-MTGACoach-Frontend") or "").strip().lower()[:32]
    user_agent = (request.headers.get("User-Agent") or "").strip()[:256]
    if frontend not in {"winui", "pyside", "tui", "standalone", "unknown", ""}:
        frontend = "unknown"
    return {
        "install_id": install_id,
        "client_version": version,
        "frontend": frontend,
        "user_agent": user_agent,
    }


def _record_client_telemetry(request: Request, license_key: str) -> None:
    """Persist client telemetry for a validated subscriber request."""
    metadata = _extract_client_metadata(request)
    install_id = metadata.get("install_id", "")
    if not install_id:
        return
    db.upsert_client_install(
        license_key=license_key,
        install_id=install_id,
        client_version=metadata.get("client_version", ""),
        frontend=metadata.get("frontend", ""),
        user_agent=metadata.get("user_agent", ""),
        last_ip=request.client.host if request.client else "",
    )


def _require_license(request: Request) -> dict:
    """Validate license key and return subscriber info."""
    key = _extract_license_key(request)
    if not key:
        raise HTTPException(401, "Missing license key")

    sub = db.check_license(key)
    if not sub:
        raise HTTPException(401, "Invalid license key")

    if sub["status"] not in ("active", "trial"):
        raise HTTPException(402, f"Subscription {sub['status']}. Renew at mtgacoach.com/subscribe")

    _record_client_telemetry(request, sub["license_key"])
    return sub


def _require_admin(request: Request):
    """Check admin credentials via Basic auth or X-Admin-Key header."""
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key and admin_key == admin_password:
        return True

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        import base64
        decoded = base64.b64decode(auth[6:]).decode()
        username, _, password = decoded.partition(":")
        admin_user = config.get("admin", {}).get("username", "admin")
        if username == admin_user and password == admin_password:
            return True

    raise HTTPException(403, "Admin access required")


# =========================================================================
#  OpenAI-compatible API endpoints (used by mtgacoach client)
# =========================================================================

@app.get("/v1/models")
async def list_models(request: Request):
    """List available models. Requires valid license key."""
    key = _extract_license_key(request)
    if key:
        sub = db.check_license(key)
        if not sub or sub["status"] not in ("active", "trial"):
            raise HTTPException(401, "Invalid or expired license key")
        _record_client_telemetry(request, sub["license_key"])

    models = router.get_all_models()
    return {"object": "list", "data": models}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, sub: dict = Depends(_require_license)):
    """Proxy chat completions to the best available provider."""
    body = await request.json()
    model = body.get("model")
    stream = body.get("stream", False)

    provider = router.select_provider(model)
    if not provider:
        raise HTTPException(503, "No AI provider available. Try again later.")

    logger.info(f"Routing {model or 'default'} to {provider.name} "
                f"(subscriber={sub['email'] or sub['license_key'][:12]}...)")

    try:
        if stream:
            return await _handle_streaming(provider, body, sub)
        else:
            return await _handle_non_streaming(provider, body, sub)
    except httpx.HTTPStatusError as e:
        provider.mark_failure()
        detail = ""
        try:
            detail = f" — {e.response.text[:200]}"
        except Exception:
            pass
        logger.error(f"Provider {provider.name} returned {e.response.status_code}{detail}")

        # Try next provider
        fallback = router.select_provider(model)
        if fallback and fallback.name != provider.name:
            logger.info(f"Falling back to {fallback.name}")
            try:
                if stream:
                    return await _handle_streaming(fallback, body, sub)
                else:
                    return await _handle_non_streaming(fallback, body, sub)
            except Exception as e2:
                fallback.mark_failure()
                raise HTTPException(502, f"All providers failed: {e2}")

        raise HTTPException(502, f"Provider error: {e.response.status_code}")
    except Exception as e:
        provider.mark_failure()
        logger.error(f"Provider {provider.name} error: {e}")
        raise HTTPException(502, f"Provider error: {e}")


async def _handle_non_streaming(provider, body: dict, sub: dict) -> JSONResponse:
    """Handle a non-streaming chat completion request."""
    body["stream"] = False
    response = await provider.forward_chat(body, http_client)
    response.raise_for_status()
    provider.mark_success()

    data = response.json()

    # Log usage
    usage = data.get("usage", {})
    db.log_usage(
        sub["license_key"],
        body.get("model", "unknown"),
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        provider.name,
    )

    return JSONResponse(content=data)


async def _handle_streaming(provider, body: dict, sub: dict) -> StreamingResponse:
    """Handle a streaming chat completion request."""
    body["stream"] = True

    async def event_stream():
        prompt_tokens = 0
        completion_tokens = 0
        try:
            async for line in provider.forward_chat_stream(body, http_client):
                if line.startswith("data: "):
                    yield line + "\n\n"
                    # Try to extract usage from final chunk
                    if line.strip() == "data: [DONE]":
                        continue
                    try:
                        chunk_data = json.loads(line[6:])
                        usage = chunk_data.get("usage", {})
                        if usage:
                            prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                            completion_tokens = usage.get("completion_tokens", completion_tokens)
                    except json.JSONDecodeError:
                        pass
                elif line.strip():
                    yield f"data: {line}\n\n"

            provider.mark_success()

            # Log usage (best effort from stream)
            db.log_usage(
                sub["license_key"],
                body.get("model", "unknown"),
                prompt_tokens,
                completion_tokens,
                provider.name,
            )
        except Exception as e:
            provider.mark_failure()
            logger.error(f"Stream error from {provider.name}: {e}")
            error_data = {"error": {"message": str(e), "type": "proxy_error"}}
            yield f"data: {json.dumps(error_data)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(16).rstrip('=')}"


def _response_skeleton(response: dict[str, Any]) -> dict[str, Any]:
    base = dict(response)
    base["status"] = "in_progress"
    base["completed_at"] = None
    base["output"] = []
    return base


def _store_response(
    response_id: str,
    response: dict[str, Any],
    conversation_messages: list[dict[str, Any]],
) -> None:
    _response_store[response_id] = {
        "response": response,
        "conversation_messages": conversation_messages,
        "stored_at": time.time(),
    }
    _response_store.move_to_end(response_id)
    while len(_response_store) > _RESPONSE_STORE_MAX:
        _response_store.popitem(last=False)


def _get_stored_response(response_id: str) -> Optional[dict[str, Any]]:
    item = _response_store.get(response_id)
    if item:
        _response_store.move_to_end(response_id)
    return item


def _responses_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise HTTPException(400, f"Unsupported content payload: {type(content).__name__}")

    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(400, "Unsupported content part in response input.")
        part_type = part.get("type")
        if part_type in {"input_text", "output_text"}:
            parts.append(str(part.get("text", "")))
        elif part_type == "refusal":
            parts.append(str(part.get("refusal", "")))
        else:
            raise HTTPException(400, f"Unsupported content part type: {part_type}")
    return "".join(parts)


def _tool_output_to_text(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    return json.dumps(output, separators=(",", ":"), ensure_ascii=False)


def _pseudo_tool_name(tool_type: str) -> str:
    if tool_type == "function":
        raise ValueError("Function tools must use their declared name.")
    return f"__mtgacoach_{tool_type}"


def _chat_tool_calls_for_response_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    item_type = str(item.get("type", ""))
    if item_type == "function_call":
        call_id = str(item.get("call_id") or item.get("id") or _new_id("call"))
        return [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": str(item.get("name", "")),
                "arguments": str(item.get("arguments", "")),
            },
        }]
    if item_type == "local_shell_call":
        return [{
            "id": str(item.get("id") or item.get("call_id") or _new_id("tool")),
            "type": "function",
            "function": {
                "name": _pseudo_tool_name("local_shell"),
                "arguments": json.dumps(item.get("action", {}), separators=(",", ":"), ensure_ascii=False),
            },
        }]
    if item_type == "shell_call":
        return [{
            "id": str(item.get("call_id") or item.get("id") or _new_id("tool")),
            "type": "function",
            "function": {
                "name": _pseudo_tool_name("shell"),
                "arguments": json.dumps(item.get("action", {}), separators=(",", ":"), ensure_ascii=False),
            },
        }]
    if item_type == "apply_patch_call":
        return [{
            "id": str(item.get("call_id") or item.get("id") or _new_id("tool")),
            "type": "function",
            "function": {
                "name": _pseudo_tool_name("apply_patch"),
                "arguments": json.dumps(item.get("operation", {}), separators=(",", ":"), ensure_ascii=False),
            },
        }]
    raise HTTPException(400, f"Unsupported response input item type: {item_type}")


def _responses_input_item_to_chat_messages(item: Any) -> list[dict[str, Any]]:
    if isinstance(item, str):
        return [{"role": "user", "content": item}]
    if not isinstance(item, dict):
        raise HTTPException(400, f"Unsupported response input item: {type(item).__name__}")

    role = item.get("role")
    item_type = item.get("type")
    if role in {"user", "system", "developer"} and (item_type in {None, "message"}):
        return [{
            "role": str(role),
            "content": _responses_content_to_text(item.get("content", "")),
        }]
    if role == "assistant" and item_type == "message":
        text = _responses_content_to_text(item.get("content", []))
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if item.get("phase"):
            message["name"] = str(item["phase"])
        return [message]

    if item_type in {"function_call", "local_shell_call", "shell_call", "apply_patch_call"}:
        return [{
            "role": "assistant",
            "content": "",
            "tool_calls": _chat_tool_calls_for_response_item(item),
        }]
    if item_type == "function_call_output":
        return [{
            "role": "tool",
            "tool_call_id": str(item.get("call_id", "")),
            "content": _tool_output_to_text(item.get("output", "")),
        }]
    if item_type == "local_shell_call_output":
        return [{
            "role": "tool",
            "tool_call_id": str(item.get("id", "")),
            "content": _tool_output_to_text(item.get("output", "")),
        }]
    if item_type in {"shell_call_output", "apply_patch_call_output"}:
        return [{
            "role": "tool",
            "tool_call_id": str(item.get("call_id", "")),
            "content": _tool_output_to_text(item.get("output", "")),
        }]
    if item_type == "item_reference":
        # Best-effort no-op. previous_response_id rehydrates stored context.
        return []

    raise HTTPException(400, f"Unsupported response input item type: {item_type}")


def _responses_input_to_chat_messages(input_value: Any) -> list[dict[str, Any]]:
    if input_value is None:
        return []
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if isinstance(input_value, dict):
        return _responses_input_item_to_chat_messages(input_value)
    if not isinstance(input_value, list):
        raise HTTPException(400, f"Unsupported responses input type: {type(input_value).__name__}")

    messages: list[dict[str, Any]] = []
    for item in input_value:
        messages.extend(_responses_input_item_to_chat_messages(item))
    return messages


def _function_tool_schema(parameters: Any) -> dict[str, Any]:
    if isinstance(parameters, dict):
        return parameters
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _tool_to_chat_tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        raise HTTPException(400, "Unsupported tool definition.")
    tool_type = str(tool.get("type", ""))

    if tool_type == "function":
        return {
            "type": "function",
            "function": {
                "name": str(tool.get("name", "")),
                "description": tool.get("description"),
                "parameters": _function_tool_schema(tool.get("parameters")),
                "strict": tool.get("strict", True),
            },
        }

    if tool_type == "apply_patch":
        return {
            "type": "function",
            "function": {
                "name": _pseudo_tool_name("apply_patch"),
                "description": "Apply a create, update, or delete patch operation to a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["create_file", "delete_file", "update_file"],
                        },
                        "path": {"type": "string"},
                        "diff": {"type": "string"},
                    },
                    "required": ["type", "path"],
                    "additionalProperties": False,
                },
            },
        }

    if tool_type == "local_shell":
        return {
            "type": "function",
            "function": {
                "name": _pseudo_tool_name("local_shell"),
                "description": "Execute a local shell command in the user's workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["exec"]},
                        "command": {"type": "array", "items": {"type": "string"}},
                        "env": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "timeout_ms": {"type": "integer"},
                        "user": {"type": "string"},
                        "working_directory": {"type": "string"},
                    },
                    "required": ["type", "command", "env"],
                    "additionalProperties": False,
                },
            },
        }

    if tool_type == "shell":
        return {
            "type": "function",
            "function": {
                "name": _pseudo_tool_name("shell"),
                "description": "Execute one or more shell commands and capture their output.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "commands": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "max_output_length": {"type": "integer"},
                        "timeout_ms": {"type": "integer"},
                    },
                    "required": ["commands"],
                    "additionalProperties": False,
                },
            },
        }

    raise HTTPException(400, f"Unsupported Responses tool type: {tool_type}")


def _responses_tools_to_chat_tools(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise HTTPException(400, "Responses tools must be an array.")
    return [_tool_to_chat_tool(tool) for tool in tools]


def _parse_tool_arguments(arguments: str, tool_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(502, f"Upstream returned invalid JSON arguments for {tool_name}: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(502, f"Upstream returned non-object arguments for {tool_name}.")
    return parsed


def _responses_tool_choice_to_chat_choice(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return None

    choice_type = str(tool_choice.get("type", ""))
    if choice_type in {"auto", "none", "required"}:
        return choice_type
    if choice_type == "function":
        name = str(tool_choice.get("name") or tool_choice.get("function", {}).get("name", ""))
        if name:
            return {"type": "function", "function": {"name": name}}
    if choice_type in {"apply_patch", "local_shell", "shell"}:
        return {"type": "function", "function": {"name": _pseudo_tool_name(choice_type)}}
    return None


def _responses_text_to_chat_response_format(text_config: Any) -> Any:
    if not isinstance(text_config, dict):
        return None
    fmt = text_config.get("format")
    if not isinstance(fmt, dict):
        return None
    fmt_type = fmt.get("type")
    if fmt_type == "json_schema":
        json_schema = fmt.get("json_schema") or fmt
        if isinstance(json_schema, dict):
            return {"type": "json_schema", "json_schema": json_schema}
    if fmt_type == "json_object":
        return {"type": "json_object"}
    return None


def _chat_message_text_and_tools(message: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    content = message.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
        text = "".join(text_parts)
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    return text, tool_calls


def _response_output_items_to_chat_history(output_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    pending_tool_calls: list[dict[str, Any]] = []

    for item in output_items:
        item_type = item.get("type")
        if item_type == "message":
            text = _responses_content_to_text(item.get("content", []))
            history.append({"role": "assistant", "content": text})
            continue
        if item_type == "function_call":
            pending_tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or _new_id("call")),
                "type": "function",
                "function": {
                    "name": str(item.get("name", "")),
                    "arguments": str(item.get("arguments", "")),
                },
            })
            continue
        if item_type == "local_shell_call":
            pending_tool_calls.append({
                "id": str(item.get("id") or item.get("call_id") or _new_id("tool")),
                "type": "function",
                "function": {
                    "name": _pseudo_tool_name("local_shell"),
                    "arguments": json.dumps(item.get("action", {}), separators=(",", ":"), ensure_ascii=False),
                },
            })
            continue
        if item_type == "shell_call":
            pending_tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or _new_id("tool")),
                "type": "function",
                "function": {
                    "name": _pseudo_tool_name("shell"),
                    "arguments": json.dumps(item.get("action", {}), separators=(",", ":"), ensure_ascii=False),
                },
            })
            continue
        if item_type == "apply_patch_call":
            pending_tool_calls.append({
                "id": str(item.get("call_id") or item.get("id") or _new_id("tool")),
                "type": "function",
                "function": {
                    "name": _pseudo_tool_name("apply_patch"),
                    "arguments": json.dumps(item.get("operation", {}), separators=(",", ":"), ensure_ascii=False),
                },
            })

    if pending_tool_calls:
        history.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
    return history


def _chat_message_to_response_output_items(message: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    content_text, tool_calls = _chat_message_text_and_tools(message)
    output_items: list[dict[str, Any]] = []
    history_messages: list[dict[str, Any]] = []

    if content_text:
        message_item = {
            "id": _new_id("msg"),
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{
                "type": "output_text",
                "text": content_text,
                "annotations": [],
            }],
        }
        output_items.append(message_item)

    pending_tool_calls: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        tool_name = str(function.get("name", ""))
        tool_call_id = str(tool_call.get("id") or _new_id("call"))
        arguments = str(function.get("arguments", ""))

        if tool_name == _pseudo_tool_name("apply_patch"):
            output_items.append({
                "id": _new_id("apply_patch"),
                "call_id": tool_call_id,
                "type": "apply_patch_call",
                "status": "completed",
                "operation": _parse_tool_arguments(arguments, tool_name),
            })
            pending_tool_calls.append({
                "id": tool_call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": arguments},
            })
            continue

        if tool_name == _pseudo_tool_name("local_shell"):
            output_items.append({
                "id": tool_call_id,
                "call_id": _new_id("local_shell_call"),
                "type": "local_shell_call",
                "status": "completed",
                "action": _parse_tool_arguments(arguments, tool_name),
            })
            pending_tool_calls.append({
                "id": tool_call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": arguments},
            })
            continue

        if tool_name == _pseudo_tool_name("shell"):
            output_items.append({
                "id": _new_id("shell_call"),
                "call_id": tool_call_id,
                "type": "shell_call",
                "status": "completed",
                "action": _parse_tool_arguments(arguments, tool_name),
                "environment": None,
            })
            pending_tool_calls.append({
                "id": tool_call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": arguments},
            })
            continue

        output_items.append({
            "id": _new_id("fc"),
            "call_id": tool_call_id,
            "name": tool_name,
            "arguments": arguments,
            "type": "function_call",
            "status": "completed",
        })
        pending_tool_calls.append({
            "id": tool_call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": arguments},
        })

    if pending_tool_calls:
        history_messages.append({
            "role": "assistant",
            "content": content_text if content_text else "",
            "tool_calls": pending_tool_calls,
        })
    elif content_text:
        history_messages.append({"role": "assistant", "content": content_text})

    return output_items, history_messages


def _responses_request_to_chat_body(body: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    previous_messages: list[dict[str, Any]] = []
    previous_response_id = body.get("previous_response_id")
    if previous_response_id:
        stored = _get_stored_response(str(previous_response_id))
        if not stored:
            raise HTTPException(404, f"Unknown previous_response_id: {previous_response_id}")
        previous_messages = list(stored["conversation_messages"])

    current_messages = _responses_input_to_chat_messages(body.get("input"))
    messages: list[dict[str, Any]] = []

    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "developer", "content": str(instructions)})
    messages.extend(previous_messages)
    messages.extend(current_messages)

    if not messages:
        raise HTTPException(400, "Responses requests require input or previous_response_id.")

    chat_body: dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "stream": False,
    }

    for key in ("temperature", "top_p", "parallel_tool_calls", "reasoning"):
        if key in body:
            chat_body[key] = body[key]

    if body.get("max_output_tokens") is not None:
        chat_body["max_completion_tokens"] = body["max_output_tokens"]
    if body.get("tools"):
        chat_body["tools"] = _responses_tools_to_chat_tools(body["tools"])
    tool_choice = _responses_tool_choice_to_chat_choice(body.get("tool_choice"))
    if tool_choice is not None:
        chat_body["tool_choice"] = tool_choice
    response_format = _responses_text_to_chat_response_format(body.get("text"))
    if response_format is not None:
        chat_body["response_format"] = response_format

    return chat_body, previous_messages + current_messages


async def _dispatch_chat_completion(body: dict[str, Any], sub: dict) -> tuple[dict[str, Any], Any]:
    model = body.get("model")
    provider = router.select_provider(model)
    if not provider:
        raise HTTPException(503, "No AI provider available. Try again later.")

    logger.info(
        "Routing %s via Responses shim to %s (subscriber=%s...)",
        model or "default",
        provider.name,
        sub["email"] or sub["license_key"][:12],
    )

    try:
        response = await provider.forward_chat(body, http_client)
        response.raise_for_status()
        provider.mark_success()
        return response.json(), provider
    except httpx.HTTPStatusError as e:
        provider.mark_failure()
        detail = ""
        try:
            detail = f" — {e.response.text[:200]}"
        except Exception:
            pass
        logger.error("Provider %s returned %s%s", provider.name, e.response.status_code, detail)

        fallback = router.select_provider(model)
        if fallback and fallback.name != provider.name:
            logger.info("Falling back to %s for Responses shim", fallback.name)
            try:
                response = await fallback.forward_chat(body, http_client)
                response.raise_for_status()
                fallback.mark_success()
                return response.json(), fallback
            except Exception as fallback_exc:
                fallback.mark_failure()
                raise HTTPException(502, f"All providers failed: {fallback_exc}") from fallback_exc
        raise HTTPException(502, f"Provider error: {e.response.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        provider.mark_failure()
        logger.error("Provider %s error: %s", provider.name, e)
        raise HTTPException(502, f"Provider error: {e}")


def _chat_completion_to_response(
    original_body: dict[str, Any],
    chat_data: dict[str, Any],
    conversation_input: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    choices = chat_data.get("choices") or []
    if not choices:
        raise HTTPException(502, "Upstream chat completion returned no choices.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise HTTPException(502, "Upstream chat completion returned an invalid message payload.")

    output_items, output_history = _chat_message_to_response_output_items(message)
    usage = chat_data.get("usage") or {}
    response_id = _new_id("resp")
    created_at = float(chat_data.get("created") or time.time())

    response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "completed_at": time.time(),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": original_body.get("instructions"),
        "metadata": original_body.get("metadata"),
        "model": chat_data.get("model") or original_body.get("model"),
        "output": output_items,
        "parallel_tool_calls": bool(original_body.get("parallel_tool_calls", True)),
        "temperature": original_body.get("temperature"),
        "tool_choice": original_body.get("tool_choice", "auto"),
        "tools": original_body.get("tools", []),
        "top_p": original_body.get("top_p"),
        "max_output_tokens": original_body.get("max_output_tokens"),
        "previous_response_id": original_body.get("previous_response_id"),
        "text": original_body.get("text"),
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "input_tokens_details": {
                "cached_tokens": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0),
            },
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
            "output_tokens_details": {
                "reasoning_tokens": int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0),
            },
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        },
    }

    conversation_history = list(conversation_input)
    conversation_history.extend(output_history)
    _store_response(response_id, response, conversation_history)
    return response, conversation_history


def _stream_events_for_response(response: dict[str, Any]):
    async def event_stream():
        seq = 1
        initial = _response_skeleton(response)
        yield f"data: {json.dumps({'type': 'response.created', 'sequence_number': seq, 'response': initial})}\n\n"
        seq += 1

        for output_index, item in enumerate(response.get("output", [])):
            item_type = item.get("type")
            if item_type == "message":
                skeleton = {
                    "id": item["id"],
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                }
                if item.get("phase"):
                    skeleton["phase"] = item["phase"]
                yield f"data: {json.dumps({'type': 'response.output_item.added', 'sequence_number': seq, 'output_index': output_index, 'item': skeleton})}\n\n"
                seq += 1

                for content_index, part in enumerate(item.get("content", [])):
                    part_type = part.get("type")
                    if part_type == "output_text":
                        empty_part = {
                            "type": "output_text",
                            "text": "",
                            "annotations": part.get("annotations", []),
                        }
                        yield f"data: {json.dumps({'type': 'response.content_part.added', 'sequence_number': seq, 'output_index': output_index, 'content_index': content_index, 'item_id': item['id'], 'part': empty_part})}\n\n"
                        seq += 1
                        text = str(part.get("text", ""))
                        yield f"data: {json.dumps({'type': 'response.output_text.delta', 'sequence_number': seq, 'output_index': output_index, 'content_index': content_index, 'item_id': item['id'], 'delta': text, 'logprobs': []})}\n\n"
                        seq += 1
                        yield f"data: {json.dumps({'type': 'response.output_text.done', 'sequence_number': seq, 'output_index': output_index, 'content_index': content_index, 'item_id': item['id'], 'text': text, 'logprobs': []})}\n\n"
                        seq += 1
                        yield f"data: {json.dumps({'type': 'response.content_part.done', 'sequence_number': seq, 'output_index': output_index, 'content_index': content_index, 'item_id': item['id'], 'part': part})}\n\n"
                        seq += 1
                yield f"data: {json.dumps({'type': 'response.output_item.done', 'sequence_number': seq, 'output_index': output_index, 'item': item})}\n\n"
                seq += 1
                continue

            if item_type == "function_call":
                skeleton = dict(item)
                skeleton["status"] = "in_progress"
                skeleton["arguments"] = ""
                yield f"data: {json.dumps({'type': 'response.output_item.added', 'sequence_number': seq, 'output_index': output_index, 'item': skeleton})}\n\n"
                seq += 1
                arguments = str(item.get("arguments", ""))
                yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'sequence_number': seq, 'output_index': output_index, 'item_id': item['id'], 'delta': arguments})}\n\n"
                seq += 1
                yield f"data: {json.dumps({'type': 'response.function_call_arguments.done', 'sequence_number': seq, 'output_index': output_index, 'item_id': item['id'], 'name': item.get('name', ''), 'arguments': arguments})}\n\n"
                seq += 1
                yield f"data: {json.dumps({'type': 'response.output_item.done', 'sequence_number': seq, 'output_index': output_index, 'item': item})}\n\n"
                seq += 1
                continue

            yield f"data: {json.dumps({'type': 'response.output_item.added', 'sequence_number': seq, 'output_index': output_index, 'item': item})}\n\n"
            seq += 1
            yield f"data: {json.dumps({'type': 'response.output_item.done', 'sequence_number': seq, 'output_index': output_index, 'item': item})}\n\n"
            seq += 1

        yield f"data: {json.dumps({'type': 'response.completed', 'sequence_number': seq, 'response': response})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/responses")
async def create_response(request: Request, sub: dict = Depends(_require_license)):
    """Compatibility shim: translate Responses API requests into chat completions."""
    body = await request.json()
    chat_body, conversation_input = _responses_request_to_chat_body(body)
    chat_data, provider = await _dispatch_chat_completion(chat_body, sub)

    usage = chat_data.get("usage", {}) if isinstance(chat_data, dict) else {}
    db.log_usage(
        sub["license_key"],
        chat_body.get("model", "unknown"),
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
        provider.name,
    )

    response_obj, _conversation_history = _chat_completion_to_response(body, chat_data, conversation_input)
    if body.get("stream"):
        return _stream_events_for_response(response_obj)
    return JSONResponse(content=response_obj)


@app.get("/v1/responses/{response_id}")
async def retrieve_response(response_id: str, request: Request, _sub: dict = Depends(_require_license)):
    stored = _get_stored_response(response_id)
    if not stored:
        raise HTTPException(404, f"Unknown response id: {response_id}")

    response_obj = stored["response"]
    stream_flag = str(request.query_params.get("stream", "")).lower()
    if stream_flag in {"1", "true", "yes"}:
        return _stream_events_for_response(response_obj)
    return JSONResponse(content=response_obj)


@app.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, _sub: dict = Depends(_require_license)):
    if response_id in _response_store:
        del _response_store[response_id]
    return Response(status_code=204)


@app.post("/v1/responses/{response_id}/cancel")
async def cancel_response(response_id: str, _sub: dict = Depends(_require_license)):
    stored = _get_stored_response(response_id)
    if not stored:
        raise HTTPException(404, f"Unknown response id: {response_id}")
    response_obj = dict(stored["response"])
    if response_obj.get("status") == "completed":
        return JSONResponse(content=response_obj)
    response_obj["status"] = "cancelled"
    response_obj["completed_at"] = time.time()
    _store_response(response_id, response_obj, stored["conversation_messages"])
    return JSONResponse(content=response_obj)


# =========================================================================
#  Subscription endpoints (used by mtgacoach client)
# =========================================================================

@app.post("/v1/subscription/check")
async def check_subscription(request: Request):
    """Check subscription status. Returns status + any pending messages."""
    key = _extract_license_key(request)
    if not key:
        # Also try JSON body
        try:
            body = await request.json()
            key = body.get("license_key", "")
        except Exception:
            pass

    if not key:
        return JSONResponse(status_code=401, content={
            "status": "invalid",
            "message": "No license key provided.",
        })

    sub = db.check_license(key)
    if not sub:
        return JSONResponse(status_code=401, content={
            "status": "invalid",
            "message": "Invalid license key.",
        })

    _record_client_telemetry(request, sub["license_key"])

    messages = db.get_messages_after(0)  # Client tracks last_seen_message_id

    result = {
        "status": sub["status"],
        "message": "",
        "expires_at": sub.get("expires_at"),
        "messages": [
            {"id": m["id"], "title": m["title"], "body": m["body"],
             "priority": m["priority"], "created_at": m["created_at"]}
            for m in messages
        ],
    }

    if sub["status"] == "expired":
        result["message"] = "Subscription expired. Renew at mtgacoach.com/subscribe"
    elif sub["status"] == "revoked":
        result["message"] = "Subscription revoked."

    status_code = 200 if sub["status"] in ("active", "trial") else 402
    return JSONResponse(status_code=status_code, content=result)


@app.get("/v1/subscription/messages")
async def get_messages(request: Request):
    """Get service messages for a subscriber."""
    key = _extract_license_key(request)
    if not key:
        raise HTTPException(401, "Missing license key")

    sub = db.check_license(key)
    if not sub:
        raise HTTPException(401, "Invalid license key")

    _record_client_telemetry(request, sub["license_key"])

    messages = db.get_messages_after(0)
    return {
        "messages": [
            {"id": m["id"], "title": m["title"], "body": m["body"],
             "priority": m["priority"], "created_at": m["created_at"]}
            for m in messages
        ]
    }


# =========================================================================
#  Web pages (landing, subscribe, admin)
# =========================================================================

@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    return templates.TemplateResponse(request=request, name="landing.html")


@app.get("/subscribe", response_class=HTMLResponse)
async def subscribe_page(request: Request):
    return templates.TemplateResponse(request=request, name="subscribe.html")


@app.post("/subscribe/request")
async def subscribe_request(request: Request):
    """Block public self-service key issuance.

    Existing subscribers remain valid, but new keys must be created through
    Patreon or the admin dashboard. This prevents anonymous callers from
    minting or recovering active customer license keys.
    """
    body = await request.json()
    email = body.get("email", "").strip()
    if not email:
        raise HTTPException(400, "Email is required")

    logger.warning("Blocked public self-service signup attempt for %s", email)
    raise HTTPException(
        403,
        "Self-service key issuance is disabled. Subscribe via Patreon or contact support.",
    )


# =========================================================================
#  Patreon integration
# =========================================================================

PATREON_CLIENT_ID = os.environ.get("PATREON_CLIENT_ID", "")
PATREON_CLIENT_SECRET = os.environ.get("PATREON_CLIENT_SECRET", "")
PATREON_CREATOR_TOKEN = os.environ.get("PATREON_CREATOR_TOKEN", "")
PATREON_WEBHOOK_SECRET = os.environ.get("PATREON_WEBHOOK_SECRET", "")


@app.post("/patreon/webhook")
async def patreon_webhook(request: Request):
    """Handle Patreon webhook events for membership changes."""
    import hashlib
    import hmac

    body_bytes = await request.body()

    # Verify webhook signature
    if PATREON_WEBHOOK_SECRET:
        signature = request.headers.get("X-Patreon-Signature", "")
        expected = hmac.new(
            PATREON_WEBHOOK_SECRET.encode(),
            body_bytes,
            hashlib.md5,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("Patreon webhook: invalid signature")
            raise HTTPException(403, "Invalid signature")

    event_type = request.headers.get("X-Patreon-Event", "")
    data = json.loads(body_bytes)
    logger.info(f"Patreon webhook: {event_type}")

    # Extract patron info from the webhook payload
    patron_email = ""
    patron_name = ""
    patron_id = ""
    pledge_active = False

    try:
        # The included array has user and tier data
        included = data.get("included", [])
        for item in included:
            if item.get("type") == "user":
                attrs = item.get("attributes", {})
                patron_email = attrs.get("email", "")
                patron_name = attrs.get("full_name", "")
                patron_id = item.get("id", "")

        # Check membership attributes
        member_attrs = data.get("data", {}).get("attributes", {})
        patron_status = member_attrs.get("patron_status", "")
        pledge_active = patron_status == "active_patron"

    except Exception as e:
        logger.error(f"Patreon webhook parse error: {e}")
        return {"ok": True}  # Always return 200 to Patreon

    if event_type in ("members:pledge:create", "members:create"):
        if patron_email and pledge_active:
            # Check if already exists
            with db.get_db() as conn:
                existing = conn.execute(
                    "SELECT * FROM subscribers WHERE email = ?",
                    (patron_email,),
                ).fetchone()

            if existing:
                # Reactivate if previously revoked/expired
                if existing["status"] != "active":
                    db.update_subscriber(
                        existing["license_key"],
                        status="active",
                        notes=f"Patreon reactivated (patron_id={patron_id})",
                    )
                    logger.info(f"Patreon: reactivated {patron_email}")
                else:
                    logger.info(f"Patreon: {patron_email} already active")
            else:
                result = db.create_subscriber(
                    email=patron_email,
                    name=patron_name,
                    days=0,  # No expiry — managed by Patreon
                    notes=f"Patreon patron (patron_id={patron_id})",
                )
                logger.info(f"Patreon: created subscriber {patron_email} -> {result['license_key'][:20]}...")

    elif event_type in ("members:pledge:delete", "members:delete"):
        if patron_email:
            with db.get_db() as conn:
                existing = conn.execute(
                    "SELECT * FROM subscribers WHERE email = ?",
                    (patron_email,),
                ).fetchone()
            if existing:
                db.revoke_subscriber(existing["license_key"])
                logger.info(f"Patreon: revoked {patron_email}")

    elif event_type == "members:pledge:update":
        if patron_email:
            with db.get_db() as conn:
                existing = conn.execute(
                    "SELECT * FROM subscribers WHERE email = ?",
                    (patron_email,),
                ).fetchone()
            if existing:
                if pledge_active and existing["status"] != "active":
                    db.update_subscriber(existing["license_key"], status="active")
                    logger.info(f"Patreon: reactivated {patron_email}")
                elif not pledge_active and existing["status"] == "active":
                    db.revoke_subscriber(existing["license_key"])
                    logger.info(f"Patreon: revoked (pledge inactive) {patron_email}")

    return {"ok": True}


@app.get("/patreon/callback")
async def patreon_callback(request: Request):
    """Handle Patreon OAuth callback — patron links their account to get a license key."""
    code = request.query_params.get("code", "")
    if not code:
        return HTMLResponse("<h1>Error</h1><p>No authorization code received.</p>", status_code=400)

    # Exchange code for access token
    try:
        token_resp = await http_client.post(
            "https://www.patreon.com/api/oauth2/token",
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": PATREON_CLIENT_ID,
                "client_secret": PATREON_CLIENT_SECRET,
                "redirect_uri": "https://mtgacoach.com/patreon/callback",
            },
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token", "")

        if not access_token:
            logger.error(f"Patreon OAuth failed: {token_data}")
            return HTMLResponse("<h1>Error</h1><p>Could not authenticate with Patreon.</p>", status_code=400)

        # Get patron identity
        identity_resp = await http_client.get(
            "https://www.patreon.com/api/oauth2/v2/identity"
            "?fields%5Buser%5D=email,full_name"
            "&include=memberships",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        identity = identity_resp.json()
        user_data = identity.get("data", {}).get("attributes", {})
        patron_email = user_data.get("email", "")
        patron_name = user_data.get("full_name", "")

        if not patron_email:
            return HTMLResponse("<h1>Error</h1><p>Could not get your email from Patreon.</p>", status_code=400)

        # Find or create subscriber
        with db.get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM subscribers WHERE email = ?",
                (patron_email,),
            ).fetchone()

        if existing:
            license_key = existing["license_key"]
            if existing["status"] != "active":
                db.update_subscriber(license_key, status="active")
        else:
            result = db.create_subscriber(
                email=patron_email, name=patron_name, days=0,
                notes="Patreon OAuth signup",
            )
            license_key = result["license_key"]
            logger.info(f"Patreon OAuth: created {patron_email}")

        # Show the license key
        return HTMLResponse(f"""
        <html><head><title>mtgacoach - Welcome!</title>
        <style>
            body {{ font-family: -apple-system, sans-serif; background: #0a0a0f; color: #e0e0e8; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
            .card {{ background: #12121a; border: 1px solid #2a2a3a; border-radius: 16px; padding: 40px; text-align: center; max-width: 500px; }}
            h1 {{ color: #22c55e; }}
            .key {{ font-family: monospace; background: #0a0a0f; padding: 16px; border-radius: 8px; word-break: break-all; margin: 16px 0; cursor: pointer; font-size: 0.9rem; }}
            .key:hover {{ background: #2a2a3a; }}
            code {{ background: #2a2a3a; padding: 2px 8px; border-radius: 4px; }}
        </style></head><body>
        <div class="card">
            <h1>Welcome, {patron_name or 'Patron'}!</h1>
            <p>Your license key is:</p>
            <div class="key" onclick="navigator.clipboard.writeText('{license_key}').then(()=>this.textContent='Copied!')" title="Click to copy">{license_key}</div>
            <p>Enter this in the mtgacoach app with:<br><code>/key {license_key}</code></p>
        </div>
        </body></html>
        """)

    except Exception as e:
        logger.error(f"Patreon OAuth error: {e}")
        return HTMLResponse(f"<h1>Error</h1><p>{e}</p>", status_code=500)


# =========================================================================
#  Admin API (for managing subscribers + messages)
# =========================================================================

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html")


@app.get("/admin/api/subscribers")
async def admin_list_subscribers(request: Request, _=Depends(_require_admin)):
    subs = db.list_subscribers()
    # Add usage info
    usage = {u["license_key"]: u for u in db.get_all_usage_summary(30)}
    clients = db.get_client_summary(30)
    for s in subs:
        u = usage.get(s["license_key"], {})
        c = clients.get(s["license_key"], {})
        s["requests_30d"] = u.get("requests", 0)
        s["tokens_30d"] = u.get("total_prompt", 0) + u.get("total_completion", 0)
        s["installs_30d"] = c.get("installs_30d", 0)
        s["frontends_30d"] = c.get("frontends_30d", "")
        s["latest_frontend"] = c.get("latest_frontend", "")
        s["latest_version"] = c.get("latest_version", "")
        s["latest_install_id"] = c.get("latest_install_id", "")
        s["last_seen_at"] = c.get("last_seen_at", 0)
    return subs


@app.post("/admin/api/subscribers")
async def admin_create_subscriber(request: Request, _=Depends(_require_admin)):
    body = await request.json()
    result = db.create_subscriber(
        email=body.get("email", ""),
        name=body.get("name", ""),
        days=body.get("days", 30),
        notes=body.get("notes", ""),
    )
    return result


@app.put("/admin/api/subscribers/{key}")
async def admin_update_subscriber(key: str, request: Request, _=Depends(_require_admin)):
    body = await request.json()
    if body.get("action") == "extend":
        db.extend_subscriber(key, body.get("days", 30))
        return {"ok": True}
    elif body.get("action") == "revoke":
        db.revoke_subscriber(key)
        return {"ok": True}
    elif body.get("action") == "activate":
        db.update_subscriber(key, status="active")
        return {"ok": True}
    else:
        db.update_subscriber(key, **{k: v for k, v in body.items() if k != "action"})
        return {"ok": True}


@app.delete("/admin/api/subscribers/{key}")
async def admin_delete_subscriber(key: str, request: Request, _=Depends(_require_admin)):
    db.delete_subscriber(key)
    return {"ok": True}


@app.get("/admin/api/messages")
async def admin_list_messages(request: Request, _=Depends(_require_admin)):
    return db.list_messages()


@app.post("/admin/api/messages")
async def admin_create_message(request: Request, _=Depends(_require_admin)):
    body = await request.json()
    msg_id = db.create_message(
        title=body["title"],
        body=body["body"],
        priority=body.get("priority", "normal"),
        target=body.get("target", "all"),
    )
    return {"id": msg_id}


@app.delete("/admin/api/messages/{msg_id}")
async def admin_delete_message(msg_id: int, request: Request, _=Depends(_require_admin)):
    db.delete_message(msg_id)
    return {"ok": True}


@app.get("/admin/api/usage")
async def admin_usage(request: Request, _=Depends(_require_admin)):
    return db.get_all_usage_summary(30)


@app.get("/admin/api/logs")
async def admin_logs(request: Request, _=Depends(_require_admin)):
    """Return the last N lines of the proxy log."""
    lines = int(request.query_params.get("lines", "200"))
    log_file = LOG_DIR / "proxy.log"
    if not log_file.exists():
        return {"lines": []}
    with open(log_file) as f:
        all_lines = f.readlines()
    return {"lines": [l.rstrip() for l in all_lines[-lines:]]}


# =========================================================================
#  Health check
# =========================================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "providers": [
            {"name": p.name, "available": p.available, "models": p.models}
            for p in router.providers
        ],
    }


if __name__ == "__main__":
    import uvicorn
    host = config.get("server", {}).get("host", "0.0.0.0")
    port = config.get("server", {}).get("port", 8443)
    uvicorn.run(app, host=host, port=port)
