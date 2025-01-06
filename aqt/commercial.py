import os
import platform
import subprocess
import tempfile
from logging import Logger, getLogger
from pathlib import Path
from typing import Optional

import requests

from .exceptions import ArchiveDownloadError, CliInputError
from .installer import Version


class CommercialInstaller:
    def __init__(
        self,
        target: str,
        arch: str,
        version: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        logger: Optional[Logger] = None,
    ):
        self.target = target
        self.arch = arch
        self.version = Version(version)
        self.username = username
        self.password = password
        self.logger = logger or getLogger(__name__)

        self.os_name = platform.system().lower()
        if self.os_name == "darwin":
            self.os_name = "mac"

        self.installer_filename = self._get_installer_filename()
        self.qt_account = self._get_qt_account_path()

    def _get_installer_filename(self) -> str:
        """Get OS-specific installer filename"""
        base = "qt-unified"
        version = "4.6.1"  # Latest installer version

        if self.os_name == "windows":
            return f"{base}-windows-x64-{version}-online.exe"
        elif self.os_name == "mac":
            return f"{base}-macOS-x64-{version}-online.dmg"
        else:
            return f"{base}-linux-x64-{version}-online.run"

    def _get_qt_account_path(self) -> Path:
        """Get OS-specific qtaccount.ini path"""
        if self.os_name == "windows":
            return Path(os.environ["APPDATA"]) / "Qt" / "qtaccount.ini"
        elif self.os_name == "mac":
            return Path.home() / "Library" / "Application Support" / "Qt" / "qtaccount.ini"
        else:
            return Path.home() / ".local" / "share" / "Qt" / "qtaccount.ini"

    def _download_installer(self, target_path: Path):
        """Download Qt online installer"""
        url = f"https://download.qt.io/official_releases/online_installers/{self.installer_filename}"

        try:
            response = requests.get(url, stream=True)
            response.raise_for_status()

            total = response.headers.get("content-length", 0)

            with open(target_path, "wb") as f:
                if total:
                    desc = f"Downloading {self.installer_filename}"
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)

            if self.os_name != "windows":
                os.chmod(target_path, 0o755)

        except requests.exceptions.RequestException as e:
            raise ArchiveDownloadError(f"Failed to download installer: {str(e)}")

    def _get_package_name(self) -> str:
        """Convert aqt parameters to Qt package name"""
        qt_version = f"{self.version.major}{self.version.minor}{self.version.patch}"
        return f"qt.qt{self.version.major}.{qt_version}.{self.arch}"

    def _get_install_command(self, installer_path: Path) -> list:
        """Build installation command"""
        cmd = [str(installer_path)]

        # Authentication
        if self.username and self.password:
            cmd.extend(["--email", self.username, "--pw", self.password])

        # Unattended options
        cmd.extend(
            [
                "--accept-licenses",
                "--accept-obligations",
                "--confirm-command",
                "--default-answer",
                "install",
                self._get_package_name(),
            ]
        )

        return cmd

    def install(self):
        """Run commercial installation"""
        # Verify auth
        if not self.qt_account.exists() and not (self.username and self.password):
            raise CliInputError(
                "No Qt account credentials found. Either provide --user and --password "
                f"or ensure {self.qt_account} exists"
            )

        # Create temp dir for installer
        with tempfile.TemporaryDirectory() as temp_dir:
            installer_path = Path(temp_dir) / self.installer_filename

            # Download installer
            self.logger.info(f"Downloading Qt online installer to {installer_path}")
            self._download_installer(installer_path)

            # Run installation
            self.logger.info("Starting Qt installation")
            cmd = self._get_install_command(installer_path)

            try:
                subprocess.check_call(cmd)
            except subprocess.CalledProcessError as e:
                raise CliInputError(f"Qt installation failed with code {e.returncode}")

        self.logger.info("Qt installation completed successfully")
