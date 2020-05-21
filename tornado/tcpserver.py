# -*- coding: utf-8 -*-
#
# Copyright 2011 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""A non-blocking, single-threaded TCP server."""
from __future__ import absolute_import, division, print_function

import errno
import os
import socket

from tornado import gen
from tornado.log import app_log
from tornado.ioloop import IOLoop
from tornado.iostream import IOStream, SSLIOStream
from tornado.netutil import bind_sockets, add_accept_handler, ssl_wrap_socket
from tornado import process
from tornado.util import errno_from_exception

try:
    import ssl
except ImportError:
    # ssl is not available on Google App Engine.
    ssl = None


class TCPServer(object):
    r"""A non-blocking, single-threaded TCP server.

    To use `TCPServer`, define a subclass which overrides the `handle_stream`
    method. For example, a simple echo server could be defined like this::

      from tornado.tcpserver import TCPServer
      from tornado.iostream import StreamClosedError
      from tornado import gen

      class EchoServer(TCPServer):
          async def handle_stream(self, stream, address):
              while True:
                  try:
                      data = await stream.read_until(b"\n")
                      await stream.write(data)
                  except StreamClosedError:
                      break

    To make this server serve SSL traffic, send the ``ssl_options`` keyword
    argument with an `ssl.SSLContext` object. For compatibility with older
    versions of Python ``ssl_options`` may also be a dictionary of keyword
    arguments for the `ssl.wrap_socket` method.::

       ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
       ssl_ctx.load_cert_chain(os.path.join(data_dir, "mydomain.crt"),
                               os.path.join(data_dir, "mydomain.key"))
       TCPServer(ssl_options=ssl_ctx)

    `TCPServer` initialization follows one of three patterns:

    1. `listen`: simple single-process::

            server = TCPServer()
            server.listen(8888)
            IOLoop.current().start()

    2. `bind`/`start`: simple multi-process::

            server = TCPServer()
            server.bind(8888)
            server.start(0)  # Forks multiple sub-processes
            IOLoop.current().start()

       When using this interface, an `.IOLoop` must *not* be passed
       to the `TCPServer` constructor.  `start` will always start
       the server on the default singleton `.IOLoop`.

    3. `add_sockets`: advanced multi-process::

            sockets = bind_sockets(8888)
            tornado.process.fork_processes(0)
            server = TCPServer()
            server.add_sockets(sockets)
            IOLoop.current().start()

       The `add_sockets` interface is more complicated, but it can be
       used with `tornado.process.fork_processes` to give you more
       flexibility in when the fork happens.  `add_sockets` can
       also be used in single-process servers if you want to create
       your listening sockets in some way other than
       `~tornado.netutil.bind_sockets`.

    .. versionadded:: 3.1
       The ``max_buffer_size`` argument.

    .. versionchanged:: 5.0
       The ``io_loop`` argument has been removed.
    """
    def __init__(self, ssl_options=None, max_buffer_size=None,
                 read_chunk_size=None):
        self.ssl_options = ssl_options
        self._sockets = {}   # fd -> socket object
        self._handlers = {}  # fd -> remove_handler callable
        self._pending_sockets = []
        self._started = False
        self._stopped = False
        self.max_buffer_size = max_buffer_size
        self.read_chunk_size = read_chunk_size

        # Verify the SSL options. Otherwise we don't get errors until clients
        # connect. This doesn't verify that the keys are legitimate, but
        # the SSL module doesn't do that until there is a connected socket
        # which seems like too much work
        # 验证SSL选项。
        # 否则，在客户端连接之前我们不会出错。
        # 这不会验证密钥是否合法，但是SSL模块不会这样做，直到有一个连接的套接字，这样的话似乎需要的工作太多了
        if self.ssl_options is not None and isinstance(self.ssl_options, dict):
            # Only certfile is required: it can contain both keys
            if 'certfile' not in self.ssl_options:
                raise KeyError('missing key "certfile" in ssl_options')

            if not os.path.exists(self.ssl_options['certfile']):
                raise ValueError('certfile "%s" does not exist' %
                                 self.ssl_options['certfile'])
            if ('keyfile' in self.ssl_options and
                    not os.path.exists(self.ssl_options['keyfile'])):
                raise ValueError('keyfile "%s" does not exist' %
                                 self.ssl_options['keyfile'])

    # 监听端口
    def listen(self, port, address=""):
        """Starts accepting connections on the given port.
            开始接受给定端口上的连接。

        This method may be called more than once to listen on multiple ports.
        `listen` takes effect immediately; it is not necessary to call
        `TCPServer.start` afterwards.  It is, however, necessary to start
        the `.IOLoop`.
        可以多次调用此方法以侦听多个端口。 “listen”立即生效；
        之后，不必调用`TCPServer.start`。
         但是，有必要启动.IOLoop。
        """
        sockets = bind_sockets(port, address=address)
        self.add_sockets(sockets)

    # 添加监听socket，绑定回调方法
    def add_sockets(self, sockets):
        """Makes this server start accepting connections on the given sockets.
        使该服务器开始接受给定套接字上的连接。

        The ``sockets`` parameter is a list of socket objects such as
        those returned by `~tornado.netutil.bind_sockets`.
        `add_sockets` is typically used in combination with that
        method and `tornado.process.fork_processes` to provide greater
        control over the initialization of a multi-process server.

         套接字参数是套接字对象的列表，例如〜tornado.netutil.bind_sockets返回的套接字对象。
        `add_sockets`通常与该方法和`tornado.process.fork_processes`结合使用，以更好地控制多进程服务器的初始化。
        """
        for sock in sockets:
            self._sockets[sock.fileno()] = sock
            self._handlers[sock.fileno()] = add_accept_handler(
                sock, self._handle_connection)

    # 可以添加外部 自己创建的 socket
    def add_socket(self, socket):
        """Singular version of `add_sockets`.  Takes a single socket object."""
        self.add_sockets([socket])

    # 监听端口， 可以在“start”之前多次调用此方法以侦听多个端口或接口。
    def bind(self, port, address=None, family=socket.AF_UNSPEC, backlog=128,
             reuse_port=False):
        """Binds this server to the given port on the given address.
        将此服务器绑定到给定地址上的给定端口。

        To start the server, call `start`. If you want to run this server
        in a single process, you can call `listen` as a shortcut to the
        sequence of `bind` and `start` calls.
        要启动服务器，请调用“启动”。 如果要在单个进程中运行此服务器，则可以调用“监听”作为快捷键“绑定”和“启动”的顺序。

        Address may be either an IP address or hostname.  If it's a hostname,
        the server will listen on all IP addresses associated with the
        name.  Address may be an empty string or None to listen on all
        available interfaces.  Family may be set to either `socket.AF_INET`
        or `socket.AF_INET6` to restrict to IPv4 or IPv6 addresses, otherwise
        both will be used if available.
        地址可以是IP地址或主机名。 如果是主机名，则服务器将侦听与该名称关联的所有IP地址。
        地址可以是空字符串，也可以是“无”以侦听所有可用接口。
        可以将Family设置为`socket.AF_INET`或`socket.AF_INET6`以限制为IPv4或IPv6地址，否则将同时使用两者（如果可用）。

        The ``backlog`` argument has the same meaning as for
        `socket.listen <socket.socket.listen>`. The ``reuse_port`` argument
        has the same meaning as for `.bind_sockets`.
        backlog参数与socket.listen <socket.socket.listen>的含义相同。 reuse_port参数的含义与.bind_sockets的含义相同。

        This method may be called multiple times prior to `start` to listen
        on multiple ports or interfaces.
        可以在“开始”之前多次调用此方法以侦听多个端口或接口。

        .. versionchanged:: 4.4
           Added the ``reuse_port`` argument.
        """
        sockets = bind_sockets(port, address=address, family=family,
                               backlog=backlog, reuse_port=reuse_port)
        if self._started:  # 如果已经启动了，就直接加入ioloop运行
            self.add_sockets(sockets)
        else:  # 还未运行，先放入等待队列
            self._pending_sockets.extend(sockets)

    # 启动监听
    def start(self, num_processes=1):
        """Starts this server in the `.IOLoop`.

        By default, we run the server in this process and do not fork any
        additional child process.
        默认情况下，我们在当前进程运行服务器，并且不会派生任何其他子进程。

        If num_processes is ``None`` or <= 0, we detect the number of cores
        available on this machine and fork that number of child
        processes. If num_processes is given and > 1, we fork that
        specific number of sub-processes.
        如果 num_processes 为``None''或<= 0，我们将检测到此计算机上可用的内核数量并派生该数量的子进程。
        如果给定num_processes且> 1，则我们分叉该特定数量的子进程。

        Since we use processes and not threads, there is no shared memory
        between any server code.
        由于我们使用进程而不是线程，因此任何服务器代码之间都没有共享内存。

        Note that multiple processes are not compatible with the autoreload
        module (or the ``autoreload=True`` option to `tornado.web.Application`
        which defaults to True when ``debug=True``).
        When using multiple processes, no IOLoops can be created or
        referenced until after the call to ``TCPServer.start(n)``.
        请注意，多个进程与autoreload模块不兼容
        （或tornado.web.Application的autoreload = True选项，当debug = True时默认为True）。
        当使用多个进程时，没有IOLoops 可以创建或引用直到调用TCPServer.start（n）之后。
        """
        assert not self._started  # 已启动，再调用就报错
        self._started = True  # 设置为已启动
        if num_processes != 1:  # 不是1
            process.fork_processes(num_processes)  # todo zzy 创建子进程运行
        sockets = self._pending_sockets
        self._pending_sockets = []
        self.add_sockets(sockets)

    # 停止监听，停止服务
    def stop(self):
        """Stops listening for new connections.

        Requests currently in progress may still continue after the
        server is stopped.
        """
        if self._stopped:
            return
        self._stopped = True
        for fd, sock in self._sockets.items():
            assert sock.fileno() == fd
            # Unregister socket from IOLoop
            self._handlers.pop(fd)()  # 从ioloop移除回调方法
            sock.close()  # 关闭socket

    # 要被覆盖
    def handle_stream(self, stream, address):
        """Override to handle a new `.IOStream` from an incoming connection.

        This method may be a coroutine; if so any exceptions it raises
        asynchronously will be logged. Accepting of incoming connections
        will not be blocked by this coroutine.

        If this `TCPServer` is configured for SSL, ``handle_stream``
        may be called before the SSL handshake has completed. Use
        `.SSLIOStream.wait_for_handshake` if you need to verify the client's
        certificate or use NPN/ALPN.

        .. versionchanged:: 4.2
           Added the option for this method to be a coroutine.
        """
        raise NotImplementedError()

    # accep连接后的回调方法
    def _handle_connection(self, connection, address):
        if self.ssl_options is not None:
            assert ssl, "Python 2.6+ and OpenSSL required for SSL"
            try:  # todo zzy ssl加密传输需要包装一下
                connection = ssl_wrap_socket(connection,
                                             self.ssl_options,
                                             server_side=True,
                                             do_handshake_on_connect=False)
            except ssl.SSLError as err:
                if err.args[0] == ssl.SSL_ERROR_EOF:
                    return connection.close()
                else:
                    raise
            except socket.error as err:
                # If the connection is closed immediately after it is created
                # (as in a port scan), we can get one of several errors.
                # wrap_socket makes an internal call to getpeername,
                # which may return either EINVAL (Mac OS X) or ENOTCONN
                # (Linux).  If it returns ENOTCONN, this error is
                # silently swallowed by the ssl module, so we need to
                # catch another error later on (AttributeError in
                # SSLIOStream._do_ssl_handshake).
                # To test this behavior, try nmap with the -sT flag.
                # https://github.com/tornadoweb/tornado/pull/750
                if errno_from_exception(err) in (errno.ECONNABORTED, errno.EINVAL):
                    return connection.close()
                else:
                    raise
        try:  # 根据是否ssl加密传输包装不同的stream对象
            if self.ssl_options is not None:
                stream = SSLIOStream(connection,
                                     max_buffer_size=self.max_buffer_size,
                                     read_chunk_size=self.read_chunk_size)
            else:
                stream = IOStream(connection,
                                  max_buffer_size=self.max_buffer_size,
                                  read_chunk_size=self.read_chunk_size)

            future = self.handle_stream(stream, address)
            if future is not None:  # 这个不一定，大多情况下是None，即handle_stream不返回数据
                IOLoop.current().add_future(gen.convert_yielded(future),
                                            lambda f: f.result())
        except Exception:
            app_log.error("Error in connection callback", exc_info=True)
