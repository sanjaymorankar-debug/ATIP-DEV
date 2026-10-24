"""Canary run only (TEST_SPEC.md S6.4): refuse and record every network connection that
leaves this machine. Loaded by Python at start-up when this folder is on PYTHONPATH; the
canary run also points the proxy variables at a dead local port (CANARY_PROXY_PORT), so
libraries that resolve names in C still cannot get out, and Python ones are recorded."""
import ipaddress
import os
import socket

from _canary_log import record

DEAD_PORT = int(os.environ.get("CANARY_DEAD_PORT", "9"))
_LOOP = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _is_local(host):
    if host is None:
        return True
    h = host.decode() if isinstance(host, bytes) else str(host)
    if h in _LOOP:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


_getaddrinfo = socket.getaddrinfo


def _gai(host, *a, **k):
    if not _is_local(host):
        record("dns", host)
        raise socket.gaierror(socket.EAI_NONAME, f"CANARY: no network ({host})")
    return _getaddrinfo(host, *a, **k)


_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex


def _blocked(sock, addr):
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(addr, tuple):
        return None
    if not _is_local(addr[0]):
        return "connect"
    if len(addr) > 1 and addr[1] == DEAD_PORT:
        return "dead_port"                   # a request via the proxy, or the PostgreSQL runtime
    return None


def _conn(self, addr):
    kind = _blocked(self, addr)
    if kind:
        record(kind, addr)
        raise OSError(101, f"CANARY: no network ({addr})")
    return _connect(self, addr)


def _conn_ex(self, addr):
    kind = _blocked(self, addr)
    if kind:
        record(kind, addr)
        return 101
    return _connect_ex(self, addr)


socket.getaddrinfo = _gai
socket.socket.connect = _conn
socket.socket.connect_ex = _conn_ex
