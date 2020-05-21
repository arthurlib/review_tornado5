# -*- coding: utf-8 -*-
#
# Copyright 2014 Facebook
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

"""A non-blocking TCP connection factory.
"""
from __future__ import absolute_import, division, print_function

import functools
import socket
import numbers
import datetime

from tornado.concurrent import Future, future_add_done_callback
from tornado.ioloop import IOLoop
from tornado.iostream import IOStream
from tornado import gen
from tornado.netutil import Resolver
from tornado.platform.auto import set_close_exec
from tornado.gen import TimeoutError
from tornado.util import timedelta_to_seconds

_INITIAL_CONNECT_TIMEOUT = 0.3


class _Connector(object):
    """A stateless implementation of the "Happy Eyeballs" algorithm.

    "Happy Eyeballs" is documented in RFC6555 as the recommended practice
    for when both IPv4 and IPv6 addresses are available.

    In this implementation, we partition the addresses by family, and
    make the first connection attempt to whichever address was
    returned first by ``getaddrinfo``.  If that connection fails or
    times out, we begin a connection in parallel to the first address
    of the other family.  If there are additional failures we retry
    with other addresses, keeping one connection attempt per family
    in flight at a time.

    http://tools.ietf.org/html/rfc6555

    """
    def __init__(self, addrinfo, connect):
        self.io_loop = IOLoop.current()
        self.connect = connect  # 传入的connect方法

        self.future = Future()
        self.timeout = None  # 去连接ipv6的延时任务
        self.connect_timeout = None  # 连接远程的超时报错 延时任务
        self.last_error = None
        self.remaining = len(addrinfo)  # addrinfo个数
        self.primary_addrs, self.secondary_addrs = self.split(addrinfo)
        self.streams = set()

    # 区分ipv4和ipv6,放入两个列表中
    @staticmethod
    def split(addrinfo):
        """Partition the ``addrinfo`` list by address family.

        Returns two lists.  The first list contains the first entry from
        ``addrinfo`` and all others with the same family, and the
        second list contains all other addresses (normally one list will
        be AF_INET and the other AF_INET6, although non-standard resolvers
        may return additional families).
        """
        primary = []
        secondary = []
        primary_af = addrinfo[0][0]
        for af, addr in addrinfo:
            if af == primary_af:
                primary.append((af, addr))
            else:
                secondary.append((af, addr))
        return primary, secondary

    # 开始尝试连接
    def start(self, timeout=_INITIAL_CONNECT_TIMEOUT, connect_timeout=None):
        self.try_connect(iter(self.primary_addrs))  # 包装成可迭代对像传入，尝试连接
        self.set_timeout(timeout)  # 设置延时任务，固定0.3秒之后ipv4还没连上(future没有完成)，就会同时尝试连接ipv6
        if connect_timeout is not None:  # 连接超时设置，这个 connect_timeout 超时时间设置的时整个连接操作的超时时间
            self.set_connect_timeout(connect_timeout)
        return self.future

    # 尝试连接
    def try_connect(self, addrs):
        try:
            af, addr = next(addrs)
        except StopIteration:
            # We've reached the end of our queue, but the other queue
            # might still be working.  Send a final error on the future
            # only when both queues are finished.
            if self.remaining == 0 and not self.future.done():
                self.future.set_exception(self.last_error or
                                          IOError("connection failed"))
            return
        stream, future = self.connect(af, addr)  # 去连接
        self.streams.add(stream)  # 将此次stream放入集合
        future_add_done_callback(  # 为此次stream.connect future设置连接成功回调
            future, functools.partial(self.on_connect_done, addrs, af, addr))

    # 连接成功回调
    def on_connect_done(self, addrs, af, addr, future):
        self.remaining -= 1  # 远程地址数量减一，尝试连接数
        try:
            stream = future.result()  # 返回连接成功后的stream
        except Exception as e:  # 要是报错了
            if self.future.done():
                return
            # Error: try again (but remember what happened so we have an
            # error to raise in the end)
            self.last_error = e  # 保存当前错误
            self.try_connect(addrs)  # 继续尝试连接下一个dns info, addrs == iter(self.primary_addrs)
            if self.timeout is not None:  # 不是None，表示，连接ipv6的任务还未启动，还没进入到ioloop
                # 这三行逻辑，当第一次连接失败的时候，想要立即启动ipv6连接
                # If the first attempt failed, don't wait for the
                # timeout to try an address from the secondary queue.
                self.io_loop.remove_timeout(self.timeout)  # 第一次尝试连接报错的时候，移除尝试连接ipv6的延时任务
                self.on_timeout()  # 再次直接设置尝试ipv6连接的任务
            return
        self.clear_timeouts()  # 移除超时任务
        if self.future.done():
            # 如果有多个流连接成功，从第二个开始，就直接关闭
            # This is a late arrival; just drop it.
            stream.close()
        else:
            self.streams.discard(stream)  # 移除当前流
            self.future.set_result((af, addr, stream))  # 设置返回值，即当前成功连接的stream
            self.close_streams()  # 移除剩下的所有流

    # 添加延时任务，到时间回调函数，尝试ipv6连接
    def set_timeout(self, timeout):
        # 添加一个延时任务
        self.timeout = self.io_loop.add_timeout(self.io_loop.time() + timeout,
                                                self.on_timeout)

    # 如果超时没连上，就尝试用ipv6连接
    def on_timeout(self):
        self.timeout = None  # 把延时任务重置为None
        if not self.future.done():
            self.try_connect(iter(self.secondary_addrs))

    # 未被使用
    def clear_timeout(self):
        if self.timeout is not None:
            self.io_loop.remove_timeout(self.timeout)

    # 设置 连接超时，延时任务
    def set_connect_timeout(self, connect_timeout):
        # 添加延时任务，尝试连接超时的时候回调
        self.connect_timeout = self.io_loop.add_timeout(
            connect_timeout, self.on_connect_timeout)

    # 连接超时的回调，此处设置超时error
    def on_connect_timeout(self):
        if not self.future.done():  # 如果当future没有完成
            self.future.set_exception(TimeoutError()) # 设置timeoutError
        self.close_streams()  # 关闭所有流

    # 清除所有超时任务
    def clear_timeouts(self):
        if self.timeout is not None:
            self.io_loop.remove_timeout(self.timeout)
        if self.connect_timeout is not None:
            self.io_loop.remove_timeout(self.connect_timeout)

    # 关闭所有stream
    def close_streams(self):
        for stream in self.streams:
            stream.close()


class TCPClient(object):
    """A non-blocking TCP connection factory.

    .. versionchanged:: 5.0
       The ``io_loop`` argument (deprecated since version 4.1) has been removed.
    """
    def __init__(self, resolver=None):
        if resolver is not None:
            self.resolver = resolver
            self._own_resolver = False
        else:
            self.resolver = Resolver()  # dns解析器
            self._own_resolver = True

    def close(self):
        if self._own_resolver:
            self.resolver.close()

    @gen.coroutine
    def connect(self, host, port, af=socket.AF_UNSPEC, ssl_options=None,
                max_buffer_size=None, source_ip=None, source_port=None,
                timeout=None):
        """Connect to the given host and port.

        Asynchronously returns an `.IOStream` (or `.SSLIOStream` if
        ``ssl_options`` is not None).

        Using the ``source_ip`` kwarg, one can specify the source
        IP address to use when establishing the connection.
        In case the user needs to resolve and
        use a specific interface, it has to be handled outside
        of Tornado as this depends very much on the platform.

        Raises `TimeoutError` if the input future does not complete before
        ``timeout``, which may be specified in any form allowed by
        `.IOLoop.add_timeout` (i.e. a `datetime.timedelta` or an absolute time
        relative to `.IOLoop.time`)

        Similarly, when the user requires a certain source port, it can
        be specified using the ``source_port`` arg.

        .. versionchanged:: 4.5
           Added the ``source_ip`` and ``source_port`` arguments.

        .. versionchanged:: 5.0
           Added the ``timeout`` argument.
        """
        if timeout is not None:
            if isinstance(timeout, numbers.Real):
                timeout = IOLoop.current().time() + timeout
            elif isinstance(timeout, datetime.timedelta):
                timeout = IOLoop.current().time() + timedelta_to_seconds(timeout)
            else:
                raise TypeError("Unsupported timeout %r" % timeout)
        if timeout is not None:
            # 如果设置了超时时间
            addrinfo = yield gen.with_timeout(
                timeout, self.resolver.resolve(host, port, af))
        else:
            addrinfo = yield self.resolver.resolve(host, port, af)
        connector = _Connector(
            addrinfo,  # dns解析出来的信息
            functools.partial(self._create_stream, max_buffer_size,
                              source_ip=source_ip, source_port=source_port)
        )
        af, addr, stream = yield connector.start(connect_timeout=timeout)
        # TODO: For better performance we could cache the (af, addr)
        # information here and re-use it on subsequent connections to
        # the same host. (http://tools.ietf.org/html/rfc6555#section-4.2)
        # 连接ssl操作
        if ssl_options is not None:
            if timeout is not None:
                # 如果设置了超时时间
                stream = yield gen.with_timeout(timeout, stream.start_tls(
                    False, ssl_options=ssl_options, server_hostname=host))
            else:
                stream = yield stream.start_tls(False, ssl_options=ssl_options,
                                                server_hostname=host)
        raise gen.Return(stream)  # 返回连接成功的流

    # 创建流，socket连接，用stream包装后返回
    def _create_stream(self, max_buffer_size, af, addr, source_ip=None,
                       source_port=None):
        # Always connect in plaintext; we'll convert to ssl if necessary
        # after one connection has completed.
        source_port_bind = source_port if isinstance(source_port, int) else 0  # 本地端口默认0
        source_ip_bind = source_ip
        if source_port_bind and not source_ip:  # 传入了本地端口但是没有传入本地ip
            # User required a specific port, but did not specify
            # a certain source IP, will bind to the default loopback.
            source_ip_bind = '::1' if af == socket.AF_INET6 else '127.0.0.1'
            # Trying to use the same address family as the requested af socket:
            # - 127.0.0.1 for IPv4
            # - ::1 for IPv6
        socket_obj = socket.socket(af)
        set_close_exec(socket_obj.fileno())
        if source_port_bind or source_ip_bind:  # 有特定的ip和端口
            # If the user requires binding also to a specific IP/port.
            try:
                socket_obj.bind((source_ip_bind, source_port_bind))  # 绑定自己设置的本地ip和端口
            except socket.error:
                socket_obj.close()
                # Fail loudly if unable to use the IP/port.
                raise
        try:
            stream = IOStream(socket_obj,  # 包装一下
                              max_buffer_size=max_buffer_size)
        except socket.error as e:
            fu = Future()
            fu.set_exception(e)
            return fu
        else:
            return stream, stream.connect(addr)  # 返回stream对象，尝试连接的future
