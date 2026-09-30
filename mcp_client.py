"""MCP client: Mistral answers questions, using SOAP services (via the MCP server) only with permission.

For every question:
  1. A routing call asks Mistral whether one registered SOAP service can handle the WHOLE request.
  2. If so, the user is asked for permission to invoke it.
  3. Approved -> Mistral answers through the MCP tools; otherwise it answers from its own knowledge.

Usage:
  python mcp_client.py "What is 2 plus 3?"
  python mcp_client.py                  # interactive mode
  python mcp_client.py --yes "..."      # approve SOAP calls without asking
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mistralai.client import Mistral

MODEL = os.environ.get("MISTRAL_MODEL", "mistral-medium-latest")
SERVER_SCRIPT = Path(__file__).with_name("mcp_server.py")
MAX_STEPS = 10

ROUTER_PROMPT = """You decide whether a user request should be handled by one of these SOAP services:
{services}

Choose a service ONLY if the entire request can be completed using that service alone.
If any part of the request needs something the service does not do (a different kind of
operation, general knowledge, an explanation, ...), choose null.
Reply with JSON only: {{"service": "<service name>" or null, "reason": "<one short sentence>"}}"""

TOOL_PROMPT = """You answer using the SOAP service '{service}' through tools.
1. Call list_soap_operations(service="{service}") to get the operations and their arguments_template.
2. Call call_soap_operation with `arguments` shaped exactly like the template (same field names).
3. Answer from the response. If the response contains a Fault, report its message."""

DIRECT_PROMPT = "Answer the user's request from your own knowledge."


def to_mistral_tools(tools) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema},
        }
        for t in tools
    ]


def result_text(result) -> str:
    text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
    return f"ERROR: {text}" if result.is_error else text


def content_text(content) -> str:
    if isinstance(content, list):  # some models return content chunks
        return "".join(getattr(chunk, "text", "") or "" for chunk in content)
    return content or ""


async def complete(mistral: Mistral, messages: list[dict], **kwargs):
    response = await asyncio.to_thread(mistral.chat.complete, model=MODEL, messages=messages, **kwargs)
    return response.choices[0].message


async def route(mistral: Mistral, services: dict[str, str], history: list[dict], question: str) -> tuple[str | None, str]:
    listing = "\n".join(f"- {name}: {description}" for name, description in services.items())
    messages = [
        {"role": "system", "content": ROUTER_PROMPT.format(services=listing)},
        *history,
        {"role": "user", "content": question},
    ]
    message = await complete(mistral, messages, response_format={"type": "json_object"})
    try:
        decision = json.loads(content_text(message.content))
    except json.JSONDecodeError:
        return None, "routing reply was not valid JSON"
    service = decision.get("service")
    return (service if service in services else None), decision.get("reason", "")


async def run_tools(session: ClientSession, mistral: Mistral, tools: list[dict], messages: list[dict]) -> str:
    for _ in range(MAX_STEPS):
        message = await complete(mistral, messages, tools=tools, tool_choice="auto")
        calls = message.tool_calls or []
        if not calls:
            return content_text(message.content)

        messages.append({
            "role": "assistant",
            "content": content_text(message.content),
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments
                        if isinstance(call.function.arguments, str)
                        else json.dumps(call.function.arguments),
                    },
                }
                for call in calls
            ],
        })
        for call in calls:
            args = call.function.arguments
            args = json.loads(args or "{}") if isinstance(args, str) else args
            print(f"  -> {call.function.name}({json.dumps(args)})", file=sys.stderr)
            try:
                text = result_text(await session.call_tool(call.function.name, args))
            except Exception as exc:
                text = f"ERROR: {exc}"
            print(f"  <- {text[:500]}", file=sys.stderr)
            messages.append({"role": "tool", "name": call.function.name, "tool_call_id": call.id, "content": text})
    return "Stopped: too many tool-calling steps."


async def confirm(service: str, description: str) -> bool:
    try:
        reply = await asyncio.to_thread(input, f"Use SOAP service '{service}' ({description}) to answer? [y/N] ")
    except EOFError:
        return False
    return reply.strip().lower() in ("y", "yes")


async def handle(session, mistral, tools, services, history, question, auto_approve) -> str:
    service, reason = await route(mistral, services, history, question)
    use_service = False
    if service:
        print(f"[router] '{service}' can handle this: {reason}", file=sys.stderr)
        use_service = auto_approve or await confirm(service, services[service])
    else:
        print(f"[router] no SOAP service applies: {reason}", file=sys.stderr)

    user_message = {"role": "user", "content": question}
    if use_service:
        messages = [{"role": "system", "content": TOOL_PROMPT.format(service=service)}, *history, user_message]
        answer = await run_tools(session, mistral, tools, messages)
        print(f"[answered via SOAP service '{service}']", file=sys.stderr)
    else:
        messages = [{"role": "system", "content": DIRECT_PROMPT}, *history, user_message]
        answer = content_text((await complete(mistral, messages)).content)
        print("[answered from Mistral's own knowledge]", file=sys.stderr)

    # Keep only the question and final answer so later turns don't carry tool-call messages.
    history += [user_message, {"role": "assistant", "content": answer}]
    return answer


async def main() -> None:
    parser = argparse.ArgumentParser(description="Ask Mistral; it may use SOAP services via MCP with your permission.")
    parser.add_argument("prompt", nargs="*", help="Question to ask; omit for interactive mode.")
    parser.add_argument("--yes", action="store_true", help="Approve SOAP service calls without asking.")
    args = parser.parse_args()

    server = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)], env=dict(os.environ))
    mistral = Mistral(api_key=os.environ["MISTRAL_API_KEY"])

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = to_mistral_tools((await session.list_tools()).tools)
            services = json.loads(result_text(await session.call_tool("list_soap_services", {})))
            history: list[dict] = []

            if args.prompt:
                print(await handle(session, mistral, tools, services, history, " ".join(args.prompt), args.yes))
                return

            print(f"Connected. SOAP services: {sorted(services)}. Type 'exit' to quit.")
            while True:
                try:
                    question = (await asyncio.to_thread(input, "> ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if question.lower() in ("exit", "quit"):
                    break
                if question:
                    print(await handle(session, mistral, tools, services, history, question, args.yes))


if __name__ == "__main__":
    asyncio.run(main())
