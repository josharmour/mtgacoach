"""AI proxy and Responses shim routes."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import patreon
import state
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from state import (
    LOG_DIR,
    _RESPONSE_STORE_MAX,
    _extract_client_metadata,
    _extract_license_key,
    _get_db,
    _get_stored_response,
    _record_client_telemetry,
    _reload_providers,
    _require_admin,
    _require_license,
    _resolved_default_model,
    _resolved_provider_configs,
    _response_store,
    _store_response,
    templates,
)

def _get_patreon():
    import sys
    import patreon
    return sys.modules.get("patreon", patreon)

logger = logging.getLogger("website.proxy")

router = APIRouter()

# =========================================================================
#  OpenAI-compatible API endpoints (used by mtgacoach client)
# =========================================================================

@router.get("/v1/models")
async def list_models(request: Request):
    """List available models. Requires valid license key."""
    key = _extract_license_key(request)
    if key:
        sub = _get_db().check_license(key)
        if not sub or sub["status"] not in ("active", "trial"):
            raise HTTPException(401, "Invalid or expired license key")
        _record_client_telemetry(request, sub["license_key"])

    models = state.router.get_all_models()
    return {"object": "list", "data": models}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, sub: dict = Depends(_require_license)):
    """Proxy chat completions to the best available provider.

    Routing is admin-controlled: the client's `model` field is intentionally
    ignored. Either the subscriber has an admin-assigned model (set in the
    Subscribers admin tab), or the cluster `default_model` is used. This
    lets the operator centrally control cost/capability per user without
    trusting whatever the desktop client happens to send.
    """
    body = await request.json()
    stream = body.get("stream", False)
    requested = body.get("model")

    assigned = (sub.get("assigned_model") or "").strip()
    default = _resolved_default_model()
    model = assigned or default or None

    if model:
        body["model"] = model
    if requested and requested != model:
        logger.info(
            f"Subscriber {sub['license_key'][:12]}... routed to {model!r} "
            f"(client asked for {requested!r}; "
            f"{'admin pin' if assigned else 'cluster default'})"
        )
        # The client thought it was talking to a GPT-5-class reasoning model
        # (e.g. "gpt-5.4") and may have sent reasoning_effort / verbosity. A
        # local model like Gemma 4 *honors* reasoning_effort by emitting tokens
        # into the reasoning channel and leaving `content` empty â€” which surfaces
        # to the user as "empty advice". Since we've routed to a different model,
        # drop those GPT-5-specific controls so the served model returns content.
        for _k in ("reasoning_effort", "reasoning", "thinking_config", "thinking", "verbosity"):
            body.pop(_k, None)

    provider = state.router.select_provider(model)
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
            detail = f" â€” {e.response.text[:200]}"
        except Exception:
            pass
        logger.error(f"Provider {provider.name} returned {e.response.status_code}{detail}")

        # Try next provider
        fallback = state.router.select_provider(model)
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
    response = await provider.forward_chat(body, state.http_client)
    response.raise_for_status()
    provider.mark_success()

    data = response.json()

    # Log usage
    usage = data.get("usage", {})
    _get_db().log_usage(
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
            async for line in provider.forward_chat_stream(body, state.http_client):
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
            _get_db().log_usage(
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
    if item_type == "custom_tool_call":
        return [{
            "id": str(item.get("call_id") or item.get("id") or _new_id("tool")),
            "type": "function",
            "function": {
                "name": str(item.get("name", "")),
                "arguments": json.dumps({"input": str(item.get("input", ""))}, separators=(",", ":"), ensure_ascii=False),
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

    if item_type in {"function_call", "local_shell_call", "shell_call", "apply_patch_call", "custom_tool_call"}:
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
    if item_type in {"shell_call_output", "apply_patch_call_output", "custom_tool_call_output"}:
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

    if tool_type == "custom":
        name = str(tool.get("name", "")).strip()
        if not name:
            raise HTTPException(400, "custom tool requires a name")
        description = str(tool.get("description") or "")
        fmt = tool.get("format") if isinstance(tool.get("format"), dict) else {}
        fmt_type = str(fmt.get("type", "text"))
        if fmt_type == "grammar":
            grammar = fmt.get("grammar")
            if grammar:
                description = (description + "\n\nThe `input` must conform to this grammar:\n" + json.dumps(grammar, ensure_ascii=False)).strip()
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string", "description": "The body of the tool invocation (free-form text)."},
                    },
                    "required": ["input"],
                    "additionalProperties": False,
                },
                "strict": True,
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


def _chat_message_to_response_output_items(
    message: dict[str, Any],
    custom_tool_names: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    content_text, tool_calls = _chat_message_text_and_tools(message)
    output_items: list[dict[str, Any]] = []
    history_messages: list[dict[str, Any]] = []
    custom_tool_names = custom_tool_names or set()

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

        if tool_name in custom_tool_names:
            parsed = _parse_tool_arguments(arguments, tool_name)
            tool_input = ""
            if isinstance(parsed, dict):
                tool_input = str(parsed.get("input", ""))
            elif isinstance(parsed, str):
                tool_input = parsed
            output_items.append({
                "id": _new_id("custom"),
                "call_id": tool_call_id,
                "type": "custom_tool_call",
                "status": "completed",
                "name": tool_name,
                "input": tool_input,
            })
            pending_tool_calls.append({
                "id": tool_call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": arguments},
            })
            continue

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
    provider = state.router.select_provider(model)
    if not provider:
        raise HTTPException(503, "No AI provider available. Try again later.")

    logger.info(
        "Routing %s via Responses shim to %s (subscriber=%s...)",
        model or "default",
        provider.name,
        sub["email"] or sub["license_key"][:12],
    )

    try:
        response = await provider.forward_chat(body, state.http_client)
        response.raise_for_status()
        provider.mark_success()
        return response.json(), provider
    except httpx.HTTPStatusError as e:
        provider.mark_failure()
        detail = ""
        try:
            detail = f" â€” {e.response.text[:200]}"
        except Exception:
            pass
        logger.error("Provider %s returned %s%s", provider.name, e.response.status_code, detail)

        fallback = state.router.select_provider(model)
        if fallback and fallback.name != provider.name:
            logger.info("Falling back to %s for Responses shim", fallback.name)
            try:
                response = await fallback.forward_chat(body, state.http_client)
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


def _custom_tool_names_from_body(body: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "custom":
            name = str(tool.get("name", "")).strip()
            if name:
                names.add(name)
    return names


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

    output_items, output_history = _chat_message_to_response_output_items(
        message, _custom_tool_names_from_body(original_body)
    )
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

            if item_type == "custom_tool_call":
                skeleton = dict(item)
                skeleton["status"] = "in_progress"
                skeleton["input"] = ""
                yield f"data: {json.dumps({'type': 'response.output_item.added', 'sequence_number': seq, 'output_index': output_index, 'item': skeleton})}\n\n"
                seq += 1
                tool_input = str(item.get("input", ""))
                yield f"data: {json.dumps({'type': 'response.custom_tool_call_input.delta', 'sequence_number': seq, 'output_index': output_index, 'item_id': item['id'], 'delta': tool_input})}\n\n"
                seq += 1
                yield f"data: {json.dumps({'type': 'response.custom_tool_call_input.done', 'sequence_number': seq, 'output_index': output_index, 'item_id': item['id'], 'name': item.get('name', ''), 'input': tool_input})}\n\n"
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


@router.post("/v1/responses")
async def create_response(request: Request, sub: dict = Depends(_require_license)):
    """Compatibility shim: translate Responses API requests into chat completions."""
    body = await request.json()
    chat_body, conversation_input = _responses_request_to_chat_body(body)
    chat_data, provider = await _dispatch_chat_completion(chat_body, sub)

    usage = chat_data.get("usage", {}) if isinstance(chat_data, dict) else {}
    _get_db().log_usage(
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


@router.get("/v1/responses/{response_id}")
async def retrieve_response(response_id: str, request: Request, _sub: dict = Depends(_require_license)):
    stored = _get_stored_response(response_id)
    if not stored:
        raise HTTPException(404, f"Unknown response id: {response_id}")

    response_obj = stored["response"]
    stream_flag = str(request.query_params.get("stream", "")).lower()
    if stream_flag in {"1", "true", "yes"}:
        return _stream_events_for_response(response_obj)
    return JSONResponse(content=response_obj)


@router.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, _sub: dict = Depends(_require_license)):
    if response_id in _response_store:
        del _response_store[response_id]
    return Response(status_code=204)


@router.post("/v1/responses/{response_id}/cancel")
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
#  Free trial (desktop app first run â€” no Patreon required)
# =========================================================================

# machine_id is a sha256 hex digest computed client-side; nothing else is
# accepted, so the endpoint can't be used to mint keys for arbitrary strings.
_MACHINE_ID_RE = re.compile(r"^[0-9a-f]{64}$")
TRIAL_DAYS = 7


def _trial_iso(dt: datetime) -> str:
    """ISO8601 UTC with a trailing Z, second precision."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_trial_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@router.post("/api/trial")
async def request_trial(request: Request):
    """Auto-provision a 7-day trial key on the desktop app's first run.

    One trial per machine_id, ever. While the trial is active, repeat calls
    return the same key; after it lapses, 403 gates on a Patreon subscription.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid JSON body")

    machine_id = str(body.get("machine_id", "")).strip().lower()
    app_version = str(body.get("app_version", "")).strip()
    if not _MACHINE_ID_RE.fullmatch(machine_id):
        raise HTTPException(422, "machine_id must be a 64-character sha256 hex digest")

    now = datetime.now(timezone.utc)

    existing = _get_db().get_trial(machine_id)
    if existing:
        try:
            expires = _parse_trial_iso(existing["expires_at"] or "")
        except ValueError:
            expires = now  # unparseable record â€” treat as lapsed
        if expires > now:
            return {
                "key": existing["litellm_key"],
                "expires_at": existing["expires_at"],
                "status": "existing",
            }
        raise HTTPException(403, "trial_expired")

    try:
        key = await _get_patreon().mint_trial_key(machine_id)
    except Exception as e:
        logger.error(f"Trial mint failed for machine {machine_id[:12]}...: {e}")
        raise HTTPException(503, "Trial provisioning is temporarily unavailable. Try again later.")

    expires_at = _trial_iso(now + timedelta(days=TRIAL_DAYS))
    _get_db().create_trial(machine_id, key, _trial_iso(now), expires_at)
    logger.info(
        f"Trial key issued for machine {machine_id[:12]}... "
        f"(app_version={app_version or 'unknown'}, expires {expires_at})"
    )
    return {"key": key, "expires_at": expires_at, "status": "created"}



# =========================================================================
#  MageZero MCTS Neural Inference Proxy Endpoints
# =========================================================================

MAGEZERO_UPSTREAM_URL = os.environ.get("MAGEZERO_UPSTREAM_URL", "http://10.0.0.10:50052")


@router.get("/magezero/healthz")
async def magezero_health():
    """Proxy health check to upstream MageZero neural inference engine."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        try:
            resp = await client.get(f"{MAGEZERO_UPSTREAM_URL.rstrip('/')}/healthz")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
            )
        except Exception as e:
            return JSONResponse({"status": "unavailable", "error": str(e)}, status_code=503)


@router.post("/magezero/evaluate")
async def magezero_evaluate(request: Request):
    """Proxy neural tensor evaluation requests to upstream MageZero."""
    # Check subscriber license or valid trial
    _require_license(request)
    body = await request.body()
    content_type = request.headers.get("content-type", "application/x-msgpack")
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            resp = await client.post(
                f"{MAGEZERO_UPSTREAM_URL.rstrip('/')}/evaluate",
                content=body,
                headers={"Content-Type": content_type, "User-Agent": "MtgACoachGateway/2.7"},
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/x-msgpack"),
            )
        except Exception as e:
            logger.warning(f"MageZero upstream evaluate error: {e}")
            raise HTTPException(502, f"MageZero upstream unavailable: {e}")


# =========================================================================
