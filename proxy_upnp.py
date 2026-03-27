#!/usr/bin/env python3
from __future__ import annotations

import argparse
import email.utils
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
UPNP_NS = "urn:schemas-upnp-org:device-1-0"
NS = {"upnp": UPNP_NS}
ET.register_namespace("", UPNP_NS)

MULTICAST_HOST = "239.255.255.250"
MULTICAST_PORT = 1900
PROXY_UUID_NAMESPACE = uuid.UUID("9ad2f9d7-6e62-44df-91d1-b3c3a845fad4")

LOGGER = logging.getLogger("proxy_upnp")


@dataclass(frozen=True)
class DeviceProfile:
    friendly_name: str | None
    device_type: str | None
    service_types: tuple[str, ...]


@dataclass(frozen=True)
class ProxyConfig:
    upstream_description_url: str
    fixed_uuid: str
    mac_address: str | None
    bind_host: str
    http_port: int
    advertise_host: str
    cache_ttl: int
    request_timeout: float
    ssdp_max_age: int
    notify_interval: int
    server_header: str

    @property
    def location_url(self) -> str:
        return f"http://{self.advertise_host}:{self.http_port}/description.xml"

    @property
    def upstream_base(self) -> str:
        return upstream_base_url(self.upstream_description_url)


def normalize_uuid(value: str) -> str:
    raw = value.removeprefix("uuid:").strip()
    return str(uuid.UUID(raw))


def local_mac_address() -> str:
    node = uuid.getnode()
    octets = [f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -1, -8)]
    return ":".join(octets)


def derive_uuid_from_mac(mac_address: str) -> str:
    return str(uuid.uuid5(PROXY_UUID_NAMESPACE, mac_address.lower()))


def upstream_base_url(description_url: str) -> str:
    parsed = urllib.parse.urlparse(description_url)
    path = parsed.path or "/"
    base_path = path.rsplit("/", 1)[0] + "/"
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, base_path, "", "", "")
    )


def detect_local_ip(target_host: str, target_port: int) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((target_host, target_port))
        return probe.getsockname()[0]
    finally:
        probe.close()


def fetch_url(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "xiaodu-dlna-proxy/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def absolute_url(base_url: str, candidate: str) -> str:
    text = candidate.strip()
    if not text:
        return text
    return urllib.parse.urljoin(base_url, text)


def _child_text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    child = element.find(f"upnp:{name}", NS)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def extract_profile(root: ET.Element) -> DeviceProfile:
    device = root.find("upnp:device", NS)
    friendly_name = _child_text(device, "friendlyName")
    device_type = _child_text(device, "deviceType")
    service_types: list[str] = []
    if device is not None:
        for service in device.findall(".//upnp:service", NS):
            service_type = _child_text(service, "serviceType")
            if service_type:
                service_types.append(service_type)
    return DeviceProfile(
        friendly_name=friendly_name,
        device_type=device_type,
        service_types=tuple(dict.fromkeys(service_types)),
    )


def rewrite_description_xml(
    original_xml: bytes, fixed_uuid: str, description_url: str
) -> tuple[bytes, DeviceProfile]:
    root = ET.fromstring(original_xml)
    base_url = upstream_base_url(description_url)
    device = root.find("upnp:device", NS)
    if device is None:
        raise ValueError("UPnP description has no root device")

    friendly_name = device.find("upnp:friendlyName", NS)
    if friendly_name is not None:
        original_name = (friendly_name.text or "").strip()
        if original_name:
            friendly_name.text = f"{original_name} (proxy)"
        else:
            friendly_name.text = "UPnP Proxy"

    udn_elements = root.findall(".//upnp:UDN", NS)
    fixed_uuid_text = f"uuid:{fixed_uuid}"
    fixed_uuid_obj = uuid.UUID(fixed_uuid)
    for index, udn in enumerate(udn_elements):
        original_value = (udn.text or "").strip().removeprefix("uuid:")
        if index == 0:
            udn.text = fixed_uuid_text
        elif original_value:
            udn.text = f"uuid:{uuid.uuid5(fixed_uuid_obj, original_value)}"

    url_like_tags = {
        "presentationURL",
        "manufacturerURL",
        "modelURL",
        "SCPDURL",
        "controlURL",
        "eventSubURL",
        "url",
    }
    for element in root.iter():
        if "}" not in element.tag:
            continue
        local_name = element.tag.rsplit("}", 1)[1]
        if local_name in url_like_tags and element.text and element.text.strip():
            element.text = absolute_url(base_url, element.text)

    url_base = root.find("upnp:URLBase", NS)
    if url_base is None:
        url_base = ET.SubElement(root, f"{{{UPNP_NS}}}URLBase")
    url_base.text = base_url

    profile = extract_profile(root)
    rewritten_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return rewritten_xml, profile


class UpstreamDescriptionCache:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self._lock = threading.Lock()
        self._description_xml: bytes | None = None
        self._profile: DeviceProfile | None = None
        self._expires_at = 0.0

    def get(self, force_refresh: bool = False) -> tuple[bytes, DeviceProfile]:
        with self._lock:
            cache_is_fresh = (
                not force_refresh
                and self._description_xml is not None
                and time.time() < self._expires_at
            )
            if cache_is_fresh:
                return self._description_xml, self._profile  # type: ignore[return-value]

        raw_xml = fetch_url(
            self.config.upstream_description_url, timeout=self.config.request_timeout
        )
        rewritten_xml, profile = rewrite_description_xml(
            raw_xml,
            fixed_uuid=self.config.fixed_uuid,
            description_url=self.config.upstream_description_url,
        )

        with self._lock:
            self._description_xml = rewritten_xml
            self._profile = profile
            self._expires_at = time.time() + self.config.cache_ttl
            return rewritten_xml, profile

    def get_stale_safe(self) -> tuple[bytes, DeviceProfile]:
        try:
            return self.get()
        except Exception:
            with self._lock:
                if self._description_xml is not None and self._profile is not None:
                    LOGGER.exception("Using stale description after refresh failure")
                    return self._description_xml, self._profile
            raise


class DescriptionHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "xiaodu-dlna-proxy/1.0"

    def do_GET(self) -> None:
        if self.path in ("/", "/description.xml"):
            self.serve_description()
            return
        if self.path == "/healthz":
            self.serve_healthz()
            return
        self.proxy_to_upstream()

    def do_HEAD(self) -> None:
        if self.path in ("/", "/description.xml"):
            self.serve_description(include_body=False)
            return
        if self.path == "/healthz":
            self.serve_healthz(include_body=False)
            return
        self.proxy_to_upstream(include_body=False)

    def do_POST(self) -> None:
        self.proxy_to_upstream()

    def do_SUBSCRIBE(self) -> None:  # noqa: N802
        self.proxy_to_upstream()

    def do_UNSUBSCRIBE(self) -> None:  # noqa: N802
        self.proxy_to_upstream()

    def do_NOTIFY(self) -> None:  # noqa: N802
        self.proxy_to_upstream()

    def serve_healthz(self, include_body: bool = True) -> None:
        payload = b"ok\n"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if include_body:
            self.wfile.write(payload)

    def serve_description(self, include_body: bool = True) -> None:
        cache: UpstreamDescriptionCache = self.server.cache  # type: ignore[attr-defined]
        try:
            description_xml, _profile = cache.get_stale_safe()
        except Exception as exc:
            LOGGER.exception("Failed to build description.xml")
            payload = f"upstream fetch failed: {exc}\n".encode()
            self.send_response(HTTPStatus.BAD_GATEWAY)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/xml; charset="utf-8"')
        self.send_header("Cache-Control", f"max-age={self.server.config.cache_ttl}")  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(description_xml)))
        self.end_headers()
        if include_body:
            self.wfile.write(description_xml)

    def proxy_to_upstream(self, include_body: bool = True) -> None:
        config: ProxyConfig = self.server.config  # type: ignore[attr-defined]
        upstream_url = urllib.parse.urljoin(config.upstream_base, self.path)
        request_body = self._read_request_body()
        request_headers = self._build_upstream_headers()
        request = urllib.request.Request(
            upstream_url,
            data=request_body,
            headers=request_headers,
            method=self.command,
        )

        try:
            with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
                status = response.status
                response_headers = response.headers
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = exc.headers
            response_body = exc.read()
        except urllib.error.URLError as exc:
            LOGGER.warning("Upstream proxy request failed for %s: %s", upstream_url, exc)
            payload = f"upstream proxy failed: {exc}\n".encode()
            self.send_response(HTTPStatus.BAD_GATEWAY)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if include_body:
                self.wfile.write(payload)
            return

        self.send_response(status)
        for header, value in response_headers.items():
            if header.lower() in {
                "connection",
                "content-length",
                "date",
                "server",
                "transfer-encoding",
            }:
                continue
            self.send_header(header, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if include_body:
            self.wfile.write(response_body)

    def _build_upstream_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": "xiaodu-dlna-proxy/1.0"}
        for key, value in self.headers.items():
            if key.lower() in {
                "accept-encoding",
                "connection",
                "content-length",
                "host",
                "transfer-encoding",
            }:
                continue
            headers[key] = value
        return headers

    def _read_request_body(self) -> bytes | None:
        length = self.headers.get("Content-Length")
        if not length:
            return None
        try:
            size = int(length)
        except ValueError:
            return None
        if size <= 0:
            return None
        return self.rfile.read(size)

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)


class DescriptionHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        cache: UpstreamDescriptionCache,
        config: ProxyConfig,
    ):
        super().__init__(server_address, handler_class)
        self.cache = cache
        self.config = config

    def handle_error(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        _exc_type, exc, _tb = sys.exc_info()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            LOGGER.debug("Client %s:%s disconnected", *client_address)
            return
        super().handle_error(request, client_address)


def build_advertisements(profile: DeviceProfile, fixed_uuid: str) -> list[tuple[str, str]]:
    uuid_value = f"uuid:{fixed_uuid}"
    advertisements = [
        ("upnp:rootdevice", f"{uuid_value}::upnp:rootdevice"),
        (uuid_value, uuid_value),
    ]
    if profile.device_type:
        advertisements.append((profile.device_type, f"{uuid_value}::{profile.device_type}"))
    for service_type in profile.service_types:
        advertisements.append((service_type, f"{uuid_value}::{service_type}"))
    return advertisements


def format_http_date(epoch_seconds: float | None = None) -> str:
    return email.utils.formatdate(epoch_seconds, usegmt=True)


class SSDPServer:
    def __init__(self, config: ProxyConfig, cache: UpstreamDescriptionCache):
        self.config = config
        self.cache = cache
        self.stop_event = threading.Event()
        self.socket = self._create_socket()
        self.listener_thread = threading.Thread(
            target=self._listen_loop, name="ssdp-listener", daemon=True
        )
        self.notify_thread = threading.Thread(
            target=self._notify_loop, name="ssdp-notify", daemon=True
        )

    def _create_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", MULTICAST_PORT))
        except OSError:
            sock.bind((self.config.bind_host, MULTICAST_PORT))
        membership = socket.inet_aton(MULTICAST_HOST) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(1.0)
        return sock

    def start(self) -> None:
        self.listener_thread.start()
        self.notify_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._send_notify("ssdp:byebye")
        self.socket.close()
        self.listener_thread.join(timeout=2)
        self.notify_thread.join(timeout=2)

    def _listen_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                payload, address = self.socket.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return

            text = payload.decode("utf-8", errors="ignore")
            if not text.startswith("M-SEARCH * HTTP/1.1"):
                continue
            headers = parse_headers(text)
            if headers.get("man", "").strip('"').lower() != "ssdp:discover":
                continue
            search_target = headers.get("st", "")
            LOGGER.info("Received M-SEARCH for %s from %s:%s", search_target, *address)
            try:
                self._reply_to_search(address, search_target)
            except Exception:
                LOGGER.exception("Failed to reply to M-SEARCH")

    def _notify_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._send_notify("ssdp:alive")
            except Exception:
                LOGGER.exception("Failed to send SSDP notify")
            self.stop_event.wait(self.config.notify_interval)

    def _reply_to_search(self, address: tuple[str, int], search_target: str) -> None:
        _, profile = self.cache.get_stale_safe()
        for nt, usn in build_advertisements(profile, self.config.fixed_uuid):
            if search_target not in ("ssdp:all", nt):
                continue
            response = "\r\n".join(
                [
                    "HTTP/1.1 200 OK",
                    f"CACHE-CONTROL: max-age={self.config.ssdp_max_age}",
                    f"DATE: {format_http_date()}",
                    "EXT:",
                    f"LOCATION: {self.config.location_url}",
                    f"SERVER: {self.config.server_header}",
                    f"ST: {nt}",
                    f"USN: {usn}",
                    "",
                    "",
                ]
            ).encode("utf-8")
            self.socket.sendto(response, address)

    def _send_notify(self, nts: str) -> None:
        _, profile = self.cache.get_stale_safe()
        for nt, usn in build_advertisements(profile, self.config.fixed_uuid):
            lines = [
                "NOTIFY * HTTP/1.1",
                f"HOST: {MULTICAST_HOST}:{MULTICAST_PORT}",
                f"CACHE-CONTROL: max-age={self.config.ssdp_max_age}",
                f"LOCATION: {self.config.location_url}",
                f"NT: {nt}",
                f"NTS: {nts}",
                f"SERVER: {self.config.server_header}",
                f"USN: {usn}",
                "",
                "",
            ]
            message = "\r\n".join(lines).encode("utf-8")
            self.socket.sendto(message, (MULTICAST_HOST, MULTICAST_PORT))


def parse_headers(raw_request: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    lines = raw_request.split("\r\n")
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Proxy a UPnP MediaRenderer with a stable UUID."
    )
    parser.add_argument(
        "--upstream-description-url",
        required=True,
        help="Original description.xml URL from the speaker.",
    )
    parser.add_argument(
        "--fixed-uuid",
        help=(
            "Stable UUID to expose. Accepts both raw UUID and uuid:UUID forms. "
            "If omitted, a deterministic UUID is derived from this machine's MAC address."
        ),
    )
    parser.add_argument(
        "--bind-host",
        default="0.0.0.0",
        help="HTTP bind host. Default: 0.0.0.0",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=18080,
        help="HTTP port for the proxy description.xml. Default: 18080",
    )
    parser.add_argument(
        "--advertise-host",
        help="IP or host placed into SSDP LOCATION. Defaults to the detected local LAN IP.",
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=15,
        help="Seconds to cache the fetched description.xml. Default: 15",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=5.0,
        help="Seconds for upstream HTTP requests. Default: 5.0",
    )
    parser.add_argument(
        "--ssdp-max-age",
        type=int,
        default=1800,
        help="SSDP CACHE-CONTROL max-age. Default: 1800",
    )
    parser.add_argument(
        "--notify-interval",
        type=int,
        default=30,
        help="How often to multicast ssdp:alive. Default: 30",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log level. Default: INFO",
    )
    return parser


def parse_config(args: argparse.Namespace) -> ProxyConfig:
    upstream = urllib.parse.urlparse(args.upstream_description_url)
    if upstream.scheme not in {"http", "https"} or not upstream.hostname:
        raise ValueError("upstream-description-url must include scheme and host")

    upstream_port = upstream.port
    if upstream_port is None:
        upstream_port = 443 if upstream.scheme == "https" else 80

    mac_address: str | None = None
    if args.fixed_uuid:
        fixed_uuid = normalize_uuid(args.fixed_uuid)
    else:
        mac_address = local_mac_address()
        fixed_uuid = derive_uuid_from_mac(mac_address)

    advertise_host = args.advertise_host or detect_local_ip(upstream.hostname, upstream_port)
    return ProxyConfig(
        upstream_description_url=args.upstream_description_url,
        fixed_uuid=fixed_uuid,
        mac_address=mac_address,
        bind_host=args.bind_host,
        http_port=args.http_port,
        advertise_host=advertise_host,
        cache_ttl=args.cache_ttl,
        request_timeout=args.request_timeout,
        ssdp_max_age=args.ssdp_max_age,
        notify_interval=args.notify_interval,
        server_header="xiaodu-dlna-proxy/1.0 UPnP/1.0 DLNADOC/1.50",
    )


def warm_cache(cache: UpstreamDescriptionCache) -> DeviceProfile:
    _, profile = cache.get(force_refresh=True)
    return profile


def run_server(config: ProxyConfig) -> None:
    cache = UpstreamDescriptionCache(config)
    profile = warm_cache(cache)
    LOGGER.info(
        "Loaded upstream device %s (%s)",
        profile.friendly_name or "<unknown>",
        profile.device_type or "<unknown type>",
    )
    LOGGER.info("Proxy LOCATION: %s", config.location_url)
    LOGGER.info("Proxy UUID: uuid:%s", config.fixed_uuid)
    if config.mac_address:
        LOGGER.info("Proxy UUID source MAC: %s", config.mac_address)

    http_server = DescriptionHTTPServer(
        (config.bind_host, config.http_port),
        DescriptionHandler,
        cache=cache,
        config=config,
    )
    ssdp_server = SSDPServer(config, cache)

    ssdp_server.start()
    try:
        http_server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOGGER.info("Shutting down")
    finally:
        http_server.shutdown()
        ssdp_server.stop()
        http_server.server_close()


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = parse_config(args)
        run_server(config)
    except (ValueError, urllib.error.URLError) as exc:
        LOGGER.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
