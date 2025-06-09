from dotenv import load_dotenv
load_dotenv()

import socketio
from openhands.server.app import app as base_app
from openhands.server.listen_socket import sio

@base_app.get('/saas')
def is_saas():
    return {'saas': True}

app = socketio.ASGIApp(sio, other_asgi_app=base_app)

