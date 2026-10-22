# Every model module is imported here, so the app registry (and with it
# `makemigrations`) sees all models whatever the settings module: the
# orchestrator's URLconf does not import the dashboard views, and without this
# `makemigrations` there would plan to drop their tables.
from .user import User
from .competition import ApplicationSetting, Competition, Round
from .team import AccessTokens, Team, TeamMembership
from .vm import VirtualMachine
from .scoring import FlagCapture, FlagCaptureAttempt
from .logging import Logging, LoggingAttempt
from .worms import MalwareArtifact, Upload, WormDeployment, WormDeploymentPlan
from .topology import TopologySnapshot
from .monitoring import NetworkEvent, NetworkFlow
