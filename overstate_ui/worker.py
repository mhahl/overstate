"""RQ worker entrypoint: python -m overstate_ui.worker

No app context here on purpose: each job builds its own app inside
the forked work horse (see tasks.isolated_app), so nothing
connection-like crosses the fork.
"""


def main() -> None:
    import redis
    from rq import Queue, Worker

    from .config import Config
    from .tasks import QUEUE_NAME

    conn = redis.from_url(Config.REDIS_URL)
    Worker([Queue(QUEUE_NAME, connection=conn)], connection=conn).work()


if __name__ == "__main__":
    main()
