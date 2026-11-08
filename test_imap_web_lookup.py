#!/usr/bin/env python3
"""Discover IMAP host for a domain using web autodiscovery.

This script checks the common email client discovery URLs:
- https://autoconfig.<domain>/mail/config-v1.1.xml
- http://autoconfig.<domain>/mail/config-v1.1.xml
- https://<domain>/.well-known/autoconfig/mail/config-v1.1.xml
- http://<domain>/.well-known/autoconfig/mail/config-v1.1.xml
- https://autodiscover.<domain>/autodiscover/autodiscover.xml
- http://autodiscover.<domain>/autodiscover/autodiscover.xml

It returns the first discovered IMAP host and port.
"""

import sys
import ssl
import socket
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT = 15

URL_TEMPLATES = [
    "https://autoconfig.{domain}/mail/config-v1.1.xml",
    "http://autoconfig.{domain}/mail/config-v1.1.xml",
    "https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
    "http://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
    "https://autodiscover.{domain}/autodiscover/autodiscover.xml",
    "http://autodiscover.{domain}/autodiscover/autodiscover.xml",
]


def fetch_url(url):
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read()
            return response.getcode(), content_type, data
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), None
    except Exception as ex:
        return None, None, None


def parse_imap_from_autoconfig(xml_data):
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return None

    # Thunderbird-style autoconfig format
    for incoming in root.findall('.//incomingServer'):
        server_type = incoming.get('type', '').lower() or (incoming.findtext('type') or '').lower()
        protocol = (incoming.findtext('protocol') or '').lower()
        if server_type == 'imap' or protocol == 'imap':
            host = incoming.findtext('hostname') or incoming.findtext('server') or incoming.findtext('host')
            port = incoming.findtext('port')
            username = incoming.findtext('username')
            ssl_type = (incoming.findtext('socketType') or incoming.findtext('ssl') or '').lower()
            if host and port:
                return host.strip(), int(port.strip()), ssl_type

    # Outlook/Exchange autodiscover format
    for server in root.findall('.//Server'):
        server_type = (server.findtext('Type') or '').lower()
        if server_type == 'imap':
            host = server.findtext('Host')
            port = server.findtext('Port')
            ssl_type = (server.findtext('SSL') or '').lower()
            if host and port:
                return host.strip(), int(port.strip()), ssl_type

    # Generic fallback by searching for IMAP entries
    for incoming in root.findall('.//incomingServer'):
        protocol = (incoming.findtext('protocol') or incoming.findtext('type') or '').lower()
        if 'imap' in protocol:
            host = incoming.findtext('hostname') or incoming.findtext('server') or incoming.findtext('host')
            port = incoming.findtext('port')
            if host and port:
                return host.strip(), int(port.strip()), None

    return None


def is_port_open(host, port, use_ssl=False):
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            if use_ssl:
                context = ssl.create_default_context()
                with context.wrap_socket(sock, server_hostname=host):
                    pass
            return True
    except Exception:
        return False


def discover_imap(domain):
    results = []
    for template in URL_TEMPLATES:
        url = template.format(domain=domain)
        print(f"Trying {url}")
        code, content_type, data = fetch_url(url)
        if code and data:
            print(f"  succeeded: HTTP {code}, content-type={content_type}")
            discovered = parse_imap_from_autoconfig(data)
            if discovered:
                host, port, ssl_type = discovered
                print(f"  discovered IMAP: {host}:{port} ssl={ssl_type}")
                return host, port, ssl_type, url
            else:
                print("  no IMAP config found in response")
        elif code:
            print(f"  HTTP status {code}")
        else:
            print("  request failed")

    # Fallback candidates based on domain patterns
    candidates = [f"mail.{domain}", f"imap.{domain}", f"smtp.{domain}"]
    for host in candidates:
        print(f"Trying fallback host {host}:993")
        if is_port_open(host, 993, use_ssl=True):
            return host, 993, 'ssl', 'fallback'
    return None


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python test_imap_web_lookup.py <domain>")
        sys.exit(1)

    domain = sys.argv[1].strip()
    result = discover_imap(domain)
    if result:
        host, port, ssl_type, source = result
        print(f"IMAP host for {domain}: {host}:{port} (source={source})")
    else:
        print(f"Unable to discover IMAP host for {domain}")
