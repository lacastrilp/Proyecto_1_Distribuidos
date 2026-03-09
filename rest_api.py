from flask import Flask, request, jsonify
import groupsapp_pb2_grpc
import groupsapp_pb2
import grpc

app = Flask(__name__)

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Faltan el nombre de usuario o la contraseña'}), 400

    channel = None
    try:
        channel = grpc.insecure_channel('localhost:50051')
        stub = groupsapp_pb2_grpc.AuthServiceStub(channel)
        response = stub.Login(groupsapp_pb2.LoginRequest(username=data['username'], password=data['password']))
        return jsonify({'token': response.token, 'message': response.message}), 200
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAUTHENTICATED:
            return jsonify({'error': e.details() or 'Credenciales inválidas'}), 401
        return jsonify({'error': f'Error de gRPC: {e.details()}'}), 500
    except Exception as e:
        return jsonify({'error': f'Ocurrió un error inesperado: {str(e)}'}), 500
    finally:
        if channel:
            channel.close()

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': 'Faltan el nombre de usuario o la contraseña'}), 400

    channel = None
    try:
        channel = grpc.insecure_channel('localhost:50051')
        stub = groupsapp_pb2_grpc.AuthServiceStub(channel)
        response = stub.Register(groupsapp_pb2.RegisterRequest(username=data.get('username'), password=data.get('password')))
        return jsonify({'token': response.token, 'message': response.message}), 201
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.ALREADY_EXISTS:
            return jsonify({'error': e.details() or 'El usuario ya existe'}), 409
        return jsonify({'error': f'Error de gRPC: {e.details()}'}), 500
    except Exception as e:
        return jsonify({'error': f'Ocurrió un error inesperado: {str(e)}'}), 500
    finally:
        if channel:
            channel.close()

if __name__ == '__main__':
    app.run(port=5000, debug=True)