#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# stream.py
# Copyright (C) 2021 KunoiSayami
#
# This module is part of raspberry-pi-camera-stream and is released under
# the AGPL v3 License: https://www.gnu.org/licenses/agpl-3.0.txt
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations
import asyncio
import io
import picamera
import logging
from threading import Condition
from aiohttp import web
import aiohttp
import weakref
import concurrent.futures

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)


# http://picamera.readthedocs.io/en/latest/recipes2.html#web-streaming
class BufferWriter:
    def __init__(self, camera: picamera.PiCamera):
        self.buffer = io.BytesIO()
        self.frame = None
        self.camera = camera
        self.condition = Condition()

    def write(self, buf):
        if buf.startswith(b'\xff\xd8'):
            # New frame, copy the existing buffer's content and notify all
            # clients it's available
            self.buffer.truncate()
            with self.condition:
                self.frame = self.buffer.getvalue()
                self.condition.notify_all()
            self.buffer.seek(0)
        return self.buffer.write(buf)

    @classmethod
    def new(cls) -> BufferWriter:
        camera = picamera.PiCamera(resolution='640x480', framerate=24)
        self = cls(camera)
        camera.start_recording(self, format='mjpeg')
        return self

    def close(self) -> None:
        self.camera.stop_recording()


class WsCoroutine:
    def __init__(self, ws: web.WebSocketResponse, buffer_writer: BufferWriter):
        self.ws = ws
        self.stop_event = asyncio.Event()
        self.writer = buffer_writer

    async def runnable(self) -> None:
        while True:
            with self.writer.condition:
                self.writer.condition.wait()
                await self.ws.send_bytes(self.writer.frame)
            if self.stop_event.is_set():
                return
            await asyncio.sleep(0.5)

    def req_stop(self):
        logger.debug('Request stop')
        self.stop_event.set()


class Server:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.camera = None
        self.camera_lock = asyncio.Lock()
        self.website = web.Application()
        self.bind = host
        self.port = port
        self.site = None
        self._fetched = False
        self.runner = web.AppRunner(self.website)
        self.website['websockets'] = weakref.WeakSet()
        self._idled = False

    async def enable_camera(self) -> web.Response:
        async with self.camera_lock:
            if self.camera is not None:
                return web.json_response(dict(status=400, body="camera already enabled"))
            self.camera = BufferWriter.new()
            return web.json_response(dict(status=200, body="OK"))

    async def disable_camera(self) -> web.Response:
        async with self.camera_lock:
            if self.camera is None:
                return web.json_response(dict(status=400, body="camera not enabled"))
            self.camera.close()
            self.camera = None
            return web.json_response(dict(status=200, body="OK"))

    async def query_camera(self) -> web.Response:
        return web.json_response(dict(status=200, body="enabled" if self.camera is None else "disabled"))

    async def start(self) -> None:
        self.website.router.add_get('/enable', self.enable_camera)
        self.website.router.add_get('/disable', self.disable_camera)
        self.website.router.add_get('/query', self.query_camera)
        self.website.router.add_get('/data', self.handle_websocket)
        self.website.on_shutdown.append(self.handle_web_shutdown)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.bind, self.port)
        await self.site.start()
        logger.info('Listen websocket on ws%s://%s:%d%s', self.bind, self.port)

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        logger.info('Accept websocket from %s', request.headers.get('X-Real-IP', request.remote))

        await ws.prepare(request)
        request.app['websockets'].add(ws)
        wsc = WsCoroutine(ws, self.camera)
        future = asyncio.run_coroutine_threadsafe(wsc.runnable(), asyncio.get_event_loop())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    if msg.data == 'close':
                        await ws.close()
                        break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.exception('ws connection closed with exception', ws.exception())
                    break
        finally:
            wsc.req_stop()
            request.app['websockets'].discard(ws)
            try:
                future.exception(timeout=1)
            except concurrent.futures.TimeoutError:
                pass
            except:
                logger.exception('Got exception while process coroutine')
        logger.info('websocket connection closed')
        return ws

    @staticmethod
    async def handle_web_shutdown(app: web.Application) -> None:
        for ws in set(app['websockets']):
            await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message='Server shutdown')


