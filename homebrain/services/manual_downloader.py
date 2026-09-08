"""Manual downloader using DuckDuckGo search."""
import logging
import time
from pathlib import Path
from typing import Optional, List, Callable, Any
import requests
from functools import lru_cache
import asyncio

logger = logging.getLogger(__name__)


@lru_cache(maxsize=128)
def _cached_search(query: str, max_results: int) -> tuple[List[dict], bool]:
    """Cached search to avoid repeated identical searches."""
    results, error = _perform_search(query, max_results)
    return results, error is None


def _perform_search(query: str, max_results: int) -> tuple[List[dict], Optional[str]]:
    """Perform actual DuckDuckGo search with multiple fallback strategies."""
    from duckduckgo_search import DDGS
    
    # Strategy 1: Use HTML backend (more reliable than lite)
    try:
        logger.info(f"Attempting search with HTML backend: {query}")
        results = []
        
        with DDGS() as ddgs:
            # Specify backend to avoid lite backend rate limiting
            file_results = list(ddgs.text(
                query, 
                max_results=max_results,
                backend="html"  # Use HTML backend instead of default lite
            ))
            
            for result in file_results:
                url = result.get('href', '') or result.get('url', '')
                title = result.get('title', '')
                
                # Filter for PDF links
                if '.pdf' in url.lower() or 'pdf' in title.lower():
                    results.append({
                        'title': title,
                        'url': url,
                        'source': 'duckduckgo'
                    })
        
        if results:
            logger.info(f"HTML backend found {len(results)} potential manuals")
            return results, None
            
    except Exception as e:
        logger.warning(f"HTML backend failed: {e}")
    
    # Strategy 2: Fallback to API backend with delays
    try:
        logger.info("Falling back to API backend with rate limiting protection")
        results = []
        
        with DDGS() as ddgs:
            # Add small delay between requests to avoid rate limiting
            time.sleep(1)  # Be polite to the service
            file_results = list(ddgs.text(
                query,
                max_results=max_results,
                backend="api"  # Try API backend
            ))
            
            for result in file_results:
                url = result.get('href', '') or result.get('url', '')
                title = result.get('title', '')
                
                if '.pdf' in url.lower() or 'pdf' in title.lower():
                    results.append({
                        'title': title,
                        'url': url,
                        'source': 'duckduckgo'
                    })
        
        if results:
            logger.info(f"API backend found {len(results)} potential manuals")
            return results, None
            
    except Exception as e:
        logger.warning(f"API backend failed: {e}")
    
    # Strategy 3: Direct requests with proper headers
    logger.info("Using direct HTTP requests as final fallback")
    return _direct_duckduckgo_search(query, max_results)


def search_for_manuals(brand: str, model: str, max_results: int = 10) -> tuple[List[dict], str | None]:
    """
    Search for device manuals using DuckDuckGo with multiple fallback strategies.
    
    Args:
        brand: Device brand (e.g., "Samsung")
        model: Device model (e.g., "QN90A")
        max_results: Maximum number of results to return
        
    Returns:
        Tuple of (List of search result dictionaries, error_message or None)
    """
    try:
        query = f"{brand} {model} manual pdf"
        logger.info(f"Searching for manuals: {query}")
        
        # Use cached search to avoid repeated identical queries
        results, success = _cached_search(query, max_results)
        
        if not success and not results:
            # Clear cache and retry if we got cached failure
            _cached_search.cache_clear()
            results, _ = _cached_search(query, max_results)
        
        logger.info(f"Found {len(results)} potential manuals")
        
        if not results:
            return [], "No manuals found. Try a different brand/model or search terms."
            
        return results, None
        
    except Exception as e:
        error_detail = f"Search failed: {str(e)}. Please try again later."
        logger.error(f"Search failed: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return [], error_detail


def _direct_duckduckgo_search(query: str, max_results: int) -> tuple[List[dict], Optional[str]]:
    """
    Direct DuckDuckGo search using HTTP requests with proper headers.
    
    This is the most reliable fallback when the library fails.
    """
    import urllib.parse
    
    encoded_query = urllib.parse.quote(query)
    url = f"https://duckduckgo.com/html/?q={encoded_query}"
    
    # Mimic a real browser to avoid blocking
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        # Simple HTML parsing for PDF links
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        
        results = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            # Skip internal DuckDuckGo links
            if href.startswith('/') or href.startswith('#'):
                continue
                
            title = link.get_text(strip=True)
            
            # Look for PDF links or pages mentioning PDF
            if '.pdf' in href.lower() or 'pdf' in title.lower():
                results.append({
                    'title': title[:100],
                    'url': href,
                    'source': 'duckduckgo-direct'
                })
                
                if len(results) >= max_results:
                    break
        
        # Add delay to be respectful
        time.sleep(2)
        
        logger.info(f"Direct search found {len(results)} results")
        return results, None if results else "Direct search completed but no PDFs found"
        
    except Exception as e:
        logger.error(f"Direct search failed: {e}")
        return [], f"Direct search failed: {str(e)}"
        
    except Exception as e:
        logger.error(f"Fallback search failed: {e}")
        return []


def download_pdf(url: str, save_path: Path, timeout: int = 30) -> bool:
    """
    Download a PDF file from URL.
    
    Args:
        url: PDF download URL
        save_path: Local path to save the PDF
        timeout: Request timeout in seconds
        
    Returns:
        True if download successful, False otherwise
    """
    try:
        logger.info(f"Downloading PDF from {url}")
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        response = requests.get(url, headers=headers, stream=True, timeout=timeout)
        response.raise_for_status()
        
        # Check if it's actually a PDF
        content_type = response.headers.get('content-type', '')
        if 'pdf' not in content_type.lower():
            # Try to check file extension
            if not url.lower().endswith('.pdf'):
                logger.warning(f"Content may not be a PDF: {url}")
        
        # Save with streaming for large files
        save_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(save_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        
        file_size = save_path.stat().st_size
        logger.info(f"Downloaded {file_size} bytes to {save_path}")
        
        # Validate it's a valid PDF
        with open(save_path, 'rb') as f:
            header = f.read(8)
            if not header.startswith(b'%PDF'):
                logger.warning(f"File may not be a valid PDF: {save_path}")
                return False
        
        return True
        
    except Exception as e:
        logger.error(f"Download failed for {url}: {e}")
        # Clean up partial file
        if save_path.exists():
            save_path.unlink()
        return False


def download_manuals_for_device(
    brand: str, 
    model: str, 
    device_dir: Path,
    max_downloads: int = 5,
    progress_callback: Optional[Callable[[str], None]] = None
) -> tuple[int, List[str], str | None]:
    """
    Download manuals for a specific device.
    
    Args:
        brand: Device brand
        model: Device model
        device_dir: Directory to save PDFs
        max_downloads: Maximum number of PDFs to download
        progress_callback: Optional callback function that receives progress messages
        
    Returns:
        Tuple of (downloaded_count, list_of_filenames, error_message or None)
    """
    device_dir.mkdir(parents=True, exist_ok=True)
    
    # Search for manuals (now returns tuple of results, error)
    if progress_callback:
        progress_callback(f"Searching for manuals: {brand} {model} manual pdf")
    
    results, search_error = search_for_manuals(brand, model, max_results=15)
    
    if not results:
        error_detail = search_error or f"No manuals found for {brand} {model}"
        logger.info(f"No manuals found for {brand} {model}")
        if progress_callback:
            progress_callback(f"❌ No manuals found: {error_detail}")
        return 0, [], error_detail
    
    if progress_callback:
        progress_callback(f"✅ Found {len(results)} potential manuals")
    
    downloaded = []
    failed = []
    
    # Download up to max_downloads PDFs
    for i, result in enumerate(results[:max_downloads]):
        filename = f"{brand}_{model}_manual_{i+1}.pdf"
        save_path = device_dir / filename
        
        if progress_callback:
            progress_callback(f"📥 Downloading: {result.get('title', 'Unknown')} ({i+1}/{len(results[:max_downloads])})")
        
        if download_pdf(result['url'], save_path):
            downloaded.append(filename)
            if progress_callback:
                progress_callback(f"✅ Downloaded: {filename}")
        else:
            failed.append(filename)
            if progress_callback:
                progress_callback(f"❌ Failed to download: {filename}")
    
    logger.info(f"Downloaded {len(downloaded)} manuals, failed {len(failed)}")
    return len(downloaded), downloaded


async def download_manuals_for_device_with_progress(
    brand: str, 
    model: str, 
    device_dir: Path,
    max_downloads: int = 5
) -> tuple[int, List[str], str | None]:
    """
    Async version of download with progress updates.
    This wraps the sync version and adds async support for streaming.
    
    Args:
        brand: Device brand
        model: Device model
        device_dir: Directory to save PDFs
        max_downloads: Maximum number of PDFs to download
        
    Returns:
        Tuple of (downloaded_count, list_of_filenames, error_message or None)
    """
    # Run in executor to avoid blocking
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: download_manuals_for_device(brand, model, device_dir, max_downloads)
    )
    return result


async def download_manuals_for_device_with_callbacks(
    brand: str, 
    model: str, 
    device_dir: Path,
    max_downloads: int = 5,
    progress_callback: Optional[Callable[[str], None]] = None
) -> tuple[int, List[str], str | None]:
    """
    Download manuals with progress callbacks for streaming.
    
    Args:
        brand: Device brand
        model: Device model
        device_dir: Directory to save PDFs
        max_downloads: Maximum number of PDFs to download
        progress_callback: Callback function that receives progress messages
        
    Returns:
        Tuple of (downloaded_count, list_of_filenames, error_message or None)
    """
    device_dir.mkdir(parents=True, exist_ok=True)
    
    # Send search start message
    if progress_callback:
        try:
            progress_callback('data: {"type": "step", "message": "Searching for ' + brand + ' ' + model + ' manuals on DuckDuckGo...", "status": "searching"}\n\n')
        except:
            pass
    
    # Search for manuals
    results, search_error = search_for_manuals(brand, model, max_results=15)
    
    if not results:
        error_detail = search_error or f"No manuals found for {brand} {model}"
        logger.info(f"No manuals found for {brand} {model}")
        if progress_callback:
            try:
                progress_callback('data: {"type": "step", "message": "No manuals found: ' + error_detail + '", "status": "error"}\n\n')
            except:
                pass
        return 0, [], error_detail
    
    # Send found message
    if progress_callback:
        try:
            progress_callback('data: {"type": "step", "message": "Found ' + str(len(results)) + ' potential manuals", "status": "found"}\n\n')
        except:
            pass
    
    downloaded = []
    failed = []
    
    # Download up to max_downloads PDFs
    for i, result in enumerate(results[:max_downloads]):
        filename = f"{brand}_{model}_manual_{i+1}.pdf"
        save_path = device_dir / filename
        
        title = result.get('title', 'Unknown')
        if progress_callback:
            try:
                progress_callback('data: {"type": "step", "message": "Downloading: ' + title + ' (' + str(i+1) + '/' + str(len(results[:max_downloads])) + ')", "status": "downloading"}\n\n')
            except:
                pass
        
        if download_pdf(result['url'], save_path):
            downloaded.append(filename)
            if progress_callback:
                try:
                    progress_callback('data: {"type": "step", "message": "Downloaded: ' + filename + '", "status": "success"}\n\n')
                except:
                    pass
        else:
            failed.append(filename)
            if progress_callback:
                try:
                    progress_callback('data: {"type": "step", "message": "Failed to download: ' + filename + '", "status": "error"}\n\n')
                except:
                    pass
    
    logger.info(f"Downloaded {len(downloaded)} manuals, failed {len(failed)}")
    return len(downloaded), downloaded
