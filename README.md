# Markdown to PDF Converter

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Convert markdown files to PDF, EPUB, or MOBI with Mermaid and PlantUML diagram support.

## Quick Start

```bash
# Install Poetry (if needed)
curl -sSL https://install.python-poetry.org | python3 -

# Setup
git clone https://github.com/lggomez/markdowntopdf-notes-system && cd markdowntopdf-notes-system
poetry install
poetry run playwright install chromium

# Convert
poetry run md2pdf --source ./docs
```

## Prerequisites

- **Python 3.8+**
- **Pandoc**: https://pandoc.org/installing.html
- **Calibre** (optional, for MOBI): https://calibre-ebook.com/download

## Installation

```bash
poetry install
poetry run playwright install chromium
```

## Usage

### Basic Usage

```bash
# PDF conversion (default settings)
poetry run md2pdf --source ./docs

# EPUB/MOBI conversion
poetry run md2ebook --format epub --source ./docs
poetry run md2ebook --format mobi --source ./docs
```

### Advanced Examples

```bash
# Limit diagram size with screen-optimized profile  
poetry run md2pdf --source ./docs --output-dir ./my-pdfs --profile a4-screen --max-diagram-width 1200 --max-diagram-height 1600

# Resize diagrams to 80% of rendered size
poetry run md2pdf --source ./docs --max-diagram-width 80% --max-diagram-height 80%

# Resize diagrams to 50% for smaller file sizes
poetry run md2pdf --source ./docs --max-diagram-width 50% --max-diagram-height 50% --profile a4-screen

# Debug mode with custom margins and temp files preserved
poetry run md2pdf --source ./technical-docs --output-dir ./output --margins "0.75in 0.5in" --debug --no-cleanup

# Allow larger diagrams but cap at 2400x3200
poetry run md2pdf --source ./docs --max-diagram-width 2400 --max-diagram-height 3200 --max-workers 8 --profile a4-print

# EPUB with percentage-based diagram sizing
poetry run md2ebook --format epub --source ./book-chapters --output-dir ./ebooks --author "John Doe" --language "en" --profile kindle-paperwhite-11 --max-diagram-width 70% --max-diagram-height 70%

# Sequential processing for debugging specific diagram issues
poetry run md2pdf --source ./docs --no-parallel --debug --no-cleanup --max-diagram-width 1680 --max-diagram-height 2240

# MOBI conversion with all custom settings
poetry run md2ebook --format mobi --source ./manuscript --output-dir ./publish --author "Jane Smith" --language "en" --profile kindle-large --max-diagram-width 1200 --max-diagram-height 1600 --max-workers 4 --debug

# Maintenance: Clear cache and regenerate all
poetry run md2pdf --cleanup-db && poetry run md2pdf --source ./docs

# Force regeneration without clearing database (e.g., after code updates)
poetry run md2pdf --force --source ./docs
```

### Common Options

- `--source DIR` - Source directory (default: `./docs`)
- `--output-dir DIR` - Output directory (default: `./output`)
- `--profile {a4-print,a4-screen}` - Style profile (default: `a4-print`)
- `--max-diagram-width VALUE` - Max diagram width: pixels (only if rendered exceeds, e.g., `1680`) or percentage (e.g., `80%`, max 100%) - Default: `1680`
- `--max-diagram-height VALUE` - Max diagram height: pixels (only if rendered exceeds, e.g., `2240`) or percentage (e.g., `80%`, max 100%) - Default: `2240`
- `--margins "TOP RIGHT BOTTOM LEFT"` - Page margins (default: `"1in 0.75in"`)
- `--max-workers NUM` - Parallel workers (default: `4`)
- `--no-parallel` - Disable parallel processing
- `--force` - Force regeneration of all files, bypassing document verification cache
- `--debug` - Enable debug logging
- `--no-cleanup` - Keep temporary files
- `--cleanup-db` - Clear verification database and recreate with current schema

### Ebook-Specific Options

- `--format {pdf,epub,mobi}` - Output format (default: `pdf`)
- `--author "NAME"` - Author metadata (default: `"Unknown Author"`)
- `--language CODE` - Language code (default: `"en"`)
- `--profile` - Additional profiles: `kindle-basic`, `kindle-large`, `kindle-paperwhite-11`

## Features

- Renders Mermaid and PlantUML diagrams as images with **intelligent rendering** (dimension stability detection)
- **Configurable diagram dimensions** with automatic resizing
- **Per-diagram resize control** with `<!-- no-resize -->`, `<!-- upscale:X% -->`, and `<!-- downscale:X% -->` modifiers
- **Automatic page numbering** in PDF footer (centered)
- **Smart verification** (skips unchanged files) with optional bypass via `--force`
- **Parallel processing** for fast batch conversions
- Style profiles: `a4-print` (12px) or `a4-screen` (15.6px)
- Page breaks: `<!-- page-break -->` or `<div class="page-break"></div>`
- Output: `output/pdf/`, `output/epub/`, `output/mobi/`

## Configuration

**Environment Variables:**
```bash
export MD2PDF_SOURCE_DIR="./docs"
export MD2PDF_OUTPUT_DIR="./output"
export MD2PDF_MAX_DIAGRAM_WIDTH="1680"
export MD2PDF_MAX_DIAGRAM_HEIGHT="2240"
```

**Config File:** `~/.config/markdown-to-pdf/config.json` (Linux/Mac) or `%APPDATA%/markdown-to-pdf/config.json` (Windows)

**Precedence:** CLI > Environment > Config > Defaults

**Diagram Dimensions:**

Configure maximum diagram dimensions globally. Rendered images are automatically resized to fit within these bounds while maintaining aspect ratio.

**Supports two formats:**
- **Maximum pixels**: `1680`, `2400`, etc. - Only resizes if rendered diagram exceeds this size
- **Percentage of rendered size**: `80%`, `50%`, etc. - Always scales to percentage (max 100%)

```bash
# Via CLI - Maximum pixels (only resize if diagram exceeds 1200x1600)
poetry run md2pdf --max-diagram-width 1200 --max-diagram-height 1600 --source ./docs

# Via CLI - Percentage of rendered size (always resizes)
poetry run md2pdf --max-diagram-width 80% --max-diagram-height 80% --source ./docs

# Via environment variables
export MD2PDF_MAX_DIAGRAM_WIDTH="1200"
export MD2PDF_MAX_DIAGRAM_HEIGHT="80%"

# Via config file: ~/.config/markdown-to-pdf/config.json
{
  "max_diagram_width": "80%",
  "max_diagram_height": "80%"
}
```

**How it works:**
- Diagrams render at high resolution (default viewport: 1680x2240)
- **Pixels**: Maximum constraint - only resize if rendered exceeds this (e.g., a 800px diagram stays 800px with `--max-diagram-width 1200`)
- **Percentage**: Always resizes based on rendered size (e.g., `80%` of a 2000px diagram = 1600px)
- Aspect ratio is always preserved
- Only downscales (never upscales, except for percentage >100%)
- No changes required to your markdown diagram code

**Per-Diagram Control:**

You can control resizing for individual diagrams using HTML comments on the line above:

```markdown
# This diagram will be resized according to global settings
\`\`\`mermaid
graph TD
    A --> B
\`\`\`

# This diagram keeps its original rendered size (no resize)
<!-- no-resize -->
\`\`\`mermaid
graph TD
    A --> B
\`\`\`

# This diagram is scaled to 150% (1.5x larger)
<!-- upscale:150% -->
\`\`\`mermaid
graph TD
    A --> B
\`\`\`

# This diagram is downscaled to 67% of original size
<!-- downscale:67% -->
\`\`\`mermaid
graph TD
    A --> B
\`\`\`

# Works with PlantUML too
<!-- upscale:200% -->
\`\`\`plantuml
@startuml
A -> B
@enduml
\`\`\`
```

**Available modifiers:**
- `<!-- no-resize -->` - Keep original rendered size (skip all resizing)
- `<!-- upscale:X% -->` - Scale up to X% of original (X > 100, e.g., `150%` = 1.5x larger, `200%` = 2x larger)
- `<!-- downscale:X% -->` - Scale down to X% of original (0 < X < 100, e.g., `67%` = 67% of original, `50%` = half size)

## Troubleshooting

**Debug Mode:**
```bash
# Enable detailed logging and keep temporary files
poetry run md2pdf --debug --no-cleanup --source ./docs

# Check temp directory for intermediate files
ls -la ./temp/
```

**Common Issues:**

1. **Diagrams not rendering**: Check Playwright installation
   ```bash
   poetry run playwright install chromium
   ```

2. **Cache issues or after updates**: Force regeneration or clear verification database
   ```bash
   # Quick: Force regeneration (keeps verification data)
   poetry run md2pdf --force --source ./docs
   
   # Full reset: Clear and recreate the database
   poetry run md2pdf --cleanup-db
   ```
   Use `--force` to bypass verification cache and regenerate all files (useful after rendering changes or code updates). Use `--cleanup-db` for full database reset (useful after schema changes)

3. **Large diagrams cut off**: Increase maximum diagram dimensions
   ```bash
   poetry run md2pdf --max-diagram-width 2400 --max-diagram-height 3200 --source ./docs
   ```

4. **Parallel processing issues**: Disable parallel mode
   ```bash
   poetry run md2pdf --no-parallel --debug --source ./docs
   ```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

Copyright (c) 2025 Markdown to PDF Converter
