from dotenv import load_dotenv
load_dotenv()

import socketio
from openhands.server.app import app as base_app
from openhands.server.listen_socket import sio
from server.routes.auth import auth_router

@base_app.get('/saas')
def is_saas():
    return {'saas': True}

# Include auth router to provide authentication endpoints
base_app.include_router(auth_router)

app = socketio.ASGIApp(sio, other_asgi_app=base_app)

