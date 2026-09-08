"""PDF text extraction using pypdf."""
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def extract_text_from_pdf(pdf_path: str) -> tuple[str, int]:
    """
    Extract text from a PDF file.
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        Tuple of (extracted_text, page_count)
        
    Raises:
        FileNotFoundError: If PDF doesn't exist
        Exception: If extraction fails
    """
    try:
        from pypdf import PdfReader
        
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")
        
        reader = PdfReader(str(path))
        pages = []
        
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
        
        full_text = "\n\n".join(pages)
        page_count = len(reader.pages)
        
        logger.info(f"Extracted {page_count} pages from {pdf_path}")
        return full_text, page_count
        
    except Exception as e:
        logger.error(f"Failed to extract text from {pdf_path}: {e}")
        raise


def extract_pages_from_pdf(pdf_path: str) -> list[str]:
    """Extract text from a PDF file one page at a time.

    Args:
        pdf_path: Path to the PDF file

    Returns:
        List with one text entry per PDF page (empty string for pages
        without extractable text), so page numbers stay accurate.

    Raises:
        FileNotFoundError: If PDF doesn't exist
        Exception: If extraction fails
    """
    from pypdf import PdfReader

    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception as e:  # a broken page shouldn't kill the whole manual
            logger.warning(f"Failed to extract page {len(pages) + 1} of {pdf_path}: {e}")
            text = ""
        pages.append(text.strip())

    logger.info(f"Extracted {len(pages)} pages (per-page) from {pdf_path}")
    return pages


def split_text_into_chunks(text: str, max_chunk_size: int = 2000) -> list[str]:
    """
    Split text into manageable chunks for indexing.
    
    Args:
        text: Full text content
        max_chunk_size: Maximum characters per chunk
        
    Returns:
        List of text chunks
    """
    if len(text) <= max_chunk_size:
        return [text]
    
    chunks = []
    words = text.split()
    current_chunk = []
    current_length = 0
    
    for word in words:
        if current_length + len(word) > max_chunk_size and current_chunk:
            chunks.append(" ".join(current_chunk))
            current_chunk = [word]
            current_length = len(word)
        else:
            current_chunk.append(word)
            current_length += len(word) + 1
    
    if current_chunk:
        chunks.append(" ".join(current_chunk))
    
    return chunks
