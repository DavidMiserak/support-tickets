"""TransformerSummarizer — DistilBART-CNN-6-6 via HuggingFace pipeline.

Runs synchronous CPU inference wrapped in run_in_executor so it does not
block the arq event loop. The model is loaded once at worker startup via
``load_model()``; per-job calls only invoke the already-loaded pipeline.
"""

import asyncio
import logging
import threading
from typing import Any, cast

logger = logging.getLogger(__name__)

_MODEL_NAME = "sshleifer/distilbart-cnn-6-6"


class TransformerSummarizer:
    """DistilBART summarizer with lazy model loading."""

    def __init__(self) -> None:
        self._pipe: Any = None
        self._inference_lock = threading.Lock()

    def is_available(self) -> bool:
        """True only when both torch and transformers are importable."""
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401

            return True
        except ImportError:
            logger.warning(
                "TransformerSummarizer unavailable: torch or transformers not installed. "
                "Install optional deps with: pip install -r requirements-ml.txt"
            )
            return False

    def load_model(self) -> None:
        """Load the HuggingFace pipeline. Called once in arq on_startup.

        Blocks for 1–5 s on first run (model weights already cached) or
        30–90 s on first run with no HuggingFace cache.
        """
        from transformers import pipeline

        logger.info("TransformerSummarizer: loading %s (CPU)", _MODEL_NAME)
        self._pipe = pipeline(
            "summarization",
            model=_MODEL_NAME,
            device=-1,  # CPU
        )
        logger.info("TransformerSummarizer: model loaded")

    async def summarize(self, text: str) -> str:
        """Run inference in a thread so the event loop stays unblocked."""
        if self._pipe is None:
            raise RuntimeError("TransformerSummarizer.load_model() was not called")

        loop = asyncio.get_running_loop()

        def _run_inference() -> list[dict[str, str]]:
            with self._inference_lock:
                return cast(
                    list[dict[str, str]],
                    self._pipe(
                        text,
                        max_length=130,
                        min_length=10,
                        truncation=True,
                    ),
                )

        result = await loop.run_in_executor(None, _run_inference)
        return result[0]["summary_text"]
