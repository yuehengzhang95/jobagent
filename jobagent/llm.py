"""One structured-output call to Claude.

Backend is chosen automatically:
  - ANTHROPIC_API_KEY set  -> Anthropic Python SDK (messages.parse)
  - otherwise              -> `claude -p --json-schema` (uses your Claude Code login)
"""
import json
import os
import subprocess
import sys
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

SDK_MODELS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}


class LLMError(RuntimeError):
    pass


def structured(
    system: str,
    prompt: str,
    schema: Type[T],
    model: str = "opus",
    effort: Optional[str] = None,
    label: str = "",
    web: bool = False,
) -> T:
    """web=True lets the model use WebSearch/WebFetch (CLI backend only)."""
    if os.environ.get("ANTHROPIC_API_KEY") and not web:
        return _via_sdk(system, prompt, schema, model)
    return _via_cli(system, prompt, schema, model, effort, label, web)


def _via_sdk(system: str, prompt: str, schema: Type[T], model: str) -> T:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=SDK_MODELS.get(model, model),
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    if response.stop_reason == "refusal":
        raise LLMError(f"model refused: {response.stop_details}")
    if response.parsed_output is None:
        raise LLMError(f"no parsed output (stop_reason={response.stop_reason})")
    return response.parsed_output


def _via_cli(
    system: str, prompt: str, schema: Type[T], model: str, effort: Optional[str], label: str,
    web: bool = False,
) -> T:
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema.model_json_schema()),
        "--system-prompt", system,
    ]
    if web:
        cmd += ["--tools", "WebSearch", "WebFetch", "--allowedTools", "WebSearch", "WebFetch"]
    else:
        cmd += ["--tools", ""]
    if effort:
        cmd += ["--effort", effort]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise LLMError(f"claude CLI failed ({proc.returncode}): {proc.stderr or proc.stdout}"[:2000])
    out = json.loads(proc.stdout)
    if out.get("is_error") or out.get("structured_output") is None:
        raise LLMError(f"claude CLI returned no structured output: {proc.stdout[:2000]}")
    cost = out.get("total_cost_usd")
    if cost is not None:
        print(f"  [llm] {label or schema.__name__}: {model}, ${cost:.3f}", file=sys.stderr)
    return schema.model_validate(out["structured_output"])
