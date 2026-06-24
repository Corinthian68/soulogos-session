import sys
from unittest.mock import MagicMock

# discord-ext-voice-recv may not be installed in the test environment.
# Stub it before any test imports so that `import soulogos_session.bot` succeeds.
sys.modules.setdefault("discord.ext.voice_recv", MagicMock())
