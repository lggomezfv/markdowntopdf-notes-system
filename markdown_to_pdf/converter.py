#!/usr/bin/env python3
"""
Markdown to PDF converter using Puppeteer approach (inspired by vscode-markdown-pdf).
This uses Playwright (Python equivalent of Puppeteer) for better PDF generation control.

MIT License - Copyright (c) 2025 Markdown to PDF Converter
"""

import os
import sys
import subprocess
import tempfile
import shutil
import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
import asyncio
from playwright.async_api import async_playwright
import plantuml
from colorama import init, Fore, Back, Style
from PIL import Image, ImageFilter
from .verification import DocumentStateManager, calculate_file_hash
from .config import Config
from .dependencies import check_dependencies
from concurrent.futures import ProcessPoolExecutor, as_completed
import threading
import multiprocessing
from tqdm import tqdm

# --- Module-level worker infrastructure for ProcessPoolExecutor ---
# ProcessPoolExecutor requires picklable callables. We use a module-level
# converter instance per worker process so each gets its own browser, event
# loop, and DB connection — fully isolated from other workers.
_worker_converter = None


def _init_worker_process(converter_kwargs: dict) -> None:
    """Initializer called once per worker process. Creates a process-local converter."""
    global _worker_converter
    _worker_converter = MarkdownToPDFConverter(**converter_kwargs)


def _worker_convert_file(md_file: Path) -> tuple:
    """Top-level function executed in worker process. Converts a single file."""
    return _worker_converter._convert_single_file(md_file)


# Initialize colorama for cross-platform colored output
init(autoreset=True)


class MarkdownToPDFConverter:
    """Markdown to PDF converter using Playwright (Puppeteer approach)."""
    
    # Style profiles configuration
    STYLE_PROFILES = {
        "a4-print": {
            "name": "A4 Print (Default)",
            "description": "Standard print-optimized styling with 12px base font",
            "font_scale": 1.0,
            "base_font_size": "12px"
        },
        "a4-screen": {
            "name": "A4 Screen (Large)",
            "description": "Screen-optimized styling with 30% larger fonts for better readability",
            "font_scale": 1.3,
            "base_font_size": "15.6px"
        }
    }
    
    def __init__(self, source_dir: str, output_dir: str, temp_dir: str, page_margins: str = "1in 0.75in", debug: bool = False, db_path: Optional[str] = None, style_profile: str = "a4-print", max_workers: int = 4, max_diagram_width = 1680, max_diagram_height = 2240, force_regenerate: bool = False, save_html: bool = False, save_html_bundle: bool = False):
        """Initialize the converter.
        
        Args:
            max_diagram_width: Max width in pixels (int, only resize if rendered exceeds) or percentage of rendered size (str like "80%")
            max_diagram_height: Max height in pixels (int, only resize if rendered exceeds) or percentage of rendered size (str like "80%")
            force_regenerate: If True, bypass verification and regenerate all files
            save_html: If True, save the intermediate HTML alongside the PDF
            save_html_bundle: If True, save HTML with external image assets in output/html/{stem}/
        """
        self.source_dir = Path(source_dir)
        self.output_dir = Path(output_dir)
        self.temp_dir = Path(temp_dir)
        self.page_margins = page_margins
        self.debug = debug
        self.style_profile = style_profile
        self.max_workers = max_workers
        self.diagram_width = max_diagram_width
        self.diagram_height = max_diagram_height
        self.force_regenerate = force_regenerate
        self.save_html = save_html
        self.save_html_bundle = save_html_bundle
        self._lock = threading.Lock()  # For logging in sequential mode
        
        # Browser and event loop state (process-isolated; each worker process gets its own converter)
        self._local = type('BrowserState', (), {})()
        
        # Performance optimization: reuse PlantUML client for connection pooling
        # Get PlantUML server URL from config (supports local or external server)
        from .config import Config
        config = Config()
        self._plantuml_server = config.get_plantuml_server()
        self._plantuml_client = plantuml.PlantUML(url=self._plantuml_server)
        self._log_debug(f"Using PlantUML server: {self._plantuml_server}")
        
        # Create format-specific output directories
        self.pdf_dir = self.output_dir / "pdf"
        self.html_dir = self.output_dir / "html" if self.save_html else None
        
        # Validate style profile
        if style_profile not in self.STYLE_PROFILES:
            available_profiles = ", ".join(self.STYLE_PROFILES.keys())
            raise ValueError(f"Invalid style profile '{style_profile}'. Available profiles: {available_profiles}")
        
        # Initialize document state manager with configurable db_path
        if db_path is None:
            config = Config()
            db_path = config.get_db_path()
        self.state_manager = DocumentStateManager(db_path)
        
        # Create directories
        self.output_dir.mkdir(exist_ok=True)
        self.pdf_dir.mkdir(exist_ok=True)
        if self.html_dir:
            self.html_dir.mkdir(exist_ok=True)
        self.temp_dir.mkdir(exist_ok=True)
        
        # Log style profile information
        profile_info = self.STYLE_PROFILES[self.style_profile]
        self._log_info(f"Using style profile: {profile_info['name']} - {profile_info['description']}")
        self._log_debug(f"Default diagram dimensions: {self.diagram_width}x{self.diagram_height}px")
    
    def _log_debug(self, message: str) -> None:
        """Log debug message with color (only if debug mode is enabled)."""
        if self.debug:
            with self._lock:
                print(f"{Fore.CYAN}[DEBUG]{Style.RESET_ALL} {message}")
    
    def _log_info(self, message: str) -> None:
        """Log info message with color."""
        with self._lock:
            print(f"{Fore.GREEN}[INFO]{Style.RESET_ALL} {message}")
    
    def _log_warning(self, message: str) -> None:
        """Log warning message with color."""
        with self._lock:
            print(f"{Fore.YELLOW}[WARNING]{Style.RESET_ALL} {message}")
    
    def _log_error(self, message: str) -> None:
        """Log error message with color."""
        with self._lock:
            print(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {message}")
    
    def _log_success(self, message: str) -> None:
        """Log success message with color."""
        with self._lock:
            print(f"{Fore.GREEN}[OK]{Style.RESET_ALL} {message}")
    
    async def _launch_browser(self) -> None:
        """Launch a fresh Chromium browser instance."""
        tls = self._local
        if not hasattr(tls, 'playwright') or tls.playwright is None:
            tls.playwright = await async_playwright().start()
        tls.browser = await tls.playwright.chromium.launch(
            headless=True,
            args=[
                '--disable-dev-shm-usage',  # Use /tmp instead of /dev/shm (prevents OOM crashes)
                '--disable-gpu',             # No GPU in headless mode
                '--no-sandbox',              # Required in some environments
            ]
        )
    
    async def _ensure_browser(self) -> None:
        """Ensure browser instance is initialized and ready. Reuses existing browser if available."""
        tls = self._local
        
        if not hasattr(tls, 'browser') or tls.browser is None or not tls.browser.is_connected():
            self._log_debug("Initializing browser instance")
            await self._launch_browser()
        
        # Create a new page, restarting the browser if it turns out to be dead
        if hasattr(tls, 'page') and tls.page and not tls.page.is_closed():
            try:
                await tls.page.close()
            except Exception:
                pass
        try:
            tls.page = await tls.browser.new_page()
        except Exception:
            # Browser reported connected but is actually dead — restart it
            self._log_warning("Browser connection stale, restarting...")
            await self._close_browser()
            await self._launch_browser()
            tls.page = await tls.browser.new_page()
    
    async def _close_browser(self) -> None:
        """Close browser and cleanup resources."""
        tls = self._local
        
        # Grab references and null them out first to prevent double-close on crash
        page = getattr(tls, 'page', None)
        browser = getattr(tls, 'browser', None)
        pw = getattr(tls, 'playwright', None)
        tls.page = None
        tls.browser = None
        tls.playwright = None
        
        try:
            if page and not page.is_closed():
                await page.close()
        except Exception:
            pass
        try:
            if browser and browser.is_connected():
                await browser.close()
        except Exception:
            pass
        try:
            if pw:
                await pw.stop()
        except Exception:
            pass
        
        self._log_debug("Browser instance closed and cleaned up")
    
    def _validate_margin(self, margin_str: str) -> str:
        """Validate and normalize a single margin value."""
        import re
        
        # Extract numeric value and unit
        match = re.match(r'^(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(cm|in|mm|pt|px)?$', margin_str.strip())
        if not match:
            raise ValueError(f"Invalid margin format: '{margin_str}'. Use format like '1in', '2.5cm', '10mm', etc.")
        
        value_str, unit = match.groups()
        value = float(value_str)
        
        # Set default unit to 'in' if not specified
        if not unit:
            unit = 'in'
        
        # Convert to inches for validation
        if unit == 'cm':
            value_inches = value / 2.54
        elif unit == 'mm':
            value_inches = value / 25.4
        elif unit == 'pt':
            value_inches = value / 72
        elif unit == 'px':
            value_inches = value / 96  # Assuming 96 DPI
        else:  # 'in'
            value_inches = value
        
        # Validate range: minimum 0 inches, maximum 3 inches
        if value_inches < 0:
            raise ValueError(f"Margin cannot be negative: '{margin_str}'. Minimum value is 0.")
        elif value_inches > 3:
            raise ValueError(f"Margin too large: '{margin_str}'. Maximum value is 3 inches (7.62cm).")
        
        return f"{value}{unit}"
    
    def _parse_margins(self) -> Dict[str, str]:
        """Parse margin string into individual margin values."""
        margin_parts = self.page_margins.split()
        
        if len(margin_parts) == 1:
            # All margins same
            margin = self._validate_margin(margin_parts[0])
            return {'top': margin, 'right': margin, 'bottom': margin, 'left': margin}
        elif len(margin_parts) == 2:
            # Vertical and horizontal
            vertical = self._validate_margin(margin_parts[0])
            horizontal = self._validate_margin(margin_parts[1])
            return {'top': vertical, 'right': horizontal, 'bottom': vertical, 'left': horizontal}
        elif len(margin_parts) == 4:
            # Top, right, bottom, left
            return {
                'top': self._validate_margin(margin_parts[0]),
                'right': self._validate_margin(margin_parts[1]),
                'bottom': self._validate_margin(margin_parts[2]),
                'left': self._validate_margin(margin_parts[3])
            }
        else:
            raise ValueError(f"Invalid margin format: '{self.page_margins}'. Use 1, 2, or 4 values.")
    
    def _convert_margin_to_cm(self, margin_str: str) -> float:
        """Convert margin string to centimeters for Puppeteer."""
        import re
        
        match = re.match(r'^(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(cm|in|mm|pt|px)?$', margin_str.strip())
        if not match:
            return 2.54  # default 1 inch in cm
        
        value_str, unit = match.groups()
        value = float(value_str)
        
        if not unit:
            unit = 'in'
        
        # Convert to cm
        if unit == 'cm':
            return value
        elif unit == 'in':
            return value * 2.54
        elif unit == 'mm':
            return value / 10
        elif unit == 'pt':
            return value * 0.0352778
        elif unit == 'px':
            return value * 0.0264583
        else:
            return value * 2.54
    
    def _get_viewport_dimensions(self) -> tuple[int, int]:
        """Get viewport dimensions for diagram rendering (always integers).
        
        The viewport is set larger than the target output dimensions so diagrams
        render at their natural size with full detail.  The final image size is
        then determined by _fit_diagram_to_page_width and scale annotations.
        
        Returns:
            Tuple of (width, height) in pixels for viewport size
        """
        # Use 2x the target max dimensions so diagrams have room to
        # lay out at full fidelity before being fit to page width.
        default_width = 3360
        default_height = 4480
        
        if isinstance(self.diagram_width, int):
            width = self.diagram_width * 2
        else:
            width = default_width
            
        if isinstance(self.diagram_height, int):
            height = self.diagram_height * 2
        else:
            height = default_height
            
        return width, height
    
    def _get_page_width_px(self) -> int:
        """Get the target page content width in pixels for diagram sizing.
        
        Returns self.diagram_width if it's an integer, otherwise 1680 as default.
        """
        if isinstance(self.diagram_width, int):
            return self.diagram_width
        return 1680
    
    def _fit_diagram_to_page_width(self, image_path: Path, scale_percent: float = 100.0) -> bool:
        """Resize a diagram image so its width equals page_width * scale_percent / 100.
        
        Unlike _resize_image (which only shrinks), this always resizes — up or down —
        so the diagram fills the intended portion of the page width.
        
        Args:
            image_path: Path to the rendered diagram PNG
            scale_percent: Target width as percentage of page width (100 = full width)
            
        Returns:
            True if successful
        """
        try:
            target_width = int(self._get_page_width_px() * scale_percent / 100.0)
            
            with Image.open(image_path) as img:
                if img.mode not in ('RGB', 'RGBA'):
                    if img.mode in ('P', 'PA', 'LA') and 'transparency' in img.info:
                        img = img.convert('RGBA')
                    else:
                        img = img.convert('RGB')
                
                orig_width, orig_height = img.size
                
                if orig_width == target_width:
                    self._log_debug(f"Diagram already at target width {target_width}px")
                    return True
                
                scale_factor = target_width / orig_width
                new_width = target_width
                new_height = int(orig_height * scale_factor)
                
                is_upscaling = scale_factor > 1.0
                resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                
                # Apply sharpening
                if is_upscaling:
                    sharpened = resized.filter(ImageFilter.UnsharpMask(radius=1.0, percent=200, threshold=2))
                else:
                    sharpened = resized.filter(ImageFilter.UnsharpMask(radius=0.5, percent=150, threshold=3))
                
                if image_path.suffix.lower() in ['.png']:
                    sharpened.save(image_path, format='PNG', compress_level=6, optimize=False)
                elif image_path.suffix.lower() in ['.jpg', '.jpeg']:
                    sharpened.save(image_path, format='JPEG', quality=100, subsampling=0, optimize=False)
                else:
                    sharpened.save(image_path, optimize=False)
                
                direction = "upscaled" if is_upscaling else "downscaled"
                self._log_debug(f"Fit diagram to page: {orig_width}x{orig_height} -> {new_width}x{new_height} ({direction}, {scale_percent}% of page width)")
            
            return True
        except Exception as e:
            self._log_warning(f"Failed to fit diagram to page width: {e}")
            return False
    
    def _parse_dimension_value(self, value, original_size: int) -> Optional[int]:
        """Parse dimension value that can be max pixels (int) or percentage (str).
        
        Args:
            value: int for max pixels (only resize if original exceeds), 
                   str like "80%" for percentage of original (always applied, max 100%)
            original_size: Original dimension in pixels (for percentage calculation)
            
        Returns:
            Target dimension in pixels, or None if no resizing needed
        """
        if value is None:
            return None
            
        # If it's already an int (max pixels constraint)
        if isinstance(value, int):
            # Only resize if original exceeds the max
            if original_size > value:
                return value
            else:
                return None  # Keep original size
            
        # If it's a string, check for percentage
        if isinstance(value, str):
            value_stripped = value.strip()
            if value_stripped.endswith('%'):
                try:
                    percent = float(value_stripped[:-1])
                    if percent > 0:
                        # Always apply percentage (supports both downsizing and upscaling)
                        return int(original_size * percent / 100.0)
                except ValueError:
                    pass
            else:
                # Try parsing as int string
                try:
                    parsed_int = int(value_stripped)
                    # Only resize if original exceeds the max
                    if original_size > parsed_int:
                        return parsed_int
                    else:
                        return None  # Keep original size
                except ValueError:
                    pass
        
        return None
    
    def _resize_image(self, image_path: Path, max_width = None, max_height = None) -> bool:
        """Resize image while maintaining aspect ratio.
        
        Args:
            image_path: Path to the image file
            max_width: Maximum width - int (pixels), str ("80%"), or None (uses self.diagram_width)
            max_height: Maximum height - int (pixels), str ("80%"), or None (uses self.diagram_height)
            
        Returns:
            True if resize was successful, False otherwise
        """
        try:
            if max_width is None:
                max_width = self.diagram_width
            if max_height is None:
                max_height = self.diagram_height
            
            # Open the image
            with Image.open(image_path) as img:
                # Convert image to RGB/RGBA mode if needed (fixes "wrong mode" errors)
                # Some diagram renderers produce images in palette mode (P) or other modes
                # that don't support all PIL operations like UnsharpMask filter
                original_mode = img.mode
                mode_converted = False
                if img.mode not in ('RGB', 'RGBA'):
                    # Preserve transparency if present
                    if img.mode in ('P', 'PA', 'LA') and 'transparency' in img.info:
                        img = img.convert('RGBA')
                    else:
                        img = img.convert('RGB')
                    mode_converted = True
                    self._log_debug(f"Converted image from mode {original_mode} to {img.mode} for processing")
                
                # Get original dimensions
                orig_width, orig_height = img.size
                
                self._log_debug(f"Checking resize for {image_path.name}: original={orig_width}x{orig_height}, max={max_width}x{max_height}")
                
                # Parse dimensions (resolve percentages based on original size)
                target_width = self._parse_dimension_value(max_width, orig_width)
                target_height = self._parse_dimension_value(max_height, orig_height)
                
                self._log_debug(f"Parsed target dimensions: width={target_width}, height={target_height}")
                
                # If no valid dimensions, check if we need to save mode conversion
                if target_width is None and target_height is None:
                    if mode_converted:
                        # Save the mode-converted image even though no resize is needed
                        if image_path.suffix.lower() in ['.png']:
                            img.save(image_path, format='PNG', compress_level=6, optimize=False)
                        elif image_path.suffix.lower() in ['.jpg', '.jpeg']:
                            img.save(image_path, format='JPEG', quality=100, subsampling=0, optimize=False)
                        else:
                            img.save(image_path, optimize=False)
                        self._log_debug(f"Saved mode-converted image (no resize needed)")
                    else:
                        self._log_debug(f"Image {orig_width}x{orig_height} is within max constraints {max_width}x{max_height}, no resize needed")
                    return True
                
                # Calculate scaling factor to fit within target dimensions while maintaining aspect ratio
                if target_width is not None and target_height is not None:
                    width_ratio = target_width / orig_width
                    height_ratio = target_height / orig_height
                    scale_factor = min(width_ratio, height_ratio)
                elif target_width is not None:
                    scale_factor = target_width / orig_width
                else:  # target_height is not None
                    scale_factor = target_height / orig_height
                
                # Only resize if scale factor differs from 1.0
                if scale_factor != 1.0:
                    new_width = int(orig_width * scale_factor)
                    new_height = int(orig_height * scale_factor)
                    
                    is_upscaling = scale_factor > 1.0
                    
                    # Resize using high-quality Lanczos resampling (best for both up and downscaling)
                    resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                    
                    # Apply sharpening optimized for the scaling direction
                    if is_upscaling:
                        # For upscaling: Use stronger sharpening to enhance details and reduce softness
                        # radius: larger for upscaling to cover more area
                        # percent: higher to enhance edges more aggressively
                        # threshold: lower to sharpen more details
                        sharpened = resized_img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=200, threshold=2))
                    else:
                        # For downscaling: Use moderate sharpening to compensate for resize blur
                        sharpened = resized_img.filter(ImageFilter.UnsharpMask(radius=0.5, percent=150, threshold=3))
                    
                    # Save with maximum quality settings
                    # For PNG: compress_level 0-9 (higher = smaller file but still lossless)
                    # For JPEG: quality 95-100 (near-lossless)
                    if image_path.suffix.lower() in ['.png']:
                        sharpened.save(image_path, format='PNG', compress_level=6, optimize=False)
                    elif image_path.suffix.lower() in ['.jpg', '.jpeg']:
                        sharpened.save(image_path, format='JPEG', quality=100, subsampling=0, optimize=False)
                    else:
                        sharpened.save(image_path, optimize=False)
                    
                    scale_direction = "upscaled" if is_upscaling else "downscaled"
                    self._log_debug(f"Resized image from {orig_width}x{orig_height} to {new_width}x{new_height} ({scale_direction} with optimized sharpening)")
                else:
                    self._log_debug(f"Image {orig_width}x{orig_height} already within target bounds, no resize needed")
            
            return True
            
        except Exception as e:
            self._log_error(f"Failed to resize image {image_path}: {e}")
            return False
    
    async def _render_mermaid_diagram(self, mermaid_code: str, output_path: Path) -> tuple[bool, str]:
        """Render Mermaid diagram to image using Playwright."""
        try:
            # Reuse browser instance
            await self._ensure_browser()
            page = self._local.page
            
            # Set viewport for high-resolution rendering using fixed pixel dimensions
            viewport_width, viewport_height = self._get_viewport_dimensions()
            await page.set_viewport_size({"width": viewport_width, "height": viewport_height})
            await page.emulate_media(media="screen")
                
            # Create HTML with Mermaid
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <script src="https://unpkg.com/mermaid@10.6.1/dist/mermaid.min.js"></script>
                <style>
                    body {{
                        margin: 0;
                        padding: 10px;
                        background: white;
                        font-family: Arial, sans-serif;
                    }}
                    .mermaid {{
                        text-align: center;
                        background: white;
                        display: inline-block;
                        padding: 5px;
                    }}
                    .mermaid svg {{
                        max-width: none;
                        height: auto;
                        display: block;
                        font-family: Arial, sans-serif;
                    }}
                    .mermaid .node rect {{
                        rx: 3;
                        ry: 3;
                    }}
                    .mermaid .edgePath .path {{
                        stroke-width: 1.5px;
                    }}
                </style>
            </head>
            <body>
                <div class="mermaid">
                    {mermaid_code}
                </div>
                <script>
                    // Load Mermaid configuration from file if available
                    let mermaidConfig = {{
                        startOnLoad: true,
                        theme: 'default',
                        themeVariables: {{
                            primaryColor: '#ff6b6b',
                            primaryTextColor: '#333',
                            primaryBorderColor: '#ff6b6b',
                            lineColor: '#333',
                            secondaryColor: '#4ecdc4',
                            tertiaryColor: '#45b7d1'
                        }},
                        flowchart: {{
                            useMaxWidth: false,
                            htmlLabels: true,
                            curve: 'basis',
                            nodeSpacing: 30,
                            rankSpacing: 30,
                            diagramMarginX: 10,
                            diagramMarginY: 5
                        }},
                        sequence: {{
                            useMaxWidth: false,
                            diagramMarginY: 3,
                            diagramMarginX: 10,
                            messageFontSize: 12,
                            actorFontSize: 12,
                            actorMargin: 20,
                            messageMargin: 10
                        }},
                        gantt: {{
                            useMaxWidth: false
                        }},
                        graph: {{
                            useMaxWidth: false,
                            nodeSpacing: 30,
                            rankSpacing: 30,
                            diagramMarginX: 10,
                            diagramMarginY: 5
                        }}
                    }};
                    
                    mermaid.initialize(mermaidConfig);
                </script>
            </body>
            </html>
            """
            
            await page.set_content(html_content)
            
            # Wait for Mermaid to render - use dynamic polling instead of fixed 3s wait
            # Wait for Mermaid to render using dimension stability detection
            max_wait_time = 5000  # 5 seconds max
            poll_interval = 100  # 100ms polling
            stability_checks = 3  # Number of consecutive stable checks required
            elapsed_time = 0
            
            # First, wait for SVG to appear
            svg_found = False
            while elapsed_time < max_wait_time and not svg_found:
                svg_element = await page.query_selector('.mermaid svg')
                if svg_element:
                    svg_content = await svg_element.inner_html()
                    if svg_content and len(svg_content.strip()) > 0:
                        self._log_debug(f"Mermaid SVG detected in {elapsed_time}ms")
                        svg_found = True
                        break
                
                await page.wait_for_timeout(poll_interval)
                elapsed_time += poll_interval
            
            if not svg_found:
                self._log_warning(f"Mermaid SVG not found after {max_wait_time}ms")
            else:
                # Wait for dimensions to stabilize
                mermaid_element = await page.query_selector('.mermaid')
                if mermaid_element:
                    stable_count = 0
                    last_box = None
                    stability_start = elapsed_time
                    
                    while elapsed_time < max_wait_time and stable_count < stability_checks:
                        await page.wait_for_timeout(50)  # Short wait between checks
                        current_box = await mermaid_element.bounding_box()
                        
                        if current_box and last_box:
                            # Check if dimensions are stable (within 1px tolerance for floating point)
                            width_stable = abs(current_box['width'] - last_box['width']) < 1
                            height_stable = abs(current_box['height'] - last_box['height']) < 1
                            
                            if width_stable and height_stable:
                                stable_count += 1
                            else:
                                stable_count = 0  # Reset if dimensions changed
                        
                        last_box = current_box
                        elapsed_time += 50
                    
                    if stable_count >= stability_checks:
                        self._log_debug(f"Mermaid diagram layout stabilized in {elapsed_time - stability_start}ms (total: {elapsed_time}ms)")
                    else:
                        self._log_warning(f"Mermaid diagram dimensions did not stabilize after {max_wait_time}ms")
            
            # Scale the SVG up to the target page width BEFORE rasterizing.
            # SVGs are vector graphics, so this produces a perfectly crisp image
            # at any resolution — no blurry upscaling of a small raster.
            target_width = self._get_page_width_px()
            scaled = await page.evaluate(f"""() => {{
                const svg = document.querySelector('.mermaid svg');
                if (!svg) return false;
                const viewBox = svg.getAttribute('viewBox');
                if (viewBox) {{
                    const parts = viewBox.split(/[\\s,]+/);
                    const natW = parseFloat(parts[2]);
                    const natH = parseFloat(parts[3]);
                    if (natW > 0 && natH > 0) {{
                        const scale = {target_width} / natW;
                        svg.setAttribute('width', {target_width});
                        svg.setAttribute('height', Math.ceil(natH * scale));
                        svg.style.maxWidth = 'none';
                        return true;
                    }}
                }}
                // Fallback: use current rendered size
                const rect = svg.getBoundingClientRect();
                if (rect.width > 0) {{
                    const scale = {target_width} / rect.width;
                    svg.setAttribute('width', {target_width});
                    svg.setAttribute('height', Math.ceil(rect.height * scale));
                    svg.style.maxWidth = 'none';
                    return true;
                }}
                return false;
            }}""")
            if scaled:
                self._log_debug(f"Scaled SVG to {target_width}px wide before rasterization")
            
            # Brief pause for the browser to re-layout at the new SVG size
            await page.wait_for_timeout(100)
            
            # Take screenshot of the scaled element
            mermaid_element = await page.query_selector('.mermaid')
            if mermaid_element:
                bounding_box = await mermaid_element.bounding_box()
                if bounding_box:
                    await mermaid_element.screenshot(
                        path=str(output_path), 
                        type='png',
                        scale='device'
                    )
                else:
                    await page.screenshot(
                        path=str(output_path), 
                        type='png', 
                        full_page=True,
                        scale='device'
                    )
            else:
                await page.screenshot(
                    path=str(output_path), 
                    type='png', 
                    full_page=True,
                    scale='device'
                )
            
            return True, ""
                
        except Exception as e:
            error_msg = f"Failed to render Mermaid diagram: {e}"
            self._log_error(error_msg)
            return False, error_msg
    
    def _render_plantuml_diagram(self, plantuml_code: str, output_path: Path, max_retries: int = 3) -> tuple[bool, str]:
        """Render PlantUML diagram to image using the plantuml library.
        
        Retries on transient network errors (SSL, connection, timeout) with exponential backoff.
        Creates a fresh client on each retry to avoid reusing broken connections.
        """
        last_error_msg = ""
        client = self._plantuml_client
        
        for attempt in range(1, max_retries + 1):
            try:
                # Render the diagram to PNG and get raw image data
                image_data = client.processes(plantuml_code)
                
                # Write the image data to file
                with open(output_path, 'wb') as f:
                    f.write(image_data)
                
                # Check if the file was created successfully
                if output_path.exists() and output_path.stat().st_size > 0:
                    if attempt > 1:
                        self._log_debug(f"PlantUML diagram rendered successfully on attempt {attempt}")
                    self._log_debug(f"PlantUML diagram rendered successfully to: {output_path}")
                    return True, ""
                else:
                    last_error_msg = "PlantUML diagram file was not created or is empty"
                    self._log_error(last_error_msg)
                    return False, last_error_msg
                    
            except Exception as e:
                # Handle exception carefully - some PlantUML exceptions don't have proper string representation
                error_type = type(e).__name__
                try:
                    error_details = str(e)
                except:
                    error_details = "Unknown error (exception string conversion failed)"
                last_error_msg = f"Failed to render PlantUML diagram: {error_type}: {error_details}"
                
                # Check if this is a transient network error worth retrying
                transient_keywords = ["SSL", "SSLError", "ConnectionError", "ConnectionReset", 
                                      "TimeoutError", "Timeout", "BrokenPipe", "RemoteDisconnected"]
                is_transient = any(kw.lower() in last_error_msg.lower() for kw in transient_keywords)
                
                if is_transient and attempt < max_retries:
                    wait_time = 2 ** attempt  # 2s, 4s
                    self._log_warning(f"PlantUML request failed (attempt {attempt}/{max_retries}): {error_type}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    # Create a fresh client to discard any broken SSL/connection state
                    client = plantuml.PlantUML(url=self._plantuml_server)
                else:
                    self._log_error(last_error_msg)
                    return False, last_error_msg
        
        # Should not reach here, but just in case
        return False, last_error_msg
    
    def _replace_mermaid_with_images(self, content: str, file_id: str = "", filename: str = "") -> str:
        """Replace Mermaid code blocks with image references.
        
        Supports HTML comment modifiers on line above diagram:
        <!-- no-resize -->
        ```mermaid
        graph TD
            A --> B
        ```
        
        <!-- scale:150% -->
        ```mermaid
        graph TD
            A --> B
        ```
        """
        import re
        
        # Pattern to match optional HTML comment modifiers on line above, followed by mermaid block
        # Handles both Unix (\n) and Windows (\r\n) line endings
        # Captures: no-resize OR scale:X%
        mermaid_pattern = r'(?:<!--\s*(?:(no-resize)|scale:(\d+)%)\s*-->\s*\r?\n)?```mermaid\r?\n(.*?)\r?\n```'
        
        # Find all matches with their modifiers
        matches = list(re.finditer(mermaid_pattern, content, re.DOTALL | re.IGNORECASE))
        
        if not matches:
            return content
        
        # Process with progress bar
        desc = f"  {filename} - Mermaid" if filename else "  Mermaid diagrams"
        for i, match in enumerate(tqdm(matches, desc=desc, unit="diagram", leave=False)):
            no_resize_modifier = match.group(1)  # "no-resize" or None
            scale_percent = match.group(2)  # percentage digits or None
            mermaid_code = match.group(3)
            full_block = match.group(0)
            
            # Determine resize behavior
            skip_resize = no_resize_modifier is not None
            target_scale = 100.0  # Default: fill page width
            if scale_percent:
                percent_value = float(scale_percent)
                if percent_value > 0:
                    target_scale = percent_value
                else:
                    self._log_warning(f"Scale percentage must be greater than 0%, got {scale_percent}%. Using default (100%).")
            
            # Create unique image path using file_id to avoid race conditions
            image_path = self.temp_dir / f"mermaid_diagram_{file_id}_{i}.png"
            
            # Render Mermaid diagram
            modifier_info = ""
            if skip_resize:
                modifier_info = " (no-resize)"
            elif target_scale != 100.0:
                modifier_info = f" (scale:{target_scale}%)"
            self._log_debug(f"Rendering Mermaid diagram {i} to: {image_path}{modifier_info}")
            
            # Reuse event loop from file processing
            success, error_msg = self._local.event_loop.run_until_complete(
                self._render_mermaid_diagram(mermaid_code, image_path)
            )
            if not success:
                raise RuntimeError(f"Mermaid diagram {i} failed to render: {error_msg}")
            
            # Fit diagram to page width (or scaled fraction of it)
            if skip_resize:
                self._log_debug(f"Skipping resize for Mermaid diagram {i} due to no-resize modifier")
            else:
                self._fit_diagram_to_page_width(image_path, scale_percent=target_scale)
            
            self._log_debug(f"Mermaid diagram rendered successfully, using path: {image_path}")
            # Replace the code block with image reference
            content = content.replace(
                full_block,
                f"![]({image_path})"
            )
        
        return content
    
    def _replace_plantuml_with_images(self, content: str, file_id: str = "", filename: str = "") -> str:
        """Replace PlantUML code blocks with image references.
        
        Supports HTML comment modifiers on line above diagram:
        <!-- no-resize -->
        ```plantuml
        @startuml
        A -> B
        @enduml
        ```
        
        <!-- scale:150% -->
        ```plantuml
        @startuml
        A -> B
        @enduml
        ```
        """
        import re
        
        # Pattern to match optional HTML comment modifiers on line above, followed by plantuml block
        # Handles both Unix (\n) and Windows (\r\n) line endings
        # Captures: no-resize OR scale:X%
        plantuml_pattern = r'(?:<!--\s*(?:(no-resize)|scale:(\d+)%)\s*-->\s*\r?\n)?```plantuml\r?\n(.*?)\r?\n```'
        
        # Find all matches with their modifiers
        matches = list(re.finditer(plantuml_pattern, content, re.DOTALL | re.IGNORECASE))
        
        if not matches:
            return content
        
        # Process with progress bar
        desc = f"  {filename} - PlantUML" if filename else "  PlantUML diagrams"
        for i, match in enumerate(tqdm(matches, desc=desc, unit="diagram", leave=False)):
            no_resize_modifier = match.group(1)  # "no-resize" or None
            scale_percent = match.group(2)  # percentage digits or None
            plantuml_code = match.group(3)
            full_block = match.group(0)
            
            # Determine resize behavior
            skip_resize = no_resize_modifier is not None
            target_scale = 100.0  # Default: fill page width
            if scale_percent:
                percent_value = float(scale_percent)
                if percent_value > 0:
                    target_scale = percent_value
                else:
                    self._log_warning(f"Scale percentage must be greater than 0%, got {scale_percent}%. Using default (100%).")
            
            # Create unique image path using file_id to avoid race conditions
            image_path = self.temp_dir / f"plantuml_diagram_{file_id}_{i}.png"
            
            # Render PlantUML diagram
            modifier_info = ""
            if skip_resize:
                modifier_info = " (no-resize)"
            elif target_scale != 100.0:
                modifier_info = f" (scale:{target_scale}%)"
            self._log_debug(f"Rendering PlantUML diagram {i} to: {image_path}{modifier_info}")
            
            success, error_msg = self._render_plantuml_diagram(plantuml_code, image_path)
            if not success:
                raise RuntimeError(f"PlantUML diagram {i} failed to render: {error_msg}")
            
            # Fit diagram to page width (or scaled fraction of it)
            if skip_resize:
                self._log_debug(f"Skipping resize for PlantUML diagram {i} due to no-resize modifier")
            else:
                self._fit_diagram_to_page_width(image_path, scale_percent=target_scale)
            
            self._log_debug(f"PlantUML diagram rendered successfully, using path: {image_path}")
            # Replace the code block with image reference
            content = content.replace(
                full_block,
                f"![]({image_path})"
            )
        
        return content
    
    def _process_page_breaks(self, content: str) -> str:
        """Process page break markers in markdown content."""
        import re
        
        # Option 1: HTML comment page breaks
        # <!-- page-break -->
        content = re.sub(
            r'<!--\s*page-break\s*-->',
            '<div class="page-break"></div>',
            content,
            flags=re.IGNORECASE
        )
        
        # Option 2: HTML div with page-break class
        # <div class="page-break"></div> (already in correct format)
        
        # Option 3: Custom code block page breaks
        # ```page-break
        content = re.sub(
            r'```page-break\n```',
            '<div class="page-break"></div>',
            content,
            flags=re.IGNORECASE
        )
        
        # Option 4: Custom tag page breaks
        # <page-break>
        content = re.sub(
            r'<page-break>',
            '<div class="page-break"></div>',
            content,
            flags=re.IGNORECASE
        )
        
        # Option 5: Horizontal rule with page-break class (Pandoc attribute syntax)
        # ---
        # {.page-break}
        content = re.sub(
            r'---\s*\n\s*\{\.page-break\}',
            '<div class="page-break"></div>',
            content,
            flags=re.IGNORECASE | re.MULTILINE
        )
        
        # Count page breaks for debugging
        page_break_count = content.count('<div class="page-break"></div>')
        if page_break_count > 0:
            self._log_debug(f"Processed {page_break_count} page break(s)")
        
        return content
    
    def _filter_sections_for_print(self, content: str) -> str:
        """Filter out sections that should be ignored for print profiles."""
        import re
        
        # For print profiles, remove Table of contents sections
        if self.style_profile == "a4-print":
            # Pattern to match "## Table of contents" or "### Table of contents" heading and everything until the next heading
            # This includes the heading itself and all content until the next heading of same or higher level
            toc_pattern = r'^#{2,3}\s+Table\s+of\s+contents\s*$.*?(?=^#{1,3}\s|\Z)'
            
            # Use MULTILINE and DOTALL flags to match across lines
            filtered_content = re.sub(toc_pattern, '', content, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE)
            
            # Clean up any extra whitespace that might be left
            filtered_content = re.sub(r'\n\s*\n\s*\n', '\n\n', filtered_content)
            
            # Log the filtering action
            if filtered_content != content:
                self._log_debug("Filtered out 'Table of contents' section for print profile")
            
            return filtered_content
        
        return content
    
    def _process_and_embed_images(self, content: str, md_file: Path) -> str:
        """Process and embed referenced images into the temp directory."""
        import re
        import shutil
        
        # Find all image references (both markdown and HTML img tags)
        # Pattern for markdown images: ![alt](path)
        markdown_img_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
        # Pattern for HTML img tags: <img src="path" ...>
        html_img_pattern = r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>'
        
        processed_content = content
        md_dir = md_file.parent
        
        # Process markdown image references
        for match in re.finditer(markdown_img_pattern, content):
            alt_text = match.group(1)
            img_path = match.group(2)
            
            # Skip if it's already a temp file or absolute URL
            if img_path.startswith('http') or img_path.startswith('data:'):
                continue
            
            # Check if this is a diagram generated by our script (Mermaid or PlantUML)
            if img_path.startswith('temp/') or img_path.startswith('temp\\'):
                # Convert relative temp path to absolute path for pandoc
                if not os.path.isabs(img_path):
                    # Convert to absolute path
                    abs_img_path = self.temp_dir / img_path.replace('temp/', '').replace('temp\\', '')
                    if abs_img_path.exists():
                        # Replace the relative path with absolute path
                        processed_content = processed_content.replace(
                            f"]({img_path})",
                            f"]({abs_img_path})"
                        )
                        self._log_debug(f"Converted temp image path to absolute: {img_path} -> {abs_img_path}")
                    else:
                        self._log_warning(f"Temp image not found: {abs_img_path}")
                continue
                
            # Resolve relative path from markdown file location
            if not os.path.isabs(img_path):
                full_img_path = md_dir / img_path
            else:
                full_img_path = Path(img_path)
            
            if full_img_path.exists():
                # Copy image to temp directory
                temp_img_name = f"embedded_{full_img_path.stem}_{full_img_path.suffix}"
                temp_img_path = self.temp_dir / temp_img_name
                
                try:
                    shutil.copy2(full_img_path, temp_img_path)
                    self._log_debug(f"Embedded image: {img_path} -> {temp_img_name}")
                    
                    # Update the reference in content
                    old_ref = f"![{alt_text}]({img_path})"
                    new_ref = f"![{alt_text}]({temp_img_path})"
                    processed_content = processed_content.replace(old_ref, new_ref)
                    
                except Exception as e:
                    self._log_warning(f"Failed to embed image {img_path}: {e}")
            else:
                self._log_warning(f"Image not found: {full_img_path}")
        
        # Process HTML img tags
        for match in re.finditer(html_img_pattern, content):
            img_path = match.group(1)
            
            # Skip if it's already a temp file or absolute URL
            if img_path.startswith('http') or img_path.startswith('data:'):
                continue
            
            # Check if this is a diagram generated by our script (Mermaid or PlantUML)
            if img_path.startswith('temp/') or img_path.startswith('temp\\'):
                # Convert relative temp path to absolute path for pandoc
                if not os.path.isabs(img_path):
                    # Convert to absolute path
                    abs_img_path = self.temp_dir / img_path.replace('temp/', '').replace('temp\\', '')
                    if abs_img_path.exists():
                        # Replace the relative path with absolute path
                        processed_content = processed_content.replace(
                            f"]({img_path})",
                            f"]({abs_img_path})"
                        )
                        self._log_debug(f"Converted temp image path to absolute: {img_path} -> {abs_img_path}")
                    else:
                        self._log_warning(f"Temp image not found: {abs_img_path}")
                continue
                
            # Resolve relative path from markdown file location
            if not os.path.isabs(img_path):
                full_img_path = md_dir / img_path
            else:
                full_img_path = Path(img_path)
            
            if full_img_path.exists():
                # Copy image to temp directory
                temp_img_name = f"embedded_{full_img_path.stem}_{full_img_path.suffix}"
                temp_img_path = self.temp_dir / temp_img_name
                
                try:
                    shutil.copy2(full_img_path, temp_img_path)
                    self._log_debug(f"Embedded HTML image: {img_path} -> {temp_img_name}")
                    
                    # Update the reference in content
                    old_ref = match.group(0)
                    new_ref = old_ref.replace(img_path, str(temp_img_path))
                    processed_content = processed_content.replace(old_ref, new_ref)
                    
                except Exception as e:
                    self._log_warning(f"Failed to embed HTML image {img_path}: {e}")
            else:
                self._log_warning(f"HTML image not found: {full_img_path}")
        
        return processed_content
    
    def _create_html_template(self, content: str, margins: Dict[str, str], title: str) -> str:
        """Create HTML template with proper styling, margins, and document title."""
        
        # Get style profile configuration
        profile = self.STYLE_PROFILES[self.style_profile]
        font_scale = profile["font_scale"]
        base_font_size = profile["base_font_size"]
        
        # Convert margins to cm for CSS
        top_cm = self._convert_margin_to_cm(margins['top'])
        right_cm = self._convert_margin_to_cm(margins['right'])
        bottom_cm = self._convert_margin_to_cm(margins['bottom'])
        left_cm = self._convert_margin_to_cm(margins['left'])
        
        html_template = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        @page {{
            margin: {top_cm}cm {right_cm}cm {bottom_cm}cm {left_cm}cm;
            size: A4 portrait;
            width: 210mm;
            height: 297mm;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.4;
            color: #333;
            max-width: none;
            margin: 0;
            padding: 0;
            font-size: {base_font_size};
            width: 100%;
            box-sizing: border-box;
        }}
        
        /* Ensure content fits within A4 page boundaries */
        * {{
            box-sizing: border-box;
        }}
        
        h1, h2, h3, h4, h5, h6 {{
            color: #2c3e50;
            margin-top: 0.8em;
            margin-bottom: 0.3em;
            font-weight: 600;
        }}
        
        h1 {{
            font-size: {1.6 * font_scale:.1f}em;
            border-bottom: 2px solid #3498db;
            padding-bottom: 0.2em;
        }}
        
        h2 {{
            font-size: {1.3 * font_scale:.1f}em;
            border-bottom: 1px solid #bdc3c7;
            padding-bottom: 0.1em;
        }}
        
        h3 {{
            font-size: {1.1 * font_scale:.1f}em;
        }}
        
        h4 {{
            font-size: {1.0 * font_scale:.1f}em;
            text-decoration: underline;
        }}
        
        h5 {{
            font-size: {0.9 * font_scale:.1f}em;
            text-decoration: underline;
        }}
        
        h6 {{
            font-size: {0.8 * font_scale:.1f}em;
            text-decoration: underline;
        }}
        
        p {{
            margin: 0.5em 0;
            text-align: justify;
        }}
        
        code {{
            background-color: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 3px;
            padding: 0.1em 0.3em;
            font-family: 'Courier New', Consolas, monospace;
            font-size: {0.8 * font_scale:.1f}em;
            color: #e83e8c;
        }}
        
        pre {{
            background-color: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 5px;
            padding: 0.5em;
            overflow-x: auto;
            margin: 0.5em 0;
            font-size: {0.8 * font_scale:.1f}em;
        }}
        
        pre code {{
            background: none;
            border: none;
            padding: 0;
            color: #333;
        }}
        
        blockquote {{
            border-left: 4px solid #3498db;
            margin: 0.5em 0;
            padding: 0.3em 0.8em;
            background-color: #f8f9fa;
            color: #555;
        }}
        
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 0.5em 0;
            font-size: {base_font_size} !important;
            font-family: inherit !important;
        }}
        
        th, td {{
            border: 1px solid #ddd;
            padding: 0.3em;
            text-align: left;
            font-size: {base_font_size} !important;
            font-family: inherit !important;
        }}
        
        th {{
            background-color: #f8f9fa;
            font-weight: 600;
            font-size: {base_font_size} !important;
            font-family: inherit !important;
        }}
        
        ul, ol {{
            margin: 0.5em 0;
            padding-left: 1.5em;
            display: block;
        }}
        
        li {{
            margin: 0.2em 0;
            display: list-item;
            list-style-type: disc;
        }}
        
        ul li {{
            list-style-type: disc;
        }}
        
        ol li {{
            list-style-type: decimal;
        }}
        
        /* Ensure nested lists work properly */
        ul ul, ol ol, ul ol, ol ul {{
            margin: 0.2em 0;
            padding-left: 1.2em;
        }}
        
        ul ul li {{
            list-style-type: circle;
        }}
        
        ul ul ul li {{
            list-style-type: square;
        }}
        
        img {{
            max-width: 100%;
            height: auto;
            display: block;
            margin: 0.5em auto;
        }}
        
        /* Specific styling for Mermaid diagram images */
        img[alt*=""] {{
            margin: 0.3em auto;
            padding: 0;
            border: none;
            background: transparent;
        }}
        
        a {{
            color: #3498db;
            text-decoration: none;
        }}
        
        a:hover {{
            text-decoration: underline;
        }}
        
        .page-break {{
            page-break-before: always;
        }}
        
        /* Better page break handling for A4 */
        h1, h2, h3 {{
            page-break-after: avoid;
            break-after: avoid;
        }}
        
        h1, h2, h3, h4, h5, h6 {{
            page-break-inside: avoid;
            break-inside: avoid;
        }}
        
        p, li {{
            orphans: 3;
            widows: 3;
        }}
        
        /* Prevent large elements from breaking across pages */
        pre, blockquote, table, img {{
            page-break-inside: avoid;
            break-inside: avoid;
        }}
        
        /* Ensure tables fit within page width */
        table {{
            max-width: 100%;
            table-layout: auto;
        }}
        
        /* Force table font inheritance and override any defaults */
        table, table *, table th, table td, table tr {{
            font-size: {base_font_size} !important;
            font-family: inherit !important;
            line-height: inherit !important;
        }}
        
        /* Additional specificity for markdown-generated tables */
        body table, body table th, body table td {{
            font-size: {base_font_size} !important;
            font-family: inherit !important;
        }}
    </style>
</head>
<body>
    {content}
</body>
</html>
        """
        
        return html_template

    def _extract_title(self, md_file: Path, content: str) -> str:
        """Extract the document title from markdown content.

        Preference order:
        1) First ATX H1 heading starting with '# '
        2) Setext H1 style (line followed by '===')
        3) Humanized filename stem
        """
        import re

        # 1) ATX H1: lines that start with '# ' but not '## '
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith('# '):
                heading_text = stripped[2:].strip()
                if heading_text:
                    return heading_text

        # 2) Setext H1: a line followed by a line of '=' (at least 3)
        lines = content.splitlines()
        for i in range(len(lines) - 1):
            current_line = lines[i].rstrip()
            underline = lines[i + 1].strip()
            if current_line and re.fullmatch(r"=\s*=+", underline) or re.fullmatch(r"=+", underline):
                return current_line.strip()

        # 3) Fallback to humanized filename stem
        stem = md_file.stem.replace('_', ' ').replace('-', ' ').strip()
        return stem.title() if stem else md_file.stem
    
    async def _convert_html_to_pdf(self, html_file: Path, output_pdf: Path, margins: Dict[str, str]) -> bool:
        """Convert HTML to PDF using Playwright (Puppeteer approach).
        
        Retries once with a fresh browser if the browser process crashes mid-conversion.
        """
        max_attempts = 2
        
        for attempt in range(1, max_attempts + 1):
            try:
                # Reuse browser instance
                await self._ensure_browser()
                page = self._local.page
                
                # Load HTML file
                await page.goto(html_file.absolute().as_uri())
                
                # Wait for content to load
                await page.wait_for_load_state('networkidle')
                
                # Convert margins to cm for PDF generation
                top_cm = self._convert_margin_to_cm(margins['top'])
                right_cm = self._convert_margin_to_cm(margins['right'])
                bottom_cm = self._convert_margin_to_cm(margins['bottom'])
                left_cm = self._convert_margin_to_cm(margins['left'])
                
                # Generate PDF with precise A4 settings
                await page.pdf(
                    path=str(output_pdf),
                    format='A4',
                    width='210mm',
                    height='297mm',
                    margin={
                        'top': f'{top_cm}cm',
                        'right': f'{right_cm}cm',
                        'bottom': f'{bottom_cm}cm',
                        'left': f'{left_cm}cm'
                    },
                    print_background=True,
                    prefer_css_page_size=True,
                    display_header_footer=True,
                    header_template='<div></div>',
                    footer_template='<div style="font-size: 10px; text-align: center; width: 100%; margin: 0 auto;"><span class="pageNumber"></span></div>',
                    scale=1.0
                )
                
                return True
                
            except Exception as e:
                error_msg = str(e)
                is_crash = any(kw in error_msg for kw in [
                    "Connection closed", "Browser has been closed", "Target closed",
                    "crashed", "Protocol error"
                ])
                
                if is_crash and attempt < max_attempts:
                    self._log_warning(f"Browser crashed during PDF generation, restarting and retrying...")
                    # Force-reset browser state so _ensure_browser creates a fresh one
                    await self._close_browser()
                else:
                    self._log_error(f"Failed to convert HTML to PDF: {e}")
                    return False
        
        return False
    
    def _save_html_bundle(self, enhanced_html: str, md_file: Path) -> None:
        """Save the styled HTML with image assets as external files.
        
        Creates output/html/{stem}/{stem}.html with an assets/ subdirectory
        containing all referenced images. Absolute temp-dir paths in the HTML
        are rewritten to relative assets/ paths.
        """
        import re
        
        stem = md_file.stem
        bundle_dir = self.output_dir / "html" / stem
        assets_dir = bundle_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        
        html = enhanced_html
        temp_prefix = str(self.temp_dir)
        
        # Find all src="..." and url(...) references pointing to temp-dir files
        # Covers <img src="...">, CSS url(...), and any other src attributes
        patterns = [
            (r'src="([^"]*)"', 'src="{}"'),
            (r"src='([^']*)'", "src='{}'"),
            (r'url\(([^)]+)\)', 'url({})'),
        ]
        
        copied_files: dict[str, str] = {}  # abs_path -> relative asset path
        
        for pattern, template in patterns:
            for match in re.finditer(pattern, html):
                file_ref = match.group(1).strip().strip("'\"")
                
                if not file_ref.startswith(temp_prefix) and not file_ref.startswith('/'):
                    continue
                
                ref_path = Path(file_ref)
                if not ref_path.exists() or not ref_path.is_file():
                    continue
                
                # Copy once, reuse the relative path for duplicates
                abs_key = str(ref_path)
                if abs_key not in copied_files:
                    asset_name = ref_path.name
                    dest = assets_dir / asset_name
                    shutil.copy2(ref_path, dest)
                    copied_files[abs_key] = f"assets/{asset_name}"
                
                # Replace the absolute path with the relative one
                html = html.replace(file_ref, copied_files[abs_key])
        
        output_html = bundle_dir / f"{stem}.html"
        with open(output_html, 'w', encoding='utf-8') as f:
            f.write(html)
        
        self._log_info(f"Saved HTML bundle to {bundle_dir} ({len(copied_files)} assets)")
    
    def _convert_single_file(self, md_file: Path) -> tuple[str, str]:
        """Convert a single markdown file to PDF. Returns (status, filename).
        
        Status is one of: 'converted', 'skipped', 'failed'.
        """
        try:
            filename = md_file.name
            output_pdf = self.pdf_dir / f"{md_file.stem}.pdf"
            
            # Check if conversion is needed before processing (unless force_regenerate is True)
            if not self.force_regenerate:
                # Fast path: if output file is missing, always regenerate
                if not output_pdf.exists():
                    self._log_info(f"Output pdf missing for {filename} - regenerating")
                else:
                    current_markdown_hash = calculate_file_hash(md_file)
                    
                    if not self.state_manager.needs_regeneration(filename, current_markdown_hash, output_pdf, self.style_profile,
                                                                self.diagram_width, self.diagram_height, self.page_margins, True):
                        self._log_info(f"Skipping {filename} - PDF is up to date")
                        return "skipped", filename
            
            try:
                if self._convert_md_to_pdf(md_file, output_pdf):
                    return "converted", filename
                else:
                    return "failed", filename
            finally:
                # Clean up browser and event loop resources after processing file
                tls = self._local
                if hasattr(tls, 'event_loop') and tls.event_loop:
                    tls.event_loop.run_until_complete(self._close_browser())
                    tls.event_loop.close()
                    tls.event_loop = None
                
        except Exception as e:
            self._log_error(f"Error processing {md_file.name}: {e}")
            return "failed", md_file.name

    def _convert_md_to_pdf(self, md_file: Path, output_pdf: Path) -> bool:
        """Convert markdown file to PDF."""
        try:
            # Create event loop once for entire file processing
            tls = self._local
            if not hasattr(tls, 'event_loop') or tls.event_loop is None or tls.event_loop.is_closed():
                tls.event_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(tls.event_loop)
            
            # Calculate current markdown hash for saving state
            current_markdown_hash = calculate_file_hash(md_file)
            filename = md_file.name
            
            self._log_info(f"Converting {filename} - markdown has changed or PDF missing")
            
            # Create progress bar for this file's conversion steps
            with tqdm(total=6, desc=f"  {filename}", unit="step", leave=False) as pbar:
                # Step 1: Read markdown content
                pbar.set_description(f"  {filename} - Reading")
                with open(md_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                pbar.update(1)
                
                # Step 2: Process content
                pbar.set_description(f"  {filename} - Processing")
                processed_content = self._filter_sections_for_print(content)
                file_id = md_file.stem
                pbar.update(1)
                
                # Step 3: Process diagrams
                pbar.set_description(f"  {filename} - Diagrams")
                processed_content = self._replace_mermaid_with_images(processed_content, file_id, filename)
                # Close browser after Mermaid rendering to free memory before PlantUML + PDF steps
                tls.event_loop.run_until_complete(self._close_browser())
                processed_content = self._replace_plantuml_with_images(processed_content, file_id, filename)
                pbar.update(1)
                
                # Step 4: Process page breaks and images
                pbar.set_description(f"  {filename} - Images")
                processed_content = self._process_page_breaks(processed_content)
                processed_content = self._process_and_embed_images(processed_content, md_file)
                pbar.update(1)
                
                # Step 5: Convert to HTML
                pbar.set_description(f"  {filename} - HTML")
                doc_title = self._extract_title(md_file, content)
                temp_md = self.temp_dir / f"temp_{md_file.name}"
                with open(temp_md, 'w', encoding='utf-8') as f:
                    f.write(processed_content)
                
                html_file = self.temp_dir / f"{md_file.stem}.html"
                cmd = [
                    "pandoc",
                    str(temp_md),
                    "-o", str(html_file),
                    "--standalone",
                    "--self-contained",
                    "--css", "data:text/css,",
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    self._log_error(f"Pandoc failed: {result.stderr}")
                    return False
                
                with open(html_file, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                
                margins = self._parse_margins()
                enhanced_html = self._create_html_template(html_content, margins, doc_title)
                enhanced_html_file = self.temp_dir / f"enhanced_{md_file.stem}.html"
                with open(enhanced_html_file, 'w', encoding='utf-8') as f:
                    f.write(enhanced_html)
                
                # Save HTML output if requested
                if self.html_dir:
                    output_html = self.html_dir / f"{md_file.stem}.html"
                    shutil.copy2(enhanced_html_file, output_html)
                    self._log_debug(f"Saved HTML to {output_html}")
                if self.save_html_bundle:
                    self._save_html_bundle(enhanced_html, md_file)
                pbar.update(1)
                
                # Step 6: Convert to PDF
                pbar.set_description(f"  {filename} - PDF")
                self._log_debug(f"Converting HTML to PDF with margins: {margins}")
                success = tls.event_loop.run_until_complete(
                    self._convert_html_to_pdf(enhanced_html_file, output_pdf, margins)
                )
                pbar.update(1)
            
            if success:
                pdf_hash = calculate_file_hash(output_pdf)
                self.state_manager.save_document_state(
                    filename, current_markdown_hash, pdf_hash, self.style_profile,
                    self.diagram_width, self.diagram_height, self.page_margins, True
                )
                self._log_success(f"Converted {md_file.name} to {output_pdf.name}")
                return True
            else:
                self._log_error(f"Failed to convert {md_file.name}")
                return False
                
        except Exception as e:
            self._log_error(f"Error converting {md_file.name}: {e}")
            return False
    
    def convert_all(self, cleanup: bool = True, parallel: bool = True) -> None:
        """Convert all markdown files in source directory to PDF."""
        md_files = list(self.source_dir.glob("*.md"))
        
        if not md_files:
            self._log_warning("No markdown files found in source directory.")
            return
        
        # Filter out README.md
        md_files = [f for f in md_files if f.name != "README.md"]
        
        self._log_info("Starting markdown to PDF conversion...")
        self._log_info(f"Source directory: {self.source_dir.absolute()}")
        self._log_info(f"Output directory: {self.pdf_dir.absolute()}")
        self._log_info(f"Found {len(md_files)} markdown files: {[f.name for f in md_files]}")
        
        if parallel and len(md_files) > 1:
            self._log_info(f"Using parallel processing with {self.max_workers} workers")
            self._convert_all_parallel(md_files, cleanup)
        else:
            self._log_info("Using sequential processing")
            self._convert_all_sequential(md_files, cleanup)
    
    def _get_constructor_kwargs(self) -> dict:
        """Return the kwargs needed to reconstruct this converter in a worker process."""
        return {
            'source_dir': str(self.source_dir),
            'output_dir': str(self.output_dir),
            'temp_dir': str(self.temp_dir),
            'page_margins': self.page_margins,
            'debug': self.debug,
            'db_path': str(self.state_manager.db_path),
            'style_profile': self.style_profile,
            'max_workers': 1,  # Workers don't spawn sub-workers
            'max_diagram_width': self.diagram_width,
            'max_diagram_height': self.diagram_height,
            'force_regenerate': self.force_regenerate,
            'save_html': self.save_html,
            'save_html_bundle': self.save_html_bundle,
        }
    
    def _convert_all_parallel(self, md_files: List[Path], cleanup: bool) -> None:
        """Convert files in parallel using ProcessPoolExecutor.
        
        Each worker runs in its own OS process with a separate Chromium instance,
        so a browser crash in one worker cannot corrupt other workers or the main process.
        """
        success_count = 0
        skipped_count = 0
        failed_count = 0
        
        converter_kwargs = self._get_constructor_kwargs()
        
        # Use 'spawn' context to get clean processes (no forked Playwright state)
        mp_context = multiprocessing.get_context('spawn')
        
        with ProcessPoolExecutor(
            max_workers=self.max_workers,
            mp_context=mp_context,
            initializer=_init_worker_process,
            initargs=(converter_kwargs,)
        ) as executor:
            # Submit all tasks
            future_to_file = {executor.submit(_worker_convert_file, md_file): md_file for md_file in md_files}
            
            # Process completed tasks with progress bar
            with tqdm(total=len(md_files), desc="Converting files", unit="file") as pbar:
                for future in as_completed(future_to_file):
                    md_file = future_to_file[future]
                    try:
                        status, filename = future.result()
                        if status == "converted":
                            success_count += 1
                            pbar.set_postfix_str(f"Converted: {filename}")
                        elif status == "skipped":
                            skipped_count += 1
                            pbar.set_postfix_str(f"Skipped: {filename}")
                        else:
                            failed_count += 1
                            pbar.set_postfix_str(f"Failed: {filename}")
                    except Exception as e:
                        self._log_error(f"Worker process error for {md_file.name}: {e}")
                        failed_count += 1
                        pbar.set_postfix_str(f"Failed: {md_file.name}")
                    finally:
                        pbar.update(1)
        
        total_processed = success_count + skipped_count + failed_count
        self._log_success(f"Parallel conversion complete: {success_count} files converted, {skipped_count} files skipped, {failed_count} files failed ({total_processed}/{len(md_files)} total)")
        self._log_info(f"PDF files saved to: {self.pdf_dir.absolute()}")
        
        if cleanup:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self._log_debug(f"Cleaned up temporary directory: {self.temp_dir}")
    
    def _convert_all_sequential(self, md_files: List[Path], cleanup: bool) -> None:
        """Convert files sequentially (original implementation)."""
        success_count = 0
        skipped_count = 0
        failed_count = 0
        
        # Use progress bar for sequential conversion
        for md_file in tqdm(md_files, desc="Converting files", unit="file"):
            status, filename = self._convert_single_file(md_file)
            if status == "converted":
                success_count += 1
            elif status == "skipped":
                skipped_count += 1
            else:
                failed_count += 1
        
        total_processed = success_count + skipped_count + failed_count
        self._log_success(f"Sequential conversion complete: {success_count} files converted, {skipped_count} files skipped, {failed_count} files failed ({total_processed}/{len(md_files)} total)")
        self._log_info(f"PDF files saved to: {self.pdf_dir.absolute()}")
        
        if cleanup:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self._log_debug(f"Cleaned up temporary directory: {self.temp_dir}")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Convert markdown files to PDF with Mermaid and PlantUML support (Puppeteer approach)")
    parser.add_argument("--source", default=None, help="Source directory (default: from config/env/docs)")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: from config/env/output). PDF files will be saved in output/pdf/ subfolder")
    parser.add_argument("--temp-dir", default=None, help="Temporary files directory (default: from config/env/temp)")
    parser.add_argument("--db-path", default=None, help="Database path (default: from config/env/user config dir)")
    parser.add_argument("--margins", default="1in 0.75in", help="Page margins in CSS format (default: '1in 0.75in'). Range: 0-3 inches. Use 1, 2, or 4 values. Units: in, cm, mm, pt, px")
    parser.add_argument("--profile", default="a4-print", choices=["a4-print", "a4-screen"], help="Style profile for PDF generation (default: 'a4-print'). Available: a4-print (standard), a4-screen (30% larger fonts)")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep temporary files")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for detailed output")
    parser.add_argument("--cleanup-db", action="store_true", help="Clear all document state records from database and exit")
    parser.add_argument("--max-workers", type=int, default=4, help="Maximum number of parallel workers for PDF conversion (default: 4)")
    parser.add_argument("--no-parallel", action="store_true", help="Disable parallel processing and use sequential conversion")
    parser.add_argument("--max-diagram-width", type=str, default=None, help="Maximum diagram width: pixels (e.g., 1680, only if rendered exceeds) or percentage of rendered size (e.g., 80%%, max 100%%). Default: 1680")
    parser.add_argument("--max-diagram-height", type=str, default=None, help="Maximum diagram height: pixels (e.g., 2240, only if rendered exceeds) or percentage of rendered size (e.g., 80%%, max 100%%). Default: 2240")
    parser.add_argument("--force", action="store_true", help="Force regeneration of all files, bypassing document verification")
    parser.add_argument("--save-html", action="store_true", help="Save the intermediate HTML files alongside PDFs (output/html/)")
    parser.add_argument("--save-html-bundle", action="store_true", help="Save HTML with external image assets in output/html/{name}/")
    
    args = parser.parse_args()
    
    # Build config from CLI args
    cli_config = {}
    if args.source:
        cli_config["source_dir"] = args.source
    if args.output_dir:
        cli_config["output_dir"] = args.output_dir
    if args.temp_dir:
        cli_config["temp_dir"] = args.temp_dir
    if args.db_path:
        cli_config["db_path"] = args.db_path
    if args.max_diagram_width:
        # Parse dimension value (can be int string or percentage)
        from .config import parse_dimension_value
        parsed = parse_dimension_value(args.max_diagram_width)
        if parsed is not None:
            cli_config["max_diagram_width"] = parsed
    if args.max_diagram_height:
        # Parse dimension value (can be int string or percentage)
        from .config import parse_dimension_value
        parsed = parse_dimension_value(args.max_diagram_height)
        if parsed is not None:
            cli_config["max_diagram_height"] = parsed
    
    config = Config(cli_config)
    
    # Handle database cleanup if requested
    if args.cleanup_db:
        try:
            state_manager = DocumentStateManager(config.get_db_path())
            count = state_manager.clear_all_documents()
            print(f"{Fore.GREEN}[OK]{Style.RESET_ALL} Cleared {count} document state records from database")
            return
        except Exception as e:
            print(f"{Fore.RED}[ERROR]{Style.RESET_ALL} Failed to cleanup database: {e}")
            sys.exit(1)
    
    # Check dependencies
    if not check_dependencies(check_optional=False):
        sys.exit(1)
    
    # Run conversion
    converter = MarkdownToPDFConverter(
        config.get_source_dir(), 
        config.get_output_dir(), 
        config.get_temp_dir(), 
        args.margins, 
        args.debug, 
        db_path=config.get_db_path(),
        style_profile=args.profile,
        max_workers=args.max_workers,
        max_diagram_width=config.get_max_diagram_width(),
        max_diagram_height=config.get_max_diagram_height(),
        force_regenerate=args.force,
        save_html=args.save_html,
        save_html_bundle=args.save_html_bundle
    )
    converter.convert_all(cleanup=not args.no_cleanup, parallel=not args.no_parallel)


if __name__ == "__main__":
    main()
