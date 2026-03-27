import unittest
import xml.etree.ElementTree as ET

from proxy_upnp import (
    NS,
    build_advertisements,
    derive_uuid_from_mac,
    rewrite_description_xml,
)


SAMPLE_XML = b"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Sample Renderer</friendlyName>
    <UDN>uuid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa</UDN>
    <iconList>
      <icon>
        <url>/icon.png</url>
      </icon>
    </iconList>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <SCPDURL>/transport.xml</SCPDURL>
        <controlURL>/control/transport</controlURL>
        <eventSubURL>/event/transport</eventSubURL>
      </service>
    </serviceList>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:Foo:1</deviceType>
        <UDN>uuid:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb</UDN>
      </device>
    </deviceList>
  </device>
</root>
"""


class RewriteDescriptionXmlTests(unittest.TestCase):
    def test_derive_uuid_from_mac_is_deterministic(self) -> None:
        self.assertEqual(
            derive_uuid_from_mac("aa:bb:cc:dd:ee:ff"),
            "2855295e-6d77-5dde-b2e6-727a5b378ebd",
        )

    def test_rewrites_root_uuid_and_relative_urls(self) -> None:
        rewritten_xml, profile = rewrite_description_xml(
            SAMPLE_XML,
            fixed_uuid="11111111-2222-3333-4444-555555555555",
            description_url="http://192.168.41.104:49495/description.xml",
        )

        root = ET.fromstring(rewritten_xml)
        udns = [element.text for element in root.findall(".//upnp:UDN", NS)]
        self.assertEqual(udns[0], "uuid:11111111-2222-3333-4444-555555555555")
        self.assertNotEqual(udns[1], "uuid:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

        url_base = root.find("upnp:URLBase", NS)
        self.assertIsNotNone(url_base)
        self.assertEqual(url_base.text, "http://192.168.41.104:49495/")

        icon_url = root.find(".//upnp:icon/upnp:url", NS)
        self.assertEqual(icon_url.text, "http://192.168.41.104:49495/icon.png")

        control_url = root.find(".//upnp:service/upnp:controlURL", NS)
        self.assertEqual(
            control_url.text,
            "http://192.168.41.104:49495/control/transport",
        )

        friendly_name = root.find(".//upnp:device/upnp:friendlyName", NS)
        self.assertEqual(friendly_name.text, "Sample Renderer (proxy)")

        self.assertEqual(profile.friendly_name, "Sample Renderer (proxy)")
        self.assertEqual(profile.device_type, "urn:schemas-upnp-org:device:MediaRenderer:1")
        self.assertEqual(
            profile.service_types,
            ("urn:schemas-upnp-org:service:AVTransport:1",),
        )

    def test_build_advertisements_includes_root_uuid_device_and_services(self) -> None:
        _, profile = rewrite_description_xml(
            SAMPLE_XML,
            fixed_uuid="11111111-2222-3333-4444-555555555555",
            description_url="http://192.168.41.104:49495/description.xml",
        )
        advertisements = build_advertisements(
            profile, fixed_uuid="11111111-2222-3333-4444-555555555555"
        )

        self.assertIn(
            (
                "upnp:rootdevice",
                "uuid:11111111-2222-3333-4444-555555555555::upnp:rootdevice",
            ),
            advertisements,
        )
        self.assertIn(
            (
                "uuid:11111111-2222-3333-4444-555555555555",
                "uuid:11111111-2222-3333-4444-555555555555",
            ),
            advertisements,
        )
        self.assertIn(
            (
                "urn:schemas-upnp-org:device:MediaRenderer:1",
                "uuid:11111111-2222-3333-4444-555555555555::urn:schemas-upnp-org:device:MediaRenderer:1",
            ),
            advertisements,
        )
        self.assertIn(
            (
                "urn:schemas-upnp-org:service:AVTransport:1",
                "uuid:11111111-2222-3333-4444-555555555555::urn:schemas-upnp-org:service:AVTransport:1",
            ),
            advertisements,
        )


if __name__ == "__main__":
    unittest.main()
