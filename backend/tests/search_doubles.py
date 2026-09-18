"""Doubles for the search tests: a deterministic counter and a deterministic embedder.

The suite runs offline, so nothing here loads a model or downloads weights. What it must not
do is stand in for the *code under test*: the collection is still a real Qdrant (in-process),
the database is still real PostgreSQL, and the collection validation still compares against
384 dimensions. Only the model is replaced.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import hashlib
import math

# The real model's dimension. The double produces vectors of this shape rather than a
# convenient one, because "does the collection validate against what we write" is one of the
# things these tests exist to check.
VECTOR_SIZE = 384

# What the real tokenizer adds around every input: [CLS] and [SEP].
_SPECIAL_TOKENS = 2


class WordTokenCounter:
    """One token per whitespace-separated word, plus the two special tokens.

    Deterministic and model-free, so a chunk-size assertion means the same thing on every
    machine. The counts are not the real tokenizer's, and nothing here claims they are — the
    real counts are checked in the integration run.
    """

    def __init__(self, tokens_per_word: int = 1) -> None:
        self._per_word = tokens_per_word

    def count_tokens(self, text: str) -> int:
        return len(text.split()) * self._per_word + _SPECIAL_TOKENS


class StubEmbedder:
    """A hashing bag-of-words embedder: real vectors, no model.

    Cosine similarity between two of its vectors reflects how many words they share. That is
    enough for the tests to be meaningful — a query about export controls really does retrieve
    passages that mention export controls — while staying deterministic and instant.

    It is not a semantic model, and no test here asserts anything that would need one.
    """

    def __init__(self, *, model_name: str = "stub/hashing-embedder") -> None:
        self._model_name = model_name
        self._counter = WordTokenCounter()

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return VECTOR_SIZE

    def count_tokens(self, text: str) -> int:
        return self._counter.count_tokens(text)

    def embed_passages(self, texts) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * VECTOR_SIZE
        for word in text.lower().split():
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % VECTOR_SIZE] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            # An empty text still needs a vector of the right shape; an all-zero one is
            # rejected by cosine distance, so this stands in as a unit vector.
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


class FailingEmbedder(StubEmbedder):
    """An embedder whose model cannot be used, for testing that path."""

    def embed_query(self, text: str) -> list[float]:
        from app.embeddings import EmbeddingUnavailableError

        raise EmbeddingUnavailableError("the model is unavailable in this test")
