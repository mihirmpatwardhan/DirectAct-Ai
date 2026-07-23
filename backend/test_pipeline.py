import asyncio
import logging
from app.core.database import init_db, AsyncSessionLocal
from app.services.orchestrator import orchestrator
from app.models.models import Session

logging.basicConfig(level=logging.INFO)

async def main():
    await init_db()
    async with AsyncSessionLocal() as db:
        session = Session(name="Diagnostic Session")
        db.add(session)
        await db.commit()
        print(f"Created session: {session.id}")

        command = "open google.com and search for AI news"
        print(f"\n1. Processing command: '{command}'")
        res = await orchestrator.process(command, session.id, db)
        print(f"Orchestrator Result: status={res.status}, task_type={res.decision.task_type.value}, threat={res.threat_level}")

        print("\n2. Executing action through Playwright Engine...")
        out = await orchestrator.execute_action(res.action_id, session.id, db)
        print(f"Execution Output: {out}")

if __name__ == "__main__":
    asyncio.run(main())
