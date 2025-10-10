#!/usr/bin/env python3
"""
Markdown to PDF converter using Puppeteer approach (inspired by vscode-markdown-pdf).
This uses Playwright (Python equivalent of Puppeteer) for better PDF generation control.
"""

import os
import sys
import subprocess
import tempfile
import shutil
import json
from pathlib import Path
from typing import List, Dict, Any
import asyncio
from playwright.async_api import async_playwright
import plantuml
from colorama import init, Fore, Back, Style

# Initialize colorama for cross-platform colored output
init(autoreset=True)


class MarkdownToPDFConverter:
    """Markdown to PDF converter using Playwright (Puppeteer approach)."""
    
    def __init__(self, source_dir: str, pdf_dir: str, temp_dir: str, page_margins: str = "1in 0.75in", debug: bool = False):
        """Initialize the converter."""
        self.source_dir = Path(source_dir)
        self.pdf_dir = Path(pdf_dir)
        self.temp_dir = Path(temp_dir)
        self.page_margins = page_margins
        self.debug = debug
        
        # Create directories
        self.pdf_dir.mkdir(exist_ok=True)
        self.temp_dir.mkdir(exist_ok=True)
    
    def _log_debug(self, message: str) -> None:
        """Log debug message with color (only if debug mode is enabled)."""
        if self.debug:
            print(f"{Fore.CYAN}[DEBUG]{Style.RESET_ALL} {message}")
    
    def _log_info(self, message: str) -> None:
        """Log info message with color."""
        print(f"{Fore.GREEN}[INFO]{Style.RESET_ALL} {message}")
    
    def _log_warning(self, message: str) -> None:
        """Log warning message with color."""
        print(f"{Fore.YELLOW}[WARNING]{Style.RESET_ALL} {message}")
    
    def _log_error(self, message: str) -> None:
        """Log error message with color."""
        print(f"{Fore.RED}[ERROR]{Style.RESET_ALL} {message}")
    
    def _log_success(self, message: str) -> None:
        """Log success message with color."""
        print(f"{Fore.GREEN}[OK]{Style.RESET_ALL} {message}")
    
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
    
    async def _render_mermaid_diagram(self, mermaid_code: str, output_path: Path) -> bool:
        """Render Mermaid diagram to image using Playwright."""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                
                # Set viewport for high-resolution rendering (reduced by ~30% for better fit)
                await page.set_viewport_size({"width": 1680, "height": 2240})
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
                
                # Wait for Mermaid to render
                await page.wait_for_timeout(3000)
                
                # Get the mermaid element and take screenshot of just that element
                mermaid_element = await page.query_selector('.mermaid')
                if mermaid_element:
                    # Get the bounding box of the mermaid element
                    bounding_box = await mermaid_element.bounding_box()
                    if bounding_box:
                        # Take high-resolution screenshot of just the mermaid element
                        await mermaid_element.screenshot(
                            path=str(output_path), 
                            type='png',
                            scale='device'
                        )
                    else:
                        # Fallback to full page if bounding box not available
                        await page.screenshot(
                            path=str(output_path), 
                            type='png', 
                            full_page=True,
                            scale='device'
                        )
                else:
                    # Fallback to full page if mermaid element not found
                    await page.screenshot(
                        path=str(output_path), 
                        type='png', 
                        full_page=True,
                        scale='device'
                    )
                
                await browser.close()
                
                return True
                
        except Exception as e:
            self._log_error(f"Failed to render Mermaid diagram: {e}")
            return False
    
    def _render_plantuml_diagram(self, plantuml_code: str, output_path: Path) -> bool:
        """Render PlantUML diagram to image using the plantuml library."""
        try:
            # Create PlantUML instance with default server URL
            puml = plantuml.PlantUML(url='http://www.plantuml.com/plantuml')
            
            # Render the diagram to PNG and get raw image data
            image_data = puml.processes(plantuml_code)
            
            # Write the image data to file
            with open(output_path, 'wb') as f:
                f.write(image_data)
            
            # Check if the file was created successfully
            if output_path.exists() and output_path.stat().st_size > 0:
                self._log_debug(f"PlantUML diagram rendered successfully to: {output_path}")
                return True
            else:
                self._log_error(f"PlantUML diagram file was not created or is empty")
                return False
                
        except Exception as e:
            self._log_error(f"Failed to render PlantUML diagram: {e}")
            return False
    
    def _replace_mermaid_with_images(self, content: str) -> str:
        """Replace Mermaid code blocks with image references."""
        import re
        
        mermaid_pattern = r'```mermaid\n(.*?)\n```'
        mermaid_blocks = re.findall(mermaid_pattern, content, re.DOTALL)
        
        for i, mermaid_code in enumerate(mermaid_blocks):
            # Create image path
            image_path = self.temp_dir / f"mermaid_diagram_{i}.png"
            
            # Render Mermaid diagram
            self._log_debug(f"Rendering Mermaid diagram {i} to: {image_path}")
            
            # Run async function in sync context
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                success = loop.run_until_complete(self._render_mermaid_diagram(mermaid_code, image_path))
                if success:
                    self._log_debug(f"Mermaid diagram rendered successfully, using path: {image_path}")
                    # Replace the code block with image reference
                    content = content.replace(
                        f"```mermaid\n{mermaid_code}\n```",
                        f"![]({image_path})"
                    )
                else:
                    self._log_error(f"Failed to render Mermaid diagram {i}")
            finally:
                loop.close()
        
        return content
    
    def _replace_plantuml_with_images(self, content: str) -> str:
        """Replace PlantUML code blocks with image references."""
        import re
        
        plantuml_pattern = r'```plantuml\n(.*?)\n```'
        plantuml_blocks = re.findall(plantuml_pattern, content, re.DOTALL)
        
        for i, plantuml_code in enumerate(plantuml_blocks):
            # Create image path
            image_path = self.temp_dir / f"plantuml_diagram_{i}.png"
            
            # Render PlantUML diagram
            self._log_debug(f"Rendering PlantUML diagram {i} to: {image_path}")
            
            success = self._render_plantuml_diagram(plantuml_code, image_path)
            if success:
                self._log_debug(f"PlantUML diagram rendered successfully, using path: {image_path}")
                # Replace the code block with image reference
                content = content.replace(
                    f"```plantuml\n{plantuml_code}\n```",
                    f"![]({image_path})"
                )
            else:
                self._log_error(f"Failed to render PlantUML diagram {i}")
        
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
            
            # Check if this is a Mermaid diagram generated by our script
            if img_path.startswith('temp/') or img_path.startswith('temp\\'):
                # Mermaid diagrams are already in the temp directory, no need to copy
                self._log_debug(f"Skipping Mermaid diagram (already in temp): {img_path}")
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
            
            # Check if this is a Mermaid diagram generated by our script
            if img_path.startswith('temp/') or img_path.startswith('temp\\'):
                # Mermaid diagrams are already in the temp directory, no need to copy
                self._log_debug(f"Skipping Mermaid diagram (already in temp): {img_path}")
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
            font-size: 12px;
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
            font-size: 1.6em;
            border-bottom: 2px solid #3498db;
            padding-bottom: 0.2em;
        }}
        
        h2 {{
            font-size: 1.3em;
            border-bottom: 1px solid #bdc3c7;
            padding-bottom: 0.1em;
        }}
        
        h3 {{
            font-size: 1.1em;
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
            font-size: 0.8em;
            color: #e83e8c;
        }}
        
        pre {{
            background-color: #f8f9fa;
            border: 1px solid #e9ecef;
            border-radius: 5px;
            padding: 0.5em;
            overflow-x: auto;
            margin: 0.5em 0;
            font-size: 0.8em;
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
            font-size: 0.9em;
        }}
        
        th, td {{
            border: 1px solid #ddd;
            padding: 0.3em;
            text-align: left;
        }}
        
        th {{
            background-color: #f8f9fa;
            font-weight: 600;
        }}
        
        ul, ol {{
            margin: 0.5em 0;
            padding-left: 1.5em;
        }}
        
        li {{
            margin: 0.2em 0;
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
        """Convert HTML to PDF using Playwright (Puppeteer approach)."""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                
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
                    display_header_footer=False,
                    scale=1.0
                )
                
                await browser.close()
                return True
                
        except Exception as e:
            self._log_error(f"Failed to convert HTML to PDF: {e}")
            return False
    
    def _convert_md_to_pdf(self, md_file: Path, output_pdf: Path) -> bool:
        """Convert markdown file to PDF."""
        try:
            # Read markdown content
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Process Mermaid diagrams
            processed_content = self._replace_mermaid_with_images(content)
            
            # Process PlantUML diagrams
            processed_content = self._replace_plantuml_with_images(processed_content)
            
            # Process page breaks
            processed_content = self._process_page_breaks(processed_content)
            
            # Process and embed referenced images
            processed_content = self._process_and_embed_images(processed_content, md_file)
            
            # Determine document title from content (fallback to humanized filename)
            doc_title = self._extract_title(md_file, content)
            
            # Convert markdown to HTML using pandoc
            temp_md = self.temp_dir / f"temp_{md_file.name}"
            with open(temp_md, 'w', encoding='utf-8') as f:
                f.write(processed_content)
            
            # Convert markdown to HTML
            html_file = self.temp_dir / f"{md_file.stem}.html"
            cmd = [
                "pandoc",
                str(temp_md),
                "-o", str(html_file),
                "--standalone",
                "--self-contained",
                "--css", "data:text/css,",  # Empty CSS, we'll add our own
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                self._log_error(f"Pandoc failed: {result.stderr}")
                return False
            
            # Read the generated HTML
            with open(html_file, 'r', encoding='utf-8') as f:
                html_content = f.read()
            
            # Parse margins
            margins = self._parse_margins()
            
            # Create enhanced HTML template using the detected title
            enhanced_html = self._create_html_template(html_content, margins, doc_title)
            
            # Write enhanced HTML
            enhanced_html_file = self.temp_dir / f"enhanced_{md_file.stem}.html"
            with open(enhanced_html_file, 'w', encoding='utf-8') as f:
                f.write(enhanced_html)
            
            # Convert HTML to PDF using Playwright
            self._log_debug(f"Converting HTML to PDF with margins: {margins}")
            
            # Run async function in sync context
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                success = loop.run_until_complete(
                    self._convert_html_to_pdf(enhanced_html_file, output_pdf, margins)
                )
                if success:
                    self._log_success(f"Converted {md_file.name} to {output_pdf.name}")
                    return True
                else:
                    self._log_error(f"Failed to convert {md_file.name}")
                    return False
            finally:
                loop.close()
                
        except Exception as e:
            self._log_error(f"Error converting {md_file.name}: {e}")
            return False
    
    def convert_all(self, cleanup: bool = True) -> None:
        """Convert all markdown files in source directory to PDF."""
        md_files = list(self.source_dir.glob("*.md"))
        
        if not md_files:
            self._log_warning("No markdown files found in source directory.")
            return
        
        self._log_info("Starting markdown to PDF conversion...")
        self._log_info(f"Found {len(md_files)} markdown files: {[f.name for f in md_files]}")
        
        success_count = 0
        for md_file in md_files:
            if md_file.name == "README.md":
                continue
            output_pdf = self.pdf_dir / f"{md_file.stem}.pdf"
            if self._convert_md_to_pdf(md_file, output_pdf):
                success_count += 1
        
        self._log_success(f"Conversion complete: {success_count}/{len(md_files)} files converted successfully")
        self._log_info(f"PDF files saved to: {self.pdf_dir.absolute()}")
        
        if cleanup:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self._log_debug(f"Cleaned up temporary directory: {self.temp_dir}")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Convert markdown files to PDF with Mermaid and PlantUML support (Puppeteer approach)")
    parser.add_argument("--source", default="docs", help="Source directory (default: docs)")
    parser.add_argument("--pdf-dir", default="pdf", help="PDF output directory (default: pdf)")
    parser.add_argument("--temp-dir", default="temp", help="Temporary files directory (default: temp)")
    parser.add_argument("--margins", default="1in 0.75in", help="Page margins in CSS format (default: '1in 0.75in'). Range: 0-3 inches. Use 1, 2, or 4 values. Units: in, cm, mm, pt, px")
    parser.add_argument("--no-cleanup", action="store_true", help="Keep temporary files")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging for detailed output")
    
    args = parser.parse_args()
    
    # Check dependencies
    try:
        subprocess.run(["pandoc", "--version"], capture_output=True, check=True)
        print(f"{Fore.GREEN}✓{Style.RESET_ALL} Pandoc is available")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"{Fore.RED}✗{Style.RESET_ALL} Error: pandoc is required but not found. Please install pandoc.")
        sys.exit(1)
    
    try:
        import playwright
        print(f"{Fore.GREEN}✓{Style.RESET_ALL} Playwright is available")
    except ImportError:
        print(f"{Fore.RED}✗{Style.RESET_ALL} Error: playwright is required but not found. Please install playwright.")
        print("Run: pip install playwright && playwright install chromium")
        sys.exit(1)
    
    try:
        import plantuml
        print(f"{Fore.GREEN}✓{Style.RESET_ALL} PlantUML is available")
    except ImportError:
        print(f"{Fore.RED}✗{Style.RESET_ALL} Error: plantuml is required but not found. Please install plantuml.")
        print("Run: pip install plantuml")
        sys.exit(1)
    
    try:
        import colorama
        print(f"{Fore.GREEN}✓{Style.RESET_ALL} Colorama is available")
    except ImportError:
        print(f"{Fore.RED}✗{Style.RESET_ALL} Error: colorama is required but not found. Please install colorama.")
        print("Run: pip install colorama")
        sys.exit(1)
    
    # Run conversion
    converter = MarkdownToPDFConverter(args.source, args.pdf_dir, args.temp_dir, args.margins, args.debug)
    converter.convert_all(cleanup=not args.no_cleanup)


if __name__ == "__main__":
    main()
