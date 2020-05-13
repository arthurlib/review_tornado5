from tornado import web, ioloop, gen
import datetime
import tornado
import time


#
# def run_task_loop(fun, interval):
#     task = ioloop.PeriodicCallback(fun, interval)
#     task.start()
#     return task
#
#
# def run_task_at(fun, dtime):
#     t = datetime.datetime.strptime(dtime, "%Y-%m-%d %H:%M:%S").timetuple()
#     return tornado.ioloop.IOLoop.current().add_timeout(int(time.mktime(t)), fun)
#

def run_task_after(fun, delay):
    return tornado.ioloop.IOLoop.current().add_timeout(tornado.ioloop.time.time() + delay, fun)


@gen.coroutine
def run_task_at2(fun, dtime):
    t = datetime.datetime.strptime(dtime, "%Y-%m-%d %H:%M:%S").timetuple()

    yield gen.sleep(int(time.mktime(t)) - tornado.ioloop.time.time())
    raise gen.Return(fun())
    # return fun()


#
#
# @gen.coroutine
# def run_task_after2(fun, delay):
#     yield gen.sleep(delay)
#     raise gen.Return(fun())
#
#
# class MainHandler(web.RequestHandler):
#     def get(self):
#         self.write('Hello Tornado')
#         print(tornado.ioloop.time.time())


def p2s():
    print('2s ', datetime.datetime.now())
    return 'local done'


@gen.coroutine
def main():
    a = yield run_task_at2(p2s, "2020-05-11 10:07:00")
    print(a)
    # yield gen.sleep(0)


if __name__ == '__main__':
    # application = web.Application([
    #     (r'/', MainHandler),
    # ])
    # application.listen(8081)
    main()
    # run_task_after(p2s, 2)
    # run_task_loop(p2s, 2000)
    # ioloop.IOLoop.current().spawn_callback(lambda: run_task_after2(p2s, 4))
    # ioloop.IOLoop.current().spawn_callback(lambda: run_task_at2(p2s, "2020-03-26 19:24:00"))
    # ioloop.IOLoop.current().spawn_callback(lambda: run_task_at2(p2s, "2020-03-26 19:24:00"))
    ioloop.IOLoop.instance().start()
