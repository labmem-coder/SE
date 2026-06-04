"""把 ORM 对象组装成对外可见的视图（QueueInfo 等）。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from .models import ChargingPile, ChargingRequest
from .schemas import QueueInfo
from .scheduler import (
    estimate_wait_minutes,
    pile_queue_position,
    waiting_queue_position,
)


def build_queue_info(db: Session, req: ChargingRequest) -> QueueInfo:
    pile_code: Optional[str] = None
    if req.assigned_pile_id is not None:
        pile = db.get(ChargingPile, req.assigned_pile_id)
        if pile:
            pile_code = pile.pile_code

    return QueueInfo(
        requestId=req.id,
        requestCode=req.request_code,
        status=req.status,
        mode=req.mode,
        queueNumber=req.queue_number,
        waitingPosition=waiting_queue_position(db, req),
        estimatedWaitMinutes=estimate_wait_minutes(db, req),
        assignedPileCode=pile_code,
        pileQueuePosition=pile_queue_position(db, req),
    )
