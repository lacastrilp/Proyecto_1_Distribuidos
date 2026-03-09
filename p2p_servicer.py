import grpc
import groupsapp_pb2
import groupsapp_pb2_grpc
import sqlite3
import uuid
import time

class P2PServicer(groupsapp_pb2_grpc.MessageServiceServicer, groupsapp_pb2_grpc.PresenceServiceServicer):
    def __init__(self, username, local_db):
        self.username = username
        self.local_db = local_db  # conexión sqlite local

    def SendMessage(self, request, context):
        # Guardar mensaje localmente
        conn = sqlite3.connect(self.local_db)
        c = conn.cursor()
        msg_id = str(uuid.uuid4())
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO messages VALUES (?,?,?,?,?)",
                  (msg_id, request.group_id, request.sender, request.content, ts))
        conn.commit()
        conn.close()

        return groupsapp_pb2.MessageResponse(
            message_id=msg_id,
            message=f"✅ Mensaje recibido directamente de {request.sender}",
            timestamp=ts
        )

    def UpdatePresence(self, request_iterator, context):
        for update in request_iterator:
            print(f"\n🟢 Presencia P2P recibida: {update.username} {'online' if update.online else 'offline'}")
            yield update