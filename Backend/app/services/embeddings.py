
import logging
import random
import time

from openai import (
    OpenAI,
    OpenAIError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)

from app.services.parser import CodeChunk
from app.config import settings

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMENSIONS = 1536
BATCH_SIZE = 256

REQUEST_TIMEOUT = 30.0
MAX_ATTEMPTS = 5
INITIAL_BACKOFF = 1.0
BACKOFF_MULTIPLIER = 2.0
MAX_BACKOFF = 30.0

logger = logging.getLogger(__name__)

client = OpenAI(api_key=settings.openai_api_key)

_RETRYABLE_ERRORS: tuple[type[BaseException], ...] = (
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
)


class EmbeddingError(OpenAIError):
    """Deterministic failure surfaced by the embedding service.

    Raised after retries are exhausted or when a non-retryable OpenAI error
    occurs. Inherits from ``OpenAIError`` so existing callers that catch the
    OpenAI base error continue to work.
    """


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with retry, timeout, and contextual errors.

    Retries transient OpenAI failures (rate-limit/429, request timeouts,
    connection errors, and 5xx responses) with exponential backoff and a
    small random jitter. Raises :class:`EmbeddingError` once attempts are
    exhausted or when the underlying error is not retryable.
    """
    batch_size = len(texts)
    last_exc: BaseException | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.embeddings.create(
                input=texts,
                model=EMBED_MODEL,
                timeout=REQUEST_TIMEOUT,
            )
            return [item.embedding for item in response.data]
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
            if attempt >= MAX_ATTEMPTS:
                break
            backoff = min(
                MAX_BACKOFF,
                INITIAL_BACKOFF * (BACKOFF_MULTIPLIER ** (attempt - 1)),
            )
            sleep_for = backoff + random.uniform(0, backoff * 0.1)
            logger.warning(
                "embed_batch transient error (attempt %d/%d) "
                "model=%s batch_size=%d: %r; retrying in %.2fs",
                attempt,
                MAX_ATTEMPTS,
                EMBED_MODEL,
                batch_size,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)
        except OpenAIError as exc:
            logger.error(
                "embed_batch non-retryable OpenAI error "
                "model=%s batch_size=%d: %r",
                EMBED_MODEL,
                batch_size,
                exc,
            )
            raise EmbeddingError(
                f"Embedding request failed (model={EMBED_MODEL}, "
                f"batch_size={batch_size}): {exc!r}"
            ) from exc

    logger.error(
        "embed_batch exhausted retries model=%s batch_size=%d attempts=%d: %r",
        EMBED_MODEL,
        batch_size,
        MAX_ATTEMPTS,
        last_exc,
    )
    raise EmbeddingError(
        f"Embedding request failed after {MAX_ATTEMPTS} attempts "
        f"(model={EMBED_MODEL}, batch_size={batch_size}): {last_exc!r}"
    ) from last_exc


def embed_all(texts: list[str]) -> list[list[float]]:
    all_embeddings = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        all_embeddings.extend(embed_batch(batch))
    return all_embeddings



def build_embedding_text(chunk: CodeChunk, max_body_lines: int = 15) -> str:
    parts = []
    parts.append(chunk.signature)

    if chunk.docstring:
        parts.append(chunk.docstring)

    body_lines = chunk.source_code.split("\n")
    sig_line_count = chunk.signature.count("\n") + 1
    body_start = body_lines[sig_line_count:]

    if chunk.docstring:
        doc_line_count = chunk.docstring.count("\n") + 1
        body_start = body_start[doc_line_count:]

    body_preview = "\n".join(body_start[:max_body_lines])
    if body_preview.strip():
        parts.append(body_preview)

    return "\n".join(parts)
