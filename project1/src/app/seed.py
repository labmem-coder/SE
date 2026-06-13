"""DB 初始化：2 快 + 3 慢 充电桩、管理员、若干测试用户/车辆。"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .auth import hash_password
from .config import (
    FAST_PILE_COUNT,
    FAST_PILE_POWER_KW,
    PILE_QUEUE_CAPACITY,
    SLOW_PILE_COUNT,
    SLOW_PILE_POWER_KW,
)
from .db import SessionLocal, init_db
from .models import ChargeMode, ChargingPile, User, Vehicle

log = logging.getLogger("seed")


def _ensure_user(
    db: Session, username: str, password: str, display_name: str, is_admin: bool = False
) -> User:
    user = db.query(User).filter(User.username == username).first()
    if user:
        return user
    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        is_admin=is_admin,
    )
    db.add(user)
    db.flush()
    return user


def _ensure_vehicle(
    db: Session, plate: str, owner: User, battery_capacity: float = 60.0
) -> Vehicle:
    v = db.query(Vehicle).filter(Vehicle.license_plate == plate).first()
    if v:
        return v
    v = Vehicle(
        license_plate=plate,
        owner_id=owner.id,
        battery_capacity_kwh=battery_capacity,
    )
    db.add(v)
    db.flush()
    return v


def _ensure_pile(db: Session, code: str, mode: ChargeMode, power: float) -> ChargingPile:
    p = db.query(ChargingPile).filter(ChargingPile.pile_code == code).first()
    if p:
        return p
    p = ChargingPile(
        pile_code=code,
        mode=mode,
        power_kw=power,
        queue_capacity=PILE_QUEUE_CAPACITY,
    )
    db.add(p)
    db.flush()
    return p


def seed() -> None:
    init_db()
    with SessionLocal() as db:
        # 桩
        for i in range(1, FAST_PILE_COUNT + 1):
            _ensure_pile(db, f"F{i}", ChargeMode.FAST, FAST_PILE_POWER_KW)
        for i in range(1, SLOW_PILE_COUNT + 1):
            _ensure_pile(db, f"T{i}", ChargeMode.SLOW, SLOW_PILE_POWER_KW)

        # 管理员
        admin = _ensure_user(db, "admin", "admin", "充电站管理员", is_admin=True)

        # 几个测试用户与车辆
        alice = _ensure_user(db, "alice", "alice", "Alice")
        bob = _ensure_user(db, "bob", "bob", "Bob")
        carol = _ensure_user(db, "carol", "carol", "Carol")
        dave = _ensure_user(db, "dave", "dave", "Dave")

        _ensure_vehicle(db, "京A·EV001", alice)
        _ensure_vehicle(db, "京A·EV002", bob)
        _ensure_vehicle(db, "京A·EV003", carol)
        _ensure_vehicle(db, "京A·EV004", dave)

        db.commit()
        log.info(
            "seeded: piles=%d, admin=%s, users=alice/bob/carol/dave (password = username)",
            FAST_PILE_COUNT + SLOW_PILE_COUNT,
            admin.username,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed()
    print("seed done.")
