from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SessionState(str, Enum):
    PREPARE = "Prepare"
    PICK_UP = "PickUp"
    ON_SERVICE = "On_Service"
    PRE_HANG_UP = "PRE_HangUP"
    HANG_UP = "HangUp"


@dataclass
class UserCurrent:
    status: int = 0
    text: str = ""
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    speech_time: int = 0


@dataclass
class RobotCurrent:
    status: int = 0
    text: str = ""
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    speech_time: Optional[int] = None
    reply_id: Optional[str] = None


@dataclass
class Session:
    session_id: str
    state: SessionState
    current_time: int = 0
    user: UserCurrent = field(default_factory=UserCurrent)
    robot: RobotCurrent = field(default_factory=RobotCurrent)
    grade: int = 7
    tts_speed: float = 1.0
    chapter_id: str = ""

