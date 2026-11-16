"""本机设备名和已分配的 IPv4 地址。"""
from ipaddress import IPv4Address
import platform
import re
import socket
import subprocess


_IPV4 = r"((?:[0-9]{1,3}\.){3}[0-9]{1,3})"
_UNIX_ADDRESS = re.compile(r"\binet\s+(?:addr:)?" + _IPV4)
_WINDOWS_ADDRESS = re.compile(r"\bIPv4[^\r\n]*?" + _IPV4, re.IGNORECASE)


def local_ipv4_addresses(connection=None):
    """只读本机网络接口，优先显示当前连接实际使用的本机地址。"""
    candidates = []
    if connection is not None:
        try:
            candidates.append(connection.getsockname()[0])
        except OSError:
            pass

    system = platform.system()
    pattern = _UNIX_ADDRESS
    options = {}
    if system == "Windows":
        commands = (("ipconfig",),)
        pattern = _WINDOWS_ADDRESS
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    elif system == "Darwin":
        commands = (("/sbin/ifconfig", "-a", "-u"),)
    else:
        commands = (("ip", "-o", "-4", "addr", "show", "up"), ("ifconfig",))

    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    errors="replace", timeout=1, **options)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            candidates.extend(pattern.findall(result.stdout))
            break

    addresses = {}
    for candidate in candidates:
        try:
            address = IPv4Address(candidate)
        except ValueError:
            continue
        if not (address.is_loopback or address.is_unspecified or address.is_multicast):
            addresses[str(address)] = None
    return tuple(addresses)
