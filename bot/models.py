"""Таблицы SQLite.

users, wheel_of_balance, explore_list, priorities, essential_intent,
weekly_tactics, checkins, daily_marks, visions, vision_reflections, teams, team_members
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    MetaData,
    JSON,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    # Имена ограничений нужны миграциям: SQLite меняет таблицы пересозданием (batch mode),
    # и безымянные unique/foreign key потом не удалить и не переименовать.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class OnboardingStep(str, enum.Enum):
    WHEEL = "wheel"
    EXPLORE = "explore"
    ELIMINATE = "eliminate"
    INTENT = "intent"
    TACTICS = "tactics"
    DONE = "done"
    FINISHED = "finished"  # 12 недель позади: неделя итогов / пауза до нового цикла


class ReportTarget(str, enum.Enum):
    TEAM = "team"    # в чат команды
    ADMIN = "admin"  # лично админу
    NONE = "none"    # никому


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    onboarding_step: Mapped[OnboardingStep] = mapped_column(
        Enum(OnboardingStep, native_enum=False), default=OnboardingStep.WHEEL
    )
    is_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    send_report: Mapped[ReportTarget] = mapped_column(
        Enum(ReportTarget, native_enum=False), default=ReportTarget.TEAM
    )
    # Номер текущего 12-недельного цикла и дата его старта (неделя 1 = cycle_start)
    cycle: Mapped[int] = mapped_column(Integer, default=1)
    cycle_start: Mapped[date | None] = mapped_column(Date)
    # На шаге тактик — позиция приоритета (1..3), к которому сейчас добавляются тактики
    onboarding_position: Mapped[int | None] = mapped_column(Integer)
    # Понедельник последней недели, за которую уже отправлен отчёт (чтобы не слать повторно)
    last_reported_week: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    wheel: Mapped[list[WheelOfBalance]] = relationship(back_populates="user", cascade="all, delete-orphan")
    explore_items: Mapped[list[ExploreItem]] = relationship(back_populates="user", cascade="all, delete-orphan")
    priorities: Mapped[list[Priority]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="Priority.position"
    )
    team_membership: Mapped[TeamMember | None] = relationship(back_populates="user", cascade="all, delete-orphan")


class WheelOfBalance(Base):
    """Оценка одной сферы жизни (1–10). Хранится по циклам, чтобы сравнивать «до/после»."""

    __tablename__ = "wheel_of_balance"
    __table_args__ = (UniqueConstraint("user_id", "cycle", "sphere"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    cycle: Mapped[int] = mapped_column(Integer, default=1)
    sphere: Mapped[str] = mapped_column(String(32))
    score: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="wheel")


class ExploreItem(Base):
    """Explore: свободный список «что важно прямо сейчас»."""

    __tablename__ = "explore_list"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    cycle: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text)
    # Отмечен на шаге Eliminate (выбрать нужно ровно 3)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="explore_items")


class Priority(Base):
    """Eliminate: ровно 3 приоритета на цикл (position 1..3)."""

    __tablename__ = "priorities"
    __table_args__ = (UniqueConstraint("user_id", "cycle", "position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    cycle: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    explore_item_id: Mapped[int | None] = mapped_column(ForeignKey("explore_list.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="priorities")
    intent: Mapped[EssentialIntent | None] = relationship(back_populates="priority", cascade="all, delete-orphan")
    tactics: Mapped[list[WeeklyTactic]] = relationship(back_populates="priority", cascade="all, delete-orphan")


class EssentialIntent(Base):
    """«Почему это для тебя важно» — по одному на приоритет."""

    __tablename__ = "essential_intent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    priority_id: Mapped[int] = mapped_column(ForeignKey("priorities.id", ondelete="CASCADE"), unique=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    priority: Mapped[Priority] = relationship(back_populates="intent")


class WeeklyTactic(Base):
    """Тактика — измеримое действие по приоритету с расписанием на недели цикла.

    weeks: None — каждую неделю (1–12); иначе список номеров недель, например [2, 6, 12]
    (контрольные точки) или [5] (разовая).
    days: для еженедельных — дни недели (0 = пн … 6 = вс), например [0, 2, 4]; None — не указаны.
    """

    __tablename__ = "weekly_tactics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    priority_id: Mapped[int] = mapped_column(ForeignKey("priorities.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    weeks: Mapped[list[int] | None] = mapped_column(JSON)
    days: Mapped[list[int] | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    priority: Mapped[Priority] = relationship(back_populates="tactics")

    def in_week(self, week_number: int) -> bool:
        return self.weeks is None or week_number in self.weeks
    checkins: Mapped[list[Checkin]] = relationship(back_populates="tactic", cascade="all, delete-orphan")


class Checkin(Base):
    """Отметка выполнения тактики за конкретную неделю."""

    __tablename__ = "checkins"
    __table_args__ = (UniqueConstraint("tactic_id", "week_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tactic_id: Mapped[int] = mapped_column(ForeignKey("weekly_tactics.id", ondelete="CASCADE"), index=True)
    week_start: Mapped[date] = mapped_column(Date, index=True)  # понедельник недели
    week_number: Mapped[int] = mapped_column(Integer)  # 1..12 внутри цикла
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    tactic: Mapped[WeeklyTactic] = relationship(back_populates="checkins")


class DailyMark(Base):
    """«Сделано сегодня» по еженедельной тактике в её день (вкладка «Сегодня»).

    Недельный процент по-прежнему считается по Checkin — одна отметка на тактику за неделю;
    дневные отметки подсказывают её: все дни недели отмечены → тактика выполнена.
    """

    __tablename__ = "daily_marks"
    __table_args__ = (UniqueConstraint("tactic_id", "day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tactic_id: Mapped[int] = mapped_column(ForeignKey("weekly_tactics.id", ondelete="CASCADE"), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Vision(Base):
    """Видение на 3+ года — одно на участницу, переживает циклы. Необязательное, любые блоки можно
    оставить пустыми; редактируется и после старта (в отличие от тактического плана).
    Личное: не уходит команде и в уведомления."""

    __tablename__ = "visions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    work: Mapped[str] = mapped_column(Text, default="")     # работа и деньги
    life: Mapped[str] = mapped_column(Text, default="")     # жизнь и отношения
    me: Mapped[str] = mapped_column(Text, default="")       # ты сама: здоровье, навыки, образ жизни
    main: Mapped[str] = mapped_column(Text, default="")     # что изменилось главное
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class VisionReflection(Base):
    """Необязательная рефлексия на 12-й неделе цикла: ближе ли к видению. На скоринг не влияет."""

    __tablename__ = "vision_reflections"
    __table_args__ = (UniqueConstraint("user_id", "cycle"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    cycle: Mapped[int] = mapped_column(Integer)
    closer: Mapped[str] = mapped_column(Text, default="")   # стала ли ближе к видению
    changed: Mapped[str] = mapped_column(Text, default="")  # что изменилось
    next: Mapped[str] = mapped_column(Text, default="")     # что берём в следующие 12 недель
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Team(Base):
    """Команда (аналог WAM), максимум 3 участницы."""

    __tablename__ = "teams"

    MAX_MEMBERS = 3

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(64))
    chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)  # групповой чат команды, если есть
    # Ссылка-приглашение в чат (если бот — админ группы и смог её создать)
    invite_link: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    members: Mapped[list[TeamMember]] = relationship(back_populates="team", cascade="all, delete-orphan")


class TeamMember(Base):
    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    team: Mapped[Team] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="team_membership")
