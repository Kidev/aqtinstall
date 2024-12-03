#!/usr/bin/env python
#
# Copyright (C) 2021 David Dalcino
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
import itertools
import operator
import posixpath
import re
import secrets as random
import shutil
from abc import ABC, abstractmethod
from logging import getLogger
from typing import Callable, Dict, Generator, Iterable, Iterator, List, NamedTuple, Optional, Set, Tuple, Union, cast
from urllib.parse import ParseResult, urlparse
from xml.etree.ElementTree import Element

import bs4
from semantic_version import SimpleSpec as SemanticSimpleSpec
from texttable import Texttable

from aqt.exceptions import ArchiveConnectionError, ArchiveDownloadError, ArchiveListError, CliInputError, EmptyMetadata
from aqt.helper import Settings, get_hash, getUrl, xml_to_modules
from aqt.repository import QtRepoProperty, Version


class SimpleSpec(SemanticSimpleSpec):
    pass

    @staticmethod
    def usage() -> str:
        return (
            "See documentation at: "
            "https://python-semanticversion.readthedocs.io/en/latest/reference.html#semantic_version.SimpleSpec\n"
            "Examples:\n"
            '* "*": matches everything\n'
            '* "5": matches every version with major=5\n'
            '* "5.6": matches every version beginning with 5.6\n'
            '* "5.*.3": matches versions with major=5 and patch=3'
        )


class Versions:
    def __init__(
        self,
        versions: Union[None, Version, Iterable[Tuple[int, Iterable[Version]]]],
    ):
        if versions is None:
            self.versions: List[List[Version]] = list()
        elif isinstance(versions, Version):
            self.versions = [[versions]]
        else:
            self.versions = [list(versions_iterator) for _, versions_iterator in versions]

    def __str__(self) -> str:
        return str(self.versions)

    def __format__(self, format_spec) -> str:
        if format_spec == "":
            return "\n".join(" ".join(str(version) for version in minor_list) for minor_list in self.versions)
        elif format_spec == "s":
            return str(self.versions)
        else:
            raise TypeError("Unsupported format.")

    def __bool__(self):
        return len(self.versions) > 0 and len(self.versions[0]) > 0

    def latest(self) -> Optional[Version]:
        if not self:
            return None
        return self.versions[-1][-1]

    def __iter__(self) -> Generator[List[Version], None, None]:
        for item in self.versions:
            yield item

    def flattened(self) -> List[Version]:
        """Return a flattened list of all versions"""
        return [version for row in self for version in row]


def get_semantic_version(qt_ver: str, is_preview: bool) -> Optional[Version]:
    """Converts a Qt version string into a semantic version.
    Handles both traditional format (e.g. '51212' -> '5.12.12') and
    new format with underscores (e.g. '6_7_3' -> '6.7.3').

    Args:
        qt_ver: Version string (e.g. '51212', '600', '6_7_3')
        is_preview: Whether this is a preview version

    Returns:
        Version object or None if invalid format

    Examples:
        >>> get_semantic_version('51212', False)
        Version('5.12.12')
        >>> get_semantic_version('600', False)
        Version('6.0.0')
        >>> get_semantic_version('6_7_3', False)
        Version('6.7.3')
    """
    if not qt_ver:
        return None

    try:
        # Handle versions with underscores (new format)
        if "_" in qt_ver:
            parts = qt_ver.split("_")
            if len(parts) < 2 or len(parts) > 3:
                return None

            major = int(parts[0])
            minor = int(parts[1])
            patch = int(parts[2]) if len(parts) == 3 else 0

            version_str = f"{major}.{minor}.{patch}"
            if is_preview:
                version_str += ".dev0"
            return Version(version_str)

        # Handle traditional format (continuous digits)
        if any(not ch.isdigit() for ch in qt_ver):
            return None

        # For traditional format, construct version parts first
        if len(qt_ver) >= 4:
            major, minor, patch = int(qt_ver[:1]), int(qt_ver[1:3]), int(qt_ver[3:])
        elif len(qt_ver) == 3:
            major, minor, patch = int(qt_ver[:1]), int(qt_ver[1:2]), int(qt_ver[2:])
        elif len(qt_ver) == 2:
            major, minor, patch = int(qt_ver[:1]), int(qt_ver[1:]), 0
        else:
            return None

        # Then create the version string with appropriate preview suffix
        version_str = f"{major}.{minor}.{patch}"
        if is_preview:
            version_str += ".dev0"

        return Version(version_str)

    except ValueError:
        return None


class ArchiveId:
    CATEGORIES = QtRepoProperty.CATEGORIES
    HOSTS = QtRepoProperty.HOSTS
    TARGETS_FOR_HOST = QtRepoProperty.TARGETS_FOR_HOST
    EXTENSIONS_REQUIRED_ANDROID_QT6 = QtRepoProperty.EXTENSIONS_REQUIRED_ANDROID_QT6
    ALL_EXTENSIONS = QtRepoProperty.ALL_EXTENSIONS

    def __init__(self, category: str, host: str, target: str):
        if category not in ArchiveId.CATEGORIES:
            raise ValueError("Category '{}' is invalid".format(category))
        if host not in ArchiveId.HOSTS:
            raise ValueError("Host '{}' is invalid".format(host))
        if target not in ArchiveId.TARGETS_FOR_HOST[host]:
            raise ValueError("Target '{}' is invalid".format(target))
        self.category: str = category
        self.host: str = host
        self.target: str = target

    def is_preview(self) -> bool:
        return False

    def is_qt(self) -> bool:
        return self.category == "qt"

    def is_tools(self) -> bool:
        return self.category == "tools"

    def to_url(self) -> str:
        if (
            self.target == "desktop"
            and self.host in ("wasm_singlethread", "wasm_multithread")
            and self.version >= Version("6.7.0")
        ):
            return "online/qtsdkrepository/all_os/wasm/"
        return "online/qtsdkrepository/{os}{arch}/{target}/".format(
            os=self.host,
            arch=(
                "_x86"
                if self.host == "windows"
                else ("" if self.host in ("linux_arm64", "all_os", "windows_arm64") else "_x64")
            ),
            target=self.target,
        )

    def to_folder(self, version: Version, qt_version_no_dots: str, extension: Optional[str] = None) -> str:
        if version >= Version("6.8.0"):
            return "{category}{major}_{ver}/{category}{major}_{ver}{ext}".format(
                category=self.category,
                major=qt_version_no_dots[0],
                ver=qt_version_no_dots,
                ext="_" + extension if extension else "",
            )
        else:
            return "{category}{major}_{ver}{ext}".format(
                category=self.category,
                major=qt_version_no_dots[0],
                ver=qt_version_no_dots,
                ext="_" + extension if extension else "",
            )

    def all_extensions(self, version: Version) -> List[str]:
        if self.target == "desktop" and QtRepoProperty.is_in_wasm_range(self.host, version):
            return ["", "wasm"]
        elif self.target == "desktop" and QtRepoProperty.is_in_wasm_threaded_range(version):
            return ["", "wasm_singlethread", "wasm_multithread"]
        elif self.target == "android" and version >= Version("6.0.0"):
            return list(ArchiveId.EXTENSIONS_REQUIRED_ANDROID_QT6)
        else:
            return [""]

    def __str__(self) -> str:
        return "{cat}/{host}/{target}".format(
            cat=self.category,
            host=self.host,
            target=self.target,
        )


class TableMetadata(ABC):
    """A data class that holds tool or module details. Can be pretty-printed as a table."""

    def __init__(self, table_data: Dict[str, Dict[str, str]]):
        self.table_data: Dict[str, Dict[str, str]] = table_data
        self.format_field_for_tty("Description")

    def format_field_for_tty(self, field: str):
        for key in self.table_data.keys():
            if field in self.table_data[key] and self.table_data[key][field]:
                self.table_data[key][field] = self.table_data[key][field].replace("<br>", "\n")

    std_keys_to_headings = {
        "ReleaseDate": "Release Date",
        "DisplayName": "Display Name",
        "CompressedSize": "Download Size",
        "UncompressedSize": "Installed Size",
    }

    @classmethod
    def map_key_to_heading(cls, key: str) -> str:
        return TableMetadata.std_keys_to_headings.get(key, key)

    @property
    @abstractmethod
    def short_heading_keys(self) -> Iterable[str]: ...

    @property
    @abstractmethod
    def long_heading_keys(self) -> Iterable[str]: ...

    @property
    @abstractmethod
    def name_heading(self) -> str: ...

    def __format__(self, format_spec: str) -> str:
        short = False
        if format_spec == "{:s}":
            return str(self)
        if format_spec == "":
            max_width: int = 0
        elif format_spec == "{:T}":
            short = True
            max_width = 0
        else:
            match = re.match(r"\{?:?(\d+)t\}?", format_spec)
            if match:
                g = match.groups()
                max_width = int(g[0])
            else:
                raise ValueError("Wrong format {}".format(format_spec))
        table = Texttable(max_width=max_width)
        table.set_deco(Texttable.HEADER)

        heading_keys = self.short_heading_keys if short else self.long_heading_keys
        heading = [self.name_heading, *[self.map_key_to_heading(key) for key in heading_keys]]
        table.header(heading)
        table.add_rows(self._rows(heading_keys), header=False)
        return cast(str, table.draw())

    def __bool__(self):
        return bool(self.table_data)

    def _rows(self, keys: Iterable[str]) -> List[List[str]]:
        return [[name, *[content[key] for key in keys]] for name, content in sorted(self.table_data.items())]


class ToolData(TableMetadata):
    """A data class hold tool details."""

    @property
    def short_heading_keys(self) -> Iterable[str]:
        return "Version", "ReleaseDate"

    @property
    def long_heading_keys(self) -> Iterable[str]:
        return "Version", "ReleaseDate", "DisplayName", "Description"

    @property
    def name_heading(self) -> str:
        return "Tool Variant Name"


class ModuleData(TableMetadata):
    """A data class hold module details."""

    @property
    def short_heading_keys(self) -> Iterable[str]:
        return ("DisplayName",)

    @property
    def long_heading_keys(self) -> Iterable[str]:
        return "DisplayName", "ReleaseDate", "CompressedSize", "UncompressedSize"

    @property
    def name_heading(self) -> str:
        return "Module Name"


class MetadataFactory:
    """Retrieve metadata of Qt variations, versions, and descriptions from Qt site."""

    Metadata = Union[List[str], Versions, ToolData, ModuleData]
    Action = Callable[[], Metadata]
    SrcDocExamplesQuery = NamedTuple(
        "SrcDocExamplesQuery", [("cmd_type", str), ("version", Version), ("is_modules_query", bool)]
    )
    ModulesQuery = NamedTuple("ModulesQuery", [("version_str", str), ("arch", str)])

    def __init__(
        self,
        archive_id: ArchiveId,
        *,
        base_url: Optional[str] = None,
        spec: Optional[SimpleSpec] = None,
        is_latest_version: bool = False,
        modules_query: Optional[ModulesQuery] = None,
        architectures_ver: Optional[str] = None,
        archives_query: Optional[List[str]] = None,
        src_doc_examples_query: Optional[SrcDocExamplesQuery] = None,
        tool_name: Optional[str] = None,
        is_long_listing: bool = False,
    ):
        """
        Construct MetadataFactory.

        :param spec:                When set, the MetadataFactory will filter out all versions of
                                    Qt that don't fit this SimpleSpec.
        :param is_latest_version:   When True, the MetadataFactory will find all versions of Qt
                                    matching filters, and only print the most recent version
        :param modules_query:       [Version of Qt, architecture] for which to list modules
        :param architectures_ver:   Version of Qt for which to list architectures
        :param archives_query:      [Qt_Version, architecture, *module_names]: used to print list of archives
        :param tool_name:           Name of a tool, without architecture, ie "tools_qtcreator" or "tools_ifw"
        :param is_long_listing:     If true, long listing is used for tools output
        """
        self.logger = getLogger("aqt.metadata")
        self.archive_id = archive_id
        self.spec = spec
        self.base_url = base_url or Settings.baseurl

        if archive_id.is_tools():
            if tool_name is not None:
                if not tool_name.startswith("tools_") and tool_name != "sdktool":
                    _tool_name = f"tools_{tool_name}"
                else:
                    _tool_name = tool_name
                if is_long_listing:
                    self.request_type = "tool long listing"
                    self._action: MetadataFactory.Action = lambda: self.fetch_tool_long_listing(_tool_name)
                else:
                    self.request_type = "tool variant names"
                    self._action = lambda: self.fetch_tool_modules(_tool_name)
            else:
                self.request_type = "tools"
                self._action = self.fetch_tools
        elif is_latest_version:
            self.request_type = "latest version"
            self._action = lambda: Versions(self.fetch_latest_version(ext=""))
        elif modules_query is not None:
            version, arch = modules_query.version_str, modules_query.arch
            if is_long_listing:
                self.request_type = "long modules"
                self._action = lambda: self.fetch_long_modules(self._to_version(version, arch), arch)
            else:
                self.request_type = "modules"
                self._action = lambda: self.fetch_modules(self._to_version(version, arch), arch)
        elif architectures_ver is not None:
            ver_str: str = architectures_ver
            self.request_type = "architectures"
            self._action = lambda: self.fetch_arches(self._to_version(ver_str, None))
        elif archives_query:
            if len(archives_query) < 2:
                raise CliInputError("The '--archives' flag requires a 'QT_VERSION' and an 'ARCHITECTURE' parameter.")
            self.request_type = "archives for modules" if len(archives_query) > 2 else "archives for qt"
            version, arch, modules = archives_query[0], archives_query[1], archives_query[2:]
            self._action = lambda: self.fetch_archives(self._to_version(version, arch), arch, modules)
        elif src_doc_examples_query is not None:
            q: MetadataFactory.SrcDocExamplesQuery = src_doc_examples_query
            if q.is_modules_query:
                self.request_type = f"modules for {q.cmd_type}"
                self._action = lambda: self.fetch_modules_sde(q.cmd_type, q.version)
            else:
                self.request_type = f"archives for {q.cmd_type}"
                self._action = lambda: self.fetch_archives_sde(q.cmd_type, q.version)
        else:
            self.request_type = "versions"
            self._action = self.fetch_versions

    def getList(self) -> Metadata:
        return self._action()

    def fetch_arches(self, version: Version) -> List[str]:
        arches = []
        qt_ver_str = self._get_qt_version_str(version)
        for extension in self.archive_id.all_extensions(version):
            modules: Dict[str, Dict[str, str]] = {}
            folder = self.archive_id.to_folder(version, qt_ver_str, extension)
            try:
                modules = self._fetch_module_metadata(folder)
            except ArchiveDownloadError as e:
                if extension == "":
                    raise
                else:
                    self.logger.debug(e)
                    self.logger.debug(
                        f"Failed to retrieve arches list with extension `{extension}`. "
                        f"Please check that this extension exists for this version of Qt: "
                        f"if not, code changes will be necessary."
                    )
                    # It's ok to swallow this error: we will still print the other available architectures that aqt can
                    # install successfully. This is to prevent future errors such as those reported in #643

            for name in modules.keys():
                ver, arch = name.split(".")[-2:]
                if ver == qt_ver_str:
                    arches.append(arch)

        return arches

    def fetch_versions(self, extension: str = "") -> Versions:
        def filter_by(ver: Version, ext: str) -> bool:
            return (self.spec is None or ver in self.spec) and ext == extension

        versions_extensions = self.get_versions_extensions(
            self.fetch_http(self.archive_id.to_url(), False), self.archive_id.category
        )
        versions = sorted([ver for ver, ext in versions_extensions if ver is not None and filter_by(ver, ext)])
        grouped = cast(Iterable[Tuple[int, Iterable[Version]]], itertools.groupby(versions, lambda version: version.minor))
        return Versions(grouped)

    def fetch_latest_version(self, ext: str) -> Optional[Version]:
        return self.fetch_versions(ext).latest()

    def fetch_tools(self) -> List[str]:
        html_doc = self.fetch_http(self.archive_id.to_url(), False)
        return list(self.iterate_folders(html_doc, self.base_url, filter_category="tools"))

    def fetch_tool_modules(self, tool_name: str) -> List[str]:
        tool_data = self._fetch_module_metadata(tool_name)
        return list(tool_data.keys())

    def fetch_tool_by_simple_spec(self, tool_name: str, simple_spec: SimpleSpec) -> Optional[Dict[str, str]]:
        # Get data for all the tool modules
        all_tools_data = self._fetch_module_metadata(tool_name)
        return self.choose_highest_version_in_spec(all_tools_data, simple_spec)

    def fetch_tool_long_listing(self, tool_name: str) -> ToolData:
        return ToolData(self._fetch_module_metadata(tool_name))

    @staticmethod
    def choose_highest_version_in_spec(
        all_tools_data: Dict[str, Dict[str, str]], simple_spec: SimpleSpec
    ) -> Optional[Dict[str, str]]:
        # Get versions of all modules. Fail if version cannot be determined.
        try:
            tools_versions = [
                (name, tool_data, Version.permissive(tool_data["Version"])) for name, tool_data in all_tools_data.items()
            ]
        except ValueError:
            return None

        # Remove items that don't conform to simple_spec
        tools_versions = [tool_item for tool_item in tools_versions if tool_item[2] in simple_spec]

        try:
            # Return the conforming item with the highest version.
            # If there are multiple items with the same version, the result will not be predictable.
            return max(tools_versions, key=operator.itemgetter(2))[1]
        except ValueError:
            # There were no tools that fit the simple_spec
            return None

    def _to_version(self, qt_ver: str, arch: Optional[str]) -> Version:
        """
        Turns a string in the form of `5.X.Y | latest` into a semantic version.
        If the string does not fit either of these forms, CliInputError will be raised.
        If qt_ver == latest, and no versions exist corresponding to the filters specified,
        then CliInputError will be raised.
        If qt_ver == latest, and an HTTP error occurs, requests.RequestException will be raised.

        :param qt_ver:  Either the literal string `latest`, or a semantic version
                        with each part separated with dots.
        """
        assert qt_ver
        if qt_ver == "latest":
            ext = QtRepoProperty.extension_for_arch(arch, True) if arch else ""
            latest_version = self.fetch_latest_version(ext)
            if not latest_version:
                msg = "There is no latest version of Qt with the criteria '{}'".format(self.describe_filters())
                raise CliInputError(msg)
            return latest_version
        try:
            version = Version(qt_ver)
        except ValueError as e:
            raise CliInputError(e) from e
        return version

    def fetch_http(self, rest_of_url: str, is_check_hash: bool = True) -> str:
        timeout = (Settings.connection_timeout, Settings.response_timeout)
        expected_hash = get_hash(rest_of_url, Settings.hash_algorithm, timeout) if is_check_hash else None
        base_urls = self.base_url, random.choice(Settings.fallbacks)

        err: BaseException = AssertionError("unraisable")

        for i, base_url in enumerate(base_urls):
            try:
                url = posixpath.join(base_url, rest_of_url)
                return getUrl(url=url, timeout=timeout, expected_hash=expected_hash)

            except (ArchiveDownloadError, ArchiveConnectionError) as e:
                err = e
                if i < len(base_urls) - 1:
                    getLogger("aqt.metadata").debug(
                        f"Connection to '{base_url}' failed. Retrying with fallback '{base_urls[i + 1]}'."
                    )
        raise err from err

    def iterate_folders(self, html_doc: str, html_url: str, *, filter_category: str = "") -> Generator[str, None, None]:
        def link_to_folder(link: bs4.element.Tag) -> str:
            raw_url: str = str(link.get("href", default=""))
            url: ParseResult = urlparse(raw_url)
            if url.scheme or url.netloc:
                return ""
            url_path: str = posixpath.normpath(url.path)
            if "/" in url_path or url_path == "." or url_path == "..":
                return ""
            return url_path

        try:
            soup: bs4.BeautifulSoup = bs4.BeautifulSoup(html_doc, "html.parser")
            for link in soup.find_all("a"):
                folder: str = link_to_folder(link)
                if not folder:
                    continue
                if folder.startswith(filter_category):
                    yield folder
                if filter_category == "tools" and folder == "sdktool":
                    yield folder
        except Exception as e:
            raise ArchiveConnectionError(
                f"Failed to retrieve the expected HTML page at {html_url}",
                suggested_action=[
                    "Check your network connection.",
                    f"Make sure that you can access {html_url} in your web browser.",
                ],
            ) from e

    def get_versions_extensions(self, html_doc: str, category: str) -> Iterator[Tuple[Optional[Version], str]]:
        def folder_to_version_extension(folder: str) -> Tuple[Optional[Version], str]:
            components = folder.split("_", maxsplit=2)
            ext = "" if len(components) < 3 else components[2]
            ver = "" if len(components) < 2 else components[1]
            return (
                get_semantic_version(qt_ver=ver, is_preview="preview" in ext),
                ext,
            )

        return map(
            folder_to_version_extension,
            self.iterate_folders(html_doc, self.base_url, filter_category=category),
        )

    @staticmethod
    def _has_nonempty_downloads(element: Element) -> bool:
        """Returns True if the element has a nonempty '<DownloadableArchives/>' tag"""
        downloads = element.find("DownloadableArchives")
        update_file = element.find("UpdateFile")
        if downloads is None or update_file is None:
            return False
        uncompressed_size = int(update_file.attrib["UncompressedSize"])
        return downloads.text is not None and uncompressed_size >= Settings.min_module_size

    def _get_qt_version_str(self, version: Version) -> str:
        """Returns a Qt version, without dots, that works in the Qt repo urls and Updates.xml files"""
        # NOTE: The url at `<base>/<host>/<target>/qt5_590/` does not exist; the real one is `qt5_59`
        patch = (
            ""
            if version.prerelease or self.archive_id.is_preview() or version in SimpleSpec("5.9.0")
            else str(version.patch)
        )
        return f"{version.major}{version.minor}{patch}"

    def _fetch_module_metadata(self, folder: str, predicate: Optional[Callable[[Element], bool]] = None):
        rest_of_url = posixpath.join(self.archive_id.to_url(), folder, "Updates.xml")
        xml = self.fetch_http(rest_of_url) if not Settings.ignore_hash else self.fetch_http(rest_of_url, False)
        return xml_to_modules(
            xml,
            predicate=predicate if predicate else MetadataFactory._has_nonempty_downloads,
        )

    def fetch_modules(self, version: Version, arch: str) -> List[str]:
        """Returns list of modules"""
        # Get standard modules first
        extension = QtRepoProperty.extension_for_arch(arch, version >= Version("6.0.0"))
        qt_ver_str = self._get_qt_version_str(version)
        # Example: re.compile(r"^(preview\.)?qt\.(qt5\.)?590\.(.+)$")
        pattern = re.compile(r"^(preview\.)?qt\.(qt" + str(version.major) + r"\.)?" + qt_ver_str + r"\.(.+)$")
        modules_meta = self._fetch_module_metadata(self.archive_id.to_folder(version, qt_ver_str, extension))

        def to_module_arch(name: str) -> Tuple[Optional[str], Optional[str]]:
            _match = pattern.match(name)
            if not _match:
                return None, None
            module_with_arch = _match.group(3)
            if "." not in module_with_arch:
                return module_with_arch, None
            module, arch = module_with_arch.rsplit(".", 1)
            if module.startswith("addons."):
                module = module[len("addons."):]
            return module, arch

        # Get regular modules
        modules: Set[str] = set()
        for name in modules_meta.keys():
            module, _arch = to_module_arch(name)
            if _arch == arch:
                modules.add(cast(str, module))

        # Check for extensions if Qt >= 6.8.0
        if version >= Version("6.8.0"):
            os_arch = self.archive_id.host + (
                "_x86"
                if self.archive_id.host == "windows"
                else ("" if self.archive_id.host in ("linux_arm64", "all_os", "windows_arm64") else "_x64")
            )

            # Convert arch for extensions path
            folder_arch, package_arch = QtRepoProperty.convert_arch_for_extension(self.archive_id.host, arch)

            # Try each known extension
            for extension_module in QtRepoProperty.known_extensions():
                extension_path = posixpath.join(
                    "online/qtsdkrepository", os_arch, "extensions", extension_module, qt_ver_str, folder_arch, "Updates.xml"
                )

                try:
                    self.fetch_http(extension_path)
                    modules.add(extension_module)
                except ArchiveDownloadError:
                    continue

        return sorted(modules)

    @staticmethod
    def require_text(element: Element, key: str) -> str:
        node = element.find(key)
        if node is None:
            raise ArchiveListError(f"Downloaded metadata does not match the expected structure. Missing key: {key}")
        return node.text or ""

    def fetch_long_modules(self, version: Version, arch: str) -> ModuleData:
        """Returns long listing of modules"""
        extension = QtRepoProperty.extension_for_arch(arch, version >= Version("6.0.0"))
        qt_ver_str = self._get_qt_version_str(version)
        # Example: re.compile(r"^(preview\.)?qt\.(qt5\.)?590(\.addons)?\.(?P<module>[^.]+)\.gcc_64$")
        pattern = re.compile(
            r"^(preview\.)?qt\.(qt"
            + str(version.major)
            + r"\.)?"
            + qt_ver_str
            + r"(\.addons)?\.(?P<module>[^.]+)\."
            + arch
            + r"$"
        )

        def matches_arch(element: Element) -> bool:
            return bool(pattern.match(MetadataFactory.require_text(element, "Name")))

        modules_meta = self._fetch_module_metadata(self.archive_id.to_folder(version, qt_ver_str, extension), matches_arch)
        m: Dict[str, Dict[str, str]] = {}
        for key, value in modules_meta.items():
            match = pattern.match(key)
            if match is not None:
                module = match.group("module")
                if module is not None:
                    m[module] = value

        return ModuleData(m)

    def fetch_modules_sde(self, cmd_type: str, version: Version) -> List[str]:
        """Returns list of modules for src/doc/examples"""
        assert cmd_type in ("doc", "examples") and self.archive_id.target in (
            "desktop",
            "qt",
        ), "Internal misuse of fetch_modules_sde"
        qt_ver_str = self._get_qt_version_str(version)
        modules_meta = self._fetch_module_metadata(self.archive_id.to_folder(version, qt_ver_str, "src_doc_examples"))
        # pattern: Match all names "qt.qt5.12345.doc.(\w+)
        pattern = re.compile(r"^qt\.(qt" + str(version.major) + r"\.)?" + qt_ver_str + r"\." + cmd_type + r"\.(.+)$")

        modules: List[str] = []
        for name in modules_meta:
            _match = pattern.match(name)
            if _match:
                modules.append(_match.group(2))
        return modules

    def fetch_archives_sde(self, cmd_type: str, version: Version) -> List[str]:
        """Returns list of archives for src/doc/examples"""
        assert cmd_type in ("src", "doc", "examples") and self.archive_id.target in (
            "desktop",
            "qt",
        ), "Internal misuse of fetch_archives_sde"
        return self.fetch_archives(version, cmd_type, [], is_sde=True)

    def fetch_archives(self, version: Version, arch: str, modules: List[str], is_sde: bool = False) -> List[str]:
        extension = "src_doc_examples" if is_sde else QtRepoProperty.extension_for_arch(arch, version >= Version("6.0.0"))
        qt_version_str = self._get_qt_version_str(version)
        nonempty = MetadataFactory._has_nonempty_downloads

        def all_modules(element: Element) -> bool:
            _module, _arch = MetadataFactory.require_text(element, "Name").split(".")[-2:]
            return _arch == arch and _module != qt_version_str and nonempty(element)

        def specify_modules(element: Element) -> bool:
            _module, _arch = MetadataFactory.require_text(element, "Name").split(".")[-2:]
            return _arch == arch and _module in modules and nonempty(element)

        def no_modules(element: Element) -> bool:
            name: Optional[str] = getattr(element.find("Name"), "text", None)
            return name is not None and name.endswith(f".{qt_version_str}.{arch}") and nonempty(element)

        predicate = no_modules if not modules else all_modules if "all" in modules else specify_modules
        try:
            mod_metadata = self._fetch_module_metadata(
                self.archive_id.to_folder(version, qt_version_str, extension), predicate=predicate
            )
        except (AttributeError, ValueError) as e:
            raise ArchiveListError(f"Downloaded metadata is corrupted. {e}") from e

        # Did we find all requested modules?
        if modules and "all" not in modules:
            requested_set = set(modules)
            actual_set = set([_name.split(".")[-2] for _name in mod_metadata.keys()])
            not_found = sorted(requested_set.difference(actual_set))
            if not_found:
                raise CliInputError(
                    f"The requested modules were not located: {not_found}", suggested_action=suggested_follow_up(self)
                )

        csv_lists = [mod["DownloadableArchives"] for mod in mod_metadata.values()]
        return sorted(set([arc.split("-")[0] for csv in csv_lists for arc in csv.split(", ")]))

    def describe_filters(self) -> str:
        if self.spec is None:
            return str(self.archive_id)
        return "{} with spec {}".format(self.archive_id, self.spec)

    def fetch_default_desktop_arch(self, version: Version, is_msvc: bool = False) -> str:
        assert self.archive_id.target == "desktop", "This function is meant to fetch desktop architectures"
        if self.archive_id.host == "linux":
            if version >= Version("6.7.0"):
                return "linux_gcc_64"
            else:
                return "gcc_64"
        elif self.archive_id.host == "linux_arm64":
            return "linux_gcc_arm64"
        elif self.archive_id.host == "mac":
            return "clang_64"
        elif self.archive_id.host == "windows" and is_msvc:
            if version >= Version("6.8.0"):
                return "win64_msvc2022_64"
            else:
                return "win64_msvc2019_64"
        arches = [arch for arch in self.fetch_arches(version) if QtRepoProperty.MINGW_ARCH_PATTERN.match(arch)]
        selected_arch = QtRepoProperty.select_default_mingw(arches, is_dir=False)
        if not selected_arch:
            raise EmptyMetadata("No default desktop architecture available")
        return selected_arch


def suggested_follow_up(meta: MetadataFactory) -> List[str]:
    """Makes an informed guess at what the user got wrong, in the event of an error."""
    msg = []
    list_cmd = "list-tool" if meta.archive_id.is_tools() else "list-qt"
    base_cmd = "aqt {0} {1.host} {1.target}".format(list_cmd, meta.archive_id)
    versions_msg = f"Please use '{base_cmd}' to show versions of Qt available."
    arches_msg = f"Please use '{base_cmd} --arch <QT_VERSION>' to show architectures available."

    if meta.archive_id.is_tools() and meta.request_type == "tool variant names":
        msg.append(f"Please use '{base_cmd}' to check what tools are available.")
    elif meta.spec is not None:
        msg.append(
            f"Please use '{base_cmd}' to check that versions of {meta.archive_id.category} "
            f"exist within the spec '{meta.spec}'."
        )
    elif meta.request_type in ("architectures", "modules", "extensions"):
        msg.append(f"Please use '{base_cmd}' to show versions of Qt available.")
        if meta.request_type == "modules":
            msg.append(f"Please use '{base_cmd} --arch <QT_VERSION>' to list valid architectures.")
    elif meta.request_type == "archives for modules":
        msg.extend([versions_msg, arches_msg, f"Please use '{base_cmd} --modules <QT_VERSION>' to show modules available."])
    elif meta.request_type == "archives for qt":
        msg.extend([versions_msg, arches_msg])

    return msg


def show_list(meta: MetadataFactory):
    try:
        output = meta.getList()
        if not output:
            raise EmptyMetadata(
                f"No {meta.request_type} available for this request.", suggested_action=suggested_follow_up(meta)
            )
        if isinstance(output, Versions):
            print(format(output))
        elif isinstance(output, TableMetadata):
            width: int = shutil.get_terminal_size((0, 40)).columns
            if width == 0:  # notty ?
                print(format(output, "{:0t}"))
            elif width < 95:  # narrow terminal
                print(format(output, "{:T}"))
            else:
                print("{0:{1}t}".format(output, width))
        elif meta.archive_id.is_tools():
            print(*output, sep="\n")
        else:
            print(*output, sep=" ")
    except (ArchiveDownloadError, ArchiveConnectionError) as e:
        e.append_suggested_follow_up(suggested_follow_up(meta))
        raise e from e
