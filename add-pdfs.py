#!/usr/bin/env python3
"""Repository-local shortcut for bulk-adding PDFs from one folder."""
import os
import sys

from pipeline.add_paper import main


if __name__ == "__main__":
    os.environ.setdefault("LLM_BASE_URL", "http://localhost:8000/v1")
    os.environ.setdefault("LLM_API_KEY", "dummy")
    sys.exit(main(sys.argv[1:]))
