"""Weekly immutable plan snapshots. Never delete marks or rewrite a prior week's plan."""
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select
from bot.models import PlanWeekSnapshot, PlanChange, Checkin, DailyMark
from bot.services import onboarding, scorecard

async def capture(session, user):
    return [{"id": p.id, "position": p.position, "title": p.title,
             "intent": p.intent.text if p.intent else "",
             "tactics": [{"id": t.id, "text": t.text, "weeks": t.weeks, "days": t.days}
                         for t in p.tactics if t.is_active]}
            for p in await onboarding.get_priorities(session, user)]

async def snapshot(session, user, n):
    return await session.scalar(select(PlanWeekSnapshot).where(
        PlanWeekSnapshot.user_id == user.id, PlanWeekSnapshot.cycle == user.cycle,
        PlanWeekSnapshot.week_number == n))

class HistoricalTactic(SimpleNamespace):
    def in_week(self, n):
        return self.weeks is None or n in self.weeks

def thaw(plan, user, n):
    result = []
    for p in plan:
        priority = SimpleNamespace(id=p["id"], title=p["title"], position=p["position"], cycle=user.cycle)
        for t in p["tactics"]:
            row = HistoricalTactic(**t, user_id=user.id, priority_id=p["id"], priority=priority, is_active=True)
            if row.in_week(n):
                result.append(row)
    return result

async def before_edit(session, user, today, effective_week, kind):
    current = scorecard.week_number(user.cycle_start, today) or 1
    effective = effective_week or current
    if effective not in {current, current + 1} or effective > 12:
        raise ValueError("bad_effective_week")
    # Once next-week changes exist, editing this week's frozen plan needs a separate branch.
    if await snapshot(session, user, effective):
        raise ValueError("scheduled_plan")
    plan = await capture(session, user)
    for n in range(1, effective):
        if not await snapshot(session, user, n):
            session.add(PlanWeekSnapshot(user_id=user.id, cycle=user.cycle, week_number=n, plan=plan))
    marks = []
    if user.cycle_start:
        end = user.cycle_start + timedelta(weeks=12)
        for c in await session.scalars(select(Checkin).where(Checkin.user_id == user.id,
                Checkin.week_start >= user.cycle_start, Checkin.week_start < end)):
            marks.append({"type": "checkin", "id": c.id, "tactic_id": c.tactic_id,
                          "week": c.week_start.isoformat(), "done": c.done})
        for d in await session.scalars(select(DailyMark).where(DailyMark.user_id == user.id,
                DailyMark.day >= user.cycle_start, DailyMark.day < end)):
            marks.append({"type": "daily", "id": d.id, "tactic_id": d.tactic_id, "day": d.day.isoformat()})
    session.add(PlanChange(user_id=user.id, cycle=user.cycle, effective_week=effective,
                           kind=kind, plan_before=plan, marks_before=marks))
    await session.flush()
    return effective
