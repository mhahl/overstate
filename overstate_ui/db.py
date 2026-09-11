"""DB engine/session helpers. One session per request, closed on teardown."""

from flask import g
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from .models import Base

_engine = None
_Session = None


def init_db(database_uri: str):
    global _engine, _Session
    _engine = create_engine(database_uri)
    _Session = scoped_session(sessionmaker(bind=_engine))
    return _engine


def create_all():
    Base.metadata.create_all(_engine)


def get_session():
    if "db_session" not in g:
        g.db_session = _Session()
    return g.db_session


def close_session(exc=None):
    sess = g.pop("db_session", None)
    if sess is not None:
        if exc is None:
            sess.commit()
        else:
            sess.rollback()
        sess.close()
