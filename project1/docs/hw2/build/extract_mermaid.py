#!/usr/bin/env python3
"""Extract mermaid code blocks from hw2_report.md, save each to its own file."""
import re
import pathlib

SRC = pathlib.Path("/Users/labmem/code/SE/project1/docs/hw2/hw2_report.md")
OUT = pathlib.Path("/Users/labmem/code/SE/project1/docs/hw2/build/diagrams")
OUT.mkdir(parents=True, exist_ok=True)

text = SRC.read_text(encoding="utf-8")
pattern = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
matches = list(pattern.finditer(text))
print(f"Found {len(matches)} mermaid blocks")

manifest = []
for i, m in enumerate(matches, start=1):
    body = m.group(1).rstrip() + "\n"
    name = f"diagram_{i:02d}.mmd"
    (OUT / name).write_text(body, encoding="utf-8")
    head = body.splitlines()[0]
    manifest.append((i, name, head))

for i, name, head in manifest:
    print(f"  {i:2d} {name} -> {head}")
