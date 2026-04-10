import json
import uuid
from urllib.parse import parse_qs

import bencode2
import gevent
import gevent.socket
import gevent.timeout
import uwsgi
from gevent.queue import Queue
from gevent.server import StreamServer

BASE_PORT = 7888
projects = {}


def get_or_create_project(name):
    if name not in projects:
        port = BASE_PORT + len(projects)
        projects[name] = {
            'port': port,
            'to_browser': Queue(),
            'sessions': {},
            'active': False
            }
        spawn_nrepl_server(name, port)
    return projects[name]


def spawn_nrepl_server(name, port):
    def nrepl_handler(socket, address):
        ctx = projects[name]
        default_sid = str(uuid.uuid4())
        ctx['sessions'][default_sid] = socket

        try:
            while True:
                data = socket.recv(4096)
                if not data:
                    break
                msg = bencode2.bdecode(data)

                def get_val(k):
                    v = msg.get(k.encode() if isinstance(k, str) else k)
                    return v.decode('utf-8'
                                   ) if isinstance(v, bytes) else v

                op = get_val('op')
                mid = get_val('id')
                sid = get_val('session') or default_sid

                if op == 'clone':
                    new_sid = str(uuid.uuid4())
                    ctx['sessions'][new_sid] = socket
                    socket.sendall(
                        bencode2.bencode(
                            {
                                b"new-session": new_sid.encode(),
                                b"id": mid.encode() if mid else b"0",
                                b"status": [b"done"]
                                }
                            )
                        )
                elif op == 'eval':
                    ctx['to_browser'].put(
                        {
                            "op": "eval",
                            "code": get_val('code'),
                            "id": mid or str(uuid.uuid4()),
                            "session": sid
                            }
                        )
                else:
                    socket.sendall(
                        bencode2.bencode(
                            {
                                b"id": mid.encode() if mid else b"0",
                                b"status": [b"done"]
                                }
                            )
                        )
        except Exception:
            pass

    server = StreamServer(('127.0.0.1', port), nrepl_handler)
    gevent.spawn(server.serve_forever)
    print(f"Started nREPL Bridge for '{name}' on port {port}")


def application(env, start_response):
    query = parse_qs(env.get('QUERY_STRING', ''))
    project_name = query.get('project', ['default'])[0]
    uwsgi.websocket_handshake(
        env['HTTP_SEC_WEBSOCKET_KEY'], env.get('HTTP_ORIGIN', '')
        )

    ctx = get_or_create_project(project_name)
    fd = uwsgi.connection_fd()

    while True:
        try:
            # Wait briefly for network activity
            gevent.socket.wait_read(fd, timeout=9)
        except (gevent.timeout.Timeout, Exception) as e:
            if "timed out" not in str(e).lower():
                break

        # 1. Editor -> Browser
        while not ctx['to_browser'].empty():
            msg = ctx['to_browser'].get_nowait()
            uwsgi.websocket_send(json.dumps(msg))

        # 2. Browser -> Editor (THE FIX: DRAIN THE BUFFER!)
        # We loop until uwsgi.websocket_recv_nb() throws an exception (which means it's empty)
        while True:
            try:
                msg = uwsgi.websocket_recv_nb()
                if not msg:
                    break  # Empty means no data or connection closed

                resp = json.loads(msg)
                sid = resp.get('session')
                mid = resp.get('id')
                target = ctx['sessions'].get(sid)

                if target:
                    payload = {
                        b"id": str(mid).encode() if mid else b"0",
                        b"session": str(sid).encode() if sid else b""
                        }

                    if "value" in resp:
                        payload[b"value"] = str(resp["value"]).encode()
                    if "out" in resp:
                        payload[b"out"] = str(resp["out"]).encode()
                    if "err" in resp:
                        payload[b"err"] = str(resp["err"]).encode()
                    if "ex" in resp:
                        payload[b"ex"] = str(resp["ex"]).encode()
                    if "status" in resp:
                        payload[b"status"] = [
                            str(s).encode() for s in resp["status"]
                            ]

                    target.sendall(bencode2.bencode(payload))
            except Exception:
                # EAGAIN / EWOULDBLOCK means the non-blocking buffer is fully empty.
                # We break the inner loop and go back to waiting.
                break

    # while True:
    #     readable = False
    #     try:
    #         gevent.socket.wait_read(fd, timeout=0.1)
    #         readable = True
    #     except (gevent.timeout.Timeout, Exception) as e:
    #         if "timed out" not in str(e).lower():
    #             break

    #     # Editor -> Browser
    #     while not ctx['to_browser'].empty():
    #         msg = ctx['to_browser'].get_nowait()
    #         uwsgi.websocket_send(json.dumps(msg))

    #     # Browser -> Editor
    #     # if readable:
    #     #     try:
    #     #         msg = uwsgi.websocket_recv_nb()
    #     #         if msg:
    #     #             resp = json.loads(msg)
    #     #             sid, mid, val = resp.get('session'), resp.get(
    #     #                 'id'
    #     #                 ), resp.get('value')
    #     #             target = ctx['sessions'].get(sid)
    #     #             if target:
    #     #                 target.sendall(
    #     #                     bencode2.bencode(
    #     #                         {
    #     #                             b"id": str(mid).encode(),
    #     #                             b"session": str(sid).encode(),
    #     #                             b"value": str(val).encode(),
    #     #                             b"status": [b"done"]
    #     #                             }
    #     #                         )
    #     #                     )
    #     #         elif msg is None:
    #     #             break
    #     #     except Exception:
    #     #         break


# # Browser -> Editor
#     if readable:
#         try:
#             msg = uwsgi.websocket_recv_nb()
#             if msg:
#                 resp = json.loads(msg)
#                 sid = resp.get('session')
#                 mid = resp.get('id')
#                 target = ctx['sessions'].get(sid)

#                 if target:
#                     # 1. Start with the required base keys
#                     payload = {
#                         b"id":
#                             str(mid).encode() if mid else b"0",
#                         b"session":
#                             str(sid).encode() if sid else b""
#                         }

#                     # 2. Dynamically forward keys ONLY if they exist in the browser message
#                     if "value" in resp:
#                         payload[b"value"] = str(resp["value"]
#                                                ).encode()
#                     if "out" in resp:
#                         payload[b"out"] = str(resp["out"]).encode()
#                     if "err" in resp:
#                         payload[b"err"] = str(resp["err"]).encode()
#                     if "ex" in resp:
#                         payload[b"ex"] = str(resp["ex"]).encode()
#                     if "status" in resp:
#                         # Status is a list in nREPL (e.g., ["done", "eval-error"])
#                         payload[b"status"] = [
#                             str(s).encode() for s in resp["status"]
#                             ]

#                     # 3. Send the preserved payload to the nREPL Python client
#                     target.sendall(bencode2.bencode(payload))

#             elif msg is None:
#                 break
#         except Exception as e:
#             print(f"Bridge WebSocket Error: {e}")
#             break

    return [b""]
