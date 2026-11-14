"""
Client for nabttsd, for the website and for other services (nabRSS...).

nabttsd listens on 127.0.0.1:10544, one JSON packet per line:
  {"type": "say", "text": "...", "request_id": "...",
   "signature": "nabmastodond/communion.wav",   (optional)
   "expiration": "2026-11-14T20:00:00"}          (optional)
and answers once the sentence is synthesized and handed to nabd:
  {"type": "response", "request_id": "...", "status": "ok"}
  {"type": "response", "request_id": "...", "status": "error",
   "message": "..."}
"""

import asyncio
import json
import os
import socket
import uuid

HOST = os.getenv("NABTTSD_HOST", "127.0.0.1")
PORT = int(os.getenv("NABTTSD_PORT", "10544"))
TIMEOUT_S = 150


def _packet(text, signature=None, expiration=None):
    packet = {"type": "say", "text": text, "request_id": uuid.uuid4().hex}
    if signature:
        packet["signature"] = signature
    if expiration:
        packet["expiration"] = expiration
    return packet


def _read_response(lines, request_id):
    for line in lines:
        packet = json.loads(line)
        if packet.get("request_id") == request_id:
            return packet
    return {"status": "error", "message": "no response from nabttsd"}


async def say(text, signature=None, expiration=None, timeout=TIMEOUT_S):
    """Make the rabbit say text (asyncio services)."""
    packet = _packet(text, signature, expiration)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(HOST, PORT), 2
        )
    except (OSError, asyncio.TimeoutError):
        return {"status": "error", "message": "nabttsd is not running"}
    try:
        writer.write((json.dumps(packet) + "\r\n").encode("utf8"))
        await writer.drain()
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout)
            if not line:
                return {"status": "error", "message": "connection closed"}
            response = json.loads(line)
            if response.get("request_id") == packet["request_id"]:
                return response
    except asyncio.TimeoutError:
        return {"status": "error", "message": "nabttsd timed out"}
    finally:
        writer.close()
        await writer.wait_closed()


def say_sync(text, signature=None, expiration=None, timeout=TIMEOUT_S):
    """Make the rabbit say text (website, scripts)."""
    packet = _packet(text, signature, expiration)
    try:
        with socket.create_connection((HOST, PORT), timeout=2) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(packet) + "\r\n").encode("utf8"))
            with sock.makefile("r", encoding="utf8") as lines:
                return _read_response(lines, packet["request_id"])
    except socket.timeout:
        return {"status": "error", "message": "nabttsd timed out"}
    except OSError:
        return {"status": "error", "message": "nabttsd is not running"}
