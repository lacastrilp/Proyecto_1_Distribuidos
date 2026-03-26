import grpc
from concurrent import futures
import groupsapp_pb2
import groupsapp_pb2_grpc
import sqlite3
import uuid
import time
import pika
from queue import Queue, Empty
import jwt
from dotenv import load_dotenv
import os
import threading
import logging

# ==================== CONFIGURACIÓN ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
load_dotenv()
# Al inicio, después de load_dotenv()
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')
SECRET_KEY = os.getenv('SECRET_KEY', 'fallback_secret')



# ==================== BASE DE DATOS SEGURA (ANTI-CORRUPCIÓN) ====================
def get_db_connection():
    """Devuelve una conexión nueva por cada operación (obligatorio con hilos)"""
    conn = sqlite3.connect('groupsapp.db', check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")      # ← Evita corrupción casi al 100%
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_database():
    """Crea tablas una sola vez al iniciar"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (username TEXT PRIMARY KEY, password TEXT, online BOOLEAN)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS groups 
                      (group_id TEXT PRIMARY KEY, group_name TEXT, admin TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS group_members 
                      (group_id TEXT, username TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS messages 
                      (message_id TEXT, group_id TEXT, sender TEXT, content TEXT, timestamp TEXT)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_messages_group_id ON messages (group_id)''')
    cursor.execute('''CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages (timestamp)''')
    conn.commit()
    conn.close()
    logging.info("✅ Base de datos inicializada correctamente (modo WAL)")

# Inicializar BD al arrancar el servidor
init_database()

# ==================== RABBITMQ ====================
# ==================== RABBITMQ (VERSIÓN SEGURA CON .env) ====================
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')
rabbit_conn = None
rabbit_channel = None
rabbit_lock = threading.Lock()

def get_persistent_publisher():
    global rabbit_conn, rabbit_channel
    with rabbit_lock:
        if rabbit_conn is None or rabbit_conn.is_closed:
            credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
            params = pika.ConnectionParameters(
                host=RABBITMQ_HOST,
                credentials=credentials,
                heartbeat=600,
                blocked_connection_timeout=300
            )
            rabbit_conn = pika.BlockingConnection(params)
            rabbit_channel = rabbit_conn.channel()
            logging.info("🐰 RabbitMQ publisher persistente iniciado")
        return rabbit_channel
    
# ==================== INTERCEPTOR ====================
class AuthInterceptor(grpc.ServerInterceptor):
    def intercept_service(self, continuation, handler_call_details):
        method = handler_call_details.method
        if method in ['/groupsapp.AuthService/Register', '/groupsapp.AuthService/Login']:
            return continuation(handler_call_details)

        metadata = dict(handler_call_details.invocation_metadata)
        if 'token' in metadata:
            try:
                jwt.decode(metadata['token'], SECRET_KEY or 'fallback_secret', algorithms=['HS256'])
                return continuation(handler_call_details)
            except jwt.InvalidTokenError:
                def abort_handler(request, context):
                    context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid token")
                return grpc.unary_unary_rpc_method_handler(abort_handler)
        else:
            def abort_handler(request, context):
                context.abort(grpc.StatusCode.UNAUTHENTICATED, "No token")
            return grpc.unary_unary_rpc_method_handler(abort_handler)

# ==================== SERVICERS ====================
class AuthServicer(groupsapp_pb2_grpc.AuthServiceServicer):
    def Register(self, request, context):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (username, password, online) VALUES (?, ?, ?)",
                           (request.username, request.password, False))
            conn.commit()
            token = jwt.encode({'username': request.username}, SECRET_KEY or 'fallback_secret', algorithm='HS256')
            conn.close()
            return groupsapp_pb2.AuthResponse(token=token, message="Registered")
        except sqlite3.IntegrityError:
            if 'conn' in locals(): conn.close()
            context.set_details('Username exists')
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            return groupsapp_pb2.AuthResponse()
        except sqlite3.Error as e:
            if 'conn' in locals(): conn.close()
            context.set_details(f"Database error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return groupsapp_pb2.AuthResponse()

    def Login(self, request, context):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT password FROM users WHERE username=?", (request.username,))
            row = cursor.fetchone()
            if row and row[0] == request.password:
                cursor.execute("UPDATE users SET online=1 WHERE username=?", (request.username,))
                conn.commit()
                token = jwt.encode({'username': request.username}, SECRET_KEY or 'fallback_secret', algorithm='HS256')
                conn.close()
                return groupsapp_pb2.AuthResponse(token=token, message="Logged in")
            else:
                conn.close()
                context.set_details('Invalid credentials')
                context.set_code(grpc.StatusCode.UNAUTHENTICATED)
                return groupsapp_pb2.AuthResponse()
        except sqlite3.Error as e:
            if 'conn' in locals(): conn.close()
            context.set_details(f"Database error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return groupsapp_pb2.AuthResponse()

class GroupServicer(groupsapp_pb2_grpc.GroupServiceServicer):
    def CreateGroup(self, request, context):
        group_id = str(uuid.uuid4())
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO groups (group_id, group_name, admin) VALUES (?, ?, ?)",
                           (group_id, request.group_name, request.admin_username))
            cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)",
                           (group_id, request.admin_username))
            conn.commit()
            conn.close()
            return groupsapp_pb2.GroupResponse(group_id=group_id, message="Group created")
        except sqlite3.Error as e:
            if 'conn' in locals(): conn.close()
            context.set_details(f"Database error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return groupsapp_pb2.GroupResponse()

    def JoinGroup(self, request, context):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)",
                           (request.group_id, request.username))
            conn.commit()
            conn.close()
            return groupsapp_pb2.GroupResponse(group_id=request.group_id, message="Joined group")
        except sqlite3.Error as e:
            if 'conn' in locals(): conn.close()
            context.set_details(f"Database error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return groupsapp_pb2.GroupResponse()

    def ListMyGroups(self, request, context):
        metadata = dict(context.invocation_metadata())
        token = metadata.get('token')
        if not token:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Token requerido")
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            username = payload['username']
        except:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Token inválido")

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT g.group_id, g.group_name 
                FROM groups g 
                JOIN group_members gm ON g.group_id = gm.group_id 
                WHERE gm.username = ?
            """, (username,))
            groups = [groupsapp_pb2.GroupInfo(group_id=row[0], group_name=row[1])
                      for row in cursor.fetchall()]
            conn.close()
            return groupsapp_pb2.ListMyGroupsResponse(groups=groups)
        except Exception as e:
            if 'conn' in locals(): conn.close()
            context.abort(grpc.StatusCode.INTERNAL, f"Error: {str(e)}")

class MessageServicer(groupsapp_pb2_grpc.MessageServiceServicer):
    def SendMessage(self, request, context):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            # Validación miembro
            cursor.execute("SELECT 1 FROM group_members WHERE group_id=? AND username=?",
                           (request.group_id, request.sender))
            if not cursor.fetchone():
                conn.close()
                context.abort(grpc.StatusCode.PERMISSION_DENIED, "No eres miembro")
                return groupsapp_pb2.MessageResponse()

            # Guardar mensaje
            message_id = str(uuid.uuid4())
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("INSERT INTO messages VALUES (?,?,?,?,?)",
                           (message_id, request.group_id, request.sender, request.content, timestamp))
            conn.commit()
            conn.close()

            # RabbitMQ
            # ==================== RABBITMQ (CORREGIDO) ====================
            # RabbitMQ - PERSISTENTE (latencia mínima)
            try:
                channel = get_persistent_publisher()
                queue_name = f"group_{request.group_id}_queue"
                channel.queue_declare(queue=queue_name, durable=True)
                
                body = f"📨 {request.sender}: {request.content}"
                channel.basic_publish(
                    exchange='',
                    routing_key=queue_name,
                    body=body.encode('utf-8'),
                    properties=pika.BasicProperties(delivery_mode=2)
                )
                logging.info(f"📤 [PERSISTENTE] Mensaje enviado a {queue_name}")
            except Exception as rmq_err:
                logging.error(f"❌ RabbitMQ (mensaje ya guardado en BD): {rmq_err}")
            
            return groupsapp_pb2.MessageResponse(message_id=message_id, message="✅ Enviado", timestamp=timestamp)
        except Exception as e:
            if 'conn' in locals(): conn.close()
            context.abort(grpc.StatusCode.INTERNAL, str(e))

    def GetMessages(self, request, context):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT message_id, sender, content, timestamp FROM messages WHERE group_id=? ORDER BY timestamp",
                           (request.group_id,))
            messages = []
            for row in cursor.fetchall():
                msg = groupsapp_pb2.MessageResponse(
                    message_id=row[0],
                    message=f"{row[1]} ({row[3]}): {row[2]}"
                )
                messages.append(msg)
            conn.close()
            return groupsapp_pb2.MessagesResponse(messages=messages)
        except sqlite3.Error as e:
            if 'conn' in locals(): conn.close()
            context.set_details(f"Database error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return groupsapp_pb2.MessagesResponse()

presence_queues = {}

class PresenceServicer(groupsapp_pb2_grpc.PresenceServiceServicer):
    def UpdatePresence(self, request_iterator, context):
        client_id = str(uuid.uuid4())
        presence_queues[client_id] = Queue()

        def broadcast_updates():
            while client_id in presence_queues:
                try:
                    update = presence_queues[client_id].get(timeout=1)
                    yield update
                except Empty:
                    continue

        threading.Thread(target=lambda: list(broadcast_updates()), daemon=True).start()

        try:
            for update in request_iterator:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET online=? WHERE username=?", (update.online, update.username))
                conn.commit()
                conn.close()
                logging.info(f"📡 Presencia actualizada: {update.username} online={update.online}")

                for q_id, q in list(presence_queues.items()):
                    if q_id != client_id:
                        q.put(update)
        except grpc.RpcError:
            logging.info("Client disconnected from presence stream.")
        finally:
            if client_id in presence_queues:
                del presence_queues[client_id]
            logging.info(f"Stream de presencia para {client_id} cerrado.")

# ==================== DISCOVERY SERVICE ====================
peer_registry = {}

class DiscoveryServicer(groupsapp_pb2_grpc.DiscoveryServiceServicer):
    def RegisterP2P(self, request, context):
        peer_registry[request.username] = (request.ip, request.p2p_port)
        return groupsapp_pb2.RegisterP2PResponse(success=True, message="P2P registrado")

    def GetGroupOnlinePeers(self, request, context):
        """AHORA FILTRA POR GRUPO + SOLO USUARIOS ONLINE"""
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT gm.username 
                FROM group_members gm 
                JOIN users u ON gm.username = u.username 
                WHERE gm.group_id = ? AND u.online = 1
            """, (request.group_id,))
            group_usernames = [row[0] for row in cursor.fetchall()]
            conn.close()

            peers = []
            for uname in group_usernames:
                if uname in peer_registry:
                    ip, port = peer_registry[uname]
                    peers.append(groupsapp_pb2.PeerInfo(
                        username=uname, ip=ip, p2p_port=port
                    ))
            return groupsapp_pb2.GetGroupPeersResponse(peers=peers)
        except Exception as e:
            context.abort(grpc.StatusCode.INTERNAL, f"Error discovery: {str(e)}")
            return groupsapp_pb2.GetGroupPeersResponse()
        
# ==================== INICIO DEL SERVIDOR ====================
def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20), interceptors=[AuthInterceptor()])
    groupsapp_pb2_grpc.add_AuthServiceServicer_to_server(AuthServicer(), server)
    groupsapp_pb2_grpc.add_GroupServiceServicer_to_server(GroupServicer(), server)
    groupsapp_pb2_grpc.add_MessageServiceServicer_to_server(MessageServicer(), server)
    groupsapp_pb2_grpc.add_DiscoveryServiceServicer_to_server(DiscoveryServicer(), server)
    groupsapp_pb2_grpc.add_PresenceServiceServicer_to_server(PresenceServicer(), server)

    server.add_insecure_port('[::]:50051')
    logging.info("🚀 Servidor corriendo en :50051 (DB en modo WAL)")
    server.start()
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logging.info("Cerrando el servidor gRPC.")
        server.stop(0)
    finally:
        logging.info("Servidor gRPC detenido.")

if __name__ == '__main__':
    serve()