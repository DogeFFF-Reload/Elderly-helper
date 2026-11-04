# -*- coding: utf-8 -*-
"""Scan 10.153.47.0/24 (Mocen_Bear hotspot subnet) for hosts with SSH (22) open."""
import socket
import concurrent.futures

NET = "10.153.47"
PORTS = (22, 8013)


def probe(ip):
    open_ports = []
    for port in PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.6)
        try:
            s.connect((ip, port))
            banner = b""
            try:
                s.settimeout(1.5)
                banner = s.recv(64)
            except Exception:
                pass
            open_ports.append((port, banner.decode(errors="replace").strip()))
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
    return (ip, open_ports) if open_ports else None


def main():
    hosts = [f"{NET}.{i}" for i in range(1, 255)]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
        for r in ex.map(probe, hosts):
            if r:
                results.append(r)
    print(f"Scan of {NET}.0/24 (ports 22, 8013):")
    if results:
        for ip, ports in results:
            for port, banner in ports:
                print(f"  {ip}:{port}  banner={banner!r}")
    else:
        print("  (no hosts with open ports)")

if __name__ == "__main__":
    main()