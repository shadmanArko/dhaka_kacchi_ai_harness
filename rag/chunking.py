"""Split raw text into overlapping, token-measured chunks ready to embed.

Deliberately knows nothing about file formats (PDF/markdown/docx/etc.) -
that's the format-specific loader's job (a later step). This module only
ever sees plain text that's already been extracted from wherever it came
from, plus a source_path/source_type the caller already knows. See
RAG_progress.md decision #12 for the design reasoning, including two
corrections made while designing this with Ahmad - worth reading if this
file's shape looks surprising later.
"""

from __future__ import annotations

# `AutoTokenizer` loads just the TOKENIZER half of a Hugging Face model -
# the rules for splitting text into the same sub-word pieces the model
# was trained on - without loading the much heavier neural network weights
# that do the actual embedding. We only need to COUNT and SPLIT on tokens
# here, not embed anything yet, so this is the lighter, more efficient
# thing to import for this file specifically.
from transformers import AutoTokenizer

# The exact model name has to match embedding.py's - a different model
# would very likely split text into different token boundaries, since
# each model's tokenizer has its own vocabulary. Switched from
# all-MiniLM-L6-v2 to BAAI/bge-m3 (RAG_progress.md decision #17):
# MiniLM's tokenizer was trained almost entirely on English text and
# badly mangled real Bengali captions from the social_share corpus (a
# 1446-character caption produced 1013 tokens - about 1.4 characters per
# token, vs. ~4 for English). bge-m3's tokenizer has much broader
# multilingual vocabulary coverage and a far higher 8192-token limit.
EMBEDDING_MODEL_NAME = "BAAI/bge-m3"

# `AutoTokenizer.from_pretrained(...)` downloads (once, then caches
# locally) and loads the tokenizer's vocabulary/rules. This happens once,
# at import time, and the same tokenizer object is reused by every call to
# chunk_text() below - re-loading it on every call would be slow for no
# benefit, since the tokenizer's rules never change while the program runs.
_tokenizer = AutoTokenizer.from_pretrained(EMBEDDING_MODEL_NAME)

# Which sub-word marker convention this tokenizer uses, detected ONCE at
# import time by tokenizing a word guaranteed to split into several
# pieces, so the per-token word-boundary check further down stays a
# one-liner. Two conventions exist in the wild:
#   - bge-m3 (SentencePiece/XLM-R - what we use today) marks the START of
#     a word with a literal "▁" (U+2581) character; a token WITHOUT that
#     marker continues the previous word. Probing "internationalization"
#     gives pieces like "▁international", "ization".
#   - WordPiece models (the retired all-MiniLM-L6-v2) did the opposite:
#     continuations got a "##" prefix ("international", "##ization").
# Found 2026-10-02 (RAG_progress.md decision #34): the original check
# looked ONLY for "##", which after the bge-m3 switch silently never
# matched anything - so chunk windows could cut words mid-token again,
# the exact bug class this logic exists to prevent.
_probe_pieces = _tokenizer.convert_ids_to_tokens(
    _tokenizer("internationalization", add_special_tokens=False)["input_ids"]
)
_UNDERSCORE_MARKER_STYLE = any(piece.startswith("▁") for piece in _probe_pieces)


def _continues_a_word(token: str) -> bool:
    """True when `token` continues the previous word rather than starting
    a new one - i.e. a chunk edge landing here would cut a word in half.
    Correct for both marker conventions; which one applies was decided by
    the probe above, at import time.
    """
    if _UNDERSCORE_MARKER_STYLE:
        return not token.startswith("▁")
    return token.startswith("##")


def chunk_text(
    text: str,
    *,
    source_path: str,
    source_type: str,
    chunk_size_tokens: int,
    overlap_tokens: int,
) -> list[dict]:
    """Split `text` into overlapping chunks, each roughly
    `chunk_size_tokens` tokens long, with `overlap_tokens` tokens of
    overlap between consecutive chunks.

    Returns a list of dicts, one per chunk, each shaped to match the
    `chunks` table's columns directly:
        {"chunk_text": str, "source_path": str, "chunk_index": int,
         "source_type": str}

    `source_path` and `source_type` are supplied by the caller (the
    format-specific loader) rather than decided here - see decision #12's
    "correction 2" for why: this function has no way to know what kind of
    source it's looking at, and shouldn't need to (a database row has no
    file extension to inspect at all).
    """
    # Overlap has to be strictly smaller than the chunk size, or the
    # sliding window below would never move forward (or would even move
    # backward) - fail loudly here with a clear message instead of
    # silently looping forever or producing nonsense chunks.
    if overlap_tokens >= chunk_size_tokens:
        raise ValueError(
            f"overlap_tokens ({overlap_tokens}) must be smaller than "
            f"chunk_size_tokens ({chunk_size_tokens}), or chunks would "
            "never advance through the text."
        )

    # An empty source produces no chunks - nothing to split, nothing to
    # embed later, so return early rather than doing pointless work below.
    if not text.strip():
        return []

    # Run the tokenizer over the whole text once. Two things about the
    # arguments matter:
    #   - add_special_tokens=False: skip the [CLS]/[SEP]-style marker
    #     tokens a model normally wants at inference time - we're only
    #     using the tokenizer here to measure/split the CONTENT, not to
    #     prepare a real model input, so those markers would just be noise
    #     in our counting.
    #   - return_offsets_mapping=True: for every token, also return the
    #     (start_character, end_character) span in the ORIGINAL text that
    #     produced it. This is what lets us slice the exact original
    #     text back out for each chunk, instead of trying to reassemble
    #     text from tokens (which can subtly mangle spacing/punctuation
    #     for some tokenizers).
    encoding = _tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
    offsets = encoding["offset_mapping"]
    token_count = len(offsets)

    # Also get each token's own text form (e.g. "▁word", "ers", "the"),
    # not just its character span - the window edges below must not land
    # in the middle of a word, and only the token STRINGS say where words
    # begin (which marker means "word start" was decided once, above, via
    # _continues_a_word()).
    token_strs = _tokenizer.convert_ids_to_tokens(encoding["input_ids"])

    # How far the window slides forward for each new chunk. If chunk size
    # is 200 tokens and overlap is 50, each new chunk starts 150 tokens
    # after the previous one started - the last 50 tokens of one chunk are
    # the same as the first 50 tokens of the next, which is the actual
    # overlap.
    stride = chunk_size_tokens - overlap_tokens

    chunks: list[dict] = []
    # chunk_index counts chunks starting at 0, matching the `chunk_index`
    # column's role as "this chunk's position within its source."
    chunk_index = 0
    # window_start is the token position where the current chunk begins.
    window_start = 0

    while window_start < token_count:
        # The window covers tokens [window_start, window_end) - Python's
        # usual half-open range convention. min(...) caps it at the last
        # token so the final, possibly shorter, chunk doesn't run off the
        # end of the token list.
        window_end = min(window_start + chunk_size_tokens, token_count)

        # Snap the window's edges outward so neither edge lands in the
        # middle of a word the tokenizer split into pieces (e.g. stops a
        # window from starting on "ization" when it should really start
        # on "▁international" one token earlier - together they are the
        # whole word "internationalization"). This only WIDENS the window
        # slightly when needed; it never shrinks it, so chunk_size_tokens
        # is still an accurate lower bound on how big each chunk roughly
        # is. _continues_a_word() knows which marker to look for.
        snap_start = window_start
        while snap_start > 0 and _continues_a_word(token_strs[snap_start]):
            snap_start -= 1
        snap_end = window_end
        while snap_end < token_count and _continues_a_word(token_strs[snap_end]):
            snap_end += 1

        # offsets[snap_start] is (start_char, end_char) for the first
        # token in this (now word-boundary-safe) window; we only need its
        # start_char. Likewise offsets[snap_end - 1] is the window's last
        # token, and we only need its end_char. Together these give the
        # exact character span in the ORIGINAL text that this whole chunk
        # covers.
        start_char = offsets[snap_start][0]
        end_char = offsets[snap_end - 1][1]

        # Slice the original text (not a reconstruction from tokens) and
        # trim any leading/trailing whitespace the slice might have picked
        # up at its edges.
        chunk_str = text[start_char:end_char].strip()

        # Guard against an edge case: certain special/whitespace-only
        # tokens can produce an empty or whitespace-only slice - skip
        # adding a useless empty chunk to the output rather than storing
        # rows with nothing retrievable in them.
        if chunk_str:
            chunks.append(
                {
                    "chunk_text": chunk_str,
                    "source_path": source_path,
                    "chunk_index": chunk_index,
                    "source_type": source_type,
                }
            )
            chunk_index += 1

        # If this window already reached the end of the text, we're done -
        # stop here instead of sliding forward into an empty/duplicate
        # final window.
        if window_end == token_count:
            break

        # Slide the window forward by `stride` tokens for the next chunk.
        window_start += stride

    return chunks
