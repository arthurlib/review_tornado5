# -*- coding: utf-8 -*-
#
# Copyright 2009 Facebook
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

"""An I/O event loop for non-blocking sockets.

On Python 3, `.IOLoop` is a wrapper around the `asyncio` event loop.

Typical applications will use a single `IOLoop` object, accessed via
`IOLoop.current` class method. The `IOLoop.start` method (or
equivalently, `asyncio.AbstractEventLoop.run_forever`) should usually
be called at the end of the ``main()`` function. Atypical applications
may use more than one `IOLoop`, such as one `IOLoop` per thread, or
per `unittest` case.

In addition to I/O events, the `IOLoop` can also schedule time-based
events. `IOLoop.add_timeout` is a non-blocking alternative to
`time.sleep`.

"""

from __future__ import absolute_import, division, print_function

import collections
import datetime
import errno
import functools
import heapq
import itertools
import logging
import numbers
import os
import select
import sys
import threading
import time
import traceback
import math
import random

from tornado.concurrent import Future, is_future, chain_future, future_set_exc_info, \
    future_add_done_callback  # noqa: E501
from tornado.log import app_log, gen_log
from tornado.platform.auto import set_close_exec, Waker
from tornado import stack_context
from tornado.util import (
    PY3, Configurable, errno_from_exception, timedelta_to_seconds,
    TimeoutError, unicode_type, import_object,
)

try:
    import signal
except ImportError:
    signal = None

try:
    from concurrent.futures import ThreadPoolExecutor  # 启动并行任务
except ImportError:
    ThreadPoolExecutor = None

if PY3:
    import _thread as thread
else:
    import thread

try:
    import asyncio  # >=py3.4
except ImportError:
    asyncio = None

_POLL_TIMEOUT = 3600.0


# ioloop基类，被使用 select 的 POllIOLoop 和使用 asyncio 的 BaseAsyncIOLoop 继承
class IOLoop(Configurable):
    """A level-triggered I/O loop.

    On Python 3, `IOLoop` is a wrapper around the `asyncio` event
    loop. On Python 2, it uses ``epoll`` (Linux) or ``kqueue`` (BSD
    and Mac OS X) if they are available, or else we fall back on
    select(). If you are implementing a system that needs to handle
    thousands of simultaneous connections, you should use a system
    that supports either ``epoll`` or ``kqueue``.

    Example usage for a simple TCP server:

    .. testcode::

        import errno
        import functools
        import socket

        import tornado.ioloop
        from tornado.iostream import IOStream

        async def handle_connection(connection, address):
            stream = IOStream(connection)
            message = await stream.read_until_close()
            print("message from client:", message.decode().strip())

        def connection_ready(sock, fd, events):
            while True:
                try:
                    connection, address = sock.accept()
                except socket.error as e:
                    if e.args[0] not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    return
                connection.setblocking(0)
                io_loop = tornado.ioloop.IOLoop.current()
                io_loop.spawn_callback(handle_connection, connection, address)

        if __name__ == '__main__':
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(0)
            sock.bind(("", 8888))
            sock.listen(128)

            io_loop = tornado.ioloop.IOLoop.current()
            callback = functools.partial(connection_ready, sock)
            io_loop.add_handler(sock.fileno(), callback, io_loop.READ)
            io_loop.start()

    .. testoutput::
       :hide:

    By default, a newly-constructed `IOLoop` becomes the thread's current
    `IOLoop`, unless there already is a current `IOLoop`. This behavior
    can be controlled with the ``make_current`` argument to the `IOLoop`
    constructor: if ``make_current=True``, the new `IOLoop` will always
    try to become current and it raises an error if there is already a
    current instance. If ``make_current=False``, the new `IOLoop` will
    not try to become current.

    In general, an `IOLoop` cannot survive a fork or be shared across
    processes in any way. When multiple processes are being used, each
    process should create its own `IOLoop`, which also implies that
    any objects which depend on the `IOLoop` (such as
    `.AsyncHTTPClient`) must also be created in the child processes.
    As a guideline, anything that starts processes (including the
    `tornado.process` and `multiprocessing` modules) should do so as
    early as possible, ideally the first thing the application does
    after loading its configuration in ``main()``.

    .. versionchanged:: 4.2
       Added the ``make_current`` keyword argument to the `IOLoop`
       constructor.

    .. versionchanged:: 5.0

       Uses the `asyncio` event loop by default. The
       ``IOLoop.configure`` method cannot be used on Python 3 except
       to redundantly specify the `asyncio` event loop.

    """
    # Constants from the epoll module
    # 这里是各种监听状态常数
    _EPOLLIN = 0x001
    _EPOLLPRI = 0x002
    _EPOLLOUT = 0x004
    _EPOLLERR = 0x008
    _EPOLLHUP = 0x010
    _EPOLLRDHUP = 0x2000
    _EPOLLONESHOT = (1 << 30)
    _EPOLLET = (1 << 31)

    # Our events map exactly to the epoll events
    # 别名
    NONE = 0
    READ = _EPOLLIN
    WRITE = _EPOLLOUT
    ERROR = _EPOLLERR | _EPOLLHUP

    # In Python 2, _current.instance points to the current IOLoop.
    # 创建全局ThreadLocal对象
    # _current.instance指向当前的IOLoop。
    _current = threading.local()  # 实现单例

    # In Python 3, _ioloop_for_asyncio maps from asyncio loops to IOLoops.
    _ioloop_for_asyncio = dict()  # 静态存放ioloop对象

    @classmethod
    def configure(cls, impl, **kwargs):
        if asyncio is not None:
            from tornado.platform.asyncio import BaseAsyncIOLoop

            if isinstance(impl, (str, unicode_type)):
                impl = import_object(impl)
            if not issubclass(impl, BaseAsyncIOLoop):
                raise RuntimeError(
                    "only AsyncIOLoop is allowed when asyncio is available")
        super(IOLoop, cls).configure(impl, **kwargs)

    # 不推荐使用  alias for `IOLoop.current()  返回当前ioloop实例，
    @staticmethod
    def instance():
        """Deprecated alias for `IOLoop.current()`.

        .. versionchanged:: 5.0

           Previously, this method returned a global singleton
           `IOLoop`, in contrast with the per-thread `IOLoop` returned
           by `current()`. In nearly all cases the two were the same
           (when they differed, it was generally used from non-Tornado
           threads to communicate back to the main thread's `IOLoop`).
           This distinction is not present in `asyncio`, so in order
           to facilitate integration with that package `instance()`
           was changed to be an alias to `current()`. Applications
           using the cross-thread communications aspect of
           `instance()` should instead set their own global variable
           to point to the `IOLoop` they want to use.

        .. deprecated:: 5.0
        """
        return IOLoop.current()

    # 不推荐使用 alias for `make_current()` ，设置ioloop实例
    def install(self):
        """Deprecated alias for `make_current()`.

        .. versionchanged:: 5.0

           Previously, this method would set this `IOLoop` as the
           global singleton used by `IOLoop.instance()`. Now that
           `instance()` is an alias for `current()`, `install()`
           is an alias for `make_current()`.

        .. deprecated:: 5.0
        """
        self.make_current()

    # 不推荐使用 alias for `clear_current() 移除ioloop实例
    @staticmethod
    def clear_instance():
        """Deprecated alias for `clear_current()`.

        .. versionchanged:: 5.0

           Previously, this method would clear the `IOLoop` used as
           the global singleton by `IOLoop.instance()`. Now that
           `instance()` is an alias for `current()`,
           `clear_instance()` is an alias for `clear_current()`.

        .. deprecated:: 5.0

        """
        IOLoop.clear_current()

    # 获取ioloop实例，若没有，根据参数看要不要创建一个
    @staticmethod
    def current(instance=True):
        """Returns the current thread's `IOLoop`.

        If an `IOLoop` is currently running or has been marked as
        current by `make_current`, returns that instance.  If there is
        no current `IOLoop` and ``instance`` is true, creates one.

        .. versionchanged:: 4.1
           Added ``instance`` argument to control the fallback to
           `IOLoop.instance()`.
        .. versionchanged:: 5.0
           On Python 3, control of the current `IOLoop` is delegated
           to `asyncio`, with this and other methods as pass-through accessors.
           The ``instance`` argument now controls whether an `IOLoop`
           is created automatically when there is none, instead of
           whether we fall back to `IOLoop.instance()` (which is now
           an alias for this method). ``instance=False`` is deprecated,
           since even if we do not create an `IOLoop`, this method
           may initialize the asyncio loop.
        """
        if asyncio is None:  # py3.4以下
            current = getattr(IOLoop._current, "instance", None)  # 尝试获取全局的ioloop对象
            if current is None and instance:  # 如果当前没有ioloop对象and instance为True要实例就创建一个
                # 创建一个ioloop对象 这里实例化，会调用父类的 __new__ 方法，会实例化ioloop并返回，使用了继承实现方法后设置的_impl
                current = IOLoop()
                if IOLoop._current.instance is not current:
                    raise RuntimeError("new IOLoop did not become current")
        else:  # py3.4以上
            try:
                loop = asyncio.get_event_loop()  # 获取 asyncio 的ioloop
            except (RuntimeError, AssertionError):
                if not instance:  # 获取不到返回 None，表示没有启动ioloop
                    return None
                raise  # 这个好像有点多余啊
            try:
                return IOLoop._ioloop_for_asyncio[loop]  # 存在，就静态变量中取出来返回
            except KeyError:
                if instance:  # 没有且要实例
                    from tornado.platform.asyncio import AsyncIOMainLoop
                    current = AsyncIOMainLoop(make_current=True)  # 创建一个ioloop返回
                else:
                    current = None  # 不然就返回None
        return current

    # 移除存在的ioloop，把自己设置为全局ioloop-（设置ioloop，已有则替换）
    # 看 initialize 的操作，也有有可能就是替换一下自己本身，就只是清空一遍的 ioloop
    def make_current(self):
        """Makes this the `IOLoop` for the current thread.
        为当前线程创建一个ioloop

        An `IOLoop` automatically becomes current for its thread
        when it is started, but it is sometimes useful to call
        `make_current` explicitly before starting the `IOLoop`,
        so that code run at startup time can find the right
        instance.
        一个“ IOLoop”在启动时会自动变为当前线程，但是有时在启动“ IOLoop”之前显式调用“ make_current”很有用，
        这样在启动时运行的代码可以找到正确的实例。

        .. versionchanged:: 4.1
           An `IOLoop` created while there is no current `IOLoop`
           will automatically become current.

        .. versionchanged:: 5.0
           This method also sets the current `asyncio` event loop.
           该方法还设置当前线程使用“ asyncio”事件循环。
        """
        # The asyncio event loops override this method.
        # asyncio事件循环将覆盖此方法。
        assert asyncio is None  # 下面 <=py3.3 时才执行，  asyncio那边事件循环有覆盖此方法。
        old = getattr(IOLoop._current, "instance", None)  # 获取全局的ioloop对象，如果有的话
        if old is not None:  # 有的话
            old.clear_current()  # 清空旧对象上的数据
        IOLoop._current.instance = self  # 全局ioloop对象设置为自己

    # 移除 current ioloop
    @staticmethod
    def clear_current():
        """Clears the `IOLoop` for the current thread.

        Intended primarily for use by test frameworks in between tests.
        主要用于测试之间的测试框架。

        .. versionchanged:: 5.0
           This method also clears the current `asyncio` event loop.
        """
        old = IOLoop.current(instance=False)
        if old is not None:
            old._clear_current_hook()  # also clears the current `asyncio` event loop.
        if asyncio is None:
            IOLoop._current.instance = None

    # 要被覆盖 tornado.platform.asyncio 文件中有定义，当 <= py3.3调用则pass
    def _clear_current_hook(self):
        """Instance method called when an IOLoop ceases to be current.

        May be overridden by subclasses as a counterpart to make_current.
        """
        pass

    # 返回自身类
    @classmethod
    def configurable_base(cls):
        return IOLoop

    # 这里默认返回 PollIOLoop 或 AsyncIOLoop
    @classmethod
    def configurable_default(cls):
        if asyncio is not None:
            from tornado.platform.asyncio import AsyncIOLoop
            return AsyncIOLoop
        return PollIOLoop

    # 初始化
    def initialize(self, make_current=None):
        if make_current is None:
            if IOLoop.current(instance=False) is None:  # 尝试获取已有的ioloop
                # 之前不存在，就创建一个ioloop
                self.make_current()
        elif make_current:
            current = IOLoop.current(instance=False)  # 尝试获取已有的ioloop
            # AsyncIO loops can already be current by this point.
            if current is not None and current is not self:  # 存在一个ioloop，如果不是本身则报错
                raise RuntimeError("current IOLoop already exists")
            self.make_current()

    # 要被覆盖
    def close(self, all_fds=False):
        """Closes the `IOLoop`, freeing any resources used.

        If ``all_fds`` is true, all file descriptors registered on the
        IOLoop will be closed (not just the ones created by the
        `IOLoop` itself).

        Many applications will only use a single `IOLoop` that runs for the
        entire lifetime of the process.  In that case closing the `IOLoop`
        is not necessary since everything will be cleaned up when the
        process exits.  `IOLoop.close` is provided mainly for scenarios
        such as unit tests, which create and destroy a large number of
        ``IOLoops``.

        An `IOLoop` must be completely stopped before it can be closed.  This
        means that `IOLoop.stop()` must be called *and* `IOLoop.start()` must
        be allowed to return before attempting to call `IOLoop.close()`.
        Therefore the call to `close` will usually appear just after
        the call to `start` rather than near the call to `stop`.

        .. versionchanged:: 3.1
           If the `IOLoop` implementation supports non-integer objects
           for "file descriptors", those objects will have their
           ``close`` method when ``all_fds`` is true.
        """
        raise NotImplementedError()

    # 要被覆盖
    def add_handler(self, fd, handler, events):
        """Registers the given handler to receive the given events for ``fd``.

        The ``fd`` argument may either be an integer file descriptor or
        a file-like object with a ``fileno()`` method (and optionally a
        ``close()`` method, which may be called when the `IOLoop` is shut
        down).

        The ``events`` argument is a bitwise or of the constants
        ``IOLoop.READ``, ``IOLoop.WRITE``, and ``IOLoop.ERROR``.

        When an event occurs, ``handler(fd, events)`` will be run.

        .. versionchanged:: 4.0
           Added the ability to pass file-like objects in addition to
           raw file descriptors.
        """
        raise NotImplementedError()

    # 要被覆盖
    def update_handler(self, fd, events):
        """Changes the events we listen for ``fd``.

        .. versionchanged:: 4.0
           Added the ability to pass file-like objects in addition to
           raw file descriptors.
        """
        raise NotImplementedError()

    # 要被覆盖
    def remove_handler(self, fd):
        """Stop listening for events on ``fd``.

        .. versionchanged:: 4.0
           Added the ability to pass file-like objects in addition to
           raw file descriptors.
        """
        raise NotImplementedError()

    # 不建议使用，要被覆盖
    def set_blocking_signal_threshold(self, seconds, action):
        """Sends a signal if the `IOLoop` is blocked for more than
        ``s`` seconds.
        ioloop被阻塞超时发送信号处理

        Pass ``seconds=None`` to disable.  Requires Python 2.6 on a unixy
        platform.

        The action parameter is a Python signal handler.  Read the
        documentation for the `signal` module for more information.
        If ``action`` is None, the process will be killed if it is
        blocked for too long.

        .. deprecated:: 5.0

           Not implemented on the `asyncio` event loop. Use the environment
           variable ``PYTHONASYNCIODEBUG=1`` instead. This method will be
           removed in Tornado 6.0.
           6.0 会移除该方法
        """
        raise NotImplementedError()

    # 不建议使用，要被覆盖
    def set_blocking_log_threshold(self, seconds):
        """Logs a stack trace if the `IOLoop` is blocked for more than
        ``s`` seconds.
        ioloop被阻塞超时时打log

        Equivalent to ``set_blocking_signal_threshold(seconds,
        self.log_stack)``

        .. deprecated:: 5.0

           Not implemented on the `asyncio` event loop. Use the environment
           variable ``PYTHONASYNCIODEBUG=1`` instead. This method will be
           removed in Tornado 6.0.
           6.0 会移除该方法
        """
        self.set_blocking_signal_threshold(seconds, self.log_stack)

    # 为当前线程打印错误栈
    def log_stack(self, signal, frame):
        """Signal handler to log the stack trace of the current thread.

        For use with `set_blocking_signal_threshold`.

        .. deprecated:: 5.1

           This method will be removed in Tornado 6.0.
        """
        gen_log.warning('IOLoop blocked for %f seconds in\n%s',
                        self._blocking_signal_threshold,
                        ''.join(traceback.format_stack(frame)))

    # 要被覆盖
    def start(self):
        """Starts the I/O loop.

        The loop will run until one of the callbacks calls `stop()`, which
        will make the loop stop after the current event iteration completes.
        """
        raise NotImplementedError()

    # 设置logger，要在start中调用
    def _setup_logging(self):
        """The IOLoop catches and logs exceptions, so it's
        important that log output be visible.  However, python's
        default behavior for non-root loggers (prior to python
        3.2) is to print an unhelpful "no handlers could be
        found" message rather than the actual log entry, so we
        must explicitly configure logging if we've made it this
        far without anything.

        This method should be called from start() in subclasses.
        """
        if not any([logging.getLogger().handlers,
                    logging.getLogger('tornado').handlers,
                    logging.getLogger('tornado.application').handlers]):
            logging.basicConfig()

    # 要被覆盖
    def stop(self):
        """Stop the I/O loop.

        If the event loop is not currently running, the next call to `start()`
        will return immediately.

        Note that even after `stop` has been called, the `IOLoop` is not
        completely stopped until `IOLoop.start` has also returned.
        Some work that was scheduled before the call to `stop` may still
        be run before the `IOLoop` shuts down.
        """
        raise NotImplementedError()

    # 单独运行一个异步方法，运行完结束ioloop
    def run_sync(self, func, timeout=None):
        """Starts the `IOLoop`, runs the given function, and stops the loop.

        The function must return either an awaitable object or
        ``None``. If the function returns an awaitable object, the
        `IOLoop` will run until the awaitable is resolved (and
        `run_sync()` will return the awaitable's result). If it raises
        an exception, the `IOLoop` will stop and the exception will be
        re-raised to the caller.
        func返回值必须是可等待对象或者None

        The keyword-only argument ``timeout`` may be used to set
        a maximum duration for the function.  If the timeout expires,
        a `tornado.util.TimeoutError` is raised.
        可以设置timeout

        This method is useful to allow asynchronous calls in a
        ``main()`` function::

            async def main():
                # do stuff...

            if __name__ == '__main__':
                IOLoop.current().run_sync(main)

        .. versionchanged:: 4.3
           Returning a non-``None``, non-awaitable value is now an error.

        .. versionchanged:: 5.0
           If a timeout occurs, the ``func`` coroutine will be cancelled.

        """
        future_cell = [None]

        def run():
            try:
                result = func()  # 执行
                if result is not None:  # 表示返回值是个可等待对象
                    from tornado.gen import convert_yielded
                    result = convert_yielded(result)  # 转换yield为future对象
            except Exception:
                # 报错处理，创建一个future并设置错误信息
                future_cell[0] = Future()
                future_set_exc_info(future_cell[0], sys.exc_info())
            else:
                if is_future(result):
                    future_cell[0] = result  # 是future的话就存
                else:
                    # 处理运行结果
                    future_cell[0] = Future()
                    future_cell[0].set_result(result)
            self.add_future(future_cell[0], lambda future: self.stop())

        self.add_callback(run)  # 先添加回调方法
        if timeout is not None:
            def timeout_callback():  # 超时的处理方法
                # If we can cancel the future, do so and wait on it. If not,
                # Just stop the loop and return with the task still pending.
                # (If we neither cancel nor wait for the task, a warning
                # will be logged).
                if not future_cell[0].cancel():
                    self.stop()  # 关闭ioloop

            timeout_handle = self.add_timeout(self.time() + timeout, timeout_callback)  # 添加延迟方法
        self.start()  # 启动ioloop 此处死循环
        if timeout is not None:
            self.remove_timeout(timeout_handle)  # 移除延时任务
        if future_cell[0].cancelled() or not future_cell[0].done():  # 超时结束，报错
            raise TimeoutError('Operation timed out after %s seconds' % timeout)
        return future_cell[0].result()  # 返回执行结果

    # 获取时间戳
    def time(self):
        """Returns the current time according to the `IOLoop`'s clock.

        The return value is a floating-point number relative to an
        unspecified time in the past.

        By default, the `IOLoop`'s time function is `time.time`.  However,
        it may be configured to use e.g. `time.monotonic` instead.
        Calls to `add_timeout` that pass a number instead of a
        `datetime.timedelta` should use this function to compute the
        appropriate time, so they can work no matter what time function
        is chosen.
        """
        return time.time()

    # 添加延时任务，做了一个兼容，然后调用call_at
    def add_timeout(self, deadline, callback, *args, **kwargs):
        """Runs the ``callback`` at the time ``deadline`` from the I/O loop.

        Returns an opaque handle that may be passed to
        `remove_timeout` to cancel.

        ``deadline`` may be a number denoting a time (on the same
        scale as `IOLoop.time`, normally `time.time`), or a
        `datetime.timedelta` object for a deadline relative to the
        current time.  Since Tornado 4.0, `call_later` is a more
        convenient alternative for the relative case since it does not
        require a timedelta object.

        Note that it is not safe to call `add_timeout` from other threads.
        Instead, you must use `add_callback` to transfer control to the
        `IOLoop`'s thread, and then call `add_timeout` from there.
        注意，从其他线程调用`add_timeout`是不安全的
        相反，您必须使用“ add_callback”将控制权转移到
        IOLoop的线程，然后从那里调用add_timeout

        Subclasses of IOLoop must implement either `add_timeout` or
        `call_at`; the default implementations of each will call
        the other.  `call_at` is usually easier to implement, but
        subclasses that wish to maintain compatibility with Tornado
        versions prior to 4.0 must use `add_timeout` instead.
        IOLoop的子类必须实现`add_timeout`或
        `call_at`;每个的默认实现将调用另一个。 `call_at`通常更容易实现
        但是如果希望与Tornado4.0之前保持兼容性的话，使用的时候必须使用 `add_timeout`。

        .. versionchanged:: 4.0
           Now passes through ``*args`` and ``**kwargs`` to the callback.
        """
        if isinstance(deadline, numbers.Real):
            return self.call_at(deadline, callback, *args, **kwargs)
        elif isinstance(deadline, datetime.timedelta):
            return self.call_at(self.time() + timedelta_to_seconds(deadline),
                                callback, *args, **kwargs)
        else:
            raise TypeError("Unsupported deadline %r" % deadline)

    # 调用call_at
    def call_later(self, delay, callback, *args, **kwargs):
        """Runs the ``callback`` after ``delay`` seconds have passed.

        Returns an opaque handle that may be passed to `remove_timeout`
        to cancel.  Note that unlike the `asyncio` method of the same
        name, the returned object does not have a ``cancel()`` method.

        See `add_timeout` for comments on thread-safety and subclassing.

        .. versionadded:: 4.0
        """
        return self.call_at(self.time() + delay, callback, *args, **kwargs)

    # 会被覆盖了
    def call_at(self, when, callback, *args, **kwargs):
        """Runs the ``callback`` at the absolute time designated by ``when``.

        ``when`` must be a number using the same reference point as
        `IOLoop.time`.

        Returns an opaque handle that may be passed to `remove_timeout`
        to cancel.  Note that unlike the `asyncio` method of the same
        name, the returned object does not have a ``cancel()`` method.

        See `add_timeout` for comments on thread-safety and subclassing.

        .. versionadded:: 4.0
        """
        return self.add_timeout(when, callback, *args, **kwargs)

    # 需要被覆盖
    def remove_timeout(self, timeout):
        """Cancels a pending timeout.

        The argument is a handle as returned by `add_timeout`.  It is
        safe to call `remove_timeout` even if the callback has already
        been run.
        """
        raise NotImplementedError()

    # 需要被覆盖
    def add_callback(self, callback, *args, **kwargs):
        """Calls the given callback on the next I/O loop iteration.

        It is safe to call this method from any thread at any time,
        except from a signal handler.  Note that this is the **only**
        method in `IOLoop` that makes this thread-safety guarantee; all
        other interaction with the `IOLoop` must be done from that
        `IOLoop`'s thread.  `add_callback()` may be used to transfer
        control from other threads to the `IOLoop`'s thread.

        To add a callback from a signal handler, see
        `add_callback_from_signal`.
        """
        raise NotImplementedError()

    # 需要被覆盖
    def add_callback_from_signal(self, callback, *args, **kwargs):
        """Calls the given callback on the next I/O loop iteration.

        Safe for use from a Python signal handler; should not be used
        otherwise.

        Callbacks added with this method will be run without any
        `.stack_context`, to avoid picking up the context of the function
        that was interrupted by the signal.
        """
        raise NotImplementedError()

    # 需要被覆盖
    def spawn_callback(self, callback, *args, **kwargs):
        """Calls the given callback on the next IOLoop iteration.

        Unlike all other callback-related methods on IOLoop,
        ``spawn_callback`` does not associate the callback with its caller's
        ``stack_context``, so it is suitable for fire-and-forget callbacks
        that should not interfere with the caller.

        .. versionadded:: 4.0
        """
        with stack_context.NullContext():
            self.add_callback(callback, *args, **kwargs)

    # 为future添加完成时的回调
    def add_future(self, future, callback):
        """Schedules a callback on the ``IOLoop`` when the given
        `.Future` is finished.

        The callback is invoked with one argument, the
        `.Future`.

        This method only accepts `.Future` objects and not other
        awaitables (unlike most of Tornado where the two are
        interchangeable).
        只接受future对象
        """
        assert is_future(future)  # 检查
        callback = stack_context.wrap(callback)  # 就是函数本身
        future_add_done_callback(  # 设置future完成回调
            future, lambda future: self.add_callback(callback, future))  # self.add_callback 这个才是真的把回调设置到ioloop中，当future完成后
        # lambda future: self.add_callback(callback, future)  这是一个中间函数，它会进入ioloop，执行后，最终要执行的callback才进入ioloop

    # 阻塞调用， 基于线程池/进程池
    def run_in_executor(self, executor, func, *args):
        """Runs a function in a ``concurrent.futures.Executor``. If
        ``executor`` is ``None``, the IO loop's default executor will be used.

        Use `functools.partial` to pass keyword arguments to ``func``.

        .. versionadded:: 5.0
        """
        if ThreadPoolExecutor is None:
            raise RuntimeError(
                "concurrent.futures is required to use IOLoop.run_in_executor")

        if executor is None:
            if not hasattr(self, '_executor'):
                from tornado.process import cpu_count
                self._executor = ThreadPoolExecutor(max_workers=(cpu_count() * 5))
            executor = self._executor
        c_future = executor.submit(func, *args)
        # Concurrent Futures are not usable with await. Wrap this in a
        # Tornado Future instead, using self.add_future for thread-safety.
        t_future = Future()
        self.add_future(c_future, lambda f: chain_future(f, t_future))
        return t_future

    # 设置默认阻塞调用执行器
    def set_default_executor(self, executor):
        """Sets the default executor to use with :meth:`run_in_executor`.

        .. versionadded:: 5.0
        """
        self._executor = executor

    # 执行回调，带错误处理
    def _run_callback(self, callback):
        """Runs a callback with error handling.

        For use in subclasses.
        """
        try:
            ret = callback()
            if ret is not None:
                from tornado import gen
                # Functions that return Futures typically swallow all
                # exceptions and store them in the Future.  If a Future
                # makes it out to the IOLoop, ensure its exception (if any)
                # gets logged too.
                try:
                    ret = gen.convert_yielded(ret)
                except gen.BadYieldError:
                    # It's not unusual for add_callback to be used with
                    # methods returning a non-None and non-yieldable
                    # result, which should just be ignored.
                    pass
                else:
                    self.add_future(ret, self._discard_future_result)
        except Exception:
            self.handle_callback_exception(callback)

    def _discard_future_result(self, future):
        """Avoid unhandled-exception warnings from spawned coroutines."""
        future.result()

    # 处理cl_func出现异常的时候，可覆盖该方法， asyncio库会覆盖此方法，6.0中会移除
    def handle_callback_exception(self, callback):
        """This method is called whenever a callback run by the `IOLoop`
        throws an exception.

        By default simply logs the exception as an error.  Subclasses
        may override this method to customize reporting of exceptions.

        The exception itself is not passed explicitly, but is available
        in `sys.exc_info`.

        .. versionchanged:: 5.0

           When the `asyncio` event loop is used (which is now the
           default on Python 3), some callback errors will be handled by
           `asyncio` instead of this method.

        .. deprecated: 5.1

           Support for this method will be removed in Tornado 6.0.
        """
        app_log.error("Exception in callback %r", callback, exc_info=True)

    # 获取 {obj.fileno(), obj},格式的元组
    def split_fd(self, fd):
        """Returns an (fd, obj) pair from an ``fd`` parameter.

        We accept both raw file descriptors and file-like objects as
        input to `add_handler` and related methods.  When a file-like
        object is passed, we must retain the object itself so we can
        close it correctly when the `IOLoop` shuts down, but the
        poller interfaces favor file descriptors (they will accept
        file-like objects and call ``fileno()`` for you, but they
        always return the descriptor itself).

        我们接受原始文件描述符和类文件对象作为 输入到`add_handler`和相关方法。

        This method is provided for use by `IOLoop` subclasses and should
        not generally be used by application code.

        .. versionadded:: 4.0
        """
        try:
            return fd.fileno(), fd
        except AttributeError:
            return fd, fd

    # 关闭io对象
    def close_fd(self, fd):
        """Utility method to close an ``fd``.

        If ``fd`` is a file-like object, we close it directly; otherwise
        we use `os.close`.
        如果``fd``是一个类似文件的对象，我们直接关闭它; 除此以外我们使用`os.close`

        This method is provided for use by `IOLoop` subclasses (in
        implementations of ``IOLoop.close(all_fds=True)`` and should
        not generally be used by application code.

        .. versionadded:: 4.0
        """
        try:
            try:
                fd.close()
            except AttributeError:
                os.close(fd)
        except OSError:
            pass


# 该类被 select,kqueue,epoll所继承
class PollIOLoop(IOLoop):
    """Base class for IOLoops built around a select-like function.

    For concrete implementations, see `tornado.platform.epoll.EPollIOLoop`
    (Linux), `tornado.platform.kqueue.KQueueIOLoop` (BSD and Mac), or
    `tornado.platform.select.SelectIOLoop` (all platforms).
    """

    def initialize(self, impl, time_func=None, **kwargs):
        super(PollIOLoop, self).initialize(**kwargs)  # IOLoop.initialize
        self._impl = impl  # 多路复用循环具体对象
        if hasattr(self._impl, 'fileno'):
            set_close_exec(self._impl.fileno())
        self.time_func = time_func or time.time  # 获取时间的方法（时间戳：秒）
        self._handlers = {}  # (obj, stack_context.wrap(handler))  （io对象， 回调函数）
        self._events = {}
        self._callbacks = collections.deque()  # 回调队列
        self._timeouts = []  # 延时队列
        self._cancellations = 0  # 被取消的延时任务的数量，要被gc
        self._running = False  # 标识是否启动
        self._stopped = False  # 标识是否停止
        self._closing = False  # 标识是否被close
        self._thread_ident = None  # 线程标识，一个魔术数字
        self._pid = os.getpid()  # 获取当前进程的pid
        self._blocking_signal_threshold = None
        self._timeout_counter = itertools.count()  # “无限”迭代器

        # Create a pipe that we send bogus data to when we want to wake
        # the I/O loop when it is idle
        # 创建一个管道，当 io loop 空闲的时候，我们想唤醒它，就发一个虚假数据
        self._waker = Waker()  # 类似于套接字的对象，可以从select（）唤醒另一个线程
        self.add_handler(self._waker.fileno(),
                         lambda fd, events: self._waker.consume(),
                         self.READ)

    # 返回当前类
    @classmethod
    def configurable_base(cls):
        return PollIOLoop

    # 默认配置，返回使用的 多路复用类， 经过封装提供了共用方法
    @classmethod
    def configurable_default(cls):
        if hasattr(select, "epoll"):
            from tornado.platform.epoll import EPollIOLoop
            return EPollIOLoop
        if hasattr(select, "kqueue"):
            # Python 2.6+ on BSD or Mac
            from tornado.platform.kqueue import KQueueIOLoop
            return KQueueIOLoop
        from tornado.platform.select import SelectIOLoop
        return SelectIOLoop

    # 关闭ioloop
    def close(self, all_fds=False):
        self._closing = True  # 标识已关闭
        self.remove_handler(self._waker.fileno())  # todo zzy
        if all_fds:
            for fd, handler in list(self._handlers.values()):
                self.close_fd(fd)  # 关闭所有io对象
        self._waker.close()  # todo zzy 通道close?
        self._impl.close()  # 底层ioloop对象close
        self._callbacks = None  # 回调队列置空
        self._timeouts = None
        if hasattr(self, '_executor'):  # todo zzy
            self._executor.shutdown()

    def add_handler(self, fd, handler, events):
        fd, obj = self.split_fd(fd)  # 转换成元组
        # 设置回调
        self._handlers[fd] = (obj, stack_context.wrap(handler))  # todo zzy 就是函数本身
        self._impl.register(fd, events | self.ERROR)  # 注册监听事件，默认必监听err状态

    # 修改fd监听
    def update_handler(self, fd, events):
        fd, obj = self.split_fd(fd)  # 转换成元组
        self._impl.modify(fd, events | self.ERROR)  # 修改监听事件

    # 取消fd监听
    def remove_handler(self, fd):
        fd, obj = self.split_fd(fd)  # 转换成元组
        self._handlers.pop(fd, None)  # todo zzy
        self._events.pop(fd, None)  # todo zzy
        try:
            self._impl.unregister(fd)  # 取消监听事件
        except Exception:
            gen_log.debug("Error deleting fd from IOLoop", exc_info=True)

    # 设置阻塞信号阈值
    def set_blocking_signal_threshold(self, seconds, action):
        if not hasattr(signal, "setitimer"):
            gen_log.error("set_blocking_signal_threshold requires a signal module "
                          "with the setitimer method")
            return
        self._blocking_signal_threshold = seconds
        if seconds is not None:
            signal.signal(signal.SIGALRM,
                          action if action is not None else signal.SIG_DFL)

    # todo zzy 不懂
    def start(self):
        if self._running:  # 已经启动
            raise RuntimeError("IOLoop is already running")
        if os.getpid() != self._pid:  # 无法跨进程共享PollIOLoops
            raise RuntimeError("Cannot share PollIOLoops across processes")
        self._setup_logging()  # 设置日志logger
        if self._stopped:  # 如果是停止的
            self._stopped = False  # 停止状态设为False
            return
        old_current = IOLoop.current(instance=False)
        if old_current is not self:
            self.make_current()
        self._thread_ident = thread.get_ident()  # 获得一个代表当前线程的魔法数字,该数字可能被复用
        self._running = True  # 标识已启动

        # signal.set_wakeup_fd closes a race condition in event loops:
        # signal.set_wakeup_fd在事件循环中关闭竞争条件：
        # a signal may arrive at the beginning of select/poll/etc
        # before it goes into its interruptible sleep, so the signal
        # will be consumed without waking the select.  The solution is
        # 信号可能会进入select / poll / etc的开头，在进入其可中断的睡眠之前，因此该信号将被消耗而不会唤醒select
        # for the (C, synchronous) signal handler to write to a pipe,
        # which will then be seen by select.
        #
        # In python's signal handling semantics, this only matters on the
        # main thread (fortunately, set_wakeup_fd only works on the main
        # thread and will raise a ValueError otherwise).
        # 在python的信号处理语义中，这仅在主线程上起作用（幸运的是，set_wakeup_fd仅在主线程上起作用，否则将引发ValueError）

        # If someone has already set a wakeup fd, we don't want to
        # disturb it.  This is an issue for twisted, which does its
        # SIGCHLD processing in response to its own wakeup fd being
        # written to.  As long as the wakeup fd is registered on the IOLoop,
        # the loop will still wake up and everything should work.
        old_wakeup_fd = None
        if hasattr(signal, 'set_wakeup_fd') and os.name == 'posix':
            # requires python 2.6+, unix.  set_wakeup_fd exists but crashes
            # the python process on windows.
            try:
                old_wakeup_fd = signal.set_wakeup_fd(self._waker.write_fileno())  # 信号缓冲，返回旧的wakeup fd
                if old_wakeup_fd != -1:
                    # 有旧的用旧的，只有一次start所以这里的逻辑一般情况下不会执行到
                    # Already set, restore previous value.  This is a little racy,
                    # but there's no clean get_wakeup_fd and in real use the
                    # IOLoop is just started once at the beginning.
                    # 已经设置，恢复之前的值。 这有点荒谬，但是没有干净的get_wakeup_fd，在实际使用中，IOLoop只是在开始时就启动了一次。
                    signal.set_wakeup_fd(old_wakeup_fd)
                    old_wakeup_fd = None
            except ValueError:
                # Non-main thread, or the previous value of wakeup_fd
                # is no longer valid.
                old_wakeup_fd = None

        try:
            while True:
                # Prevent IO event starvation by delaying new callbacks
                # to the next iteration of the event loop.
                # 通过将新的回调延迟到事件循环的下一次迭代来防止IO事件饥饿
                ncallbacks = len(self._callbacks)

                # Add any timeouts that have come due to the callback list.
                # Do not run anything until we have determined which ones
                # are ready, so timeouts that call add_timeout cannot
                # schedule anything in this iteration.
                # 添加由于回调列表而引起的任何延时任务。
                # 在确定已经准备好什么之前，不要运行任何东西，因此调用add_timeout的超时无法在此迭代中安排任何时间。
                due_timeouts = []
                if self._timeouts:
                    now = self.time()
                    while self._timeouts:
                        if self._timeouts[0].callback is None:
                            # The timeout was cancelled.  Note that the
                            # cancellation check is repeated below for timeouts
                            # that are cancelled by another timeout or callback.
                            heapq.heappop(self._timeouts)
                            self._cancellations -= 1
                        elif self._timeouts[0].deadline <= now:
                            due_timeouts.append(heapq.heappop(self._timeouts))
                        else:
                            break
                    if (self._cancellations > 512 and
                            self._cancellations > (len(self._timeouts) >> 1)):
                        # Clean up the timeout queue when it gets large and it's
                        # more than half cancellations.
                        self._cancellations = 0
                        self._timeouts = [x for x in self._timeouts
                                          if x.callback is not None]
                        heapq.heapify(self._timeouts)

                for i in range(ncallbacks):
                    self._run_callback(self._callbacks.popleft())
                for timeout in due_timeouts:
                    if timeout.callback is not None:
                        self._run_callback(timeout.callback)
                # Closures may be holding on to a lot of memory, so allow
                # them to be freed before we go into our poll wait.
                due_timeouts = timeout = None

                if self._callbacks:
                    # If any callbacks or timeouts called add_callback,
                    # we don't want to wait in poll() before we run them.
                    poll_timeout = 0.0
                elif self._timeouts:
                    # If there are any timeouts, schedule the first one.
                    # Use self.time() instead of 'now' to account for time
                    # spent running callbacks.
                    poll_timeout = self._timeouts[0].deadline - self.time()
                    poll_timeout = max(0, min(poll_timeout, _POLL_TIMEOUT))
                else:
                    # No timeouts and no callbacks, so use the default.
                    poll_timeout = _POLL_TIMEOUT

                if not self._running:
                    break

                if self._blocking_signal_threshold is not None:
                    # clear alarm so it doesn't fire while poll is waiting for
                    # events.
                    signal.setitimer(signal.ITIMER_REAL, 0, 0)

                try:
                    event_pairs = self._impl.poll(poll_timeout)
                except Exception as e:
                    # Depending on python version and IOLoop implementation,
                    # different exception types may be thrown and there are
                    # two ways EINTR might be signaled:
                    # * e.errno == errno.EINTR
                    # * e.args is like (errno.EINTR, 'Interrupted system call')
                    if errno_from_exception(e) == errno.EINTR:
                        continue
                    else:
                        raise

                if self._blocking_signal_threshold is not None:
                    signal.setitimer(signal.ITIMER_REAL,
                                     self._blocking_signal_threshold, 0)

                # Pop one fd at a time from the set of pending fds and run
                # its handler. Since that handler may perform actions on
                # other file descriptors, there may be reentrant calls to
                # this IOLoop that modify self._events
                self._events.update(event_pairs)
                while self._events:
                    fd, events = self._events.popitem()
                    try:
                        fd_obj, handler_func = self._handlers[fd]
                        handler_func(fd_obj, events)
                    except (OSError, IOError) as e:
                        if errno_from_exception(e) == errno.EPIPE:
                            # Happens when the client closes the connection
                            pass
                        else:
                            self.handle_callback_exception(self._handlers.get(fd))
                    except Exception:
                        self.handle_callback_exception(self._handlers.get(fd))
                fd_obj = handler_func = None

        finally:
            # reset the stopped flag so another start/stop pair can be issued
            self._stopped = False
            if self._blocking_signal_threshold is not None:
                signal.setitimer(signal.ITIMER_REAL, 0, 0)
            if old_current is None:
                IOLoop.clear_current()
            elif old_current is not self:
                old_current.make_current()
            if old_wakeup_fd is not None:
                signal.set_wakeup_fd(old_wakeup_fd)

    # 停止ioloop
    def stop(self):
        self._running = False  # 标识不运行
        self._stopped = True  # 标识停止
        self._waker.wake()  # todo zzy

    # 获取时间戳
    def time(self):
        return self.time_func()  # 获取时间戳

    # 添加延迟任务，覆盖了父类
    def call_at(self, deadline, callback, *args, **kwargs):
        timeout = _Timeout(
            deadline,
            # functools.partial 封装函数，提供默认参数值
            functools.partial(stack_context.wrap(callback), *args, **kwargs),  # todo zzy 就是函数本身
            self)
        heapq.heappush(self._timeouts, timeout)  # 将延迟对象，放入延迟队列
        return timeout

    # 移除超时的延迟对象，实际是标识删除，延后gc
    def remove_timeout(self, timeout):
        # 从堆删除对象太复杂，所以只要把内存中的延迟对象的cb设置为None，并增加计数，计数到一定数值就gc一下
        # Removing from a heap is complicated, so just leave the defunct
        # timeout object in the queue (see discussion in
        # http://docs.python.org/library/heapq.html).
        # If this turns out to be a problem, we could add a garbage
        # collection pass whenever there are too many dead timeouts.
        timeout.callback = None
        self._cancellations += 1

    # 添加回调函数到ioloop
    def add_callback(self, callback, *args, **kwargs):
        if self._closing:  # ioloop是close状态
            return
        # Blindly insert into self._callbacks. This is safe even
        # from signal handlers because deque.append is atomic.
        # deque.append 是原子操作，只管append就行
        self._callbacks.append(functools.partial(  # 把cb_fun放入回调队列
            stack_context.wrap(callback), *args, **kwargs))
        if thread.get_ident() != self._thread_ident:  # 当前线程不是ioloop线程(可能开了多线程)
            # This will write one byte but Waker.consume() reads many
            # at once, so it's ok to write even when not strictly
            # necessary.
            self._waker.wake()  # 唤醒ioloop，向通道写入数据，
        else:
            # If we're on the IOLoop's thread, we don't need to wake anyone.
            pass

    # todo zzy 不懂
    def add_callback_from_signal(self, callback, *args, **kwargs):
        with stack_context.NullContext():
            self.add_callback(callback, *args, **kwargs)


# 延迟任务类
class _Timeout(object):
    """An IOLoop timeout, a UNIX timestamp and a callback"""

    # Reduce memory overhead when there are lots of pending callbacks
    __slots__ = ['deadline', 'callback', 'tdeadline']

    def __init__(self, deadline, callback, io_loop):
        if not isinstance(deadline, numbers.Real):
            raise TypeError("Unsupported deadline %r" % deadline)
        self.deadline = deadline
        self.callback = callback
        self.tdeadline = (deadline, next(io_loop._timeout_counter))  # next(...) “无限”迭代器的下一个数字

    # Comparison methods to sort by deadline, with object id as a tiebreaker
    # to guarantee a consistent ordering.  The heapq module uses __le__
    # in python2.5, and __lt__ in 2.6+ (sort() and most other comparisons
    # use __lt__).
    def __lt__(self, other):
        return self.tdeadline < other.tdeadline

    def __le__(self, other):
        return self.tdeadline <= other.tdeadline


# 定时循环执行任务类
class PeriodicCallback(object):
    """Schedules the given callback to be called periodically.

    The callback is called every ``callback_time`` milliseconds.
    Note that the timeout is given in milliseconds, while most other
    time-related functions in Tornado use seconds.
    以毫秒为单位，每callback_time毫秒回调一次

    If ``jitter`` is specified, each callback time will be randomly selected
    within a window of ``jitter * callback_time`` milliseconds.
    Jitter can be used to reduce alignment of events with similar periods.
    A jitter of 0.1 means allowing a 10% variation in callback time.
    The window is centered on ``callback_time`` so the total number of calls
    within a given interval should not be significantly affected by adding
    jitter.
    jitter，执行时间的上下抖动，可以用于避免大量任务在同一时间执行，值可以为小数

    If the callback runs for longer than ``callback_time`` milliseconds,
    subsequent invocations will be skipped to get back on schedule.
    如果在下一次定时触发的时候上一个任务还未结束，则跳过

    `start` must be called after the `PeriodicCallback` is created.
    必须调用start,才会开始定时任务

    .. versionchanged:: 5.0
       The ``io_loop`` argument (deprecated since version 4.1) has been removed.

    .. versionchanged:: 5.1
       The ``jitter`` argument is added.
    """

    def __init__(self, callback, callback_time, jitter=0):
        self.callback = callback  # 回调方法
        if callback_time <= 0:
            raise ValueError("Periodic callback must have a positive callback_time")
        self.callback_time = callback_time  # 执行周期，毫秒
        self.jitter = jitter  # 时间抖动
        self._running = False  # 标识任务是否在执行
        self._timeout = None  # 延时任务_Timeout类

    # 启动任务，放入ioloop延迟队列
    def start(self):
        """Starts the timer."""
        # Looking up the IOLoop here allows to first instantiate the
        # PeriodicCallback in another thread, then start it using
        # IOLoop.add_callback().
        self.io_loop = IOLoop.current()  # 获取ioloop对象
        self._running = True  # 标识任务正在运行
        self._next_timeout = self.io_loop.time()  # 获取当前时间戳，下一次任务执行时间
        self._schedule_next()  # 设置下一次触发

    # 停止定时任务
    def stop(self):
        """Stops the timer."""
        self._running = False  # 标识为停止
        if self._timeout is not None:  # 如果有任务
            self.io_loop.remove_timeout(self._timeout)  # 移除延时任务
            self._timeout = None  # 置空

    # 返回是否正在运行
    def is_running(self):
        """Return True if this `.PeriodicCallback` has been started.

        .. versionadded:: 4.1
        """
        return self._running

    # 这个方法是 设置到延迟对象中的回调方法，在里面调用了用户设置的回调函数
    def _run(self):
        if not self._running:
            return
        try:
            return self.callback()
        except Exception:
            # 这里调用改一下异常处理
            self.io_loop.handle_callback_exception(self.callback)
        finally:
            self._schedule_next()

    # 设置下一次触发，放入ioloop延迟队列
    def _schedule_next(self):
        if self._running:  # 任务在运行
            self._update_next(self.io_loop.time())  # 更新下一次任务执行时间 _next_timeout
            self._timeout = self.io_loop.add_timeout(self._next_timeout, self._run)  # 放入ioloop的延时队列中，返回_Timeout类对象

    # 计算下一次触发时间
    def _update_next(self, current_time):
        callback_time_sec = self.callback_time / 1000.0  # 获取周期，单位秒
        if self.jitter:  # 计算时间抖动
            # apply jitter fraction
            callback_time_sec *= 1 + (self.jitter * (random.random() - 0.5))
        if self._next_timeout <= current_time:  # 记录的时间已近过时了
            # The period should be measured from the start of one call
            # to the start of the next. If one call takes too long,
            # skip cycles to get back to a multiple of the original
            # schedule.
            # 计算 math.floor(1.676) => 1
            self._next_timeout += (math.floor((current_time - self._next_timeout) /
                                              callback_time_sec) + 1) * callback_time_sec
        else:
            # If the clock moved backwards, ensure we advance the next
            # timeout instead of recomputing the same value again.
            # This may result in long gaps between callbacks if the
            # clock jumps backwards by a lot, but the far more common
            # scenario is a small NTP adjustment that should just be
            # ignored.
            #
            # Note that on some systems if time.time() runs slower
            # than time.monotonic() (most common on windows), we
            # effectively experience a small backwards time jump on
            # every iteration because PeriodicCallback uses
            # time.time() while asyncio schedules callbacks using
            # time.monotonic().
            # https://github.com/tornadoweb/tornado/issues/2333

            # tornado用time.time,asyncio用monotonic
            self._next_timeout += callback_time_sec
