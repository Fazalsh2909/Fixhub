"""Queue worker: dequeues jobs, resumes task via orchestrator. Stub loop for scaffold."""

from ..db import SessionLocal
from ..queue import dequeue


def main() -> None:
    print("fixhub worker: waiting for jobs (Ctrl-C to stop)")
    while True:
        job = dequeue(timeout=5)
        if job is None:
            continue
        print(f"got job: {job}")
        db = SessionLocal()
        try:
            from ..models import Task

            task = db.query(Task).filter_by(id=job["task_id"]).first()
            if task:
                print(
                    f"task {task.id} state={task.state} (agent run wired in api/demo path; worker picks up queue jobs next)"
                )
        finally:
            db.close()


if __name__ == "__main__":
    main()
