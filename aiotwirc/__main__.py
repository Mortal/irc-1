import sys
import types
import asyncio
import inspect
import logging
import argparse
import functools
import importlib
import traceback
import contextlib
import collections

import irc.client

from aiotwirc.stdio import async_readlines


BASE_CONFIG = dict(
    APP_NAME='aiotwirc',
    DEFAULT_USERNAME='justinfan3141592653',
    DEFAULT_PASSWORD='blah',
    USERNAME=None,
    PASSWORD=None,
    SERVER='irc.chat.twitch.tv',
    PORT=6667,
    CAPS='twitch.tv/tags twitch.tv/commands twitch.tv/membership',
    CHANNELS=(),
)


def read_config():
    config = dict(BASE_CONFIG)
    with open('twitchconfig.py') as fp:
        config_source = fp.read()
    exec(config_source, {}, config)
    ns = types.SimpleNamespace()
    for k, v in config.items():
        setattr(ns, k, v)
    return ns


def init_logging(config):
    def f(name, filename, format, outformat=None, level=logging.INFO):
        logger = logging.getLogger(name)
        handler = logging.FileHandler(filename)
        handler.formatter = logging.Formatter(format)
        logger.addHandler(handler)
        if outformat is not None:
            outhandler = logging.StreamHandler(sys.stdout)
            outhandler.formatter = logging.Formatter(outformat)
            logger.addHandler(outhandler)
        logger.setLevel(level)

    f('irc.client', 'irc.log',
      '[%(asctime)s %(name)s %(levelname)s] %(message)s', level=logging.DEBUG)
    f('aiotwirc.messages', 'messages.txt',
      '%(asctime)s %(event)r',
      '[%(asctime)s %(target)s %(source)30s] %(message)s')
    f('aiotwirc.events', 'events.txt',
      '%(asctime)s %(event)r',
      '[%(asctime)s %(type)10s] %(message)s')


class Handler:
    def __init__(self):
        self.welcomed = asyncio.Event()
        self.subhandlers = {
            m: importlib.import_module('handlers.%s' % m).Handler()
            for m in 'hostnotify ping sub highlight log'.split()
        }

    async def __call__(self, connection, event):
        if event.type == 'all_raw_messages':
            return
        for handler in [self] + list(self.subhandlers.values()):
            try:
                method = getattr(handler, 'handle_' + event.type)
            except AttributeError:
                continue
            try:
                await method(connection, event)
            except Exception:
                print('Exception in %s.handle_%s' %
                      (handler.__class__.__qualname__,
                       event.type))
                traceback.print_exc()

    async def handle_welcome(self, connection, event):
        self.welcomed.set()

    async def command_load(self, connection, *args):
        if not args:
            print("Usage: /load module")
        for m in args:
            name = 'handlers.%s' % m
            try:
                mod = importlib.import_module(name)
            except Exception:
                print("Failed to load module %s" % name)
                continue
            importlib.reload(mod)
            try:
                handler_class = mod.Handler
            except AttributeError:
                print('Could not find %s.Handler' % name)
                continue
            try:
                self.subhandlers[m] = handler_class()
            except Exception:
                print('Could not initialize %s.Handler' % name)
                continue

    async def command_unload(self, connection, *args):
        if not args:
            print("Usage: /unload module")
        for m in args:
            try:
                del self.subhandlers[m]
            except KeyError:
                print('Module %s not loaded' % m)

    async def command_quit(self, connection, *args):
        try:
            await connection.quit(' '.join(args))
        except irc.client.ServerNotConnectedError:
            pass
        await connection.disconnect()
        print("command_quit done")
        assert not connection.connected

    async def command_quot(self, connection, *args):
        await connection.send_items(*args)

    async def input_command(self, connection, method, args):
        if '_' in method:
            print('Invalid method %r' % method)
            return
        method_lower = method.lower()
        try:
            fn = getattr(self, 'command_' + method_lower)
        except AttributeError:
            fn = getattr(connection, method_lower, None)
            if not inspect.iscoroutinefunction(fn):
                print('Invalid method %r' % method)
                return
        else:
            fn = functools.partial(fn, connection)
        try:
            res = await fn(*args.split())
        except Exception:
            traceback.print_exc()
        else:
            if res is not None:
                print(res)


async def handle_stdin(loop, handler, client, config):
    async for linedata in async_readlines(loop):
        try:
            line = linedata.decode()
        except UnicodeDecodeError:
            print('Could not decode %r' % (line,))
            continue
        line = line.rstrip('\r\n')
        if line.startswith('/'):
            linedata.show()
            method, sp, args = line[1:].partition(' ')
            await handler.input_command(client, method, args)
        else:
            if line == '':
                linedata.hide()
                continue
            if not config.USERNAME:
                linedata.show()
                print("Not logged in")
            elif len(config.CHANNELS) != 1:
                linedata.show()
                print("Wrong number of channels in config (%r)" %
                      len(config.CHANNELS))
            else:
                linedata.hide()
                channel = '#'+config.CHANNELS[0]
                handler.subhandlers['log'].log_sent(
                    channel, config.USERNAME, line)
                await client.privmsg(channel, line)
    await client.quit()
    await client.disconnect()


async def main_async(loop, config):
    handler = Handler()
    client = irc.client.ServerConnection(handler, loop=loop)
    if config.USERNAME:
        username = config.USERNAME
        password = config.PASSWORD
    else:
        username = config.DEFAULT_USERNAME
        password = config.DEFAULT_PASSWORD
    await client.connect(
        config.SERVER, config.PORT, username, password, caps=TWITCH_CAPS)
    await handler.welcomed.wait()
    for c in config.CHANNELS:
        await client.join('#'+c)
    task = loop.create_task(handle_stdin(loop, handler, client, config))
    try:
        await client.disconnect()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    config = read_config()
    init_logging(config)
    loop = asyncio.get_event_loop()
    main_task = loop.create_task(main_async(loop, config))
    try:
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        main_task.cancel()
        try:
            loop.run_until_complete(main_task)
        except asyncio.CancelledError:
            pass


if __name__ == '__main__':
    main()
