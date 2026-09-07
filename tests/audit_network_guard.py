import socket
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex

# Fleet isolation: the coach's default discovery targets are explicitly
# forbidden even on loopback, so a test bug or fixture leak cannot contact the
# live self-play (50052) or coaching (50054) inference endpoints.
BLOCKED_PORTS = (50052, 50054)


def allowed(address):
    return (
        isinstance(address, tuple)
        and address[0] in ('127.0.0.1', '::1')
        and address[1] >= 32768
        and address[1] not in BLOCKED_PORTS
    )


def connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not allowed(address):
        raise OSError('Independent audit blocked non-fixture network destination')
    return _original_connect(self, address)


def connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not allowed(address):
        return 111
    return _original_connect_ex(self, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex


def test_fleet_endpoints_are_blocked():
    """Regression: the guard itself must explicitly reject 50052 and 50054."""
    for port in (50052, 50054):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(('127.0.0.1', port))
        except OSError:
            pass  # blocked by guard, or genuinely unreachable -> still isolated
        else:
            s.close()
            raise AssertionError(f'guard failed to block fleet endpoint 127.0.0.1:{port}')
    assert not allowed(('127.0.0.1', 50052))
    assert not allowed(('127.0.0.1', 50054))
    assert allowed(('127.0.0.1', 40000))
    assert not allowed(('8.8.8.8', 53))
