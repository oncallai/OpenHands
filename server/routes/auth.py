from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from openhands.core.logger import openhands_logger as logger

# Create auth router without dependencies to avoid conflicts
auth_router = APIRouter(prefix='/api')

# Debug logging to confirm router is loaded
logger.info('Auth router initialized - creating /api/authenticate endpoint (no dependencies)')


@auth_router.post('/authenticate')
async def authenticate(request: Request):
    # TEMPORARY: Bypass authentication for development
    # TODO: Remove this bypass when proper authentication is needed
    logger.info('Authentication endpoint /api/authenticate called - bypassed for development - returning success')
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={'message': 'User authenticated (development bypass)'}
    )
