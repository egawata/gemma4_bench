#!/usr/bin/env python3
"""Ollama benchmark client for gemma4:31b-it-q8_0 summarization."""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path


DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:31b-it-q8_0"
DEFAULT_INPUT = "diary.md"
DEFAULT_PROMPT_TEMPLATE = (
    "以下の文章を日本語で簡潔に要約してください。\n\n"
    "----\n"
    "{content}\n"
    "----\n"
)


def ns_to_s(ns: int) -> float:
    return ns / 1_000_000_000


def format_duration(ns: int) -> str:
    """Render nanoseconds in the same style as `ollama run --verbose`."""
    sec = ns_to_s(ns)
    if sec >= 60:
        m = int(sec // 60)
        s = sec - m * 60
        return f"{m}m{s:.6f}s"
    if sec >= 1:
        return f"{sec:.6f}s"
    ms = ns / 1_000_000
    return f"{ms:.6f}ms"


def call_generate(
    host: str, model: str, prompt: str, timeout: float, think: bool | None
) -> dict:
    url = f"{host.rstrip('/')}/api/generate"
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if think is not None:
        payload["think"] = think
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def print_stats(result: dict) -> None:
    total = result.get("total_duration", 0)
    load = result.get("load_duration", 0)
    p_count = result.get("prompt_eval_count", 0)
    p_dur = result.get("prompt_eval_duration", 0)
    e_count = result.get("eval_count", 0)
    e_dur = result.get("eval_duration", 0)

    p_rate = p_count / ns_to_s(p_dur) if p_dur else 0.0
    e_rate = e_count / ns_to_s(e_dur) if e_dur else 0.0

    print(f"total duration:       {format_duration(total)}")
    print(f"load duration:        {format_duration(load)}")
    print(f"prompt eval count:    {p_count} token(s)")
    print(f"prompt eval duration: {format_duration(p_dur)}")
    print(f"prompt eval rate:     {p_rate:.2f} tokens/s")
    print(f"eval count:           {e_count} token(s)")
    print(f"eval duration:        {format_duration(e_dur)}")
    print(f"eval rate:            {e_rate:.2f} tokens/s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ollama summarization benchmark")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Input text file")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Ollama server URL")
    parser.add_argument("--runs", type=int, default=1, help="Number of runs")
    parser.add_argument("--timeout", type=float, default=600.0, help="HTTP timeout (sec)")
    parser.add_argument("--show-response", action="store_true", help="Print model output")
    parser.add_argument("--json", action="store_true", help="Print raw JSON response")
    parser.add_argument(
        "--think",
        choices=["on", "off", "both", "default"],
        default="both",
        help="thinking mode: on / off / both (compare) / default (unset, let the model decide)",
    )
    args = parser.parse_args()

    think_modes: list[tuple[str, bool | None]]
    if args.think == "on":
        think_modes = [("think=on", True)]
    elif args.think == "off":
        think_modes = [("think=off", False)]
    elif args.think == "default":
        think_modes = [("think=default", None)]
    else:  # both
        think_modes = [("think=on", True), ("think=off", False)]

    content = Path(args.input).read_text(encoding="utf-8")
    prompt = DEFAULT_PROMPT_TEMPLATE.format(content=content)

    print(f"model: {args.model}")
    print(f"host:  {args.host}")
    print(f"input: {args.input} ({len(content)} chars)")
    print(f"runs:  {args.runs}")
    print(f"think: {args.think}")
    print()

    summary: list[dict] = []

    for i in range(1, args.runs + 1):
        for label, think in think_modes:
            header = f"=== run {i}/{args.runs} [{label}] ==="
            print(header)
            wall_start = time.monotonic()
            try:
                result = call_generate(
                    args.host, args.model, prompt, args.timeout, think
                )
            except Exception as e:
                print(f"request failed: {e}", file=sys.stderr)
                return 1
            wall_elapsed = time.monotonic() - wall_start

            thinking_text = result.get("thinking", "") or ""
            response_text = result.get("response", "") or ""

            if args.show_response:
                if thinking_text:
                    print("--- thinking ---")
                    print(thinking_text)
                print("--- response ---")
                print(response_text)
                print("----------------")

            if args.json:
                meta = {k: v for k, v in result.items() if k not in ("response", "thinking")}
                print(json.dumps(meta, ensure_ascii=False, indent=2))

            print_stats(result)
            print(f"wall clock:           {wall_elapsed:.3f}s")
            if thinking_text:
                print(f"thinking chars:       {len(thinking_text)}")
            print(f"response chars:       {len(response_text)}")
            print()

            e_count = result.get("eval_count", 0)
            e_dur = result.get("eval_duration", 0) or 0
            summary.append({
                "run": i,
                "label": label,
                "total_s": ns_to_s(result.get("total_duration", 0)),
                "eval_count": e_count,
                "eval_rate": (e_count / ns_to_s(e_dur)) if e_dur else 0.0,
                "thinking_chars": len(thinking_text),
                "response_chars": len(response_text),
            })

    if len(summary) > 1:
        print("=== summary ===")
        print(f"{'run':>3}  {'mode':<14}  {'total(s)':>9}  {'eval':>6}  {'tok/s':>7}  {'think_c':>7}  {'resp_c':>7}")
        for row in summary:
            print(
                f"{row['run']:>3}  {row['label']:<14}  "
                f"{row['total_s']:>9.2f}  {row['eval_count']:>6}  "
                f"{row['eval_rate']:>7.2f}  {row['thinking_chars']:>7}  {row['response_chars']:>7}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
