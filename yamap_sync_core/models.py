from dataclasses import dataclass
from email.message import Message


@dataclass(frozen=True)
class HttpResponse:
    body: bytes
    headers: Message
    status: int
