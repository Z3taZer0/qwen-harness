from __future__ import annotations

import argparse
import os
import sys

from .agent import Agent
from .config import Config

HELP = "/img <path> [text]  attach image | /stats | /clear | /exit"


def main() -> None:
    ap = argparse.ArgumentParser(prog="qh")
    ap.add_argument("prompt", nargs="*", help="one-shot prompt (omit for interactive)")
    ap.add_argument("-C", "--cwd", default=os.getcwd())
    ap.add_argument("--thinking", choices=["on", "off", "auto"])
    ap.add_argument("--base-url")
    a = ap.parse_args()

    cfg = Config.load()
    if a.thinking:
        cfg.thinking = a.thinking
    if a.base_url:
        cfg.base_url = a.base_url
    agent = Agent(cfg, a.cwd)
    if sys.stdin.isatty():
        agent.tb.ask = lambda q: input(f"\n? {q} [y/N] ").strip().lower() in ("y", "yes")

    if a.prompt:
        agent.user_turn(" ".join(a.prompt))
        return
    print(f"qh · {cfg.model} · ctx {cfg.context_window} · thinking={cfg.thinking}\n{HELP}")
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("/exit", "/quit"):
            break
        if line == "/clear":
            agent = Agent(cfg, a.cwd)
            agent.tb.ask = lambda q: input(f"\n? {q} [y/N] ").strip().lower() in ("y", "yes")
            continue
        if line == "/stats":
            s = agent.stats
            hit = s["cached"] / s["prompt"] if s["prompt"] else 0
            print(f"{s}  cache-hit={hit:.0%}  compactions={agent.ctx.compactions}")
            continue
        images = []
        if line.startswith("/img "):
            _, path, *rest = line.split(maxsplit=2)
            images, line = [path], (rest[0] if rest else "Describe this image.")
        try:
            agent.user_turn(line, images)
        except KeyboardInterrupt:
            print("\n[interrupted]")
        except Exception as e:
            print(f"\n[error] {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
