from sqlmodel import Session, SQLModel, create_engine, select

from app.models import AppUser, Plan, Role
from app.seed import seed_all


def _temp_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    SQLModel.metadata.create_all(engine)
    return engine


def test_seed_creates_plans_and_superadmin(tmp_path):
    engine = _temp_engine(tmp_path)
    with Session(engine) as s:
        seed_all(s)
        plans = s.exec(select(Plan)).all()
        assert {p.slug for p in plans} == {"family_friends", "trial"}
        ff = s.exec(select(Plan).where(Plan.slug == "family_friends")).one()
        assert ff.is_unlimited and ff.price_cents == 0
        trial = s.exec(select(Plan).where(Plan.slug == "trial")).one()
        assert trial.is_trial

        admins = s.exec(
            select(AppUser).where(AppUser.role == Role.superadmin)
        ).all()
        assert len(admins) == 1
        assert admins[0].password_hash and admins[0].username


def test_seed_is_idempotent(tmp_path):
    engine = _temp_engine(tmp_path)
    with Session(engine) as s:
        seed_all(s)
        seed_all(s)
        assert len(s.exec(select(Plan)).all()) == 2
        assert len(
            s.exec(select(AppUser).where(AppUser.role == Role.superadmin)).all()
        ) == 1
