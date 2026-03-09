import grpc
import groupsapp_pb2
import groupsapp_pb2_grpc
import time
import socket
import uuid
from concurrent import futures
import sqlite3

SIGNALING_ADDR = 'localhost:50051'

# ==================== SERVIDOR P2P LOCAL ====================
class P2PServicer(groupsapp_pb2_grpc.MessageServiceServicer):
    def __init__(self, username, local_db):
        self.username = username
        self.local_db = local_db

    def SendMessage(self, request, context):
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
            message=f"✅ Recibido directo de {request.sender}",
            timestamp=ts
        )


class P2PClient:
    def __init__(self):
        self.token = ""
        self.username = ""
        self.groups = {}          # nombre → id
        self.p2p_port = 0
        self.local_db = f"peer_{uuid.uuid4().hex[:8]}.db"
        self.channel = None
        self.p2p_server = None

    def init_local_db(self):
        conn = sqlite3.connect(self.local_db)
        conn.execute('''CREATE TABLE IF NOT EXISTS messages 
                        (message_id TEXT, group_id TEXT, sender TEXT, content TEXT, timestamp TEXT)''')
        conn.commit()
        conn.close()

    def start_local_p2p_server(self):
        self.p2p_port = 50052 + int(time.time()) % 1000
        server = grpc.server(futures.ThreadPoolExecutor(10))
        groupsapp_pb2_grpc.add_MessageServiceServicer_to_server(
            P2PServicer(self.username, self.local_db), server)
        server.add_insecure_port(f'[::]:{self.p2p_port}')
        server.start()
        print(f"🔌 P2P local en puerto {self.p2p_port}")
        return server

    def send_message_p2p(self, group_id, content, discovery_stub, message_stub):
        try:
            peers = discovery_stub.GetGroupOnlinePeers(
                groupsapp_pb2.GetGroupPeersRequest(group_id=group_id),
                metadata=[('token', self.token)]
            ).peers
        except:
            peers = []

        for peer in peers:
            if peer.username == self.username: continue
            try:
                ch = grpc.insecure_channel(f"{peer.ip}:{peer.p2p_port}")
                stub = groupsapp_pb2_grpc.MessageServiceStub(ch)
                resp = stub.SendMessage(groupsapp_pb2.SendMessageRequest(
                    group_id=group_id, sender=self.username, content=content))
                print(f"📤 Enviado DIRECTO a {peer.username} ✅")
                ch.close()
                return
            except:
                continue

        print("⚠️ No hay usuarios online en el grupo → usando servidor central")
        response = message_stub.SendMessage(
            groupsapp_pb2.SendMessageRequest(group_id=group_id, sender=self.username, content=content),
            metadata=[('token', self.token)]
        )
        print(response.message)

    def run(self):
        self.init_local_db()
        self.channel = grpc.insecure_channel(SIGNALING_ADDR)
        auth = groupsapp_pb2_grpc.AuthServiceStub(self.channel)
        group_stub = groupsapp_pb2_grpc.GroupServiceStub(self.channel)
        msg_stub = groupsapp_pb2_grpc.MessageServiceStub(self.channel)
        disc = groupsapp_pb2_grpc.DiscoveryServiceStub(self.channel)

        while True:
            print("\n" + "═"*70)
            print("📱 GROUPSAPP - Chat como WhatsApp")
            if not self.token:
                print("1. Registrarse   2. Iniciar sesión   3. Salir")
            else:
                print(f"👤 {self.username} | P2P: {'✅' if self.p2p_server else '❌'}")
                print("1. Crear grupo   2. Ver mis chats   3. Enviar mensaje")
                print("4. Unirme a grupo   5. Ver historial   6. Cerrar sesión")
            print("═"*70)

            choice = input("➤ ").strip()

            if choice == '2' and not self.token:
                self.username = input("Usuario: ")
                pwd = input("Contraseña: ")
                resp = auth.Login(groupsapp_pb2.LoginRequest(username=self.username, password=pwd))
                if resp.token:
                    self.token = resp.token
                    print("✅ Sesión iniciada")
                    self.p2p_server = self.start_local_p2p_server()
                    ip = socket.gethostbyname(socket.gethostname())
                    disc.RegisterP2P(groupsapp_pb2.RegisterP2PRequest(
                        username=self.username, p2p_port=self.p2p_port, ip=ip),
                        metadata=[('token', self.token)])
                else:
                    print("❌ Credenciales incorrectas")

            elif choice == '1' and self.token:  # Crear grupo
                name = input("Nombre del grupo: ")
                resp = group_stub.CreateGroup(
                    groupsapp_pb2.CreateGroupRequest(group_name=name, admin_username=self.username),
                    metadata=[('token', self.token)])
                print(f"✅ Grupo '{name}' creado")
                print(f"🔗 Comparte este ID para que otros se unan: {resp.group_id}")

            elif choice == '2' and self.token:  # Ver mis chats
                resp = group_stub.ListMyGroups(groupsapp_pb2.ListMyGroupsRequest(),
                                               metadata=[('token', self.token)])
                self.groups = {g.group_name: g.group_id for g in resp.groups}
                print("\n📋 TUS CHATS:")
                if not self.groups:
                    print("   (Aún no tienes grupos)")
                for i, name in enumerate(self.groups.keys(), 1):
                    print(f"   {i}. {name}")
                print()

            elif choice == '3' and self.token:  # Enviar mensaje
                if not self.groups:
                    print("Primero ve a opción 2 (Ver mis chats)")
                    continue
                print("Tus chats:")
                for i, name in enumerate(self.groups.keys(), 1):
                    print(f"   {i}. {name}")
                sel = input("\nElige número o nombre: ").strip()
                group_id = self.groups.get(sel) or (list(self.groups.values())[int(sel)-1] if sel.isdigit() else None)
                if not group_id:
                    print("❌ Grupo no encontrado")
                    continue
                msg = input("Mensaje: ")
                self.send_message_p2p(group_id, msg, disc, msg_stub)

            elif choice == '4' and self.token:  # Unirme a grupo
                group_id = input("Ingresa el ID del grupo: ").strip()
                resp = group_stub.JoinGroup(
                    groupsapp_pb2.JoinGroupRequest(group_id=group_id, username=self.username),
                    metadata=[('token', self.token)])
                print(f"✅ {resp.message}")
                # Refrescar lista automáticamente
                print("Actualizando lista de chats...")
                # (puedes llamar la opción 2 aquí si quieres)

            elif choice == '5' and self.token:  # Ver historial
                if not self.groups:
                    print("No tienes chats aún")
                    continue
                # ... (puedes expandir más tarde)
                print("Historial (próximamente)")

            elif choice in ('6', 'salir'):
                if self.p2p_server: self.p2p_server.stop(0)
                print("👋 ¡Hasta luego!")
                break


if __name__ == "__main__":
    P2PClient().run()