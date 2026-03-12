import grpc
import groupsapp_pb2
import groupsapp_pb2_grpc
import time
import socket
import uuid
from concurrent import futures
import sqlite3
import requests
import pika
import threading
import os
from dotenv import load_dotenv

load_dotenv()
SIGNALING_ADDR = 'localhost:50051'
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')

# ==================== RABBITMQ (para chat en vivo) ====================
def get_rabbit_connection_and_channel():
    credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
    parameters = pika.ConnectionParameters(
        host='localhost',
        credentials=credentials,
        heartbeat=600,
        blocked_connection_timeout=300
    )
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    return connection, channel

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
        self.groups = {}
        self.p2p_port = 0
        self.local_db = None
        self.channel = None
        self.p2p_server = None
        self.rabbit_threads = {}          # group_id → thread

    def init_local_db(self):
        if not self.local_db: return
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
        print(f"🔌 P2P local escuchando en puerto {self.p2p_port}")
        return server

    def start_message_listener(self, group_id):
        """CHAT EN VIVO: listener RabbitMQ en segundo plano"""
        if group_id in self.rabbit_threads:
            return

        queue_name = f"group_{group_id}_queue"

        def listener():
            try:
                connection, channel = get_rabbit_connection_and_channel()
                channel.queue_declare(queue=queue_name, durable=True)

                def callback(ch, method, properties, body):
                    print(f"\n\n📨 [EN VIVO] {body.decode('utf-8')}")

                channel.basic_consume(queue=queue_name, on_message_callback=callback, auto_ack=True)
                channel.start_consuming()
            except Exception as e:
                print(f"❌ Listener {group_id} cerrado: {e}")

        thread = threading.Thread(target=listener, daemon=True)
        thread.start()
        self.rabbit_threads[group_id] = thread
        print(f"👂 Activado chat en vivo para grupo {group_id}")

    def load_groups_and_start_listeners(self, group_stub):
        try:
            resp = group_stub.ListMyGroups(
                groupsapp_pb2.ListMyGroupsRequest(),
                metadata=[('token', self.token)]
            )
            self.groups = {g.group_name: g.group_id for g in resp.groups}
            for g in resp.groups:
                self.start_message_listener(g.group_id)
        except:
            pass

    def send_message_p2p(self, group_id, content, discovery_stub, message_stub):
        """Envío híbrido: P2P directo + siempre central (live + historial)"""
        # 1. Obtener peers del grupo
        try:
            peers = discovery_stub.GetGroupOnlinePeers(
                groupsapp_pb2.GetGroupPeersRequest(group_id=group_id),
                metadata=[('token', self.token)]
            ).peers
        except:
            peers = []

        sent_direct = False
        for peer in peers:
            if peer.username == self.username: continue
            try:
                ch = grpc.insecure_channel(f"{peer.ip}:{peer.p2p_port}")
                stub = groupsapp_pb2_grpc.MessageServiceStub(ch)
                stub.SendMessage(groupsapp_pb2.SendMessageRequest(
                    group_id=group_id, sender=self.username, content=content))
                print(f"📤 Enviado DIRECTO a {peer.username} ✅")
                sent_direct = True
                ch.close()
            except:
                continue

        # 2. Siempre al servidor central (garantiza DB + RabbitMQ live)
        try:
            response = message_stub.SendMessage(
                groupsapp_pb2.SendMessageRequest(group_id=group_id, sender=self.username, content=content),
                metadata=[('token', self.token)]
            )
            print(response.message)
            if sent_direct:
                print("   (+ también enviado directo a peers online)")
        except grpc.RpcError as e:
            print(f"❌ Error central: {e.details()}")

    # ... (show_history y el resto del menú sin cambios)

    def show_history(self, group_id):
        try:
            msg_stub = groupsapp_pb2_grpc.MessageServiceStub(self.channel)
            resp = msg_stub.GetMessages(
                groupsapp_pb2.GetMessagesRequest(group_id=group_id),
                metadata=[('token', self.token)]
            )
            print(f"\n📜 HISTORIAL DEL GRUPO:")
            if not resp.messages:
                print("   (Aún no hay mensajes)")
            for msg in resp.messages:
                print(f"   {msg.message}")
        except Exception as e:
            print(f"❌ Error al cargar historial: {e}")

    def run(self):
        self.channel = grpc.insecure_channel(SIGNALING_ADDR)
        auth = groupsapp_pb2_grpc.AuthServiceStub(self.channel)
        group_stub = groupsapp_pb2_grpc.GroupServiceStub(self.channel)
        msg_stub = groupsapp_pb2_grpc.MessageServiceStub(self.channel)
        disc = groupsapp_pb2_grpc.DiscoveryServiceStub(self.channel)

        while True:
            print("\n" + "═"*70)
            print("📱 GROUPSAPP - Chat como WhatsApp (¡EN VIVO!)")
            if not self.token:
                print("1. Registrarse     2. Iniciar sesión     3. Salir")
            else:
                print(f"👤 {self.username} | BD: {self.local_db or 'No iniciada'}")
                print("1. Crear grupo     2. Ver mis chats     3. Enviar mensaje")
                print("4. Unirme a grupo  5. Ver historial      6. Cerrar sesión")
            print("═"*70)

            choice = input("➤ ").strip().lower()

            # ==================== REGISTRO / LOGIN ====================
            if choice in ['1', 'registrarse', 'registro'] and not self.token:
                # ... (código de registro igual)
                self.username = input("Usuario: ").strip().lower()
                pwd = input("Contraseña: ")
                try:
                    resp = auth.Register(groupsapp_pb2.RegisterRequest(username=self.username, password=pwd))
                    if resp.token:
                        self.token = resp.token
                        print("✅ Registrado y sesión iniciada")
                        self.local_db = f"peer_{self.username}.db"
                        self.init_local_db()
                        self.p2p_server = self.start_local_p2p_server()

                        # === IP PÚBLICA ===
                        try:
                            public_ip = requests.get('https://api.ipify.org', timeout=5).text.strip()
                            print(f"🌍 IP pública detectada: {public_ip}")
                        except:
                            public_ip = socket.gethostbyname(socket.gethostname())
                            print(f"🏠 IP local (LAN): {public_ip}")
                        
                        disc.RegisterP2P(groupsapp_pb2.RegisterP2PRequest(
                            username=self.username, p2p_port=self.p2p_port, ip=public_ip),
                            metadata=[('token', self.token)])
                        
                        self.load_groups_and_start_listeners(group_stub)
                    else:
                        print("❌ Error al registrar")
                except grpc.RpcError as e:
                    print(f"❌ Error: {e.details()}")

            elif choice in ['2', 'iniciar sesión', 'login', 'sesion'] and not self.token:
                # ... (código de login igual, solo cambia la parte final)
                self.username = input("Usuario: ").strip().lower()
                pwd = input("Contraseña: ")
                try:
                    resp = auth.Login(groupsapp_pb2.LoginRequest(username=self.username, password=pwd))
                    if resp.token:
                        self.token = resp.token
                        print("✅ Sesión iniciada")
                        self.local_db = f"peer_{self.username}.db"
                        self.init_local_db()
                        self.p2p_server = self.start_local_p2p_server()

                        # === IP PÚBLICA ===
                        try:
                            public_ip = requests.get('https://api.ipify.org', timeout=5).text.strip()
                            print(f"🌍 IP pública detectada: {public_ip}")
                        except:
                            public_ip = socket.gethostbyname(socket.gethostname())
                            print(f"🏠 IP local (LAN): {public_ip}")
                        
                        disc.RegisterP2P(groupsapp_pb2.RegisterP2PRequest(
                            username=self.username, p2p_port=self.p2p_port, ip=public_ip),
                            metadata=[('token', self.token)])
                        
                        self.load_groups_and_start_listeners(group_stub)
                    else:
                        print("❌ Credenciales incorrectas")
                except grpc.RpcError as e:
                    print(f"❌ Error: {e.details()}")

            # ==================== OPCIONES LOGUEADO ====================
            elif choice in ['1', 'crear grupo'] and self.token:
                name = input("Nombre del grupo: ")
                resp = group_stub.CreateGroup(
                    groupsapp_pb2.CreateGroupRequest(group_name=name, admin_username=self.username),
                    metadata=[('token', self.token)])
                print(f"✅ Grupo '{name}' creado | ID: {resp.group_id}")

            elif choice in ['2', 'ver mis chats', 'chats'] and self.token:
                self.load_groups_and_start_listeners(group_stub)  # refresca
                print("\n📋 TUS CHATS:")
                if not self.groups:
                    print("   (Aún no tienes grupos)")
                for i, name in enumerate(self.groups.keys(), 1):
                    print(f"   {i}. {name}")

            elif choice in ['3', 'enviar mensaje', 'mensaje'] and self.token:
                if not self.groups:
                    print("Primero ve a opción 2")
                    continue
                print("Tus chats:")
                for i, name in enumerate(self.groups.keys(), 1):
                    print(f"   {i}. {name}")
                sel = input("\nElige número o nombre: ").strip()
                group_id = self.groups.get(sel) or (list(self.groups.values())[int(sel)-1] if sel.isdigit() and int(sel) <= len(self.groups) else None)
                if not group_id:
                    print("❌ Grupo no encontrado")
                    continue
                msg = input("Mensaje: ")
                self.send_message_p2p(group_id, msg, disc, msg_stub)

            elif choice in ['4', 'unirme a grupo', 'unir'] and self.token:
                group_id = input("Ingresa el ID del grupo: ").strip()
                resp = group_stub.JoinGroup(
                    groupsapp_pb2.JoinGroupRequest(group_id=group_id, username=self.username),
                    metadata=[('token', self.token)])
                print(f"✅ {resp.message}")
                self.start_message_listener(group_id)   # ← chat en vivo inmediato

            elif choice in ['5', 'ver historial', 'historial'] and self.token:
                if not self.groups:
                    print("No tienes chats aún")
                    continue
                print("Tus chats:")
                for i, name in enumerate(self.groups.keys(), 1):
                    print(f"   {i}. {name}")
                sel = input("\nElige número o nombre: ").strip()
                group_id = self.groups.get(sel) or (list(self.groups.values())[int(sel)-1] if sel.isdigit() else None)
                if group_id:
                    self.show_history(group_id)

            elif choice in ['6', 'salir', 'cerrar sesión']:
                if self.p2p_server: self.p2p_server.stop(0)
                print("👋 ¡Hasta luego!")
                break

            else:
                if choice:
                    print("❌ Opción no válida.")

if __name__ == "__main__":
    P2PClient().run()