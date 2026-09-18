"""Local text embeddings, via FastEmbed on the CPU. No API key, no network at query time.

The model is `BAAI/bge-small-en-v1.5`: 384 dimensions, 512 tokens, English, and the default
model FastEmbed ships with. `query_embed` and `passage_embed` apply its instruction prefix for
us — queries get `Represent this sentence for searching relevant passages:`, passages get
nothing, which is what the model card specifies.

**Nothing happens at import.** The model is loaded on first use, because an API that
downloaded ~130 MB of weights while starting would make `/health` depend on the network.

**Token counting is deliberately not done with the model's own tokenizer.** FastEmbed
configures it to truncate at 512, so asking it how long a 900-token passage is returns 512 —
a truthful-looking number that would make the chunker's limit unenforceable. `count_tokens`
therefore uses a *second* tokenizer built from the same file with truncation switched off. The
model keeps its truncation as a safety net; the counter tells the truth.

**Passages and queries are embedded with different methods on purpose.** Using one for the
other degrades retrieval, and the difference is invisible in the output, so it is worth being
explicit about which is which.
"""

from collections.abc import Iterable
from typing import Any

from tokenizers import Tokenizer

# The model this project indexes and searches with. Recorded on every passage, so an index
# built with a different model is detectable rather than silently mixed.
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIMENSION = 384
EMBEDDING_MAX_TOKENS = 512


class EmbeddingError(Exception):
    """Base class for every failure this module reports."""


class EmbeddingUnavailableError(EmbeddingError):
    """The model could not be loaded or could not produce a vector.

    A separate type because "the model is not available" and "the search found nothing" are
    different answers, and a caller that cannot tell them apart will report the wrong one.
    """


class Embedder:
    """A lazily-loaded embedding model, plus a truthful token counter.

    One instance per process is enough and is what `get_embedder` hands out. Constructing it
    downloads nothing; the first call that needs a vector does.
    """

    def __init__(
        self, *, model_name: str = EMBEDDING_MODEL, cache_dir: str | None = None
    ) -> None:
        if model_name != EMBEDDING_MODEL:
            # Refusing rather than warning. The vector size, the stored `embedding_model`
            # metadata and the chunker's 512-token limit are all tied to this model, and an
            # index built with a different one would be indistinguishable at 384 dimensions.
            raise EmbeddingError(
                f"this build indexes with {EMBEDDING_MODEL!r}; {model_name!r} is not "
                "supported. Switching models needs a re-index, not a parameter."
            )
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: Any = None
        self._counter: Tokenizer | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    def _load(self) -> Any:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:  # pragma: no cover - depends on the image
                raise EmbeddingUnavailableError(
                    "fastembed is not installed, so no passage can be embedded"
                ) from exc

            try:
                self._model = TextEmbedding(
                    model_name=self._model_name, cache_dir=self._cache_dir
                )
            except Exception as exc:
                # Download failures, an unwritable cache, a missing volume. Reported as this
                # module's own error rather than letting a raw exception escape.
                raise EmbeddingUnavailableError(
                    f"could not load {self._model_name}: {type(exc).__name__}. The first "
                    "run downloads the weights, so this needs network access once, and "
                    f"writes them to {self._cache_dir or 'the default cache'}."
                ) from exc
        return self._model

    def _count_with(self) -> Tokenizer:
        """A non-truncating copy of the model's tokenizer, built once.

        `to_str()` round-trips the tokenizer including its truncation setting, so the
        truncation has to be switched off explicitly afterwards. Without that this counter
        would report 512 for everything longer than 512 and the chunker would believe it.
        """
        if self._counter is None:
            model = self._load()
            source = getattr(getattr(model, "model", None), "tokenizer", None)
            if source is None:
                raise EmbeddingUnavailableError(
                    "the loaded model exposes no tokenizer, so passage length cannot be "
                    "measured and the 512-token limit cannot be enforced"
                )
            counter = Tokenizer.from_str(source.to_str())
            counter.no_truncation()
            self._counter = counter
        return self._counter

    def count_tokens(self, text: str) -> int:
        """Tokens the model will receive for `text`, special tokens included.

        Not truncated. This is the number the chunker's limit is checked against.
        """
        return len(self._count_with().encode(text).ids)

    def embed_passages(self, texts: Iterable[str]) -> list[list[float]]:
        """Embed documents for storage."""
        return self._embed(list(texts), query=False)

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query.

        A different method, not a different string: the model's instruction prefix belongs on
        queries only, and `query_embed` is what applies it.
        """
        vectors = self._embed([text], query=True)
        if not vectors:
            raise EmbeddingUnavailableError("the model returned no vector for the query")
        return vectors[0]

    def _embed(self, texts: list[str], *, query: bool) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        try:
            produced = model.query_embed(texts) if query else model.passage_embed(texts)
            vectors = [[float(value) for value in vector] for vector in produced]
        except Exception as exc:
            raise EmbeddingUnavailableError(
                f"{self._model_name} could not embed: {type(exc).__name__}"
            ) from exc

        for vector in vectors:
            if len(vector) != EMBEDDING_DIMENSION:
                raise EmbeddingUnavailableError(
                    f"the model produced a {len(vector)}-dimensional vector where "
                    f"{EMBEDDING_DIMENSION} was expected; the index expects "
                    f"{EMBEDDING_DIMENSION}"
                )
        return vectors


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """The process-wide embedder, configured from settings on first use.

    Importing `app.config` happens here rather than at module import so this module can be
    imported, and the counter used, without a database URL in the environment.
    """
    global _embedder
    if _embedder is None:
        from app.config import settings

        _embedder = Embedder(cache_dir=settings.fastembed_cache_path)
    return _embedder
