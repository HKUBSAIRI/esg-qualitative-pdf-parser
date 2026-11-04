from __future__ import annotations

import sys


USAGE = """usage: pdf-natural-paragraphs <command> [options]

commands:
  parse       run Docling normalization and natural-paragraph assembly
  visualize   draw final chunk boundaries over the PDF
"""


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(USAGE)
        return 0
    command = sys.argv.pop(1)
    if command == "parse":
        from .pipeline import main as command_main
    elif command == "visualize":
        from .visualize import main as command_main
    else:
        print(f"Unknown command: {command}\n\n{USAGE}", file=sys.stderr)
        return 2
    return command_main()


if __name__ == "__main__":
    raise SystemExit(main())
