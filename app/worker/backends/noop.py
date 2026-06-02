"""NoopSummarizer — a stub backend for testing and default compose setup."""


class NoopSummarizer:
    """Always available; returns a predictable stub string.

    Used as the test default (SUMMARIZER_BACKEND=noop) and as the compose
    stack default so ``docker compose up`` works without a model download.
    """

    def is_available(self) -> bool:
        return True

    async def summarize(self, text: str) -> str:
        preview = text[:80].replace("\n", " ")
        return f"[noop summary] {preview}"
