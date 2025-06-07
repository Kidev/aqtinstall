#!/usr/bin/env python3
"""
Qt Repository Target Mapper for aqtinstall
Automatically crawls Qt download repository and maps packages to their URLs
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from aqt.metadata import Version, get_semantic_version


class QtRepositoryMapper:
    def __init__(self, base_url=None, start_folder="online/qtsdkrepository", delay=0.2, quiet=False):
        # Try to import Settings from aqt if available, otherwise use default
        if base_url is None:
            try:
                from aqt.helper import Settings

                self.base_url = Settings.baseurl
            except ImportError:
                self.base_url = "https://download.qt.io"
        else:
            self.base_url = base_url.rstrip("/")

        self.start_folder = start_folder.strip("/")
        self.delay = delay
        self.quiet = quiet
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "aqtinstall-mapper/1.0 (https://github.com/miurahr/aqtinstall)"})

        self.packages = {}
        self.updates_xmls = {}  # Store Updates.xml content
        self.explored_count = 0
        self.start_time = None
        self.mapper_version = "1.0.0"

    def log(self, message, level="INFO"):
        """Print progress messages unless in quiet mode"""
        if not self.quiet:
            print(message, file=sys.stderr)

    def extract_package_info(self, package_name, path):
        """Extract Qt version, platform, component, and architecture from package name and path"""
        # Parse the path to get context
        path_parts = path.strip("/").split("/")

        # Extract version from package name (e.g., qt.qt6.683.gcc_64 -> 683)
        version_match = re.search(r"\.qt(\d)\.(\d+)\.", package_name)
        qt_version = None
        qt_version_str = ""

        if version_match:
            major = version_match.group(1)
            version_digits = version_match.group(2)
            qt_version_str = version_digits  # Keep original format like "683"

            # Check if it's a preview version
            is_preview = "preview" in package_name.lower()
            qt_version = get_semantic_version(version_digits, is_preview).__str__()

        # Extract architecture from package name (last part after final dot)
        arch_parts = package_name.split(".")
        architecture = arch_parts[-1] if len(arch_parts) > 1 else None

        # Extract component from package name
        component = ""
        if ".addons." in package_name:
            # Extract addon component (e.g., qt.qt6.663.addons.qtpdf -> qtpdf)
            component_match = re.search(r"\.addons\.(.+?)\.", package_name + ".")
            if component_match:
                component = component_match.group(1)
        elif ".debug_info" in package_name:
            component = "debug_info"
        elif package_name.startswith("qt.qt"):
            # Base Qt package
            component = ""
        else:
            # Extract other components
            parts = package_name.split(".")
            for i, part in enumerate(parts):
                if part.startswith("qt") and len(part) > 2 and part != "qt":
                    component = part
                    break

        # Platform is typically in the path
        platform = None
        if len(path_parts) >= 2:
            platform = f"{path_parts[0]}_{path_parts[1]}"

        return {
            "qt_version": qt_version,
            "qt_version_str": qt_version_str,  # Original version format
            "platform": platform,
            "component": component,
            "architecture": architecture,
        }

    def parse_directory(self, html_content, current_url):
        """Parse HTML directory listing and extract subdirectories and files"""
        soup = BeautifulSoup(html_content, "html.parser")
        subdirs = []
        files = []

        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            link_cell = cells[1]
            link = link_cell.find("a")
            if not link:
                continue

            href = link.get("href")
            if not href:
                continue

            # Skip parent directory and parameters
            if href.startswith("?") or href.startswith("/"):
                continue

            name = link.text.strip()

            if href.endswith("/"):
                # It's a directory
                subdirs.append(name.rstrip("/"))
            else:
                # It's a file
                files.append(name)

        return subdirs, files

    def crawl_directory(self, path="", depth=0):
        """Recursively crawl a directory and its subdirectories"""
        self.explored_count += 1
        indent = "     " + "  " * depth

        # Construct full URL
        if path:
            url = f"{self.base_url}/{self.start_folder}/{path}"
        else:
            url = f"{self.base_url}/{self.start_folder}/"

        self.log(f"[{self.explored_count:4d}] Exploring: {path if path else '<root>'}")

        try:
            # Add delay to be respectful to Qt servers
            if self.explored_count > 1:
                time.sleep(self.delay)

            response = self.session.get(url, timeout=30)
            response.raise_for_status()

            subdirs, files = self.parse_directory(response.text, url)

            # Check if this directory contains packages (has Updates.xml)
            has_updates = "Updates.xml" in files

            if has_updates:
                self.log(f"{indent}└── Found Updates.xml - mapping {len(subdirs)} packages")

                # Fetch and store Updates.xml content
                updates_url = f"{url}Updates.xml" if url.endswith("/") else f"{url}/Updates.xml"
                try:
                    updates_response = self.session.get(updates_url, timeout=30)
                    updates_response.raise_for_status()
                    # Store with path as key
                    updates_key = f"{path}Updates.xml" if path else "Updates.xml"
                    self.updates_xmls[updates_key] = updates_response.text
                    self.log(f"{indent}    └── Fetched Updates.xml content")
                except Exception as e:
                    self.log(f"{indent}    └── WARNING: Failed to fetch Updates.xml: {e}", "WARNING")

                # This directory contains packages
                for package_dir in subdirs:
                    if package_dir == "Parent Directory":
                        continue

                    package_path = f"{path}{package_dir}/" if path else f"{package_dir}/"

                    # Extract package info
                    package_info = self.extract_package_info(package_dir, path)

                    self.packages[package_dir] = {"url": package_path, **package_info}
            else:
                self.log(f"{indent}└── Found {len(subdirs)} subdirectories")
                # Continue crawling subdirectories
                for subdir in subdirs:
                    if subdir == "Parent Directory":
                        continue

                    subpath = f"{path}{subdir}/" if path else f"{subdir}/"
                    self.crawl_directory(subpath, depth + 1)

        except requests.RequestException as e:
            self.log(f"{indent}└── ERROR: Failed to fetch {url}: {e}", "ERROR")
        except Exception as e:
            self.log(f"{indent}└── ERROR: Unexpected error: {e}", "ERROR")

    def generate_metadata(self):
        """Generate metadata for the mapping"""
        return {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "generator_version": self.mapper_version,
            "start_folder": self.start_folder,
            "total_packages": len(self.packages),
            "qt_versions": sorted(list(set(p["qt_version"] for p in self.packages.values() if p["qt_version"] is not None))),
            "crawl_duration_seconds": int(time.time() - self.start_time) if self.start_time else 0,
            "base_url": self.base_url,
        }

    def run(self):
        """Run the crawler and generate the package mapping"""
        self.log(f"Qt Repository Target Mapper")
        self.log(f"Base URL: {self.base_url}")
        self.log(f"Starting crawl of {self.base_url}/{self.start_folder}/")
        self.log("-" * 60)

        self.start_time = time.time()

        # Start crawling from root
        self.crawl_directory()

        # Generate final mapping
        mapping = {"metadata": self.generate_metadata(), "packages": self.packages}

        duration = time.time() - self.start_time
        self.log("-" * 60)
        self.log(f"Crawl completed in {duration:.1f} seconds")
        self.log(f"Found {len(self.packages)} packages")
        self.log(f"Found {len(self.updates_xmls)} Updates.xml files")
        self.log(f"Explored {self.explored_count} directories")

        return mapping, self.updates_xmls


def main():
    parser = argparse.ArgumentParser(description="Qt Repository Target Mapper - Automatically discover and map Qt packages")
    parser.add_argument(
        "--base-url", help="Base URL for Qt downloads (default: uses aqt Settings or https://download.qt.io)", default=None
    )
    parser.add_argument(
        "--start-folder", default="online/qtsdkrepository", help="Starting folder path (default: online/qtsdkrepository)"
    )
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between requests in seconds (default: 0.2)")
    parser.add_argument("--output", "-o", default="qt-packages.json", help="Output JSON file (default: qt-packages.json)")
    parser.add_argument(
        "--updates-output",
        default="qt-updates.json",
        help="Output JSON file for Updates.xml content (default: qt-updates.json)",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress progress output")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")

    args = parser.parse_args()

    # Create mapper instance
    mapper = QtRepositoryMapper(base_url=args.base_url, start_folder=args.start_folder, delay=args.delay, quiet=args.quiet)

    try:
        # Run the crawler
        mapping, updates_xmls = mapper.run()

        # Write package mapping
        with open(args.output, "w") as f:
            if args.pretty:
                json.dump(mapping, f, indent=2, sort_keys=True)
            else:
                json.dump(mapping, f)

        # Write Updates.xml content
        with open(args.updates_output, "w") as f:
            if args.pretty:
                json.dump(updates_xmls, f, indent=2, sort_keys=True)
            else:
                json.dump(updates_xmls, f)

        if not args.quiet:
            print(f"\nPackage mapping saved to: {args.output}")
            print(f"Updates.xml content saved to: {args.updates_output}")

    except KeyboardInterrupt:
        print("\nCrawl interrupted by user", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
