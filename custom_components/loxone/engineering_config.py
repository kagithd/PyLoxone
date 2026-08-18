"""Read-only access to the complete Loxone engineering configuration."""

from __future__ import annotations

import ftplib
import io
import re
import ssl
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

LOXCC_MAGIC = 0xAABBCCEE
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_LOXCC_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 64 * 1024 * 1024
_BACKUP_NAME = re.compile(r"^sps_(?P<version>\d+)_(?P<timestamp>\d{14})\.zip$", re.IGNORECASE)
_STRUCTURAL_TYPES = {
    "Document",
    "Page",
    "Place",
    "Category",
    "User",
    "IoData",
    "Co",
    "InputRef",
    "OutputRef",
    "Permission",
}


class EngineeringConfigError(RuntimeError):
    """Raised when the engineering configuration cannot be read safely."""


@dataclass(frozen=True, slots=True)
class EngineeringElement:
    """A generic Loxone XML element prepared for later entity onboarding."""

    key: str
    xml_element: str
    loxone_type: str | None
    title: str | None
    uuid: str | None
    io_name: str | None
    parent_uuid: str | None
    room_uuid: str | None
    room: str | None
    category_uuid: str | None
    category: str | None
    suggested_platform: str | None
    attributes: dict[str, str] = field(repr=False)

    def as_public_dict(self) -> dict[str, Any]:
        """Return identity and topology data without arbitrary config values."""
        return {
            "key": self.key,
            "xml_element": self.xml_element,
            "loxone_type": self.loxone_type,
            "title": self.title,
            "uuid": self.uuid,
            "io_name": self.io_name,
            "parent_uuid": self.parent_uuid,
            "room_uuid": self.room_uuid,
            "room": self.room,
            "category_uuid": self.category_uuid,
            "category": self.category,
            "suggested_platform": self.suggested_platform,
        }


@dataclass(frozen=True, slots=True)
class EngineeringInventory:
    """Parsed snapshot of one Miniserver engineering configuration."""

    source_archive: str
    config_version: int
    config_timestamp: datetime
    downloaded_at: datetime
    xml_size: int
    elements: tuple[EngineeringElement, ...]

    @property
    def candidates(self) -> tuple[EngineeringElement, ...]:
        """Return elements that can sensibly be considered for HA onboarding."""
        return tuple(
            element
            for element in self.elements
            if element.loxone_type
            and (element.uuid or element.io_name)
            and element.loxone_type not in _STRUCTURAL_TYPES
            and not element.loxone_type.endswith("Caption")
            and (
                element.suggested_platform is not None
                or "device" in element.loxone_type.lower()
                or element.loxone_type.lower().endswith("dev")
                or element.loxone_type in {"LoxAIR", "LoxTree", "ModbusServer"}
            )
        )

    def summary(self) -> dict[str, Any]:
        """Return a compact, diagnostics-safe summary."""
        by_platform: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for element in self.candidates:
            platform = element.suggested_platform or "unmapped"
            by_platform[platform] = by_platform.get(platform, 0) + 1
            element_type = element.loxone_type or "unknown"
            by_type[element_type] = by_type.get(element_type, 0) + 1
        return {
            "source_archive": self.source_archive,
            "config_version": self.config_version,
            "config_timestamp": self.config_timestamp.isoformat(),
            "downloaded_at": self.downloaded_at.isoformat(),
            "xml_size": self.xml_size,
            "element_count": len(self.elements),
            "candidate_count": len(self.candidates),
            "candidates_by_platform": dict(sorted(by_platform.items())),
            "candidates_by_type": dict(sorted(by_type.items())),
        }


def _decompress_loxcc(data: bytes) -> bytes:
    """Decompress the documented LoxCC LZ4-style block format."""
    if len(data) < 16:
        raise EngineeringConfigError("LoxCC header is incomplete")

    magic, compressed_size, uncompressed_size, checksum = struct.unpack_from("<IIII", data)
    if magic != LOXCC_MAGIC:
        raise EngineeringConfigError(f"Unexpected LoxCC magic 0x{magic:08x}")
    compressed = memoryview(data)[16:]
    if compressed_size > len(compressed):
        raise EngineeringConfigError("LoxCC payload is shorter than its header declares")
    if uncompressed_size > MAX_XML_BYTES:
        raise EngineeringConfigError("LoxCC XML exceeds the configured safety limit")

    output = bytearray()
    position = 0
    while position < len(compressed):
        token = compressed[position]
        position += 1

        literal_length = token >> 4
        if literal_length == 15:
            while position < len(compressed):
                extra = compressed[position]
                position += 1
                literal_length += extra
                if extra != 255:
                    break
        if position + literal_length > len(compressed):
            raise EngineeringConfigError("LoxCC literal extends beyond the payload")
        output.extend(compressed[position : position + literal_length])
        position += literal_length
        if len(output) > MAX_XML_BYTES:
            raise EngineeringConfigError("Decompressed XML exceeds the safety limit")
        if position == len(compressed):
            break
        if position + 2 > len(compressed):
            raise EngineeringConfigError("LoxCC back-reference is incomplete")

        offset = int.from_bytes(compressed[position : position + 2], "little")
        position += 2
        if offset == 0 or offset > len(output):
            raise EngineeringConfigError("LoxCC contains an invalid back-reference")

        match_length = (token & 0x0F) + 4
        if token & 0x0F == 15:
            while position < len(compressed):
                extra = compressed[position]
                position += 1
                match_length += extra
                if extra != 255:
                    break
        for _ in range(match_length):
            output.append(output[-offset])
        if len(output) > MAX_XML_BYTES:
            raise EngineeringConfigError("Decompressed XML exceeds the safety limit")

    if uncompressed_size and len(output) != uncompressed_size:
        raise EngineeringConfigError(f"LoxCC size mismatch: expected {uncompressed_size}, got {len(output)}")
    if checksum and zlib.crc32(output) != checksum:
        raise EngineeringConfigError("LoxCC checksum validation failed")
    return bytes(output)


def _extract_xml(archive: bytes) -> bytes:
    """Extract and decompress sps0.LoxCC from a Miniserver backup."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise EngineeringConfigError("Engineering config archive exceeds the safety limit")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as backup:
            member = next(
                (name for name in backup.namelist() if name.lower().endswith("sps0.loxcc")),
                None,
            )
            if member is None:
                raise EngineeringConfigError("Backup does not contain sps0.LoxCC")
            info = backup.getinfo(member)
            if info.file_size > MAX_LOXCC_BYTES:
                raise EngineeringConfigError("sps0.LoxCC exceeds the safety limit")
            loxcc = backup.read(member)
    except zipfile.BadZipFile as err:
        raise EngineeringConfigError("Miniserver returned an invalid ZIP archive") from err
    return _decompress_loxcc(loxcc)


def _suggest_platform(loxone_type: str | None, io_name: str | None) -> str | None:
    """Provide a conservative platform hint without creating an HA entity yet."""
    value = (loxone_type or "").lower()
    io_value = (io_name or "").lower()
    if "pushbutton" in value or value == "button":
        return "button"
    if any(word in value for word in ("jalousie", "blind", "cover", "gate")):
        return "cover"
    if any(word in value for word in ("lightcontroller", "dimmer", "colorpicker")):
        return "light"
    if any(word in value for word in ("thermostat", "climate", "irc")):
        return "climate"
    if "dsensor" in value or "digitalinfo" in value or io_value.startswith("di"):
        return "binary_sensor"
    if "sensor" in value or value in {"weatherdata", "sysvar", "genqsensor"}:
        return "sensor"
    if "actuator" in value or "actor" in value or value in {"switch", "output"}:
        return "switch"
    return None


def parse_engineering_xml(
    xml: bytes,
    *,
    source_archive: str,
    config_version: int,
    config_timestamp: datetime,
) -> EngineeringInventory:
    """Parse all attributed XML elements, including unknown hardware types."""
    if len(xml) > MAX_XML_BYTES:
        raise EngineeringConfigError("Engineering XML exceeds the safety limit")
    try:
        lowered_prefix = xml[:4096].lower()
        if b"<!doctype" in lowered_prefix or b"<!entity" in lowered_prefix:
            raise EngineeringConfigError("Engineering XML contains forbidden declarations")
        # The size limit and declaration rejection above prevent entity expansion.
        root = ET.fromstring(xml)  # noqa: S314
    except ET.ParseError as err:
        raise EngineeringConfigError("Engineering configuration is not valid XML") from err

    rooms: dict[str, str] = {}
    categories: dict[str, str] = {}
    for node in root.iter():
        if node.tag == "C" and node.attrib.get("Type") == "Place":
            if uuid := node.attrib.get("U"):
                rooms[uuid] = node.attrib.get("Title", "")
        elif node.tag == "C" and node.attrib.get("Type") == "Category":
            if uuid := node.attrib.get("U"):
                categories[uuid] = node.attrib.get("Title", "")

    elements: list[EngineeringElement] = []

    def walk(
        node: ET.Element,
        parent_uuid: str | None = None,
        inherited_room: str | None = None,
        inherited_category: str | None = None,
    ) -> None:
        node_uuid = node.attrib.get("U")
        room_uuid = inherited_room
        category_uuid = inherited_category
        io_data = next((child for child in node if child.tag == "IoData"), None)
        if io_data is not None:
            room_uuid = io_data.attrib.get("Pr", room_uuid)
            category_uuid = io_data.attrib.get("Cr", category_uuid)

        if node.attrib:
            loxone_type = node.attrib.get("Type")
            io_name = node.attrib.get("IName")
            key = node_uuid or ":".join(
                part for part in (parent_uuid, loxone_type, io_name, node.attrib.get("Title")) if part
            )
            elements.append(
                EngineeringElement(
                    key=key or f"{node.tag}:{len(elements)}",
                    xml_element=node.tag,
                    loxone_type=loxone_type,
                    title=node.attrib.get("Title"),
                    uuid=node_uuid,
                    io_name=io_name,
                    parent_uuid=parent_uuid,
                    room_uuid=room_uuid,
                    room=rooms.get(room_uuid) if room_uuid else None,
                    category_uuid=category_uuid,
                    category=categories.get(category_uuid) if category_uuid else None,
                    suggested_platform=_suggest_platform(loxone_type, io_name),
                    attributes=dict(node.attrib),
                )
            )

        child_parent_uuid = node_uuid or parent_uuid
        for child in node:
            walk(child, child_parent_uuid, room_uuid, category_uuid)

    walk(root)
    return EngineeringInventory(
        source_archive=source_archive,
        config_version=config_version,
        config_timestamp=config_timestamp,
        downloaded_at=datetime.now(UTC),
        xml_size=len(xml),
        elements=tuple(elements),
    )


def download_engineering_inventory(
    host: str,
    username: str,
    password: str,
    *,
    ftp_port: int = 21,
    timeout: float = 30.0,
    verify_ssl: bool = True,
) -> EngineeringInventory:
    """Download the newest config through read-only explicit FTPS and parse it."""
    if verify_ssl:
        tls_context = ssl.create_default_context()
    else:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls_context.check_hostname = False
        tls_context.verify_mode = ssl.CERT_NONE

    try:
        with ftplib.FTP_TLS(context=tls_context) as ftp:
            ftp.connect(host=host, port=ftp_port, timeout=timeout)
            ftp.login(user=username, passwd=password)
            ftp.prot_p()
            ftp.cwd("/prog")
            backups: list[tuple[str, int, datetime]] = []
            for remote_name in ftp.nlst():
                filename = PurePosixPath(remote_name).name
                if match := _BACKUP_NAME.fullmatch(filename):
                    backups.append(
                        (
                            filename,
                            int(match.group("version")),
                            datetime.strptime(match.group("timestamp"), "%Y%m%d%H%M%S").replace(tzinfo=UTC),
                        )
                    )
            if not backups:
                raise EngineeringConfigError("No sps_<version>_<timestamp>.zip found in /prog")
            filename, version, timestamp = max(backups, key=lambda item: (item[2], item[1]))
            archive = bytearray()

            def append_chunk(chunk: bytes) -> None:
                archive.extend(chunk)
                if len(archive) > MAX_ARCHIVE_BYTES:
                    raise EngineeringConfigError("Engineering config archive exceeds the safety limit")

            ftp.retrbinary(f"RETR {filename}", append_chunk)
    except EngineeringConfigError:
        raise
    except (OSError, ftplib.Error) as err:
        raise EngineeringConfigError(f"Read-only FTPS config download failed: {err}") from err

    xml = _extract_xml(bytes(archive))
    return parse_engineering_xml(
        xml,
        source_archive=filename,
        config_version=version,
        config_timestamp=timestamp,
    )
