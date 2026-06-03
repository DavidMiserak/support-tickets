"""Print a DistilBART summary for the shared ML sample ticket text.

Run via: make test-ml   or   python -m scripts.demo_ml_summary
Requires: pip install -r requirements-ml.txt
"""

from __future__ import annotations

import asyncio
import sys

from app.worker.backends.transformer import TransformerSummarizer
from app.worker.ml_sample_text import ML_SAMPLE_TICKET_DESCRIPTION


async def _run() -> int:
    summarizer = TransformerSummarizer()
    if not summarizer.is_available():
        print(
            "ML dependencies not installed. Run: make install-ml",
            file=sys.stderr,
        )
        return 1

    text = ML_SAMPLE_TICKET_DESCRIPTION
    print("Loading DistilBART model (first run may download ~300 MB)...")
    summarizer.load_model()
    print("Summarizing sample ticket description...\n")

    summary = await summarizer.summarize(text)

    input_words = len(text.split())
    summary_words = len(summary.split())
    saved = len(text) - len(summary)
    pct = (saved / len(text)) * 100 if text else 0.0

    print("--- INPUT ---")
    print(text)
    print(f"\n({len(text)} chars, {input_words} words)")
    print("\n--- SUMMARY ---")
    print(summary)
    print(f"\n({len(summary)} chars, {summary_words} words)")
    print(f"\n--- SAVINGS ---")
    print(
        f"{saved} chars ({pct:.1f}% shorter), {input_words - summary_words} fewer words"
    )

    if not summary.strip() or len(summary) >= len(text):
        print(
            "\nUnexpected: summary is empty or not shorter than input.", file=sys.stderr
        )
        return 1

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
