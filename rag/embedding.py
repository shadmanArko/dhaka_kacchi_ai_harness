"""Turn plain text into 1024-dimensional vectors using the local
BAAI/bge-m3 model.

Deliberately generic at its core (embed_texts works on any list of plain
strings) so the exact same function embeds chunk text at ingestion time
AND a query string at retrieval time - see RAG_progress.md decision #13
for the reasoning behind that split, designed together with Ahmad.
"""

from __future__ import annotations

# SentenceTransformer is the actual model class - unlike chunking.py's
# AutoTokenizer (which only loads the tokenizer half), this loads the full
# neural network weights, because this file's job is to actually PRODUCE
# vectors, not just count/split tokens.
from sentence_transformers import SentenceTransformer

# Same model name as chunking.py's EMBEDDING_MODEL_NAME - written as its
# own constant here (rather than imported from chunking.py) because these
# two files are allowed to evolve independently: chunking's tokenizer and
# embedding's model happen to come from the same underlying model today,
# but nothing forces them to stay coupled to each other's imports.
# Switched from all-MiniLM-L6-v2 (384-dim) to BAAI/bge-m3 (1024-dim) -
# see chunking.py's own comment and RAG_progress.md decision #17 for why:
# MiniLM badly mangled real Bengali captions from the social_share corpus.
# IMPORTANT: this dimension change means chunks.embedding must be
# vector(1024), not vector(384) - see schema.py. Any previously-ingested
# 384-dim rows are incompatible and must be cleared, not mixed in.
EMBEDDING_MODEL_NAME = "BAAI/bge-m3"

# Loaded once, at import time, and reused by every call to embed_texts()
# below - loading the model's weights from disk is comparatively slow, so
# doing it once per program run (not once per call) is what makes batching
# actually pay off.
_model = SentenceTransformer(EMBEDDING_MODEL_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of plain-text strings into 1024-dim vectors, in one
    pass rather than one-string-at-a-time - see RAG_progress.md decision
    #13 for why batching matters here specifically (GPU/CPU parallelism
    across the batch, not a style preference).

    Works for chunk text at ingestion time AND a raw query string at
    retrieval time (wrap a single query in a one-element list to reuse
    this same function there too) - this function has no idea which case
    it's being used for, on purpose.

    Returns one 1024-float list per input string, in the same order the
    inputs were given.
    """
    # An empty input list has nothing to embed - return early rather than
    # handing an empty batch to the model for no reason.
    if not texts:
        return []

    # `_model.encode(...)` is where the actual neural network runs: each
    # string in `texts` is tokenized (using the model's own tokenizer
    # internally - this is a different code path from chunking.py's
    # AutoTokenizer, even though it's the same underlying vocabulary) and
    # passed through the network in one batched pass, producing one vector
    # per input string.
    #   convert_to_numpy=True: get back a NumPy array (the library's
    #     native output format) rather than a PyTorch tensor - simpler to
    #     convert to plain Python lists next, and we don't need anything
    #     PyTorch-specific (like GPU-resident tensors) beyond this point.
    #   normalize_embeddings=True: scale every output vector to unit
    #     length (a "unit vector"). This matters because cosine similarity
    #     - the standard way to compare embeddings for closeness - is
    #     mathematically simpler and faster to compute correctly when
    #     vectors are already normalized; some vector-index implementations
    #     assume this and give wrong results otherwise.
    vectors = _model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)

    # `vectors` is currently one NumPy array of shape (num_texts, 1024).
    # `.tolist()` converts that into a plain nested Python list
    # (list[list[float]]) - the type this function promises to return, and
    # the type that's straightforward to hand to pgvector/psycopg later
    # without any NumPy-specific handling on the database side.
    return vectors.tolist()


def embed_chunks(chunks: list[dict]) -> list[dict]:
    """Take the list of chunk dicts produced by chunking.chunk_text(),
    embed all of their chunk_text values in one batched call, and return
    the same dicts with an "embedding" key added to each one.

    Thin wrapper, used only at ingestion time - retrieval-time query
    embedding calls embed_texts() directly instead, since a query isn't a
    chunk dict.
    """
    # Pull just the text out of every chunk dict, in order - this is the
    # list embed_texts() actually needs.
    texts = [chunk["chunk_text"] for chunk in chunks]

    # One batched call for the whole list of chunks, not one call per
    # chunk - this is the actual point of having embed_texts() take a
    # batch in the first place.
    vectors = embed_texts(texts)

    # zip() pairs up each original chunk dict with its corresponding
    # vector, in the same order - safe here specifically because
    # embed_texts() is documented to preserve input order, so chunks[i]
    # and vectors[i] really do correspond to the same piece of text.
    return [{**chunk, "embedding": vector} for chunk, vector in zip(chunks, vectors, strict=True)]
