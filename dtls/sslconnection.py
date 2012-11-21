# SSL connection: state and behavior associated with the connection between
# the OpenSSL library and an individual peer. Written by Ray Brown.
"""SSL Connection

This module encapsulates the state and behavior associated with the connection
between the OpenSSL library and an individual peer when using the DTLS
protocol. It defines the application side of the interface of a client with a
DTLS server, and of a server with a DTLS client.

Classes:

  SSLConnection -- DTLS peer association

Integer constants:

  PROTOCOL_DTLSv1

The cert group must coincide in meaning and value with the one of the standard
library's ssl module, since its values can be passed to this module.

  CERT_NONE
  CERT_OPTIONAL
  CERT_REQUIRED
"""

import errno
import socket
import hmac
from logging import getLogger
from os import urandom
from weakref import proxy
from err import openssl_error, InvalidSocketError
from err import raise_ssl_error
from err import SSL_ERROR_WANT_READ, SSL_ERROR_SYSCALL
from err import ERR_COOKIE_MISMATCH, ERR_NO_CERTS
from err import ERR_NO_CIPHER, ERR_HANDSHAKE_TIMEOUT, ERR_PORT_UNREACHABLE
from x509 import _X509, decode_cert
from openssl import *
from util import _Rsrc, _BIO

_logger = getLogger(__name__)

PROTOCOL_DTLSv1 = 256
CERT_NONE = 0
CERT_OPTIONAL = 1
CERT_REQUIRED = 2

#
# One-time global OpenSSL library initialization
#
SSL_library_init()
SSL_load_error_strings()
DTLS_OPENSSL_VERSION_NUMBER = SSLeay()
DTLS_OPENSSL_VERSION = SSLeay_version(SSLEAY_VERSION)
DTLS_OPENSSL_VERSION_INFO = (
    DTLS_OPENSSL_VERSION_NUMBER >> 28 & 0xFF,  # major
    DTLS_OPENSSL_VERSION_NUMBER >> 20 & 0xFF,  # minor
    DTLS_OPENSSL_VERSION_NUMBER >> 12 & 0xFF,  # fix
    DTLS_OPENSSL_VERSION_NUMBER >> 4  & 0xFF,  # patch
    DTLS_OPENSSL_VERSION_NUMBER       & 0xF)   # status


class _CTX(_Rsrc):
    """SSL_CTX wrapper"""
    def __init__(self, value):
        super(_CTX, self).__init__(value)

    def __del__(self):
        _logger.debug("Freeing SSL CTX: %d", self._value._as_parameter)
        SSL_CTX_free(self._value)
        self._value = None


class _SSL(_Rsrc):
    """SSL structure wrapper"""
    def __init__(self, value):
        super(_SSL, self).__init__(value)

    def __del__(self):
        _logger.debug("Freeing SSL: %d", self._value._as_parameter)
        SSL_free(self._value)
        self._value = None


class _CallbackProxy(object):
    """Callback gateway to an SSLConnection object

    This class forms a weak connection between a callback method and
    an SSLConnection object. It can be passed as a callback callable
    without creating a strong reference through bound methods of
    the SSLConnection.
    """

    def __init__(self, cbm):
        self.ssl_connection = proxy(cbm.im_self)
        self.ssl_func = cbm.im_func

    def __call__(self, *args, **kwargs):
        return self.ssl_func(self.ssl_connection, *args, **kwargs)


class SSLConnection(object):
    """DTLS peer association

    This class associates two DTLS peer instances, wrapping OpenSSL library
    state including SSL (struct ssl_st), SSL_CTX, and BIO instances.
    """

    _rnd_key = urandom(16)

    def _init_server(self, peer_address):
        if self._sock.type != socket.SOCK_DGRAM:
            raise InvalidSocketError("sock must be of type SOCK_DGRAM")
        if peer_address:
            raise InvalidSocketError("server-side socket must be unconnected")

        from demux import UDPDemux
        self._udp_demux = UDPDemux(self._sock)
        self._rsock = self._udp_demux.get_connection(None)
        self._wbio = _BIO(BIO_new_dgram(self._sock.fileno(), BIO_NOCLOSE))
        self._rbio = _BIO(BIO_new_dgram(self._rsock.fileno(), BIO_NOCLOSE))
        self._ctx = _CTX(SSL_CTX_new(DTLSv1_server_method()))
        SSL_CTX_set_session_cache_mode(self._ctx.value, SSL_SESS_CACHE_OFF)
        if self._cert_reqs == CERT_NONE:
            verify_mode = SSL_VERIFY_NONE
        elif self._cert_reqs == CERT_OPTIONAL:
            verify_mode = SSL_VERIFY_PEER | SSL_VERIFY_CLIENT_ONCE
        else:
            verify_mode = SSL_VERIFY_PEER | SSL_VERIFY_CLIENT_ONCE | \
              SSL_VERIFY_FAIL_IF_NO_PEER_CERT
        self._listening = False
        self._listening_peer_address = None
        self._pending_peer_address = None
        self._config_ssl_ctx(verify_mode)
        self._cb_keepalive = SSL_CTX_set_cookie_cb(
            self._ctx.value,
            _CallbackProxy(self._generate_cookie_cb),
            _CallbackProxy(self._verify_cookie_cb))
        self._ssl = _SSL(SSL_new(self._ctx.value))
        SSL_set_accept_state(self._ssl.value)

    def _init_client(self, peer_address):
        if self._sock.type != socket.SOCK_DGRAM:
            raise InvalidSocketError("sock must be of type SOCK_DGRAM")

        self._wbio = _BIO(BIO_new_dgram(self._sock.fileno(), BIO_NOCLOSE))
        self._rbio = self._wbio
        self._ctx = _CTX(SSL_CTX_new(DTLSv1_client_method()))
        if self._cert_reqs == CERT_NONE:
            verify_mode = SSL_VERIFY_NONE
        else:
            verify_mode = SSL_VERIFY_PEER
        self._config_ssl_ctx(verify_mode)
        self._ssl = _SSL(SSL_new(self._ctx.value))
        SSL_set_connect_state(self._ssl.value)
        if peer_address:
            return lambda: self.connect(peer_address)

    def _config_ssl_ctx(self, verify_mode):
        SSL_CTX_set_verify(self._ctx.value, verify_mode)
        SSL_CTX_set_read_ahead(self._ctx.value, 1)
        if self._certfile:
            SSL_CTX_use_certificate_chain_file(self._ctx.value, self._certfile)
        if self._keyfile:
            SSL_CTX_use_PrivateKey_file(self._ctx.value, self._keyfile,
                                        SSL_FILE_TYPE_PEM)
        if self._ca_certs:
            SSL_CTX_load_verify_locations(self._ctx.value, self._ca_certs, None)
        if self._ciphers:
            try:
                SSL_CTX_set_cipher_list(self._ctx.value, self._ciphers)
            except openssl_error() as err:
                raise_ssl_error(ERR_NO_CIPHER, err)

    def _copy_server(self):
        source = self._sock
        self._sock = source._sock
        self._udp_demux = source._udp_demux
        self._rsock = self._udp_demux.get_connection(
            source._pending_peer_address)
        self._wbio = _BIO(BIO_new_dgram(self._sock.fileno(), BIO_NOCLOSE))
        self._rbio = _BIO(BIO_new_dgram(self._rsock.fileno(), BIO_NOCLOSE))
        BIO_dgram_set_peer(self._wbio.value, source._pending_peer_address)
        self._ctx = source._ctx
        self._ssl = source._ssl
        new_source_wbio = _BIO(BIO_new_dgram(source._sock.fileno(),
                                             BIO_NOCLOSE))
        new_source_rbio = _BIO(BIO_new_dgram(source._rsock.fileno(),
                                             BIO_NOCLOSE))
        source._ssl = _SSL(SSL_new(self._ctx.value))
        SSL_set_accept_state(source._ssl.value)
        source._rbio = new_source_rbio
        source._wbio = new_source_wbio
        SSL_set_bio(source._ssl.value,
                    new_source_rbio.value,
                    new_source_wbio.value)
        new_source_rbio.disown()
        new_source_wbio.disown()

    def _reconnect_unwrapped(self):
        source = self._sock
        self._sock = source._wsock
        self._udp_demux = source._demux
        self._rsock = source._rsock
        self._ctx = source._ctx
        self._wbio = _BIO(BIO_new_dgram(self._sock.fileno(), BIO_NOCLOSE))
        self._rbio = _BIO(BIO_new_dgram(self._rsock.fileno(), BIO_NOCLOSE))
        BIO_dgram_set_peer(self._wbio.value, source._peer_address)
        self._ssl = _SSL(SSL_new(self._ctx.value))
        SSL_set_accept_state(self._ssl.value)
        if self._do_handshake_on_connect:
            return lambda: self.do_handshake()

    def _check_nbio(self):
        BIO_set_nbio(self._wbio.value, self._sock.gettimeout() is not None)
        if self._wbio is not self._rbio:
            BIO_set_nbio(self._rbio.value, self._rsock.gettimeout() is not None)

    def _get_cookie(self, ssl):
        assert self._listening
        assert self._ssl.value._as_parameter == ssl._as_parameter
        if self._listening_peer_address:
            peer_address = self._listening_peer_address
        else:
            peer_address = BIO_dgram_get_peer(self._rbio.value)
        cookie_hmac = hmac.new(self._rnd_key, str(peer_address))
        return cookie_hmac.digest()

    def _generate_cookie_cb(self, ssl):
        return self._get_cookie(ssl)

    def _verify_cookie_cb(self, ssl, cookie):
        if self._get_cookie(ssl) != cookie:
            raise Exception("DTLS cookie mismatch")

    def __init__(self, sock, keyfile=None, certfile=None,
                 server_side=False, cert_reqs=CERT_NONE,
                 ssl_version=PROTOCOL_DTLSv1, ca_certs=None,
                 do_handshake_on_connect=True,
                 suppress_ragged_eofs=True, ciphers=None):
        """Constructor

        Arguments:
        these arguments match the ones of the SSLSocket class in the
        standard library's ssl module
        """

        if keyfile and not certfile or certfile and not keyfile:
            raise_ssl_error(ERR_BOTH_KEY_CERT_FILES)
        if server_side and not keyfile:
            raise_ssl_error(ERR_BOTH_KEY_CERT_FILES_SVR)
        if cert_reqs != CERT_NONE and not ca_certs:
            raise_ssl_error(ERR_NO_CERTS)

        if not ciphers:
            ciphers = "DEFAULT"

        self._sock = sock
        self._keyfile = keyfile
        self._certfile = certfile
        self._cert_reqs = cert_reqs
        self._ca_certs = ca_certs
        self._do_handshake_on_connect = do_handshake_on_connect
        self._suppress_ragged_eofs = suppress_ragged_eofs
        self._ciphers = ciphers
        self._handshake_done = False

        if isinstance(sock, SSLConnection):
            post_init = self._copy_server()
        elif isinstance(sock, _UnwrappedSocket):
            post_init = self._reconnect_unwrapped()
        else:
            try:
                peer_address = sock.getpeername()
            except socket.error:
                peer_address = None
            if server_side:
                post_init = self._init_server(peer_address)
            else:
                post_init = self._init_client(peer_address)

        SSL_set_bio(self._ssl.value, self._rbio.value, self._wbio.value)
        self._rbio.disown()
        self._wbio.disown()
        if post_init:
            post_init()

    def get_socket(self, inbound):
        """Retrieve a socket used by this connection

        When inbound is True, then the socket from which this connection reads
        data is retrieved. Otherwise the socket to which this connection writes
        data is retrieved.

        Read and write sockets differ depending on whether this is a server- or
        a client-side connection, and on whether a routing demux is in use.
        """

        if inbound and hasattr(self, "_rsock"):
            return self._rsock
        return self._sock

    def listen(self):
        """Server-side cookie exchange

        This method reads datagrams from the socket and initiates cookie
        exchange, upon whose successful conclusion one can then proceed to
        the accept method. Alternatively, accept can be called directly, in
        which case it will call this method. In order to prevent denial-of-
        service attacks, only a small, constant set of computing resources
        are used during the listen phase.

        On some platforms, listen must be called so that packets will be
        forwarded to accepted connections. Doing so is therefore recommened
        in all cases for portable code.

        Return value: a peer address if a datagram from a new peer was
        encountered, None if a datagram for a known peer was forwarded
        """

        self._pending_peer_address = None
        try:
            peer_address = self._udp_demux.service()
        except socket.timeout:
            peer_address = None
        except socket.error as sock_err:
            if sock_err.errno != errno.EWOULDBLOCK:
                _logger.exception("Unexpected socket error in listen")
                raise
            peer_address = None

        if not peer_address:
            _logger.debug("Listen returning without peer")
            return

        # The demux advises that a datagram from a new peer may have arrived
        if type(peer_address) is tuple:
            # For this type of demux, the write BIO must be pointed at the peer
            BIO_dgram_set_peer(self._wbio.value, peer_address)
            self._udp_demux.forward()
            self._listening_peer_address = peer_address

        self._check_nbio()
        self._listening = True
        try:
            _logger.debug("Invoking DTLSv1_listen for ssl: %d",
                          self._ssl.value._as_parameter)
            dtls_peer_address = DTLSv1_listen(self._ssl.value)
        except openssl_error() as err:
            if err.ssl_error == SSL_ERROR_WANT_READ:
                # This method must be called again to forward the next datagram
                _logger.debug("DTLSv1_listen must be resumed")
                return
            elif err.errqueue and err.errqueue[0][0] == ERR_COOKIE_MISMATCH:
                _logger.debug("Mismatching cookie received; aborting handshake")
                return
            _logger.exception("Unexpected error in DTLSv1_listen")
            raise
        finally:
            self._listening = False
            self._listening_peer_address = None
        if type(peer_address) is tuple:
            _logger.debug("New local peer: %s", dtls_peer_address)
            self._pending_peer_address = peer_address
        else:
            self._pending_peer_address = dtls_peer_address
        _logger.debug("New peer: %s", self._pending_peer_address)
        return self._pending_peer_address

    def accept(self):
        """Server-side UDP connection establishment

        This method returns a server-side SSLConnection object, connected to
        that peer most recently returned from the listen method and not yet
        connected. If there is no such peer, then the listen method is invoked.

        Return value: SSLConnection connected to a new peer, None if packet
        forwarding only to an existing peer occurred.
        """

        if not self._pending_peer_address:
            if not self.listen():
                _logger.debug("Accept returning without connection")
                return
        new_conn = SSLConnection(self, self._keyfile, self._certfile, True,
                                 self._cert_reqs, PROTOCOL_DTLSv1,
                                 self._ca_certs, self._do_handshake_on_connect,
                                 self._suppress_ragged_eofs, self._ciphers)
        new_peer = self._pending_peer_address
        self._pending_peer_address = None
        if self._do_handshake_on_connect:
            # Note that since that connection's socket was just created in its
            # constructor, the following operation must be blocking; hence
            # handshake-on-connect can only be used with a routing demux if
            # listen is serviced by a separate application thread, or else we
            # will hang in this call
            new_conn.do_handshake()
        _logger.debug("Accept returning new connection for new peer")
        return new_conn, new_peer

    def connect(self, peer_address):
        """Client-side UDP connection establishment

        This method connects this object's underlying socket. It subsequently
        performs a handshake if do_handshake_on_connect was set during
        initialization.

        Arguments:
        peer_address - address tuple of server peer
        """

        self._sock.connect(peer_address)
        peer_address = self._sock.getpeername()  # substituted host addrinfo
        BIO_dgram_set_connected(self._wbio.value, peer_address)
        assert self._wbio is self._rbio
        if self._do_handshake_on_connect:
            self.do_handshake()

    def do_handshake(self):
        """Perform a handshake with the peer

        This method forces an explicit handshake to be performed with either
        the client or server peer.
        """

        _logger.debug("Initiating handshake...")
        self._check_nbio()
        try:
            SSL_do_handshake(self._ssl.value)
        except openssl_error() as err:
            if err.ssl_error == SSL_ERROR_WANT_READ and \
              self.get_socket(True).gettimeout():
                raise_ssl_error(ERR_HANDSHAKE_TIMEOUT, err)
            elif err.ssl_error == SSL_ERROR_SYSCALL and err.result == -1:
                raise_ssl_error(ERR_PORT_UNREACHABLE, err)
            raise
        self._handshake_done = True
        _logger.debug("...completed handshake")

    def read(self, len=1024):
        """Read data from connection

        Read up to len bytes and return them.
        Arguments:
        len -- maximum number of bytes to read

        Return value:
        string containing read bytes
        """

        self._check_nbio()
        return SSL_read(self._ssl.value, len)

    def write(self, data):
        """Write data to connection

        Write data as string of bytes.

        Arguments:
        data -- buffer containing data to be written

        Return value:
        number of bytes actually transmitted
        """

        self._check_nbio()
        return SSL_write(self._ssl.value, data)

    def shutdown(self):
        """Shut down the DTLS connection

        This method attemps to complete a bidirectional shutdown between
        peers. For non-blocking sockets, it should be called repeatedly until
        it no longer raises continuation request exceptions.
        """

        if hasattr(self, "_listening"):
            # Listening server-side sockets cannot be shut down
            return

        self._check_nbio()
        try:
            SSL_shutdown(self._ssl.value)
        except openssl_error() as err:
            if err.result == 0:
                # close-notify alert was just sent; wait for same from peer
                # Note: while it might seem wise to suppress further read-aheads
                # with SSL_set_read_ahead here, doing so causes a shutdown
                # failure (ret: -1, SSL_ERROR_SYSCALL) on the DTLS shutdown
                # initiator side. And test_starttls does pass.
                SSL_shutdown(self._ssl.value)
            else:
                raise
        if hasattr(self, "_udp_demux"):
            # Return wrapped connected server socket (non-listening)
            return _UnwrappedSocket(self._sock, self._rsock, self._udp_demux,
                                    self._ctx,
                                    BIO_dgram_get_peer(self._wbio.value))
        # Return unwrapped client-side socket
        return self._sock

    def getpeercert(self, binary_form=False):
        """Retrieve the peer's certificate

        When binary form is requested, the peer's DER-encoded certficate is
        returned if it was transmitted during the handshake.

        When binary form is not requested, and the peer's certificate has been
        validated, then a certificate dictionary is returned. If the certificate
        was not validated, an empty dictionary is returned.

        In all cases, None is returned if no certificate was received from the
        peer.
        """

        try:
            peer_cert = _X509(SSL_get_peer_certificate(self._ssl.value))
        except openssl_error():
            return

        if binary_form:
            return i2d_X509(peer_cert.value)
        if self._cert_reqs == CERT_NONE:
            return {}
        return decode_cert(peer_cert)

    peer_certificate = getpeercert  # compatibility with _ssl call interface

    def cipher(self):
        """Retrieve information about the current cipher

        Return a triple consisting of cipher name, SSL protocol version defining
        its use, and the number of secret bits. Return None if handshaking
        has not been completed.
        """

        if not self._handshake_done:
            return

        current_cipher = SSL_get_current_cipher(self._ssl.value)
        cipher_name = SSL_CIPHER_get_name(current_cipher)
        cipher_version = SSL_CIPHER_get_version(current_cipher)
        cipher_bits = SSL_CIPHER_get_bits(current_cipher)
        return cipher_name, cipher_version, cipher_bits

    def pending(self):
        """Retrieve number of buffered bytes

        Return the number of bytes that have been read from the socket and
        buffered by this connection. Return 0 if no bytes have been buffered.
        """

        return SSL_pending(self._ssl.value)

    def get_timeout(self):
        """Retrieve the retransmission timedelta

        Since datagrams are subject to packet loss, DTLS will perform
        packet retransmission if a response is not received after a certain
        time interval during the handshaking phase. When using non-blocking
        sockets, the application must call back after that time interval to
        allow for the retransmission to occur. This method returns the
        timedelta after which to perform the call to handle_timeout, or None
        if no such callback is needed given the current handshake state.
        """

        return DTLSv1_get_timeout(self._ssl.value)

    def handle_timeout(self):
        """Perform datagram retransmission, if required

        This method should be called after the timedelta retrieved from
        get_timeout has expired, and no datagrams were received in the
        meantime. If datagrams were received, a new timeout needs to be
        requested.

        Return value:
        True -- retransmissions were performed successfully
        False -- a timeout was not in effect or had not yet expired

        Exceptions:
        Raised when retransmissions fail or too many timeouts occur.
        """

        return DTLSv1_handle_timeout(self._ssl.value)


class _UnwrappedSocket(socket.socket):
    """Unwrapped server-side socket

    Depending on UDP demux implementation, there may not be single socket
    that can be used for both reading and writing to the client socket with
    which it is associated. An object of this type is therefore returned from
    the SSLSocket's unwrap method to allow for unencrypted communication over
    the established channels, including the demux.
    """

    def __init__(self, wsock, rsock, demux, ctx, peer_address):
        socket.socket.__init__(self, _sock=rsock._sock)
        for attr in "send", "sendto", "sendall":
            try:
                delattr(self, attr)
            except AttributeError:
                pass
        self._wsock = wsock
        self._rsock = rsock  # continue to reference to hold in demux map
        self._demux = demux
        self._ctx = ctx
        self._peer_address = peer_address

    def send(self, data, flags=0):
        __doc__ = self._wsock.send.__doc__
        return self._wsock.sendto(data, flags, self._peer_address)

    def sendto(self, data, flags_or_addr, addr=None):
        __doc__ = self._wsock.sendto.__doc__
        return self._wsock.sendto(data, flags_or_addr, addr)

    def sendall(self, data, flags=0):
        __doc__ = self._wsock.sendall.__doc__
        amount = len(data)
        count = 0
        while (count < amount):
            v = self.send(data[count:], flags)
            count += v
        return amount

    def getpeername(self):
        __doc__ = self._wsock.getpeername.__doc__
        return self._peer_address

    def connect(self, addr):
        __doc__ = self._wsock.connect.__doc__
        raise ValueError("Cannot connect already connected unwrapped socket")

    connect_ex = connect