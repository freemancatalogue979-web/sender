import requests
import dns.resolver
import socket


def check_host(host, ports):
    for port in ports:
        try:
            sock = socket.create_connection((host, port), timeout=3)
            sock.close()
            return port
        except:
            pass
    return None


def ispdb_lookup(domain):
    try:
        url = f"https://autoconfig.thunderbird.net/v1.1/{domain}"
        r = requests.get(url, timeout=10)

        if r.status_code == 200:
            print("\n=== Thunderbird ISPDB ===")
            print(r.text)
            return True

    except Exception:
        pass

    return False


def srv_lookup(domain):
    print("\n=== DNS SRV Lookup ===")

    records = {
        "IMAP": f"_imaps._tcp.{domain}",
        "SMTP": f"_submission._tcp.{domain}",
        "POP3": f"_pop3s._tcp.{domain}"
    }

    found = False

    for service, record in records.items():
        try:
            answers = dns.resolver.resolve(record, "SRV")

            for r in answers:
                print(
                    f"{service}: "
                    f"{str(r.target).rstrip('.')}:"
                    f"{r.port}"
                )
                found = True

        except:
            pass

    return found


def common_hosts(domain):
    print("\n=== Host Discovery ===")

    candidates = [
        f"imap.{domain}",
        f"smtp.{domain}",
        f"mail.{domain}",
        f"webmail.{domain}"
    ]

    for host in candidates:

        imap_port = check_host(host, [993, 143])
        smtp_port = check_host(host, [465, 587, 25])

        if imap_port:
            print(f"IMAP : {host}:{imap_port}")

        if smtp_port:
            print(f"SMTP : {host}:{smtp_port}")


if __name__ == "__main__":

    domain = input("Domain: ").strip()

    ispdb_lookup(domain)
    srv_lookup(domain)
    common_hosts(domain)