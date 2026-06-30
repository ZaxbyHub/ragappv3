"""
Image search module for FR-009 (image ingestion).

Converts ImageProcessingResult (OCR text + metadata from image_processor)
into an embeddable text representation for vector search.

Usage:
    from app.services.image_search import build_searchable_text, embed_image_content, prepare_image_for_indexing, ImageIndexEntry

    # Build searchable text directly
    text = build_searchable_text(result, "photo.png")

    # Embed image content for vector search
    embedding = await embed_image_content(result, "photo.png", embedding_service)

    # Full pipeline: process image → build text → embed → return index entry
    entry = await prepare_image_for_indexing("/path/to/photo.png", "photo.png", embedding_service)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional

from app.services.image_processor import ImageProcessingResult, process_image

if TYPE_CHECKING:
    from app.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ImageIndexEntry:
    """
    Index entry produced by prepare_image_for_indexing.

    Attributes:
        text_representation: Human-readable text combining filename, OCR text,
            and structured metadata, suitable for embedding.
        embedding: Vector embedding of text_representation.
        metadata: Image metadata dict (width, height, format, mode).
        source_file_path: Absolute or relative path to the source image file.
        success: True when the full pipeline succeeded, False otherwise.
        error: Human-readable error message. None when success is True.
    """

    text_representation: str
    embedding: List[float]
    metadata: dict
    source_file_path: str
    success: bool
    error: Optional[str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_searchable_text(result: ImageProcessingResult, filename: str) -> str:
    """
    Build a single searchable text string from an ImageProcessingResult.

    Combines filename, OCR text, and structured metadata into a format suitable
    for embedding and keyword search. If OCR text is empty, still produces a
    representation from metadata so the image is findable by its properties.

    Args:
        result: ImageProcessingResult from process_image().
        filename: Original filename (e.g. "photo.png").

    Returns:
        A single string such as:
        "Image: photo.png, 1920x1080, PNG format. Extracted text: Receipt for..."

        or, when no OCR text is available:
        "Image: photo.png, 1920x1080, PNG format. No text extracted."

        or, when result is minimal / empty:
        "Image: photo.png. No metadata or text available."
    """
    parts: List[str] = [f"Image: {filename}"]

    # Add structured metadata when available
    if result.metadata:
        width = result.metadata.get("width")
        height = result.metadata.get("height")
        fmt = result.metadata.get("format", "unknown")
        mode = result.metadata.get("mode")

        dim_str = ""
        if width and height:
            dim_str = f"{width}x{height}"
        elif width:
            dim_str = str(width)
        elif height:
            dim_str = str(height)

        meta_parts: List[str] = []
        if dim_str:
            meta_parts.append(dim_str)
        meta_parts.append(f"{fmt} format")
        if mode:
            meta_parts.append(f"{mode} mode")

        parts.append(", ".join(meta_parts))

    # Add extracted text
    if result.extracted_text:
        text_preview = result.extracted_text.strip()
        parts.append(f"Extracted text: {text_preview}")
    else:
        parts.append("No text extracted.")

    return ". ".join(parts)


async def embed_image_content(
    result: ImageProcessingResult,
    filename: str,
    embedding_service: "EmbeddingService",
) -> List[float]:
    """
    Embed image content for vector search.

    Builds a searchable text representation and embeds it using the provided
    embedding service. The service's internal L1 (LRU) and L2 (Redis) caches
    are consulted automatically by embed_single.

    Args:
        result: ImageProcessingResult from process_image().
        filename: Original filename.
        embedding_service: An EmbeddingService instance (must have embed_single).

    Returns:
        Embedding vector as a list of floats.
    """
    text = build_searchable_text(result, filename)
    return await embedding_service.embed_single(text)


async def prepare_image_for_indexing(
    file_path: str,
    filename: str,
    embedding_service: "EmbeddingService",
) -> ImageIndexEntry:
    """
    Full pipeline: process image → build searchable text → embed → index entry.

    Error-tolerant: if process_image fails (file not found, corrupt image,
    missing dependencies) the function still returns an ImageIndexEntry with
    success=False and the error message populated, rather than raising.

    Args:
        file_path: Absolute or relative path to the image file.
        filename: Display filename for the search record.
        embedding_service: An EmbeddingService instance.

    Returns:
        An ImageIndexEntry ready for indexing into the vector store.
    """
    # Step 1: process the image
    result = await process_image(file_path)

    # Step 2: handle processing failure gracefully
    if not result.success:
        return ImageIndexEntry(
            text_representation="",
            embedding=[],
            metadata={},
            source_file_path=file_path,
            success=False,
            error=result.error or "Unknown image processing error",
        )

    # Step 3: build searchable text
    text_representation = build_searchable_text(result, filename)

    # Step 4: embed the text
    try:
        embedding = await embed_image_content(result, filename, embedding_service)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embedding failed for %s: %s", file_path, exc)
        return ImageIndexEntry(
            text_representation=text_representation,
            embedding=[],
            metadata=result.metadata,
            source_file_path=file_path,
            success=False,
            error=f"Embedding failed: {exc}",
        )

    # Step 5: return successful entry
    return ImageIndexEntry(
        text_representation=text_representation,
        embedding=embedding,
        metadata=result.metadata,
        source_file_path=file_path,
        success=True,
        error=None,
    )
