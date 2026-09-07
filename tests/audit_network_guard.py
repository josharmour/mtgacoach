import socket
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
def allowed(address):
    return isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1') and address[1] >= 32768
def connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not allowed(address):
        raise OSError('Independent audit blocked non-fixture network destination')
    return _original_connect(self,address)
def connect_ex(self,address):
    if self.family in (socket.AF_INET,socket.AF_INET6) and not allowed(address):
        return 111
    return _original_connect_ex(self,address)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
