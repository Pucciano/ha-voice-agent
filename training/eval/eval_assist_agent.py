"""Compare models on Home Assistant's real Assist tools, without executing them.

A request that Home Assistant sent through llm_proxy (one capture record)
supplies the system prompt, with the exposed devices, and the tool list. For
every case the script replaces the user's sentence, sends one request per
model to the upstream and checks the model's first step: which tool it calls
with which arguments, or that it only answers. Nothing reaches Home
Assistant, so nothing switches.

Standard library only, so it runs in the llm-proxy container on the Jetson,
where the upstream key already is (docs/llm_assistant.md, "Choose the model"):

  python eval_assist_agent.py --template <capture.jsonl> \
      --cases assist_agent_cases.de.json --model mistral-small-latest

The key comes from LLM_UPSTREAM_API_KEY, the URL from LLM_UPSTREAM_BASE_URL.
"""

import argparse
import json
import os
import pathlib
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--template",
        required=True,
        type=pathlib.Path,
        help="llm_proxy capture file (JSONL); its newest first-turn request "
        "with tools is used",
    )
    parser.add_argument("--cases", required=True, type=pathlib.Path)
    parser.add_argument(
        "--model", action="append", required=True, help="Repeat to compare"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LLM_UPSTREAM_BASE_URL", ""),
        help="OpenAI-compatible base URL (default: LLM_UPSTREAM_BASE_URL)",
    )
    parser.add_argument(
        "--drop-param",
        action="append",
        default=["user"],
        help="Request parameter to leave out (default: user)",
    )
    parser.add_argument(
        "--extra-body",
        default=os.environ.get("LLM_UPSTREAM_EXTRA_BODY", ""),
        help="JSON object merged into every request",
    )
    parser.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="MODEL=IN/OUT",
        help="US dollars per million input/output tokens, for the cost line",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output-json", type=pathlib.Path)
    return parser.parse_args(argv)


def load_template(path: pathlib.Path) -> dict[str, Any]:
    """The newest captured request with tools and a single user message."""

    best = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            request = record.get("request") or {}
            users = [
                m for m in request.get("messages", []) if m.get("role") == "user"
            ]
            if request.get("tools") and len(users) == 1:
                best = request
    if best is None:
        raise SystemExit(f"No first-turn request with tools in {path}")
    return best


def build_request(
    template: dict[str, Any],
    utterance: str,
    model: str,
    drop: set[str],
    extra: dict[str, Any],
) -> dict[str, Any]:
    messages = [
        message
        for message in template["messages"]
        if message.get("role") == "system"
    ]
    messages.append({"role": "user", "content": utterance})
    request = {
        key: value
        for key, value in template.items()
        if key not in drop and key not in {"messages", "stream"}
    }
    request.update(model=model, messages=messages, **extra)
    return request


def tool_name(name: str) -> str:
    """Home Assistant prefixes its tools with their API, e.g.
    `intent__HassTurnOn`; the cases name them without the prefix."""

    return name.rsplit("__", 1)[-1]


def first_step(response: dict[str, Any]) -> dict[str, Any]:
    """The tool calls or the text of the first choice."""

    message = (response.get("choices") or [{}])[0].get("message") or {}
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_invalid_json": function.get("arguments")}
        calls.append(
            {"tool": tool_name(function.get("name") or ""), "args": arguments}
        )
    return {"calls": calls, "text": message.get("content") or ""}


def _value_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, list):
        return any(_value_matches(option, actual) for option in expected)
    if isinstance(actual, list):
        return any(_value_matches(expected, item) for item in actual)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.casefold() == actual.casefold()
    return expected == actual


def _alternative_matches(alternative: dict[str, Any], step: dict) -> bool:
    if "answer" in alternative:
        pattern = alternative["answer"]
        if step["calls"]:
            return False
        return pattern is True or bool(re.search(pattern, step["text"]))
    for call in step["calls"]:
        if call["tool"] != alternative["tool"]:
            continue
        expected_args = alternative.get("args", {})
        if all(
            _value_matches(value, call["args"].get(key))
            for key, value in expected_args.items()
        ):
            return True
    return False


def check(case: dict[str, Any], step: dict[str, Any]) -> bool:
    """Any accepted alternative matches and no forbidden tool is called."""

    forbidden = set(case.get("forbid", []))
    if any(call["tool"] in forbidden for call in step["calls"]):
        return False
    return any(_alternative_matches(alt, step) for alt in case["accept"])


def post(url: str, key: str, body: dict) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as err:
        payload = {"error": err.read().decode(errors="replace")[:300]}
    return payload, time.perf_counter() - started


def _prices(values: list[str]) -> dict[str, tuple[float, float]]:
    prices = {}
    for value in values:
        model, _, pair = value.partition("=")
        inp, _, out = pair.partition("/")
        prices[model] = (float(inp), float(out))
    return prices


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.base_url:
        raise SystemExit("No --base-url and no LLM_UPSTREAM_BASE_URL")
    key = os.environ.get("LLM_UPSTREAM_API_KEY", "")
    template = load_template(args.template)
    tool_names = {
        tool_name(tool["function"]["name"]) for tool in template["tools"]
    }
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    extra = json.loads(args.extra_body) if args.extra_body.strip() else {}
    prices = _prices(args.price)
    url = f"{args.base_url.rstrip('/')}/chat/completions"
    print(f"Template tools: {', '.join(sorted(tool_names))}", file=sys.stderr)

    results = []
    for model in args.model:
        for case in cases:
            needed = {
                alt["tool"] for alt in case["accept"] if "tool" in alt
            }
            if needed and not needed & tool_names:
                results.append(
                    {"model": model, "utterance": case["utterance"],
                     "status": "skipped", "reason": "tool not offered"}
                )
                continue
            for _ in range(args.repeat):
                body = build_request(
                    template, case["utterance"], model,
                    set(args.drop_param), extra,
                )
                response, seconds = post(url, key, body)
                if "error" in response:
                    status, step = "error", {"calls": [], "text": ""}
                else:
                    step = first_step(response)
                    status = "pass" if check(case, step) else "fail"
                usage = response.get("usage") or {}
                results.append(
                    {
                        "model": model,
                        "utterance": case["utterance"],
                        "status": status,
                        "seconds": round(seconds, 2),
                        "calls": step["calls"],
                        "text": step["text"],
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "error": response.get("error"),
                    }
                )
                mark = {"pass": "ok ", "fail": "FAIL", "error": "ERR "}[status]
                summary = (
                    "; ".join(
                        f"{c['tool']}{json.dumps(c['args'], ensure_ascii=False)}"
                        for c in step["calls"]
                    )
                    or step["text"][:80]
                    or str(response.get("error"))[:80]
                )
                print(
                    f"{mark} {model:28} {seconds:5.2f}s "
                    f"{case['utterance'][:40]:40} -> {summary}"
                )

    print()
    for model in args.model:
        rows = [r for r in results if r["model"] == model]
        done = [r for r in rows if r["status"] in {"pass", "fail"}]
        passed = sum(r["status"] == "pass" for r in rows)
        errors = sum(r["status"] == "error" for r in rows)
        skipped = sum(r["status"] == "skipped" for r in rows)
        line = f"{model}: {passed}/{len(done)} pass, {errors} errors"
        line += f", {skipped} skipped"
        if done:
            line += (
                f", median {statistics.median(r['seconds'] for r in done):.2f}"
                " s"
            )
            prompt = statistics.mean(r["prompt_tokens"] or 0 for r in done)
            completion = statistics.mean(
                r["completion_tokens"] or 0 for r in done
            )
            line += f", {prompt:.0f}+{completion:.0f} tokens per step"
            if model in prices:
                inp, out = prices[model]
                cost = (prompt * inp + completion * out) / 1e6
                line += f", {cost * 100:.3f} US cents per step"
        print(line)

    if args.output_json:
        args.output_json.write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
