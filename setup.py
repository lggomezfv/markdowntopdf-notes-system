#!/usr/bin/env python3
"""
Setup script for markdown to PDF converter dependencies.
"""

import subprocess
import sys
import os


def run_command(cmd: str, description: str) -> bool:
    """Run a command and return success status."""
    print(f"Installing {description}...")
    try:
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        print(f"✓ {description} installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"✗ Failed to install {description}: {e.stderr}")
        return False


def check_command(cmd: str, description: str) -> bool:
    """Check if a command is available."""
    try:
        subprocess.run(cmd, shell=True, check=True, capture_output=True)
        print(f"✓ {description} is available")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"✗ {description} is not available")
        return False


def main():
    """Main setup function."""
    print("Setting up markdown to PDF converter...")
    
    # Check Python version
    if sys.version_info < (3, 7):
        print("Error: Python 3.7 or higher is required")
        sys.exit(1)
    
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor} detected")
    
    # Install Python dependencies
    print("\nInstalling Python dependencies...")
    if not run_command(f"{sys.executable} -m pip install -r requirements.txt", "Python packages"):
        print("Failed to install Python dependencies")
        sys.exit(1)
    
    # Install Playwright browsers
    print("\nInstalling Playwright browsers...")
    if not run_command(f"{sys.executable} -m playwright install chromium", "Playwright Chromium"):
        print("Failed to install Playwright browsers")
        sys.exit(1)
    
    # Check external dependencies
    print("\nChecking external dependencies...")
    pandoc_available = check_command("pandoc --version", "Pandoc")
    
    if not pandoc_available:
        print("\nPandoc is required but not found. Please install it from:")
        print("https://pandoc.org/installing.html")
    
    if pandoc_available:
        print("\n✓ All dependencies are ready!")
        print("\nYou can now run the converter with:")
        print("python convert_md_to_pdf.py")
    else:
        print("\n⚠ Some dependencies are missing. Please install them before running the converter.")


if __name__ == "__main__":
    main()
