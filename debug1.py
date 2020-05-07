# -*- coding: utf-8 -*-
import tornado
import tornado.ioloop
import tornado.gen


@tornado.gen.coroutine
def main():
    print(1)


tornado.ioloop.IOLoop.current().run_sync(main)
# ioloop = tornado.ioloop.IOLoop.current()
# ioloop.spawn_callback(main)
# # ioloop.call_later(2, main)
# ioloop.start()
