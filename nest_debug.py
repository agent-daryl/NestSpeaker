#!/usr/bin/env python3
"""Debug Cast v2 handshake — capture raw bytes and test minimal messages."""

import json
import socket
import ssl
import struct
import sys

from pychromecast.cast_channel_pb2 import CastMessage

SENDER_ID = "sender-42"
RECEIVER_ID = "receiver-3"

MAIN_NS = "urn:x-cast:com.google.cast.main"
CONNECTION_NS = "urn:x-cast:com.google.cast.tp.connection"

def build_msg(source_id, destination_id, namespace, payload_str):
    msg = CastMessage()
    msg.protocol_version = 0
    msg.source_id = source_id
    msg.destination_id = destination_id
    msg.namespace = namespace
    msg.payload_type = 0
    msg.payload_utf8 = payload_str
    raw = msg.SerializeToString()
    return struct.pack(">I", len(raw)) + raw

def read_msg(sock):
    sock.settimeout(3)
    try:
        hdr = sock.recv(4)
        if len(hdr) < 4:
            return None, b""
        length = struct.unpack(">I", hdr)[0]
        data = bytearray()
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return length, bytes(data)
    except socket.timeout:
        return None, b""

def parse_msg(raw):
    try:
        m = CastMessage()
        m.ParseFromString(raw)
        return m
    except:
        return None

def send_and_read(sock, label, payload_dict):
    payload = json.dumps(payload_dict)
    frame = build_msg(SENDER_ID, RECEIVER_ID, MAIN_NS, payload)
    print(f"\n>> {label}: {payload}")
    print(f"   Frame hex (first 80 bytes): {frame[:80].hex()}")
    sock.send(frame)
    length, raw = read_msg(sock)
    if raw:
        parsed = parse_msg(raw)
        if parsed:
            print(f"<< Response: ns={parsed.namespace} payload_type={parsed.payload_type}")
            print(f"   payload_utf8 = {parsed.payload_utf8}")
            print(f"   raw protobuf hex: {raw.hex()}")
        else:
            print(f"<< Parse failed, raw hex: {raw[:200].hex()}")
    else:
        print(f"<< No response (timeout)")

def main():
    ip = "10.10.100.122"
    print(f"Connecting to {ip}:8009 ...")
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(10)
    raw.connect((ip, 8009))

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    sock = ctx.wrap_socket(raw, server_hostname=ip)
    print("TLS established.")

    # Test 1: Minimal CONNECTION_REQUEST
    send_and_read(sock, "CONNECTION_REQUEST (minimal)", {
        "protocolVersion": "1.0",
        "authMethod": "NONE"
    })

    # Test 2: With sessionUuid
    send_and_read(sock, "CONNECTION_REQUEST (with sessionUuid)", {
        "protocolVersion": "1.0",
        "authMethod": "NONE",
        "sessionUuid": "test-0001"
    })

    # Test 3: Send in CONNECTION_NS instead of MAIN_NS
    payload = json.dumps({"protocolVersion": "1.0", "authMethod": "NONE"})
    frame = build_msg(SENDER_ID, RECEIVER_ID, CONNECTION_NS, payload)
    print(f"\n>> CONNECTION_REQUEST (in CONNECTION_NS): {payload}")
    sock.send(frame)
    length, raw2 = read_msg(sock)
    if raw2:
        parsed = parse_msg(raw2)
        if parsed:
            print(f"<< Response: ns={parsed.namespace}")
            print(f"   payload_utf8 = {parsed.payload_utf8}")
        else:
            print(f"<< Parse failed, raw hex: {raw2[:200].hex()}")

    # Test 4: Empty payload
    frame = build_msg(SENDER_ID, RECEIVER_ID, MAIN_NS, "")
    print(f"\n>> Empty payload in MAIN_NS")
    sock.send(frame)
    length, raw3 = read_msg(sock)
    if raw3:
        parsed = parse_msg(raw3)
        if parsed:
            print(f"<< Response: ns={parsed.namespace}")
            print(f"   payload_utf8 = {parsed.payload_utf8}")
        else:
            print(f"<< Parse failed, raw hex: {raw3[:200].hex()}")

    sock.close()
    print("\nDone.")

if __name__ == "__main__":
    main()
