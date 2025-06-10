import os

from openhands.server.config.server_config import ServerConfig
from openhands.server.types import AppMode


class OpenhandsSaasConfig(ServerConfig):
    config_cls = os.environ.get('OPENHANDS_CONFIG_CLS', '')
    app_mode = AppMode.SAAS
    storage_type = 'database'
    event_stream_class = os.environ.get(
        'EVENT_STREAM_CLASS',
        'events.saas_event_stream.SaasEventStream'
    )
    # posthog_client_key = os.environ.get('POSTHOG_CLIENT_KEY', '')
    conversation_store_class = 'server.saas_conversation_store.SaasConversationStore'
    conversation_manager_class: str = 'server.clustered_conversation_manager.ClusteredConversationManager'


    def verify_config(self):
        if not self.config_cls:
            raise ValueError('Config path not provided!')

        if not self.posthog_client_key:
            raise ValueError('Missing posthog client key in env')



